#!/usr/bin/env python3
"""A take's status, recomputed by the contract's first-match rule (agent, #7).

contracts/README.md "Statuses": code recomputes a take's status after every
ingest and every decision, from its checks, issues and decisions. No person or
model sets it directly. The rule (first match wins):

1. Any check is still `running` or ended in `error` -> review_pending.
2. A decision is recorded -> accepted_as_is or approved_for_regeneration.
3. Any measured anomaly, or any confirmed note -> flagged.
4. Any candidate note not yet confirmed or dismissed -> needs_confirmation.
5. Otherwise -> clean.
"""
from __future__ import annotations

import json
import sqlite3

from agent import db

STATUSES = (
    "review_pending",
    "needs_confirmation",
    "flagged",
    "clean",
    "accepted_as_is",
    "approved_for_regeneration",
)

# Check states that keep a take in review_pending (rule 1).
BLOCKING_CHECK_STATES = ("running", "error")
# Decisions a person can record (rule 2). approvals.decision holds one of these.
DECISION_STATUSES = ("accepted_as_is", "approved_for_regeneration")
# Issue states that mean "something to fix" (rule 3), and notes waiting (rule 4).
MEASURED_OR_CONFIRMED = ("measured", "confirmed")


def checks_of(take) -> dict:
    """The take's latest ingested check states: {check name: running|done|error}."""
    return json.loads(take["checks"] or "{}")


def compute(conn: sqlite3.Connection, take_id: str, checks: dict | None = None) -> str:
    """The contract's first-match status for a take."""
    take = db.take_row(conn, take_id)
    if take is None:
        raise KeyError(f"unknown take: {take_id}")
    if checks is None:
        checks = checks_of(take)
    # 1. Any check is still running or ended in error.
    if any(state in BLOCKING_CHECK_STATES for state in checks.values()):
        return "review_pending"
    # 2. A decision is recorded. Decisions are final: the latest one stands.
    decision = conn.execute(
        "SELECT decision FROM approvals WHERE take_id = ?"
        " ORDER BY approval_id DESC LIMIT 1",
        (take_id,),
    ).fetchone()
    if decision is not None and decision["decision"] in DECISION_STATUSES:
        return decision["decision"]
    # 3 and 4. Issues, from what the first final revision wrote plus decisions on notes.
    states = {
        row["state"]
        for row in conn.execute(
            "SELECT state FROM issues WHERE take_id = ?", (take_id,)
        )
    }
    if states & set(MEASURED_OR_CONFIRMED):
        return "flagged"
    if "candidate" in states:
        return "needs_confirmation"
    return "clean"


def apply(
    conn: sqlite3.Connection,
    take_id: str,
    actor: str = "automation",
    checks: dict | None = None,
) -> tuple[str, str]:
    """Recompute and store the status; log status_changed when it actually changed."""
    take = db.take_row(conn, take_id)
    if take is None:
        raise KeyError(f"unknown take: {take_id}")
    old = take["status"]
    new = compute(conn, take_id, checks=checks)
    if new != old:
        conn.execute(
            "UPDATE takes SET status = ?, updated_at = ? WHERE take_id = ?",
            (new, db.now_iso(), take_id),
        )
        db.log_event(
            conn,
            "status_changed",
            actor=actor,
            take_id=take_id,
            shot=take["shot"],
            detail={"old": old, "new": new},
        )
    return old, new
