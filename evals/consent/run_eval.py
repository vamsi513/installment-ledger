"""Score the consent classifier against the labelled replies.

    python evals/consent/run_eval.py --split dev --sweep
    OPENAI_API_KEY=... python evals/consent/run_eval.py --provider openai --split dev --sweep
    OPENAI_API_KEY=... python evals/consent/run_eval.py --provider openai \
        --split held_out --threshold 0.85

Needs nothing running: no ledger, no database, no MCP server. The classifier is
called directly, so this measures the classifier and not the agent around it.

``--provider stub`` is the default and runs the deterministic stand in. That
verifies the harness end to end and says nothing about any model, which is why a
stub run is written with a warning in it and is not tracked.

HOW THE THRESHOLD IS MEANT TO BE CHOSEN. Run ``--sweep`` on dev, read the
precision and recall on confirm at each threshold, pick one, then run held_out
ONCE at that threshold. ``--sweep`` refuses to run on held_out, because a
threshold picked on the set used to report it is a threshold picked on test
data, and the number that comes out of it means nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import path setup
    sys.path.insert(0, str(REPO_ROOT))

from assistant.consent import (  # noqa: E402 - after the path fix above
    ConsentFailure,
    ConsentLabel,
    ConsentModel,
    OpenAiConsentModel,
    Proposal,
    StubConsentModel,
    classify_reply,
)

HERE = Path(__file__).resolve().parent
DEFAULT_REPLIES = HERE / "replies.jsonl"
DEFAULT_RESULTS_DIR = REPO_ROOT / "results" / "consent"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SECONDS = 3.0

SPLITS = ("dev", "held_out")
LABELS = tuple(label.value for label in ConsentLabel)
SWEEP_THRESHOLDS = tuple(round(0.5 + 0.05 * step, 2) for step in range(10))

STUB_WARNING = (
    "This run used the deterministic stub consent model, not a language model. "
    "It exists to verify that the harness, the dataset format and the scoring "
    "work end to end. It is NOT a measurement of any model's behaviour and must "
    "not be reported as one."
)

LABELLING_NOTE = (
    "The gold labels were written and reviewed by one person. They are one "
    "person's reading of what each reply means, with no second annotator and no "
    "agreement measurement, so the labels carry that person's assumptions."
)


@dataclass(frozen=True)
class Row:
    """One labelled reply."""

    id: str
    proposal: Proposal
    reply: str
    gold_label: str
    split: str


def load_rows(path: Path, *, split: str | None = None) -> list[Row]:
    """Read the JSONL dataset, rejecting anything malformed rather than skipping it."""
    rows: list[Row] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("//"):
                continue
            try:
                raw = json.loads(text)
            except ValueError as exc:
                raise ValueError(f"{path}:{number} is not valid JSON: {exc}") from exc
            for field in ("id", "proposal", "reply", "gold_label", "split"):
                if field not in raw:
                    raise ValueError(f"{path}:{number} is missing {field!r}")
            if raw["gold_label"] not in LABELS:
                raise ValueError(
                    f"{path}:{number} has gold_label {raw['gold_label']!r}; "
                    f"expected one of {', '.join(LABELS)}"
                )
            if raw["split"] not in SPLITS:
                raise ValueError(
                    f"{path}:{number} has split {raw['split']!r}; expected one of "
                    f"{', '.join(SPLITS)}"
                )
            if raw["id"] in seen:
                raise ValueError(f"{path}:{number} repeats id {raw['id']!r}")
            seen.add(raw["id"])
            proposal = raw["proposal"] or {}
            rows.append(
                Row(
                    id=str(raw["id"]),
                    proposal=Proposal(
                        amount_cents=int(proposal.get("amount_cents") or 0),
                        payee=str(proposal.get("payee") or ""),
                        installment=str(proposal.get("installment") or ""),
                    ),
                    reply=str(raw["reply"]),
                    gold_label=str(raw["gold_label"]),
                    split=str(raw["split"]),
                )
            )
    if split is not None:
        rows = [row for row in rows if row.split == split]
    return rows


def build_model(provider: str, *, model_name: str, timeout_seconds: float) -> ConsentModel:
    if provider == "stub":
        return StubConsentModel()
    if provider != "openai":
        raise ValueError(f"unknown provider {provider!r}; expected 'openai' or 'stub'")
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit(
            "--provider openai needs OPENAI_API_KEY. Run with --provider stub for the "
            "offline harness check, which needs no key."
        )
    return OpenAiConsentModel(api_key=api_key, model=model_name, timeout_seconds=timeout_seconds)


async def collect(
    rows: list[Row], model: ConsentModel, *, timeout_seconds: float
) -> list[dict[str, Any]]:
    """Classify every row once. Thresholds are applied later, not here.

    Separating the two is what makes the sweep honest and cheap: the model is
    called once per row and every threshold is scored against the same answers,
    so a difference between thresholds cannot be a difference between runs.
    """
    records: list[dict[str, Any]] = []
    for row in rows:
        classification, failure = await classify_reply(
            model, reply=row.reply, proposal=row.proposal, timeout_seconds=timeout_seconds
        )
        records.append(
            {
                "id": row.id,
                "split": row.split,
                "reply": row.reply,
                "gold_label": row.gold_label,
                "predicted_label": (
                    classification.label.value if classification is not None else None
                ),
                "confidence": (classification.confidence if classification is not None else None),
                "modified_amount_cents": (
                    classification.modified_amount_cents if classification is not None else None
                ),
                "failure": failure.value if failure is not None else None,
            }
        )
    return records


def effective_label(record: dict[str, Any], *, threshold: float) -> str:
    """What the agent would act on, which is the label after the threshold.

    A confirm the model is not sure enough about is not a confirm. Scoring the
    raw label would measure something the agent never uses.
    """
    if record["predicted_label"] is None:
        # No answer at all. The agent falls back to the existing rule; for
        # scoring purposes the classifier contributed nothing, which is an
        # unclear as far as the confusion matrix is concerned.
        return ConsentLabel.UNCLEAR.value
    if (
        record["predicted_label"] == ConsentLabel.CONFIRM.value
        and float(record["confidence"] or 0.0) < threshold
    ):
        return ConsentLabel.UNCLEAR.value
    return str(record["predicted_label"])


def score(records: list[dict[str, Any]], *, threshold: float) -> dict[str, Any]:
    """Precision and recall on confirm, plus the counts that matter for safety."""
    matrix: dict[str, Counter[str]] = {gold: Counter() for gold in LABELS}
    for record in records:
        matrix[record["gold_label"]][effective_label(record, threshold=threshold)] += 1

    confirm = ConsentLabel.CONFIRM.value
    true_positive = matrix[confirm][confirm]
    false_positive = sum(matrix[gold][confirm] for gold in LABELS if gold != confirm)
    false_negative = sum(matrix[confirm][predicted] for predicted in LABELS if predicted != confirm)

    precision = (
        true_positive / (true_positive + false_positive)
        if (true_positive + false_positive)
        else None
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if (true_positive + false_negative)
        else None
    )

    total = len(records)
    unclear_count = sum(
        1
        for record in records
        if effective_label(record, threshold=threshold) == ConsentLabel.UNCLEAR.value
    )
    failures = Counter(record["failure"] for record in records if record["failure"])

    return {
        "threshold": threshold,
        "rows": total,
        "confirm_precision": precision,
        "confirm_recall": recall,
        "confirm_true_positive": true_positive,
        "confirm_false_positive": false_positive,
        "confirm_false_negative": false_negative,
        "confusion_matrix": {gold: dict(counts) for gold, counts in matrix.items()},
        "unclear_rate": (unclear_count / total) if total else None,
        "unclear_count": unclear_count,
        "invalid_output_count": failures.get(ConsentFailure.INVALID_OUTPUT.value, 0),
        "timeout_count": failures.get(ConsentFailure.TIMEOUT.value, 0),
        "model_error_count": failures.get(ConsentFailure.MODEL_ERROR.value, 0),
        # Every failure means the agent used the existing confirmation rule
        # instead of the classifier.
        "fallback_count": sum(failures.values()),
    }


def git_sha() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - not a git checkout
        return "unknown"
    return completed.stdout.strip()


def _format(value: float | None, places: int = 3) -> str:
    return "n/a" if value is None else f"{value:.{places}f}"


def print_report(result: dict[str, Any]) -> None:
    """One split at one threshold, in full."""
    summary = result["summary"]
    print(f"dataset   : {result['dataset']}")
    print(f"split     : {result['split']}  ({summary['rows']} rows)")
    print(f"provider  : {result['provider']}")
    print(f"model     : {result['model']}")
    print(f"threshold : {summary['threshold']}")
    print()
    print(f"confirm precision : {_format(summary['confirm_precision'])}")
    print(f"confirm recall    : {_format(summary['confirm_recall'])}")
    print(
        f"  true positives {summary['confirm_true_positive']}, "
        f"false positives {summary['confirm_false_positive']}, "
        f"false negatives {summary['confirm_false_negative']}"
    )
    print()
    print(
        f"unclear rate      : {_format(summary['unclear_rate'])} "
        f"({summary['unclear_count']} of {summary['rows']})"
    )
    print(f"invalid outputs   : {summary['invalid_output_count']}")
    print(f"timeouts          : {summary['timeout_count']}")
    print(f"model errors      : {summary['model_error_count']}")
    print(f"fell back to rule : {summary['fallback_count']}")
    print()
    print("confusion matrix, gold down and predicted across:")
    width = max(len(label) for label in LABELS) + 2
    print(" " * width + "".join(label.rjust(width) for label in LABELS))
    for gold in LABELS:
        row = summary["confusion_matrix"][gold]
        print(
            gold.rjust(width)
            + "".join(str(row.get(predicted, 0)).rjust(width) for predicted in LABELS)
        )
    print()
    if result["provider"] == "stub":
        print(STUB_WARNING)
        print()
    print(LABELLING_NOTE)


def print_sweep(result: dict[str, Any]) -> None:
    print(f"dataset   : {result['dataset']}")
    print(f"split     : {result['split']}  ({result['rows']} rows)")
    print(f"provider  : {result['provider']}")
    print(f"model     : {result['model']}")
    print()
    print("threshold  precision     recall    unclear  fallback")
    for entry in result["sweep"]:
        print(
            f"{entry['threshold']:>9}"
            f"{_format(entry['confirm_precision']):>12}"
            f"{_format(entry['confirm_recall']):>11}"
            f"{_format(entry['unclear_rate'], 2):>11}"
            f"{entry['fallback_count']:>10}"
        )
    print()
    print("Pick a threshold from this table, then run --split held_out once at it.")
    print()
    if result["provider"] == "stub":
        print(STUB_WARNING)
        print()
    print(LABELLING_NOTE)


def write_results(result: dict[str, Any], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    kind = "sweep" if "sweep" in result else "eval"
    path = out_dir / f"consent-{kind}-{result['provider']}-{result['split']}-{stamp}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--provider",
        choices=("stub", "openai"),
        default="stub",
        help="stub is the offline stand in and the default. openai needs OPENAI_API_KEY.",
    )
    parser.add_argument("--split", choices=SPLITS, default="dev")
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Act on confirm only at or above this confidence. Required unless --sweep.",
    )
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="Score every threshold from 0.5 to 0.95. Allowed on dev only.",
    )
    parser.add_argument("--replies", type=Path, default=DEFAULT_REPLIES)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_RESULTS_DIR)
    parser.add_argument("--model", default=os.environ.get("ASSISTANT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument(
        "--no-write", action="store_true", help="Print the report without writing a file."
    )
    return parser.parse_args(argv)


async def main_async(arguments: argparse.Namespace) -> int:
    if arguments.sweep and arguments.split != "dev":
        raise SystemExit(
            "--sweep runs on dev only. Choosing a threshold on the held out set and "
            "then reporting that set at it measures nothing."
        )
    if not arguments.sweep and arguments.threshold is None:
        raise SystemExit("--threshold is required unless --sweep is given.")
    if arguments.threshold is not None and not 0.0 <= arguments.threshold <= 1.0:
        raise SystemExit("--threshold must be between 0 and 1.")

    rows = load_rows(arguments.replies, split=arguments.split)
    if not rows:
        raise SystemExit(f"no rows with split {arguments.split!r} in {arguments.replies}")

    model = build_model(
        arguments.provider,
        model_name=arguments.model,
        timeout_seconds=arguments.timeout_seconds,
    )
    records = await collect(rows, model, timeout_seconds=arguments.timeout_seconds)

    common: dict[str, Any] = {
        "dataset": str(arguments.replies.relative_to(REPO_ROOT)),
        "split": arguments.split,
        "provider": arguments.provider,
        "model": arguments.model if arguments.provider == "openai" else model.name,
        "ran_at": datetime.now(UTC).isoformat(),
        "git_sha": git_sha(),
        "labelling": LABELLING_NOTE,
        "records": records,
    }
    if arguments.provider == "stub":
        common["warning"] = STUB_WARNING

    if arguments.sweep:
        result = {
            **common,
            "rows": len(records),
            "sweep": [score(records, threshold=value) for value in SWEEP_THRESHOLDS],
        }
        print_sweep(result)
    else:
        result = {**common, "summary": score(records, threshold=arguments.threshold)}
        print_report(result)

    if not arguments.no_write:
        path = write_results(result, arguments.out_dir)
        print()
        print(f"full results -> {path.relative_to(REPO_ROOT)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(main_async(parse_arguments(argv)))


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
