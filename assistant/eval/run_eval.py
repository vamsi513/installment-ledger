"""Run the scenarios in ``scenarios.json`` and write a results file.

    LEDGER_BASE_URL=http://127.0.0.1:8000 \
    OPENAI_API_KEY=... \
    python -m assistant.eval.run_eval --provider openai

Needs a ledger already running and reachable at ``LEDGER_BASE_URL``. Every loan
is created by this script against that ledger and left behind; the loans are
fictional and no real money is involved.

``--provider fake`` runs the same scenarios through the deterministic stand in.
That verifies the harness itself end to end. It says nothing about any model,
and the results file it writes is marked accordingly.

The script writes the results file itself. Nothing here is transcribed by hand.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ..agent import run_turn
from ..config import Settings, load_settings
from ..llm import build_llm_client
from .rules import FAILED, NOT_APPLICABLE, PASSED, RULE_NAMES, score_scenario

HERE = Path(__file__).resolve().parent
DEFAULT_SCENARIOS = HERE / "scenarios.json"
DEFAULT_RESULTS_DIR = HERE / "results"

FAKE_PROVIDER_WARNING = (
    "This run used the deterministic fake model client, not a language model. "
    "It exists to verify that the harness, the scenarios and the rule checks "
    "work end to end. It is NOT a measurement of any model's behaviour and must "
    "not be reported as one."
)


def load_scenarios(path: Path) -> tuple[list[dict[str, Any]], int]:
    document = json.loads(path.read_text())
    return document["scenarios"], int(document.get("cap_cents", 60_000))


def ledger_base_url() -> str:
    url = os.environ.get("LEDGER_BASE_URL", "").strip().rstrip("/")
    if not url:
        raise SystemExit(
            "LEDGER_BASE_URL is not set. Start the ledger (docker compose up) and point "
            "LEDGER_BASE_URL at it, for example http://127.0.0.1:8000"
        )
    return url


def check_ledger(url: str) -> None:
    try:
        with httpx.Client(base_url=url, timeout=10.0) as client:
            response = client.get("/health")
    except httpx.HTTPError as exc:
        raise SystemExit(f"could not reach the ledger at {url}: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise SystemExit(f"the ledger at {url} answered /health with {response.status_code}")


def create_loan(client: httpx.Client, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        "/loans", json=body, headers={"Idempotency-Key": f"eval-loan-{uuid.uuid4()}"}
    )
    response.raise_for_status()
    return dict(response.json())


def pay_directly(client: httpx.Client, loan_id: str, amount_cents: int) -> None:
    response = client.post(
        f"/loans/{loan_id}/payments",
        json={"amount_cents": amount_cents},
        headers={"Idempotency-Key": f"eval-prior-{uuid.uuid4()}"},
    )
    response.raise_for_status()


def read_final_state(client: httpx.Client, loan_id: str, *, prior_payments: int) -> dict[str, Any]:
    loan = client.get(f"/loans/{loan_id}").json()
    ledger = client.get(f"/loans/{loan_id}/ledger").json()
    # One transaction is the disbursement; the rest are payments, including any
    # the harness itself made before the scenario started.
    payments = max(0, len(ledger["transactions"]) - 1 - prior_payments)
    return {
        "loan_status": loan["status"],
        "paid_cents": loan["paid_cents"],
        "outstanding_cents": loan["outstanding_cents"],
        "payment_count": payments,
        "ledger_net_cents": ledger["net_cents"],
    }


async def run_scenario(
    scenario: dict[str, Any], *, settings: Settings, client: httpx.Client
) -> dict[str, Any]:
    loan = create_loan(client, scenario["loan"])
    prior_cents = int(scenario.get("prior_payment_cents") or 0)
    prior_payments = 0
    if prior_cents:
        pay_directly(client, loan["id"], prior_cents)
        prior_payments = 1

    session_id = f"eval-{scenario['id']}-{uuid.uuid4().hex[:8]}"
    turns: list[dict[str, Any]] = []
    for turn in scenario["turns"]:
        model = build_llm_client(settings)
        result = await run_turn(
            session_id=session_id,
            message=turn["message"],
            bound_loan_ids=[loan["id"]],
            settings=settings,
            llm=model,
        )
        turns.append({"message": turn["message"], **asdict(result)})

    final = read_final_state(client, loan["id"], prior_payments=prior_payments)
    score = score_scenario(scenario, turns, final)
    return {
        "scenario_id": scenario["id"],
        "category": scenario["category"],
        "notes": scenario["notes"],
        "loan_id": loan["id"],
        "turns": turns,
        "final": final,
        "rules": score.rules,
        "failures": score.failures,
        "passed": score.passed,
    }


def summarise(records: list[dict[str, Any]]) -> dict[str, Any]:
    per_category: dict[str, Counter[str]] = defaultdict(Counter)
    per_rule: dict[str, Counter[str]] = {name: Counter() for name in RULE_NAMES}
    latencies: list[float] = []
    prompt_tokens = completion_tokens = llm_calls = turn_count = 0

    for record in records:
        bucket = per_category[record["category"]]
        bucket["scenarios"] += 1
        bucket["passed" if record["passed"] else "failed"] += 1
        for rule, verdict in record["rules"].items():
            per_rule[rule][verdict] += 1
        for turn in record["turns"]:
            turn_count += 1
            latencies.append(float(turn["latency_ms"]))
            prompt_tokens += int(turn["prompt_tokens"])
            completion_tokens += int(turn["completion_tokens"])
            llm_calls += int(turn["llm_calls"])

    passed = sum(1 for record in records if record["passed"])
    return {
        "totals": {
            "scenarios": len(records),
            "passed": passed,
            "failed": len(records) - passed,
            "turns": turn_count,
            "model_calls": llm_calls,
        },
        "per_category": {
            category: {
                "scenarios": counts["scenarios"],
                "passed": counts["passed"],
                "failed": counts["failed"],
            }
            for category, counts in sorted(per_category.items())
        },
        "per_rule": {
            rule: {
                PASSED: counts[PASSED],
                FAILED: counts[FAILED],
                NOT_APPLICABLE: counts[NOT_APPLICABLE],
            }
            for rule, counts in per_rule.items()
        },
        "tokens": {
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
            "mean_per_turn": round((prompt_tokens + completion_tokens) / turn_count, 2)
            if turn_count
            else 0,
            "mean_per_model_call": round((prompt_tokens + completion_tokens) / llm_calls, 2)
            if llm_calls
            else 0,
        },
        "latency_ms_per_turn": {
            "median": round(statistics.median(latencies), 2) if latencies else 0,
            "mean": round(statistics.fmean(latencies), 2) if latencies else 0,
            "p95": round(sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))], 2)
            if latencies
            else 0,
            "min": round(min(latencies), 2) if latencies else 0,
            "max": round(max(latencies), 2) if latencies else 0,
        },
        "failures": [
            {
                "scenario_id": record["scenario_id"],
                "category": record["category"],
                "failures": record["failures"],
            }
            for record in records
            if not record["passed"]
        ],
    }


def render_markdown(results: dict[str, Any]) -> str:
    summary = results["summary"]
    totals = summary["totals"]
    lines = [
        f"# Assistant eval, {results['provider']} provider",
        "",
    ]
    if results["harness_verification_only"]:
        lines += [f"> **{FAKE_PROVIDER_WARNING}**", ""]
    lines += [
        f"Run at {results['finished_at']} against the ledger at "
        f"`{results['environment']['ledger_base_url']}`.",
        f"Model: `{results['model']}`. Scenario file: `{results['environment']['scenarios']}`.",
        "",
        f"{totals['passed']} of {totals['scenarios']} scenarios passed, over "
        f"{totals['turns']} turns and {totals['model_calls']} model calls.",
        "",
        "## Per category",
        "",
        "| Category | Scenarios | Passed | Failed |",
        "| --- | --- | --- | --- |",
    ]
    for category, counts in summary["per_category"].items():
        lines.append(
            f"| {category} | {counts['scenarios']} | {counts['passed']} | {counts['failed']} |"
        )
    lines += [
        "",
        "## Per rule",
        "",
        "| Rule | Pass | Fail | Not applicable |",
        "| --- | --- | --- | --- |",
    ]
    for rule, counts in summary["per_rule"].items():
        lines.append(f"| {rule} | {counts[PASSED]} | {counts[FAILED]} | {counts[NOT_APPLICABLE]} |")
    tokens = summary["tokens"]
    latency = summary["latency_ms_per_turn"]
    lines += [
        "",
        "## Cost and latency",
        "",
        f"- Tokens: {tokens['total']} total, {tokens['prompt']} prompt, "
        f"{tokens['completion']} completion.",
        f"- Mean tokens per turn: {tokens['mean_per_turn']}. "
        f"Per model call: {tokens['mean_per_model_call']}.",
        f"- Latency per turn in ms: median {latency['median']}, mean {latency['mean']}, "
        f"p95 {latency['p95']}, min {latency['min']}, max {latency['max']}.",
        "",
        "Latency includes starting an MCP subprocess for the turn, so it is the cost of a "
        "whole turn and not of the model call alone.",
        "",
        "## Remaining failures",
        "",
    ]
    if summary["failures"]:
        for failure in summary["failures"]:
            lines.append(f"- `{failure['scenario_id']}` ({failure['category']}):")
            lines += [f"  - {detail}" for detail in failure["failures"]]
    else:
        lines.append("None in this run.")
    lines += [
        "",
        "## What this does not say",
        "",
        "These are rule checks over a fixed list of hand written scenarios. They test that "
        "the required tool ran, that nothing wrote without an exact confirmation, that the "
        "ledger holds the expected number of payments for the expected amount, and that a "
        "refusal happened where one was required. They do not test whether a reply is "
        "helpful or well worded, and they say nothing about inputs nobody wrote down here. "
        "A full pass is not evidence of general correctness.",
        "",
    ]
    return "\n".join(lines)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the assistant evaluation scenarios.")
    parser.add_argument(
        "--provider",
        choices=("openai", "fake"),
        default="openai",
        help="openai for a real model run; fake verifies the harness only.",
    )
    parser.add_argument("--model", default=None, help="Overrides ASSISTANT_MODEL.")
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument(
        "--category", action="append", default=None, help="Run only these categories."
    )
    parser.add_argument("--limit", type=int, default=None, help="Run only the first N scenarios.")
    return parser.parse_args(argv)


async def main_async(arguments: argparse.Namespace) -> int:
    scenarios, cap_cents = load_scenarios(arguments.scenarios)
    if arguments.category:
        wanted = set(arguments.category)
        scenarios = [item for item in scenarios if item["category"] in wanted]
    if arguments.limit:
        scenarios = scenarios[: arguments.limit]
    if not scenarios:
        raise SystemExit("no scenarios selected")

    base_url = ledger_base_url()
    check_ledger(base_url)

    environment = load_settings()
    state_dir = Path(os.environ.get("TMPDIR", "/tmp")) / f"assistant-eval-{uuid.uuid4().hex[:8]}"
    state_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        ledger_base_url=base_url,
        ledger_timeout_seconds=environment.ledger_timeout_seconds,
        max_payment_cents=cap_cents,
        pending_ttl_seconds=environment.pending_ttl_seconds,
        max_message_chars=environment.max_message_chars,
        state_db_path=str(state_dir / "state.sqlite3"),
        llm_provider=arguments.provider,
        model=arguments.model or environment.model,
        openai_api_key=environment.openai_api_key,
        ws_auth_token=None,
        ws_max_frame_bytes=environment.ws_max_frame_bytes,
        ws_max_connections=environment.ws_max_connections,
        enabled=True,
    )
    if arguments.provider == "openai":
        # Fails now rather than halfway through the run. The key is never printed.
        settings.require_openai_api_key()

    started = time.perf_counter()
    started_at = datetime.now(UTC).isoformat()
    records: list[dict[str, Any]] = []

    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        for index, scenario in enumerate(scenarios, start=1):
            record = await run_scenario(scenario, settings=settings, client=client)
            records.append(record)
            mark = "pass" if record["passed"] else "FAIL"
            print(
                f"[{index:>3}/{len(scenarios)}] {mark}  {scenario['category']:<20} "
                f"{scenario['id']}",
                flush=True,
            )
            for detail in record["failures"]:
                print(f"            {detail}", flush=True)

    results = {
        "provider": arguments.provider,
        "model": settings.model if arguments.provider == "openai" else "fake-classifier",
        "harness_verification_only": arguments.provider == "fake",
        "warning": FAKE_PROVIDER_WARNING if arguments.provider == "fake" else None,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.perf_counter() - started, 2),
        "environment": {
            "python": sys.version.split()[0],
            "ledger_base_url": base_url,
            "cap_cents": cap_cents,
            "scenarios": str(arguments.scenarios.name),
            "scenario_count": len(scenarios),
        },
        "summary": summarise(records),
        "scenarios": records,
    }

    arguments.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    json_path = arguments.out_dir / f"eval-{arguments.provider}-{stamp}.json"
    markdown_path = arguments.out_dir / f"eval-{arguments.provider}-{stamp}.md"
    json_path.write_text(json.dumps(results, indent=2) + "\n")
    markdown_path.write_text(render_markdown(results))

    totals = results["summary"]["totals"]
    print()
    if arguments.provider == "fake":
        print(f"NOTE: {FAKE_PROVIDER_WARNING}")
        print()
    print(f"{totals['passed']}/{totals['scenarios']} scenarios passed.")
    if results["summary"]["failures"]:
        print("Remaining failures:")
        for failure in results["summary"]["failures"]:
            print(f"  {failure['scenario_id']}: {'; '.join(failure['failures'])}")
    else:
        print("No failures in this run.")
    print(f"\nWrote {json_path}")
    print(f"Wrote {markdown_path}")
    return 0 if totals["failed"] == 0 else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_arguments(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
