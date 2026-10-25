"""ClearFrame's SQLite file: the five tables, events and the status rule. Standard library only.

One file, `/sandbox/clearframe/state/clearframe.db`, written only by the sandbox
app, exactly as contracts/README.md "SQLite tables" says. No row is ever
deleted, and `events` is append-only.

The ingest (#7), the four actions (#10) and the evidence page (#8) all go
through this module:

- `connect(path)` opens the file and creates the tables on first use.
- `log_event(...)` appends to the audit trail.
- `recompute_status(conn, take_id, actor)` applies the contract's status rule.
  Call it after every ingest and every decision; it logs `status_changed`.

Empty values from the contract ("empty for originals", "empty for notes") are
stored as NULL.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB = Path("/sandbox/clearframe/state/clearframe.db")

STATUSES = (
    "review_pending",
    "needs_confirmation",
    "flagged",
    "clean",
    "accepted_as_is",
    "approved_for_regeneration",
)
DECISIONS = ("approved_for_regeneration", "accepted_as_is")
ISSUE_STATES = ("measured", "candidate", "confirmed", "dismissed")
EVENT_KINDS = (
    "take_seen",
    "result_ingested",
    "check_error",
    "status_changed",
    "card_posted",
    "note_confirmed",
    "note_dismissed",
    "prompt_version_added",
    "approval_refused",
    "approved",
    "accepted_as_is",
    "queued",
    "retake_linked",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS takes (
    take_id TEXT PRIMARY KEY,
    parent_take_id TEXT,
    shot INTEGER NOT NULL,
    expected_takes INTEGER,
    status TEXT NOT NULL,
    result_revision INTEGER NOT NULL,
    render_sha256 TEXT,
    fps INTEGER,
    frames INTEGER,
    brightness_series TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    take_id TEXT NOT NULL REFERENCES takes(take_id),
    issue_type TEXT NOT NULL,
    state TEXT NOT NULL,
    frame_start INTEGER NOT NULL,
    frame_end INTEGER NOT NULL,
    t_start REAL NOT NULL,
    t_end REAL NOT NULL,
    value REAL,
    threshold REAL,
    model_note TEXT,
    evidence TEXT NOT NULL,
    decided_by TEXT,
    decided_at TEXT
);
CREATE TABLE IF NOT EXISTS prompt_versions (
    take_id TEXT NOT NULL REFERENCES takes(take_id),
    version INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    fixes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (take_id, version)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    take_id TEXT NOT NULL REFERENCES takes(take_id),
    decision TEXT NOT NULL,
    prompt_version INTEGER,
    prompt_hash TEXT,
    approver TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    queue_file TEXT,
    next_take_id TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    take_id TEXT,
    shot INTEGER,
    detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS issues_by_take ON issues (take_id);
CREATE INDEX IF NOT EXISTS events_by_take ON events (take_id, kind);
"""


def now() -> str:
    """ISO 8601 with the offset, as the contract says."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open the database, creating its folder and tables on first use."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def connect_readonly(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open the database read-only, for the evidence page."""
    conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def log_event(
    conn: sqlite3.Connection,
    actor: str,
    kind: str,
    take_id: str | None = None,
    shot: int | None = None,
    detail: dict | None = None,
    at: str | None = None,
) -> None:
    """Append one event. `detail` is small JSON: no prompt text, no model notes."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    conn.execute(
        "INSERT INTO events (at, actor, kind, take_id, shot, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (at or now(), actor, kind, take_id, shot, json.dumps(detail or {}, sort_keys=True)),
    )


def has_event(conn: sqlite3.Connection, kind: str, take_id: str | None = None, shot: int | None = None,
              detail_match: dict | None = None) -> bool:
    """Whether an event of this kind exists for the take or shot (and, optionally, with these detail values)."""
    rows = conn.execute(
        "SELECT detail FROM events WHERE kind = ? AND (? IS NULL OR take_id = ?) AND (? IS NULL OR shot = ?)",
        (kind, take_id, take_id, shot, shot),
    ).fetchall()
    if detail_match is None:
        return bool(rows)
    for row in rows:
        detail = json.loads(row["detail"])
        if all(detail.get(key) == value for key, value in detail_match.items()):
            return True
    return False


def errored_checks(conn: sqlite3.Connection, take_id: str) -> list[str]:
    """The checks that ended in error in the take's final result, from its `check_error` events."""
    rows = conn.execute(
        "SELECT detail FROM events WHERE kind = 'check_error' AND take_id = ? ORDER BY event_id", (take_id,)
    ).fetchall()
    return [json.loads(row["detail"])["check"] for row in rows]


def compute_status(conn: sqlite3.Connection, take_id: str) -> str:
    """The contract's status rule, first match wins.

    1. A check still `running` (no final result ingested yet) or one that ended
       in `error` -> review_pending. The tables hold no check states, so this
       reads the `result_ingested` and `check_error` events the ingest logs.
    2. A decision is recorded -> its decision.
    3. A measured anomaly or a confirmed note -> flagged.
    4. A candidate note not yet confirmed or dismissed -> needs_confirmation.
    5. Otherwise -> clean.
    """
    if not has_event(conn, "result_ingested", take_id=take_id) or errored_checks(conn, take_id):
        return "review_pending"
    decision = conn.execute(
        "SELECT decision FROM approvals WHERE take_id = ? ORDER BY approval_id DESC LIMIT 1", (take_id,)
    ).fetchone()
    if decision:
        return decision["decision"]
    states = {row["state"] for row in conn.execute("SELECT state FROM issues WHERE take_id = ?", (take_id,))}
    if states & {"measured", "confirmed"}:
        return "flagged"
    if "candidate" in states:
        return "needs_confirmation"
    return "clean"


def recompute_status(conn: sqlite3.Connection, take_id: str, actor: str) -> tuple[str, str]:
    """Apply the status rule to one take; log `status_changed` if it moved. Returns (old, new)."""
    row = conn.execute("SELECT status, shot FROM takes WHERE take_id = ?", (take_id,)).fetchone()
    if row is None:
        raise KeyError(f"no such take: {take_id}")
    old, new = row["status"], compute_status(conn, take_id)
    if new != old:
        conn.execute("UPDATE takes SET status = ?, updated_at = ? WHERE take_id = ?", (new, now(), take_id))
        log_event(conn, actor, "status_changed", take_id, row["shot"], {"from": old, "to": new})
    return old, new
