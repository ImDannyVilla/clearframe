#!/usr/bin/env python3
"""prompt-fix: build the next prompt version when a take becomes flagged.

Pure code, no model, following contracts/README.md "Approval and the queue file":

- Start from version 1, add a blank line, then the fix lines for each measured
  or confirmed issue type, once per type, in rubric order.
- Each type's lines come from the rubric's fix library, one line per label, in
  the order Don't include, Add, Make sure. Lines are joined with a single
  newline; there is no trailing newline.
- A fix line that version 1 already contains (as a whole line) is skipped, so a
  retake that fails again doesn't repeat it. If every line is already there, no
  version is added and the take can only be accepted as is.
- A note confirmed later adds another version, built the same way from version 1.
  The evidence page always shows the latest.

The module takes any object with an sqlite3 connection in the shape of
agent/approval.py's Store; the two are integrated after #7's db lands.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

# Fix-relevant issue states, per the contract's issues table.
FIX_STATES = ("measured", "confirmed")
SCHEMA = "clearframe.queue/1"


class PromptFixError(ValueError):
    """The store or rubric can't produce a prompt version."""


def now_iso() -> str:
    """The contract's time shape: ISO 8601 with the offset."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_text(text: str) -> str:
    """`sha256:` plus 64 hex characters, over the exact UTF-8 bytes."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def fix_issue_types(store, take_id: str, rubric: dict) -> list[str]:
    """The take's measured and confirmed issue types, once each, in rubric order."""
    rows = store.conn.execute(
        "SELECT DISTINCT issue_type FROM issues WHERE take_id = ? AND state IN (?, ?)",
        (take_id, *FIX_STATES),
    ).fetchall()
    present = {row["issue_type"] for row in rows}
    return [t for t in rubric["fix_library"] if t in present]


def fix_lines_for(rubric: dict, issue_type: str) -> list[str]:
    """The fix lines for one issue type, in the order Don't include, Add, Make sure."""
    labels = rubric["fix_labels"]
    entry = rubric["fix_library"][issue_type]
    return [f"{labels[key]}: {text}" for key, text in entry.items()]


def build_version(base_text: str, rubric: dict, issue_types: list[str]):
    """The next prompt version: base_text + blank line + the fix lines.

    Each measured or confirmed issue type contributes once, in rubric order.
    Returns (text, fixes) where fixes is the list of issue types that
    contributed at least one line, in rubric order. Returns None when no line
    survives the skip rule, meaning no new version is added.
    """
    base_lines = set(base_text.splitlines())
    ordered = [t for t in rubric["fix_library"] if t in issue_types]
    lines: list[str] = []
    fixes: list[str] = []
    for issue_type in ordered:
        added = [line for line in fix_lines_for(rubric, issue_type) if line not in base_lines]
        if added:
            fixes.append(issue_type)
            lines.extend(added)
    if not lines:
        return None
    return base_text + "\n\n" + "\n".join(lines), fixes


def add_next_version(store, take_id: str, rubric: dict, at: str | None = None):
    """Insert the take's next prompt version, if the fixes add any new line.

    Writes the prompt_versions row and a `prompt_version_added` event, then
    returns {"version", "prompt", "prompt_hash", "fixes"}. Returns None when
    there is nothing to add (no fix-relevant issues, or every fix line already
    in version 1).
    """
    version_1 = store.prompt_version(take_id, 1)
    if version_1 is None:
        raise PromptFixError(f"take {take_id} has no prompt version 1")
    issue_types = fix_issue_types(store, take_id, rubric)
    if not issue_types:
        return None
    built = build_version(version_1["prompt"], rubric, issue_types)
    if built is None:
        return None
    text, fixes = built
    latest = store.latest_prompt_version(take_id)
    version = (latest["version"] if latest else 1) + 1
    at = at or now_iso()
    prompt_hash = sha256_text(text)
    store.conn.execute(
        "INSERT INTO prompt_versions (take_id, version, prompt, prompt_hash, fixes, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (take_id, version, text, prompt_hash, json.dumps(fixes), at),
    )
    store.log_event(
        "prompt_version_added",
        at=at,
        actor="prompt-fix",
        take_id=take_id,
        detail={"version": version, "fixes": fixes},
    )
    return {"version": version, "prompt": text, "prompt_hash": prompt_hash, "fixes": fixes}


def on_flagged(store, take_id: str, rubric: dict, at: str | None = None):
    """The hook for "a take becomes flagged": add the next prompt version."""
    take = store.take(take_id)
    if take is None or take["status"] != "flagged":
        return None
    return add_next_version(store, take_id, rubric, at=at)
