"""The consent dataset validator.

No LLM, no network, no database. Every case is written to a temporary file, so
nothing here depends on, or changes, the committed replies.jsonl.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "evals" / "consent" / "validate_dataset.py"
DATASET = REPO_ROOT / "evals" / "consent" / "replies.jsonl"


def load_script() -> Any:
    spec = importlib.util.spec_from_file_location("consent_validate_dataset", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["consent_validate_dataset"] = module
    spec.loader.exec_module(module)
    return module


validator = load_script()


def row(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "a",
        "proposal": {"amount_cents": 12_500, "payee": "loan 7f3a", "installment": "installment 3"},
        "reply": "yes",
        "gold_label": "confirm",
        "split": "dev",
    }
    base.update(overrides)
    return base


def write(tmp_path: Path, *rows: dict[str, Any]) -> Path:
    path = tmp_path / "replies.jsonl"
    path.write_text(
        "".join(json.dumps(item) + "\n" for item in rows),
        encoding="utf-8",
    )
    return path


def errors_for(tmp_path: Path, *rows: dict[str, Any]) -> list[str]:
    found, _, _ = validator.validate(write(tmp_path, *rows))
    return found


# --- the committed file -----------------------------------------------------


def test_the_committed_dataset_has_no_errors() -> None:
    """It is small, so it warns. It must not be wrong."""
    errors, _, rows = validator.validate(DATASET)
    assert errors == []
    assert rows


def test_a_missing_file_is_an_error(tmp_path: Path) -> None:
    errors, _, rows = validator.validate(tmp_path / "nope.jsonl")
    assert errors and "does not exist" in errors[0]
    assert rows == []


# --- required fields and allowed values -------------------------------------


@pytest.mark.parametrize("field", ["id", "proposal", "reply", "gold_label", "split"])
def test_a_missing_required_field_is_an_error(tmp_path: Path, field: str) -> None:
    incomplete = row()
    del incomplete[field]
    found = errors_for(tmp_path, incomplete)
    assert any(field in item for item in found)


def test_an_unknown_label_is_an_error(tmp_path: Path) -> None:
    found = errors_for(tmp_path, row(gold_label="probably"))
    assert any("gold_label" in item for item in found)


def test_an_unknown_split_is_an_error(tmp_path: Path) -> None:
    found = errors_for(tmp_path, row(split="test"))
    assert any("split" in item for item in found)


def test_malformed_json_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "replies.jsonl"
    path.write_text('{"id": "a", oops}\n', encoding="utf-8")
    errors, _, _ = validator.validate(path)
    assert any("not valid JSON" in item for item in errors)


def test_blank_lines_and_comments_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "replies.jsonl"
    path.write_text(
        "\n// a note about the file\n" + json.dumps(row()) + "\n\n",
        encoding="utf-8",
    )
    errors, _, rows = validator.validate(path)
    assert errors == []
    assert len(rows) == 1


# --- ids and leakage --------------------------------------------------------


def test_a_repeated_id_is_an_error(tmp_path: Path) -> None:
    found = errors_for(tmp_path, row(id="a"), row(id="a", reply="confirm"))
    assert any("repeats id" in item for item in found)


def test_the_same_reply_in_both_splits_is_an_error(tmp_path: Path) -> None:
    """Scoring held_out on text that tuned the threshold measures nothing."""
    found = errors_for(
        tmp_path,
        row(id="a", reply="go ahead", split="dev"),
        row(id="b", reply="go ahead", split="held_out"),
    )
    assert any("leaks one split into the other" in item for item in found)


def test_the_leak_check_ignores_case(tmp_path: Path) -> None:
    found = errors_for(
        tmp_path,
        row(id="a", reply="Go Ahead", split="dev"),
        row(id="b", reply="go ahead", split="held_out"),
    )
    assert any("leaks" in item for item in found)


def test_the_same_reply_twice_in_one_split_is_allowed(tmp_path: Path) -> None:
    """Two proposals can legitimately draw the same words."""
    found = errors_for(
        tmp_path,
        row(id="a", reply="yes", split="dev"),
        row(id="b", reply="yes", split="dev"),
    )
    assert found == []


def test_an_empty_reply_is_an_error(tmp_path: Path) -> None:
    found = errors_for(tmp_path, row(reply="   "))
    assert any("empty reply" in item for item in found)


# --- amounts ----------------------------------------------------------------


@pytest.mark.parametrize("amount", [0, -1, 12.5, "12500", None, True])
def test_a_proposal_amount_has_to_be_positive_whole_cents(tmp_path: Path, amount: Any) -> None:
    found = errors_for(tmp_path, row(proposal={"amount_cents": amount}))
    assert any("amount_cents" in item for item in found)


def test_a_modify_row_may_carry_whole_cents(tmp_path: Path) -> None:
    found = errors_for(
        tmp_path, row(gold_label="modify", reply="make it 50 dollars", modified_amount_cents=5_000)
    )
    assert found == []


@pytest.mark.parametrize("amount", [0, -5, 5_000.0, "5000", True])
def test_a_modify_row_with_a_bad_amount_is_an_error(tmp_path: Path, amount: Any) -> None:
    found = errors_for(
        tmp_path, row(gold_label="modify", reply="make it x", modified_amount_cents=amount)
    )
    assert any("modified_amount_cents" in item for item in found)


def test_an_amount_on_a_non_modify_row_is_an_error(tmp_path: Path) -> None:
    """The schema forbids it, so a gold label carrying one describes an answer
    the classifier is not allowed to give."""
    found = errors_for(tmp_path, row(gold_label="confirm", modified_amount_cents=5_000))
    assert any("forbids" in item for item in found)


# --- warnings do not fail ---------------------------------------------------


def test_a_small_dataset_warns_and_does_not_error(tmp_path: Path) -> None:
    errors, warnings, _ = validator.validate(write(tmp_path, row()))
    assert errors == []
    assert any("held_out has 0 rows" in item for item in warnings)


def test_a_thin_label_warns(tmp_path: Path) -> None:
    rows = [row(id=f"d{index}", reply=f"yes {index}") for index in range(3)]
    _, warnings, _ = validator.validate(write(tmp_path, *rows))
    assert any("dev has 3 confirm rows" in item for item in warnings)


def test_a_full_dataset_warns_about_nothing(tmp_path: Path) -> None:
    """The shape the warnings are asking for, so they stay silent on it."""
    rows: list[dict[str, Any]] = []
    for split, per_label in (("dev", 13), ("held_out", 7)):
        for label in validator.LABELS:
            for index in range(per_label):
                extra: dict[str, Any] = {}
                if label == "modify":
                    extra["modified_amount_cents"] = 5_000
                rows.append(
                    row(
                        id=f"{split}-{label}-{index}",
                        reply=f"{split} {label} {index}",
                        gold_label=label,
                        split=split,
                        **extra,
                    )
                )
    errors, warnings, parsed = validator.validate(write(tmp_path, *rows))
    assert errors == []
    assert warnings == []
    assert len(parsed) == 80


# --- counts and the exit code -----------------------------------------------


def test_counts_are_reported_per_split_and_label(tmp_path: Path) -> None:
    _, _, rows = validator.validate(
        write(
            tmp_path,
            row(id="a", reply="yes", gold_label="confirm", split="dev"),
            row(id="b", reply="no", gold_label="decline", split="dev"),
            row(id="c", reply="hmm", gold_label="unclear", split="held_out"),
        )
    )
    table = validator.counts(rows)
    assert table["dev"]["confirm"] == 1
    assert table["dev"]["decline"] == 1
    assert table["dev"]["total"] == 2
    assert table["held_out"]["unclear"] == 1
    assert table["held_out"]["total"] == 1


def test_main_exits_zero_on_a_usable_file(tmp_path: Path) -> None:
    assert validator.main(["--replies", str(write(tmp_path, row()))]) == 0


def test_main_exits_non_zero_on_an_error(tmp_path: Path) -> None:
    assert validator.main(["--replies", str(write(tmp_path, row(gold_label="nope")))]) == 1


def test_main_exits_zero_on_the_committed_dataset() -> None:
    assert validator.main(["--replies", str(DATASET)]) == 0
