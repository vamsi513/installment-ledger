"""The reviewer command line.

    python -m assistant.review list
    python -m assistant.review approve <pending_action_id> [--reason TEXT]
    python -m assistant.review reject  <pending_action_id> --reason TEXT
    python -m assistant.review audit   <pending_action_id>

Reads the same settings as the service, so it looks at the same state file. The
reviewer identity written to the audit trail comes from ASSISTANT_REVIEWER_ID
and is not authenticated: it is a name, not a credential.

Exit status is 0 when the command did what it says, 1 when the queue refused it.
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import UTC, datetime

from ..config import Settings, get_settings
from ..pending import PendingAction
from .service import ReviewOutcome, ReviewService

EXIT_OK = 0
EXIT_REFUSED = 1


def _stamp(epoch_seconds: float) -> str:
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%d %H:%M:%SZ")


def _format_held(action: PendingAction) -> str:
    remaining = action.expires_at - time.time()
    expiry = "expired" if remaining <= 0 else f"{int(remaining)}s left"
    return (
        f"{action.pending_action_id}  loan {action.loan_id}  "
        f"{action.amount_cents} cents  prepared {_stamp(action.created_at)}  {expiry}"
    )


def command_list(service: ReviewService, settings: Settings) -> int:
    held = service.held()
    if not service.enabled:
        print("The review queue is off (ASSISTANT_REVIEW_ENABLED), so nothing is ever held.")
    print(
        f"Policy: hold above {settings.review_threshold_cents} cents, or after "
        f"{settings.review_max_prepares} prepares on one loan within "
        f"{settings.review_window_seconds}s."
    )
    if not held:
        print("Nothing is waiting for review.")
        return EXIT_OK
    print(f"{len(held)} waiting for review:")
    for action in held:
        print(f"  {_format_held(action)}")
    return EXIT_OK


def command_decide(
    service: ReviewService,
    settings: Settings,
    *,
    pending_action_id: str,
    reason: str | None,
    approve: bool,
) -> int:
    try:
        reviewer = settings.require_reviewer_id()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED

    if approve:
        result = service.approve(pending_action_id, actor=reviewer, reason=reason)
    else:
        result = service.reject(pending_action_id, actor=reviewer, reason=reason or "")

    if result.is_refusal:
        print(result.message, file=sys.stderr)
        return EXIT_REFUSED

    verb = "approved" if approve else "rejected"
    if result.changed:
        print(f"{pending_action_id} {verb} by {reviewer}.")
    else:
        print(f"{pending_action_id} was already {verb}; nothing changed.")
    if result.outcome in {ReviewOutcome.APPROVED, ReviewOutcome.ALREADY_APPROVED}:
        print("The borrower still has to confirm it before anything is paid.")
    return EXIT_OK


def command_audit(service: ReviewService, pending_action_id: str) -> int:
    entries = service.entries_for(pending_action_id)
    if not entries:
        print(f"No audit entries for {pending_action_id}.")
        return EXIT_OK
    print(f"{len(entries)} audit entries for {pending_action_id}:")
    for entry in entries:
        transition = (
            f"{entry.old_status or '-'} -> {entry.new_status or '-'}"
            if entry.old_status != entry.new_status
            else (entry.new_status or "-")
        )
        reason = f"  reason: {entry.reason}" if entry.reason else ""
        print(
            f"  {_stamp(entry.recorded_at)}  {entry.actor:<16} {entry.action.value:<17} "
            f"{transition}{reason}"
        )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m assistant.review",
        description="Review prepared payments the policy held back.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="Show everything waiting for review.")

    approve = subparsers.add_parser("approve", help="Approve a held payment.")
    approve.add_argument("pending_action_id")
    approve.add_argument("--reason", default=None, help="Recorded in the audit trail.")

    reject = subparsers.add_parser("reject", help="Reject a held payment.")
    reject.add_argument("pending_action_id")
    reject.add_argument("--reason", required=True, help="Required, and recorded.")

    audit = subparsers.add_parser("audit", help="Show the audit trail for one payment.")
    audit.add_argument("pending_action_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    settings = get_settings()
    service = ReviewService(settings)

    if arguments.command == "list":
        return command_list(service, settings)
    if arguments.command == "audit":
        return command_audit(service, arguments.pending_action_id)
    return command_decide(
        service,
        settings,
        pending_action_id=arguments.pending_action_id,
        reason=arguments.reason,
        approve=arguments.command == "approve",
    )


if __name__ == "__main__":
    raise SystemExit(main())
