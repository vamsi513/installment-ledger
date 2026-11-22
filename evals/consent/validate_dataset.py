"""Check replies.jsonl before anyone runs an eval against it.

    python evals/consent/validate_dataset.py
    python evals/consent/validate_dataset.py --replies evals/consent/replies.jsonl

Needs nothing running and calls nothing. It reads the file and complains.

Errors fail the run, because they mean a number computed from the file would be
wrong: a missing field, a label or split that is not one of the allowed values,
a repeated id, the same reply text appearing in both splits (which leaks dev
into held_out), or a modify row whose amount is not a positive whole number of
cents.

Warnings do not fail, because they are about a dataset that is still being
written rather than one that is wrong. A held_out split under 25 rows, or a
label with fewer than 5 rows in a split, cannot support a number worth
reporting, but it is a perfectly reasonable state for a file in progress.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - import path setup
    sys.path.insert(0, str(REPO_ROOT))

HERE = Path(__file__).resolve().parent
DEFAULT_REPLIES = HERE / "replies.jsonl"

LABELS = ("confirm", "decline", "modify", "unclear")
SPLITS = ("dev", "held_out")
REQUIRED_FIELDS = ("id", "proposal", "reply", "gold_label", "split")

#: Below these a split cannot carry a number worth reporting. Warnings, not errors.
MIN_HELD_OUT_ROWS = 25
MIN_ROWS_PER_LABEL = 5

LABELLING_NOTE = (
    "The gold labels follow the convention recorded at the top of replies.jsonl: "
    "only a reply that plainly authorises this exact payment now is a confirm, and "
    "a hedged or conditional yes never is. They are one reading of what each reply "
    "means, with no second annotator and no agreement measurement."
)


def _is_whole_cents(value: Any) -> bool:
    """A positive whole number of cents, and not a bool.

    ``True`` is an ``int`` in Python and would otherwise pass as 1 cent, which
    is the kind of thing that silently becomes a payment of one cent.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate(path: Path) -> tuple[list[str], list[str], list[dict[str, Any]]]:
    """Return the errors, the warnings and the rows that parsed."""
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    # Reply text to the splits it appears in, so the same wording landing in
    # both dev and held_out is caught: scoring held_out on text that tuned the
    # threshold is not an independent measurement.
    replies_seen: dict[str, set[str]] = {}

    if not path.exists():
        return [f"{path} does not exist"], [], []

    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("//"):
                continue
            where = f"{path.name}:{number}"
            try:
                raw = json.loads(text)
            except ValueError as exc:
                errors.append(f"{where} is not valid JSON: {exc}")
                continue
            if not isinstance(raw, dict):
                errors.append(f"{where} is not a JSON object")
                continue

            missing = [field for field in REQUIRED_FIELDS if field not in raw]
            if missing:
                errors.append(f"{where} is missing {', '.join(missing)}")
                continue

            row_id = str(raw["id"])
            if row_id in seen_ids:
                errors.append(
                    f"{where} repeats id {row_id!r}, first seen at line {seen_ids[row_id]}"
                )
            else:
                seen_ids[row_id] = number

            label = raw["gold_label"]
            if label not in LABELS:
                errors.append(
                    f"{where} has gold_label {label!r}; expected one of {', '.join(LABELS)}"
                )
            split = raw["split"]
            if split not in SPLITS:
                errors.append(f"{where} has split {split!r}; expected one of {', '.join(SPLITS)}")

            reply = str(raw["reply"]).strip()
            if not reply:
                errors.append(f"{where} has an empty reply")
            else:
                replies_seen.setdefault(reply.casefold(), set()).add(str(split))

            proposal = raw["proposal"]
            if not isinstance(proposal, dict):
                errors.append(f"{where} has a proposal that is not an object")
            elif not _is_whole_cents(proposal.get("amount_cents")):
                errors.append(
                    f"{where} has proposal.amount_cents "
                    f"{proposal.get('amount_cents')!r}; expected positive whole cents"
                )

            # The schema requires an amount on a modify and forbids one anywhere
            # else, so the dataset has to agree with the schema or a gold label
            # describes an answer the classifier is not allowed to give.
            amount = raw.get("modified_amount_cents")
            if label == "modify":
                if amount is not None and not _is_whole_cents(amount):
                    errors.append(
                        f"{where} is a modify with modified_amount_cents {amount!r}; "
                        f"expected positive whole cents"
                    )
            elif amount is not None:
                errors.append(
                    f"{where} has gold_label {label!r} and still carries "
                    f"modified_amount_cents, which the schema forbids"
                )

            rows.append(raw)

    for reply, splits in replies_seen.items():
        if len(splits) > 1:
            shown = reply if len(reply) <= 60 else reply[:57] + "..."
            errors.append(
                f"the reply {shown!r} appears in both {' and '.join(sorted(splits))}, "
                f"which leaks one split into the other"
            )

    warnings = _warnings(rows)
    return errors, warnings, rows


def _warnings(rows: list[dict[str, Any]]) -> list[str]:
    warnings: list[str] = []
    per_split = Counter(str(row.get("split")) for row in rows)
    held_out = per_split.get("held_out", 0)
    if held_out < MIN_HELD_OUT_ROWS:
        warnings.append(
            f"held_out has {held_out} rows, under {MIN_HELD_OUT_ROWS}. A number "
            f"reported from it would move by whole percentage points per row."
        )
    for split in SPLITS:
        for label in LABELS:
            count = sum(
                1
                for row in rows
                if str(row.get("split")) == split and str(row.get("gold_label")) == label
            )
            if count < MIN_ROWS_PER_LABEL:
                warnings.append(
                    f"{split} has {count} {label} rows, under {MIN_ROWS_PER_LABEL}. "
                    f"Precision and recall on it would not mean much."
                )
    return warnings


def counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Rows per split and per label within each split."""
    table: dict[str, dict[str, int]] = {}
    for split in SPLITS:
        in_split = [row for row in rows if str(row.get("split")) == split]
        table[split] = {label: 0 for label in LABELS}
        for row in in_split:
            label = str(row.get("gold_label"))
            if label in table[split]:
                table[split][label] += 1
        table[split]["total"] = len(in_split)
    return table


def print_report(
    path: Path, errors: list[str], warnings: list[str], rows: list[dict[str, Any]]
) -> None:
    print(f"dataset : {path}")
    print(f"rows    : {len(rows)}")
    print()

    table = counts(rows)
    width = max(len(label) for label in (*LABELS, "total")) + 2
    header = "split".ljust(10) + "".join(label.rjust(width) for label in (*LABELS, "total"))
    print(header)
    for split in SPLITS:
        line = split.ljust(10) + "".join(
            str(table[split][label]).rjust(width) for label in (*LABELS, "total")
        )
        print(line)
    print()

    if errors:
        print(f"{len(errors)} error(s):")
        for item in errors:
            print(f"  ERROR   {item}")
        print()
    if warnings:
        print(f"{len(warnings)} warning(s), which do not fail this check:")
        for item in warnings:
            print(f"  warning {item}")
        print()
    if not errors:
        print("No errors. The file is usable." if warnings else "No errors and no warnings.")
        print()
    print(LABELLING_NOTE)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replies", type=Path, default=DEFAULT_REPLIES)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    errors, warnings, rows = validate(arguments.replies)
    print_report(arguments.replies, errors, warnings, rows)
    return 1 if errors else 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
