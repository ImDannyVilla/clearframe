"""The evidence page's bridge to the contract's four actions (#10, `agent/approval.py`). Standard library only.

The page never writes SQLite or `results/` itself. Every write is one of these
four calls, made with exactly the contract's fields ("Approval and the queue
file" -> "The four actions"):

| Action               | Fields                                                  |
| -------------------- | ------------------------------------------------------- |
| confirm_note         | issue_id, approver                                      |
| dismiss_note         | issue_id, approver                                      |
| accept_as_is         | take_id, approver                                       |
| approve_regeneration | take_id, prompt_version, prompt_hash, approver          |

`approval_actions()` calls Gael's functions in `agent.approval`. Each opens its
own `approval.Store` on the database, passes the rubric (so confirming a note
runs prompt-fix, and approve checks `approvers`) and, for approve, the queue
folder. A refusal (`approval.ApprovalRefused`) reaches the page as its reason;
#10 has already logged it as `approval_refused`.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Protocol

FIELDS = {
    "confirm_note": ("issue_id", "approver"),
    "dismiss_note": ("issue_id", "approver"),
    "accept_as_is": ("take_id", "approver"),
    "approve_regeneration": ("take_id", "prompt_version", "prompt_hash", "approver"),
}
NOT_AVAILABLE = "approval actions aren't installed (agent/approval.py, #10)"
FAILED = "the action failed; see the evidence page's log"


class Refused(Exception):
    """An action said no. The message is shown on the page."""


class Actions(Protocol):
    def __call__(self, action: str, **fields) -> None: ...


def check_fields(action: str, fields: dict) -> None:
    if action not in FIELDS or tuple(sorted(fields)) != tuple(sorted(FIELDS[action])):
        raise Refused(f"not a contract action: {action}")


def approval_actions(db_path: Path, rubric_path: Path, queue_dir: Path) -> Actions:
    """The four actions, done by `agent.approval` on the page's database."""

    def call(action: str, **fields) -> None:
        check_fields(action, fields)
        try:
            from agent import approval
            from rubric import rubric as rubric_loader
        except ImportError:
            raise Refused(NOT_AVAILABLE) from None
        policy = rubric_loader.load(rubric_path)
        store = approval.Store(db_path)
        try:
            if action == "confirm_note":
                approval.confirm_note(store, fields["issue_id"], fields["approver"], rubric=policy)
            elif action == "dismiss_note":
                approval.dismiss_note(store, fields["issue_id"], fields["approver"], rubric=policy)
            elif action == "accept_as_is":
                approval.accept_as_is(store, fields["take_id"], fields["approver"])
            else:
                approval.approve_regeneration(
                    store, fields["take_id"], int(fields["prompt_version"]), fields["prompt_hash"],
                    fields["approver"], queue_dir, policy,
                )
        except approval.ApprovalRefused as refusal:
            raise Refused(str(refusal) or "refused") from None
        except Exception:  # noqa: BLE001 - shown as a fixed message; the details go to the log
            traceback.print_exc(file=sys.stderr)
            raise Refused(FAILED) from None
        finally:
            store.close()

    return call
