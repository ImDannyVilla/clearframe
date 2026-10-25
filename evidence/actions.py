"""The evidence page's bridge to the contract's four actions (#10). Standard library only.

The page never writes SQLite or `results/` itself. Every write is one of these
four calls, made with exactly the contract's fields ("Approval and the queue
file" -> "The four actions"):

| Action               | Fields                                                  |
| -------------------- | ------------------------------------------------------- |
| confirm_note         | issue_id, approver                                      |
| dismiss_note         | issue_id, approver                                      |
| accept_as_is         | take_id, approver                                       |
| approve_regeneration | take_id, prompt_version, prompt_hash, approver          |

#10 provides them as functions of those names in `agent.actions`, called with
keyword arguments. An action that refuses raises an exception whose message is
the reason; the page shows that message. Anything else counts as done.

Until `agent.actions` exists, every action is refused with a fixed message, so
the page can be used read-only.
"""
from __future__ import annotations

import importlib
from typing import Protocol

FIELDS = {
    "confirm_note": ("issue_id", "approver"),
    "dismiss_note": ("issue_id", "approver"),
    "accept_as_is": ("take_id", "approver"),
    "approve_regeneration": ("take_id", "prompt_version", "prompt_hash", "approver"),
}
NOT_AVAILABLE = "approval actions aren't installed yet (#10)"


class Refused(Exception):
    """An action said no. The message is shown on the page."""


class Actions(Protocol):
    def __call__(self, action: str, **fields) -> None: ...


def module_actions(module_name: str = "agent.actions") -> Actions:
    """Call the four functions in `agent.actions`; refuse all four if it isn't there yet."""

    def call(action: str, **fields) -> None:
        if action not in FIELDS or tuple(sorted(fields)) != tuple(sorted(FIELDS[action])):
            raise Refused(f"not a contract action: {action}")
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            raise Refused(NOT_AVAILABLE) from None
        function = getattr(module, action, None)
        if not callable(function):
            raise Refused(NOT_AVAILABLE)
        try:
            function(**fields)
        except Exception as error:  # noqa: BLE001 - every refusal reaches the page as its reason
            raise Refused(str(error) or type(error).__name__) from None

    return call
