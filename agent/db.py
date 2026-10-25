#!/usr/bin/env python3
"""The sandbox's SQLite state (agent, #7). Standard library only.

One file, by default /sandbox/clearframe/state/clearframe.db, written only by
the sandbox app. It holds the contract's five tables — takes, issues,
prompt_versions, approvals and events — with the column names and meanings from
contracts/README.md "SQLite tables". No row is ever deleted and events is
append-only.

One operational column sits next to the contract's takes columns: `checks`
holds the latest ingested revision's check states as small JSON, so the status
rule's first match ("any check still running or ended in error") can be
recomputed after a decision without re-reading the results mount.

`reset()` implements the contract's development rerun: it MOVES the database
aside with a timestamp, never deletes it.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

DEFAULT_DB_PATH = "/sandbox/clearframe/state/clearframe.db"
DEFAULT_RESULTS_DIR = "/sandbox/clearframe/host/results"

SCHEMA = """
CREATE TABLE IF NOT EXISTS takes (
    take_id TEXT PRIMARY KEY,
    parent_take_id TEXT NOT NULL DEFAULT '',
    shot INTEGER NOT NULL,
    expected_takes INTEGER NOT NULL,
    status TEXT NOT NULL,
    result_revision INTEGER NOT NULL DEFAULT 0,
    render_sha256 TEXT NOT NULL DEFAULT '',
    fps INTEGER NOT NULL DEFAULT 0,
    frames INTEGER NOT NULL DEFAULT 0,
    brightness_series TEXT NOT NULL DEFAULT '',
    checks TEXT NOT NULL DEFAULT '{}',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    take_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    state TEXT NOT NULL,
    frame_start INTEGER NOT NULL DEFAULT 0,
    frame_end INTEGER NOT NULL DEFAULT 0,
    t_start REAL NOT NULL DEFAULT 0,
    t_end REAL NOT NULL DEFAULT 0,
    value REAL,
    threshold REAL,
    model_note TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '[]',
    decided_by TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prompt_versions (
    take_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    prompt TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    fixes TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    PRIMARY KEY (take_id, version)
);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id INTEGER PRIMARY KEY AUTOINCREMENT,
    take_id TEXT NOT NULL,
    decision TEXT NOT NULL,
    prompt_version INTEGER,
    prompt_hash TEXT NOT NULL DEFAULT '',
    approver TEXT NOT NULL,
    decided_at TEXT NOT NULL,
    queue_file TEXT NOT NULL DEFAULT '',
    next_take_id TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    take_id TEXT NOT NULL DEFAULT '',
    shot INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
"""

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


def now_iso() -> str:
    """The contract's time: ISO 8601 with the local offset, seconds precision."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open the database, creating the file and its five tables if missing."""
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    actor: str = "automation",
    take_id: str = "",
    shot: int | None = None,
    detail: dict | None = None,
    at: str | None = None,
) -> int:
    """Append one event; the table is append-only and the kinds are the contract's."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"unknown event kind: {kind}")
    detail = {} if detail is None else detail
    if not isinstance(detail, (dict, list)):
        raise TypeError("detail must be small JSON-able data")
    cur = conn.execute(
        "INSERT INTO events (at, actor, kind, take_id, shot, detail)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (at or now_iso(), actor, kind, take_id, shot, json.dumps(detail)),
    )
    return int(cur.lastrowid)


def events(conn: sqlite3.Connection, kind: str | None = None, take_id: str | None = None):
    """Events in order, optionally filtered; used by the card's post-once check."""
    sql = "SELECT * FROM events"
    where, args = [], []
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    if take_id is not None:
        where.append("take_id = ?")
        args.append(take_id)
    if where:
        sql += " WHERE " + " AND ".join(where)
    return conn.execute(sql + " ORDER BY event_id", args).fetchall()


def take_row(conn: sqlite3.Connection, take_id: str):
    return conn.execute(
        "SELECT * FROM takes WHERE take_id = ?", (take_id,)
    ).fetchone()


def reset(path: str | Path = DEFAULT_DB_PATH, at: datetime | None = None) -> Path | None:
    """Move the database aside with a timestamp, never delete it. Returns the new path."""
    path = Path(path)
    if not path.exists():
        return None
    stamp = (at or datetime.now()).strftime("%Y%m%dT%H%M%S")
    aside = path.with_name(f"{path.name}.{stamp}.bak")
    counter = 0
    while aside.exists():
        counter += 1
        aside = path.with_name(f"{path.name}.{stamp}.{counter}.bak")
    path.rename(aside)
    return aside
