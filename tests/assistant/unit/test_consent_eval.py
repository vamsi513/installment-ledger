"""The consent eval harness: loading, scoring, and how it reports failure.

No LLM, no network. The eval script lives outside the package because it is a
script, so it is loaded by path here, the same way a test would exercise any
other entry point.

The failure reporting is tested as carefully as the scoring. A run where the
classifier never answered still fills a table with zeroes, and a table reads
like a result; these assert that it says plainly that it is not one.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "evals" / "consent" / "run_eval.py"


def load_script() -> Any:
    """Import run_eval.py by path, registering it so its dataclasses resolve."""
    spec = importlib.util.spec_from_file_location("consent_run_eval", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["consent_run_eval"] = module
    spec.loader.exec_module(module)
    return module


run_eval = load_script()


def record(
    *,
    gold: str,
    predicted: str | None,
    confidence: float | None = None,
    failure: str | None = None,
) -> dict[str, Any]:
    return {
        "id": f"{gold}-{predicted}-{confidence}-{failure}",
        "split": "dev",
        "reply": "x",
        "gold_label": gold,
        "predicted_label": predicted,
        "confidence": confidence,
        "modified_amount_cents": None,
        "failure": failure,
    }


# --- the dataset ------------------------------------------------------------


def test_the_committed_dataset_loads_and_is_labelled() -> None:
    rows = run_eval.load_rows(REPO_ROOT / "evals" / "consent" / "replies.jsonl")
    assert rows, "the example rows should be there to fix the format"
    assert {row.split for row in rows} <= {"dev", "held_out"}
    for row in rows:
        assert row.gold_label in run_eval.LABELS
        assert row.reply
        assert row.proposal.amount_cents > 0


def test_splits_are_selectable() -> None:
    path = REPO_ROOT / "evals" / "consent" / "replies.jsonl"
    dev = run_eval.load_rows(path, split="dev")
    held_out = run_eval.load_rows(path, split="held_out")
    everything = run_eval.load_rows(path)
    assert len(dev) + len(held_out) == len(everything)
    assert {row.split for row in held_out} == {"held_out"}


@pytest.mark.parametrize(
    ("row", "complaint"),
    [
        ({"id": "a", "proposal": {}, "reply": "x", "split": "dev"}, "gold_label"),
        ({"id": "a", "proposal": {}, "reply": "x", "gold_label": "confirm"}, "split"),
        (
            {"id": "a", "proposal": {}, "reply": "x", "gold_label": "maybe", "split": "dev"},
            "gold_label",
        ),
        (
            {"id": "a", "proposal": {}, "reply": "x", "gold_label": "confirm", "split": "test"},
            "split",
        ),
    ],
)
def test_a_malformed_row_is_refused_rather_than_skipped(
    tmp_path: Path, row: dict[str, Any], complaint: str
) -> None:
    """Silently skipping rows would quietly shrink the dataset being reported."""
    path = tmp_path / "replies.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match=complaint):
        run_eval.load_rows(path)


def test_a_repeated_id_is_refused(tmp_path: Path) -> None:
    row = {"id": "a", "proposal": {}, "reply": "x", "gold_label": "confirm", "split": "dev"}
    path = tmp_path / "replies.jsonl"
    path.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="repeats id"):
        run_eval.load_rows(path)


# --- the threshold is applied when scoring ----------------------------------


def test_a_confirm_below_the_threshold_scores_as_unclear() -> None:
    """Scoring the raw label would measure something the agent never acts on."""
    item = record(gold="confirm", predicted="confirm", confidence=0.6)
    assert run_eval.effective_label(item, threshold=0.5) == "confirm"
    assert run_eval.effective_label(item, threshold=0.85) == "unclear"


def test_a_failed_call_scores_as_unclear() -> None:
    item = record(gold="confirm", predicted=None, failure="timeout")
    assert run_eval.effective_label(item, threshold=0.5) == "unclear"


def test_raising_the_threshold_trades_recall_for_precision() -> None:
    """The point of the sweep, as an assertion."""
    records = [
        record(gold="confirm", predicted="confirm", confidence=0.95),
        record(gold="confirm", predicted="confirm", confidence=0.60),
        record(gold="unclear", predicted="confirm", confidence=0.70),
    ]
    low = run_eval.score(records, threshold=0.5)
    high = run_eval.score(records, threshold=0.85)

    # At the low threshold both confirms count and the wrong one counts too.
    assert low["confirm_true_positive"] == 2
    assert low["confirm_false_positive"] == 1
    assert low["confirm_precision"] == pytest.approx(2 / 3)
    assert low["confirm_recall"] == pytest.approx(1.0)

    # At the high threshold only the confident one survives, which drops the
    # false positive and one true positive with it.
    assert high["confirm_true_positive"] == 1
    assert high["confirm_false_positive"] == 0
    assert high["confirm_precision"] == pytest.approx(1.0)
    assert high["confirm_recall"] == pytest.approx(0.5)


def test_the_counts_that_matter_are_reported() -> None:
    records = [
        record(gold="confirm", predicted=None, failure="timeout"),
        record(gold="confirm", predicted=None, failure="invalid_output"),
        record(gold="decline", predicted=None, failure="model_error"),
        record(gold="confirm", predicted="confirm", confidence=0.99),
    ]
    summary = run_eval.score(records, threshold=0.85)
    assert summary["timeout_count"] == 1
    assert summary["invalid_output_count"] == 1
    assert summary["model_error_count"] == 1
    # Every failure means the agent used the existing rule instead.
    assert summary["fallback_count"] == 3
    assert summary["unclear_count"] == 3
    assert summary["unclear_rate"] == pytest.approx(0.75)


def test_precision_is_not_invented_when_nothing_was_predicted() -> None:
    """Reporting 0.0 would read as a measured score. There is no score."""
    summary = run_eval.score(
        [record(gold="unclear", predicted="unclear", confidence=0.1)], threshold=0.85
    )
    assert summary["confirm_precision"] is None
    assert summary["confirm_recall"] is None


# --- how failure is reported ------------------------------------------------


def test_a_clean_run_gets_no_banner() -> None:
    records = [record(gold="confirm", predicted="confirm", confidence=0.99)]
    assert run_eval.failure_banner(records) == []


def test_some_failures_are_named_and_counted() -> None:
    records = [
        record(gold="confirm", predicted="confirm", confidence=0.99),
        record(gold="confirm", predicted=None, failure="timeout"),
    ]
    banner = run_eval.failure_banner(records)
    text = "\n".join(banner)
    assert "1 of 2 calls did not produce a decision" in text
    assert "1 timeout" in text
    assert "fell back to the existing confirmation rule" in text
    # Not a total failure, so the run is still a measurement.
    assert "NOT A MEASUREMENT" not in text


def test_a_run_where_nothing_answered_says_it_is_not_a_measurement() -> None:
    """The failure mode that matters: an unreachable classifier scoring zero.

    A table of zeroes reads like a result. This asserts the run says plainly
    that it is not one, and names the commonest cause.
    """
    records = [
        record(gold="confirm", predicted=None, failure="model_error"),
        record(gold="decline", predicted=None, failure="model_error"),
    ]
    text = "\n".join(run_eval.failure_banner(records))
    assert "2 of 2 calls did not produce a decision" in text
    assert "NOT A MEASUREMENT" in text
    assert "OPENAI_API_KEY" in text


# --- a failed run must not leave a committable file -------------------------


def test_a_normal_run_is_written_under_the_tracked_name(tmp_path: Path) -> None:
    result = {"provider": "openai", "split": "dev", "summary": {}}
    path = run_eval.write_results(result, tmp_path)
    assert path.name.startswith("consent-eval-openai-dev-")
    assert json.loads(path.read_text(encoding="utf-8"))["provider"] == "openai"


def test_a_run_where_nothing_answered_is_written_under_a_failed_name(tmp_path: Path) -> None:
    """results/consent/.gitignore only un-ignores 'consent-*-openai-*', so a
    file named this way cannot be committed as though it were a measurement."""
    result = {"provider": "openai", "split": "dev", "sweep": []}
    path = run_eval.write_results(result, tmp_path, failed=True)
    assert path.name.startswith("failed-consent-sweep-openai-dev-")
    assert not path.name.startswith("consent-")


def test_the_failed_name_is_the_normal_one_with_a_prefix(tmp_path: Path) -> None:
    """So the two are recognisable as the same run, and only one is trackable."""
    result = {"provider": "openai", "split": "held_out", "summary": {}}
    good = run_eval.write_results(result, tmp_path)
    bad = run_eval.write_results(result, tmp_path, failed=True)
    assert good.name.startswith("consent-eval-openai-held_out-")
    assert bad.name == f"failed-{good.name}" or bad.name.startswith(
        "failed-consent-eval-openai-held_out-"
    )
