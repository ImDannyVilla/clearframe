#!/usr/bin/env python3
"""Ingest result files into SQLite (agent, #7). Standard library only.

Implements contracts/README.md "Ingest rules (the automation)":

1. Read results/*.json. Skip .tmp files and any file whose revision isn't
   above takes.result_revision.
2. A new take ID: insert it as review_pending and log take_seen.
3. The first final revision: insert its issues and prompt version 1, log
   result_ingested (and check_error for each check that ended in error), then
   recompute the status. A take's issues are written once.
4. Development reruns move clearframe.db aside (see db.reset, wired to the
   poller's --reset).

The worker writes <take_id>.json.tmp and renames it, so a file being written
is never read here; a file that still parses to nothing useful is skipped.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from agent import db, status

SCHEMA = "clearframe.result/1"
FINAL_CHECK_STATES = ("done", "error")


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_final(checks: dict) -> bool:
    """The revision the worker calls final: every check done or error."""
    return bool(checks) and all(state in FINAL_CHECK_STATES for state in checks.values())


def read_result(path: Path) -> dict | None:
    """One result file, or None when it isn't a result file we can read."""
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(result, dict) or result.get("schema") != SCHEMA:
        return None
    return result


def ingest_dir(
    conn: sqlite3.Connection,
    results_dir: str | Path,
    actor: str = "automation",
) -> list[str]:
    """Ingest every new revision in results_dir, in file order.

    Returns the take IDs whose revision moved the take forward. One pass is
    one commit; re-running a pass is safe because rule 1 skips old revisions.
    """
    ingested = []
    for path in sorted(Path(results_dir).glob("*.json")):
        if path.name.endswith(".tmp"):
            continue  # rule 1: the worker writes .tmp first; never read half a file
        if ingest_file(conn, path, actor=actor):
            ingested.append(path.stem)
    conn.commit()
    return ingested


def ingest_file(
    conn: sqlite3.Connection,
    path: str | Path,
    actor: str = "automation",
) -> bool:
    """Ingest one result file. Returns True when its revision was new."""
    path = Path(path)
    result = read_result(path)
    if result is None:
        return False
    return ingest_result(conn, result, take_id=path.stem, actor=actor)


def ingest_result(
    conn: sqlite3.Connection,
    result: dict,
    take_id: str,
    actor: str = "automation",
) -> bool:
    """Ingest one parsed result under its take ID (the file name is the take ID)."""
    revision = int(result.get("revision") or 0)
    checks = dict(result.get("checks") or {})
    take = db.take_row(conn, take_id)
    if take is not None and revision <= take["result_revision"]:
        return False  # rule 1: only revisions above the highest ingested

    now = db.now_iso()
    render = result.get("render") or {}
    row = {
        "take_id": take_id,
        "parent_take_id": result.get("parent_take_id") or "",
        "shot": int(result.get("shot") or 0),
        "expected_takes": int(result.get("expected_takes") or 0),
        "result_revision": revision,
        "render_sha256": render.get("sha256") or "",
        "fps": int(render.get("fps") or 0),
        "frames": int(render.get("frames") or 0),
        "brightness_series": result.get("brightness_series") or "",
        "checks": json.dumps(checks),
        "updated_at": now,
    }
    if take is None:
        # rule 2: a new take ID starts at review_pending with take_seen.
        conn.execute(
            "INSERT INTO takes (take_id, parent_take_id, shot, expected_takes, status,"
            " result_revision, render_sha256, fps, frames, brightness_series, checks,"
            " first_seen_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["take_id"], row["parent_take_id"], row["shot"],
                row["expected_takes"], "review_pending", row["result_revision"],
                row["render_sha256"], row["fps"], row["frames"],
                row["brightness_series"], row["checks"], now, now,
            ),
        )
        db.log_event(conn, "take_seen", actor=actor, take_id=take_id,
                     shot=row["shot"], detail={"revision": revision})
    else:
        conn.execute(
            "UPDATE takes SET parent_take_id = ?, shot = ?, expected_takes = ?,"
            " result_revision = ?, render_sha256 = ?, fps = ?, frames = ?,"
            " brightness_series = ?, checks = ?, updated_at = ?"
            " WHERE take_id = ?",
            (
                row["parent_take_id"], row["shot"], row["expected_takes"],
                row["result_revision"], row["render_sha256"], row["fps"],
                row["frames"], row["brightness_series"], row["checks"],
                row["updated_at"], take_id,
            ),
        )

    # rule 3: the first final revision writes the take's issues and prompt
    # version 1, once per take, and logs result_ingested with any check_error.
    if is_final(checks):
        if not _has_issues(conn, take_id):
            _insert_issues(conn, take_id, result)
        if not _has_prompt_version(conn, take_id, 1):
            _insert_prompt_version_1(conn, take_id, result, actor, now)
        db.log_event(conn, "result_ingested", actor=actor, take_id=take_id,
                     shot=row["shot"], detail={"revision": revision})
        for check, state in checks.items():
            if state == "error":
                db.log_event(conn, "check_error", actor=actor, take_id=take_id,
                             shot=row["shot"], detail={"check": check})

    status.apply(conn, take_id, actor=actor, checks=checks)
    return True


def _has_issues(conn: sqlite3.Connection, take_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM issues WHERE take_id = ? LIMIT 1", (take_id,)
    ).fetchone() is not None


def _has_prompt_version(conn: sqlite3.Connection, take_id: str, version: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM prompt_versions WHERE take_id = ? AND version = ?",
        (take_id, version),
    ).fetchone() is not None


def _insert_issues(conn: sqlite3.Connection, take_id: str, result: dict) -> None:
    """A take's issues, written once: measured anomalies, then candidate notes."""
    items = [
        ("measured", item) for item in result.get("measured") or []
    ] + [
        ("candidate", item) for item in result.get("candidates") or []
    ]
    for counter, (state, item) in enumerate(items, start=1):
        evidence = item.get("evidence") or []
        conn.execute(
            "INSERT INTO issues (issue_id, take_id, issue_type, state, frame_start,"
            " frame_end, t_start, t_end, value, threshold, model_note, evidence)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"{take_id}-{counter}",
                take_id,
                item.get("issue_type") or "",
                state,
                int(item.get("frame_start") or 0),
                int(item.get("frame_end") or 0),
                float(item.get("t_start") or 0.0),
                float(item.get("t_end") or 0.0),
                item.get("value") if state == "measured" else None,
                item.get("threshold") if state == "measured" else None,
                item.get("model_note") or "",
                json.dumps(evidence),
            ),
        )


def _insert_prompt_version_1(
    conn: sqlite3.Connection,
    take_id: str,
    result: dict,
    actor: str,
    at: str,
) -> None:
    """The generation log's prompt becomes prompt version 1: fixes [], hash over exact text."""
    generation = result.get("generation") or {}
    prompt = generation.get("prompt") or ""
    conn.execute(
        "INSERT INTO prompt_versions (take_id, version, prompt, prompt_hash, fixes, created_at)"
        " VALUES (?, 1, ?, ?, '[]', ?)",
        (take_id, prompt, sha256_text(prompt), at),
    )
    db.log_event(conn, "prompt_version_added", actor=actor, take_id=take_id,
                 shot=int(result.get("shot") or 0), detail={"version": 1})
