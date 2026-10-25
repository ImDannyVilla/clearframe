#!/usr/bin/env python3
"""Approval and the queue file, per contracts/README.md.

The evidence page's four actions (confirm note, dismiss note, accept as is,
approve regeneration) live here as plain functions an evidence server can call;
this module never starts a server. Approval is bound to the take ID, prompt
version, prompt hash, approver and time, and the queue writer refuses it if the
prompt changed after the page showed it.

Every refusal is logged as an `approval_refused` event with the reason, then
raised as ApprovalRefused. Queue files are written as `.tmp` and renamed, never
changed afterwards.

The sqlite schema below mirrors the contract's five tables so this module is
self-contained while #7's agent/db.py is built in parallel; the two are merged
after both land. One extra table, `generation`, holds what the queue file needs
from the result file's generation block.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prompt_fix  # noqa: E402

QUEUE_SCHEMA = "clearframe.queue/1"

STATUSES = (
    "review_pending",
    "needs_confirmation",
    "flagged",
    "clean",
    "accepted_as_is",
    "approved_for_regeneration",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS takes (
    take_id TEXT PRIMARY KEY,
    parent_take_id TEXT NOT NULL DEFAULT '',
    shot INTEGER,
    expected_takes INTEGER,
    status TEXT NOT NULL,
    result_revision INTEGER NOT NULL DEFAULT 0,
    render_sha256 TEXT NOT NULL DEFAULT '',
    fps INTEGER,
    frames INTEGER,
    brightness_series TEXT NOT NULL DEFAULT '',
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS issues (
    issue_id TEXT PRIMARY KEY,
    take_id TEXT NOT NULL,
    issue_type TEXT NOT NULL,
    state TEXT NOT NULL,
    frame_start INTEGER,
    frame_end INTEGER,
    t_start REAL,
    t_end REAL,
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
    fixes TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (take_id, version)
);
CREATE TABLE IF NOT EXISTS approvals (
    approval_id INTEGER PRIMARY KEY,
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
    event_id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    kind TEXT NOT NULL,
    take_id TEXT NOT NULL DEFAULT '',
    shot INTEGER,
    detail TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS generation (
    take_id TEXT PRIMARY KEY,
    tool TEXT NOT NULL,
    model TEXT NOT NULL,
    seed TEXT NOT NULL DEFAULT 'null'
);
"""

# The contract's queue rules: generation repeats the parent's tool and model,
# and its seed only when the tool takes one. Seedance 2.0 on fal doesn't take a
# seed, so there the queue file's seed is null and the retake gets a new one.
SEEDLESS_GENERATION = {("fal.ai", "bytedance/seedance-2.0/text-to-video")}


class ApprovalRefused(Exception):
    """An action or approval was refused; `reason` is safe to show on the page."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    """Small sqlite helper for the contract's tables, one file per sandbox."""

    def __init__(self, path):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def log_event(self, kind, *, at=None, actor="automation", take_id="", shot=None, detail=None):
        """Append to events; the contract keeps it append-only."""
        self.conn.execute(
            "INSERT INTO events (at, actor, kind, take_id, shot, detail) VALUES (?, ?, ?, ?, ?, ?)",
            (
                at or now_iso(),
                actor,
                kind,
                take_id,
                shot,
                json.dumps(detail) if detail is not None else "{}",
            ),
        )
        self.conn.commit()

    def take(self, take_id: str):
        return self.conn.execute(
            "SELECT * FROM takes WHERE take_id = ?", (take_id,)
        ).fetchone()

    def issue(self, issue_id: str):
        return self.conn.execute(
            "SELECT * FROM issues WHERE issue_id = ?", (issue_id,)
        ).fetchone()

    def prompt_version(self, take_id: str, version: int):
        return self.conn.execute(
            "SELECT * FROM prompt_versions WHERE take_id = ? AND version = ?",
            (take_id, version),
        ).fetchone()

    def latest_prompt_version(self, take_id: str):
        return self.conn.execute(
            "SELECT * FROM prompt_versions WHERE take_id = ? ORDER BY version DESC LIMIT 1",
            (take_id,),
        ).fetchone()

    def events(self, take_id: str = "", kinds=None) -> list[dict]:
        query = "SELECT * FROM events"
        clauses, params = [], []
        if take_id:
            clauses.append("take_id = ?")
            params.append(take_id)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" for _ in kinds))
            params.extend(kinds)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY event_id"
        return [dict(row) for row in self.conn.execute(query, params).fetchall()]


def _refuse(store: Store, action: str, take_id: str, shot, approver: str, reason: str, at=None):
    """Every refusal is logged as approval_refused with the reason, then raised."""
    store.log_event(
        "approval_refused",
        at=at,
        actor=approver,
        take_id=take_id or "",
        shot=shot,
        detail={"action": action, "reason": reason},
    )
    raise ApprovalRefused(reason)


def recompute_status(store: Store, take_id: str, at=None) -> str:
    """The contract's first-match-wins rule, from the take's issues and decisions.

    Rule 1 (a check still running or in error) is ingest's concern: a take this
    module hasn't seen a final result for stays review_pending. Decision rows
    win over issues, then measured/confirmed issues, then waiting notes.
    """
    take = store.take(take_id)
    if take is None:
        raise ApprovalRefused(f"no take {take_id}")
    if take["result_revision"] == 0:
        status = "review_pending"
    else:
        decision = store.conn.execute(
            "SELECT decision FROM approvals WHERE take_id = ? ORDER BY approval_id DESC LIMIT 1",
            (take_id,),
        ).fetchone()
        if decision is not None:
            status = decision["decision"]
        elif store.conn.execute(
            "SELECT 1 FROM issues WHERE take_id = ? AND state IN ('measured', 'confirmed') LIMIT 1",
            (take_id,),
        ).fetchone():
            status = "flagged"
        elif store.conn.execute(
            "SELECT 1 FROM issues WHERE take_id = ? AND state = 'candidate' LIMIT 1",
            (take_id,),
        ).fetchone():
            status = "needs_confirmation"
        else:
            status = "clean"
    if status != take["status"]:
        store.conn.execute(
            "UPDATE takes SET status = ?, updated_at = ? WHERE take_id = ?",
            (status, at or now_iso(), take_id),
        )
        store.conn.commit()
        store.log_event(
            "status_changed",
            at=at,
            take_id=take_id,
            shot=take["shot"],
            detail={"old": take["status"], "new": status},
        )
    return status


def _decide_note(store: Store, issue_id: str, approver: str, rubric, decision: str, at=None) -> dict:
    at = at or now_iso()
    issue = store.issue(issue_id)
    if issue is None:
        _refuse(store, f"{decision}_note", "", None, approver, f"no issue {issue_id}", at)
    if issue["state"] != "candidate":
        _refuse(
            store,
            f"{decision}_note",
            issue["take_id"],
            None,
            approver,
            f"issue {issue_id} is {issue['state']}, not candidate",
            at,
        )
    state = "confirmed" if decision == "confirm" else "dismissed"
    store.conn.execute(
        "UPDATE issues SET state = ?, decided_by = ?, decided_at = ? WHERE issue_id = ?",
        (state, approver, at, issue_id),
    )
    store.conn.commit()
    store.log_event(
        f"note_{state}",
        at=at,
        actor=approver,
        take_id=issue["take_id"],
        detail={"issue_id": issue_id, "issue_type": issue["issue_type"]},
    )
    status = recompute_status(store, issue["take_id"], at=at)
    if rubric is not None:
        prompt_fix.on_flagged(store, issue["take_id"], rubric, at=at)
    return {"issue_id": issue_id, "state": state, "status": status}


def confirm_note(store: Store, issue_id: str, approver: str, rubric=None, at=None) -> dict:
    """Confirm a candidate note: allowed while the note is `candidate`."""
    return _decide_note(store, issue_id, approver, rubric, "confirm", at)


def dismiss_note(store: Store, issue_id: str, approver: str, rubric=None, at=None) -> dict:
    """Dismiss a candidate note: allowed while the note is `candidate`."""
    return _decide_note(store, issue_id, approver, rubric, "dismiss", at)


def accept_as_is(store: Store, take_id: str, approver: str, at=None) -> dict:
    """Accept the take: allowed from `flagged` or `needs_confirmation`."""
    at = at or now_iso()
    take = store.take(take_id)
    if take is None:
        _refuse(store, "accept_as_is", take_id, None, approver, f"no take {take_id}", at)
    if take["status"] not in ("flagged", "needs_confirmation"):
        _refuse(
            store,
            "accept_as_is",
            take_id,
            take["shot"],
            approver,
            f"take {take_id} is {take['status']}, not flagged or needs_confirmation",
            at,
        )
    approval_id = _insert_approval(store, take_id, "accepted_as_is", None, "", approver, at)
    store.log_event(
        "accepted_as_is",
        at=at,
        actor=approver,
        take_id=take_id,
        shot=take["shot"],
        detail={"approval_id": approval_id},
    )
    status = recompute_status(store, take_id, at=at)
    return {"take_id": take_id, "decision": "accepted_as_is", "approval_id": approval_id, "status": status}


def _insert_approval(store, take_id, decision, prompt_version, prompt_hash, approver, at,
                     queue_file="", next_take_id="") -> int:
    cursor = store.conn.execute(
        "INSERT INTO approvals (take_id, decision, prompt_version, prompt_hash, approver,"
        " decided_at, queue_file, next_take_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (take_id, decision, prompt_version, prompt_hash, approver, at, queue_file, next_take_id),
    )
    store.conn.commit()
    return cursor.lastrowid


def generation_for(store: Store, take_id: str) -> dict:
    row = store.conn.execute(
        "SELECT tool, model, seed FROM generation WHERE take_id = ?", (take_id,)
    ).fetchone()
    if row is None:
        raise ApprovalRefused(f"take {take_id} has no generation info")
    seed = None if (row["tool"], row["model"]) in SEEDLESS_GENERATION else json.loads(row["seed"])
    return {"tool": row["tool"], "model": row["model"], "seed": seed}


def next_retake_id(store: Store, take_id: str) -> str:
    """The contract's retake numbering, per original take: 4-2 -> 4-2r1, 4-2r1 -> 4-2r2."""
    match = re.fullmatch(r"(.+?)r([0-9]+)", take_id)
    base = match.group(1) if match else take_id
    highest = 0
    candidates = [row["take_id"] for row in store.conn.execute("SELECT take_id FROM takes")]
    candidates += [row["next_take_id"] for row in store.conn.execute(
        "SELECT next_take_id FROM approvals WHERE next_take_id != ''"
    )]
    for candidate in candidates:
        m = re.fullmatch(re.escape(base) + r"r([0-9]+)", candidate)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"{base}r{highest + 1}"


def write_queue_file(queue_dir, request_id: str, payload: dict) -> str:
    """Write <request_id>.json as .tmp, rename it, and never touch it again."""
    queue_dir = Path(queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)
    tmp = queue_dir / (request_id + ".json.tmp")
    final = queue_dir / (request_id + ".json")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, final)
    return final.name


def approve_regeneration(store: Store, take_id: str, prompt_version: int, prompt_hash: str,
                         approver: str, queue_dir, rubric: dict, at=None) -> dict:
    """Approve the fix and queue the regeneration: the contract's checks in order.

    1. approver is listed under `approvers` in the rubric.
    2. The take is `flagged`, no note is waiting, and it has a version above 1.
    3. prompt_version is the take's latest version and prompt_hash equals its
       stored hash.
    4. The hash recomputed from the stored text still matches.

    On success: an approvals row, the queue file, and `approved` + `queued`.
    Any failure: a refusal, logged as `approval_refused` with the reason.
    """
    at = at or now_iso()
    take = store.take(take_id)
    shot = take["shot"] if take is not None else None
    if take is None:
        _refuse(store, "approve_regeneration", take_id, None, approver, f"no take {take_id}", at)
    if approver not in rubric["approvers"]:
        _refuse(
            store,
            "approve_regeneration",
            take_id,
            shot,
            approver,
            f"approver {approver!r} is not listed under approvers in the rubric",
            at,
        )
    if take["status"] != "flagged":
        _refuse(
            store,
            "approve_regeneration",
            take_id,
            shot,
            approver,
            f"take {take_id} is {take['status']}, not flagged",
            at,
        )
    waiting = store.conn.execute(
        "SELECT COUNT(*) AS n FROM issues WHERE take_id = ? AND state = 'candidate'",
        (take_id,),
    ).fetchone()["n"]
    if waiting:
        _refuse(
            store,
            "approve_regeneration",
            take_id,
            shot,
            approver,
            f"{waiting} note(s) still waiting on take {take_id}",
            at,
        )
    latest = store.latest_prompt_version(take_id)
    if latest is None or latest["version"] < 2:
        _refuse(
            store,
            "approve_regeneration",
            take_id,
            shot,
            approver,
            f"take {take_id} has no prompt version above 1",
            at,
        )
    if prompt_version != latest["version"]:
        _refuse(
            store,
            "approve_regeneration",
            take_id,
            shot,
            approver,
            f"prompt_version {prompt_version} is not the latest version ({latest['version']})",
            at,
        )
    if prompt_hash != latest["prompt_hash"]:
        _refuse(
            store,
            "approve_regeneration",
            take_id,
            shot,
            approver,
            "prompt_hash does not match the stored hash",
            at,
        )
    if prompt_fix.sha256_text(latest["prompt"]) != latest["prompt_hash"]:
        _refuse(
            store,
            "approve_regeneration",
            take_id,
            shot,
            approver,
            "the stored prompt text no longer hashes to the stored hash",
            at,
        )
    generation = generation_for(store, take_id)
    next_take_id = next_retake_id(store, take_id)
    approval_id = _insert_approval(
        store, take_id, "approved_for_regeneration", prompt_version, prompt_hash,
        approver, at, queue_file=f"{take_id}-v{prompt_version}.json", next_take_id=next_take_id,
    )
    request_id = f"{take_id}-v{prompt_version}"
    payload = {
        "schema": QUEUE_SCHEMA,
        "request_id": request_id,
        "parent_take_id": take_id,
        "next_take_id": next_take_id,
        "shot": take["shot"],
        "prompt_version": prompt_version,
        "prompt_hash": prompt_hash,
        "prompt": latest["prompt"],
        "fixes": json.loads(latest["fixes"]),
        "generation": generation,
        "approval": {"approval_id": approval_id, "approver": approver, "approved_at": at},
    }
    queue_file = write_queue_file(queue_dir, request_id, payload)
    store.log_event(
        "approved",
        at=at,
        actor=approver,
        take_id=take_id,
        shot=shot,
        detail={"approval_id": approval_id, "prompt_version": prompt_version,
                "next_take_id": next_take_id},
    )
    store.log_event(
        "queued",
        at=at,
        actor=approver,
        take_id=take_id,
        shot=shot,
        detail={"queue_file": queue_file, "next_take_id": next_take_id},
    )
    status = recompute_status(store, take_id, at=at)
    return {
        "take_id": take_id,
        "decision": "approved_for_regeneration",
        "approval_id": approval_id,
        "queue_file": queue_file,
        "next_take_id": next_take_id,
        "status": status,
    }


def link_retake(store: Store, take_id: str, prompt_hash: str, at=None):
    """Link an arrived retake to the approval that queued it.

    Compares the retake's prompt hash with the approval's and logs
    `retake_linked` with the match. Returns the link result, or None when no
    approval queued this take.
    """
    at = at or now_iso()
    approval = store.conn.execute(
        "SELECT * FROM approvals WHERE next_take_id = ?"
        " AND decision = 'approved_for_regeneration' ORDER BY approval_id DESC LIMIT 1",
        (take_id,),
    ).fetchone()
    if approval is None:
        return None
    take = store.take(take_id)
    store.conn.execute(
        "UPDATE takes SET parent_take_id = ?, updated_at = ? WHERE take_id = ?",
        (approval["take_id"], at, take_id),
    )
    store.conn.commit()
    match = prompt_hash == approval["prompt_hash"]
    store.log_event(
        "retake_linked",
        at=at,
        take_id=take_id,
        shot=take["shot"] if take is not None else None,
        detail={
            "parent_take_id": approval["take_id"],
            "approval_id": approval["approval_id"],
            "prompt_hash_match": match,
        },
    )
    return {
        "take_id": take_id,
        "parent_take_id": approval["take_id"],
        "approval_id": approval["approval_id"],
        "prompt_hash_match": match,
    }


def load_result(store: Store, result_path, rubric: dict, at=None) -> dict:
    """Manual-testing ingest: load a result file's final revision.

    Inserts the take, its issues, its generation info and prompt version 1,
    recomputes the status and runs prompt-fix when the take is flagged. The
    real ingest lives in #7; this keeps the CLI demoable on its own.
    """
    at = at or now_iso()
    data = json.loads(Path(result_path).read_text(encoding="utf-8"))
    take_id = data["take_id"]
    checks = data.get("checks", {})
    take = store.take(take_id)
    if take is None:
        store.log_event("take_seen", at=at, take_id=take_id, shot=data.get("shot"),
                        detail={"revision": data.get("revision", 1)})
    revision = data.get("revision", 1)
    if take is not None and revision <= take["result_revision"]:
        return {"take_id": take_id, "status": take["status"], "final": True, "skipped": True}
    final = all(state in ("done", "error") for state in checks.values())
    parent = data.get("parent_take_id") or ""
    store.conn.execute(
        "INSERT INTO takes (take_id, parent_take_id, shot, expected_takes, status,"
        " result_revision, render_sha256, fps, frames, brightness_series,"
        " first_seen_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(take_id) DO UPDATE SET result_revision = excluded.result_revision,"
        " updated_at = excluded.updated_at",
        (
            take_id,
            parent,
            data.get("shot"),
            data.get("expected_takes"),
            "review_pending" if not final else (take["status"] if take else "review_pending"),
            revision,
            (data.get("render") or {}).get("sha256", ""),
            (data.get("render") or {}).get("fps"),
            (data.get("render") or {}).get("frames"),
            data.get("brightness_series", ""),
            at,
            at,
        ),
    )
    generation = data.get("generation") or {}
    store.conn.execute(
        "INSERT INTO generation (take_id, tool, model, seed) VALUES (?, ?, ?, ?)"
        " ON CONFLICT(take_id) DO UPDATE SET tool = excluded.tool, model = excluded.model,"
        " seed = excluded.seed",
        (take_id, generation.get("tool", ""), generation.get("model", ""),
         json.dumps(generation.get("seed"))),
    )
    store.conn.commit()
    if not final:
        return {"take_id": take_id, "status": "review_pending", "final": False}
    for check, state in checks.items():
        if state == "error":
            store.log_event("check_error", at=at, take_id=take_id, shot=data.get("shot"),
                            detail={"check": check})
    ingested = store.conn.execute(
        "SELECT 1 FROM issues WHERE take_id = ? LIMIT 1", (take_id,)
    ).fetchone() is None
    if ingested:
        counter = 0
        for item in data.get("measured", []):
            counter += 1
            _insert_issue(store, take_id, f"{take_id}-{counter}", item, "measured")
        for item in data.get("candidates", []):
            counter += 1
            _insert_issue(store, take_id, f"{take_id}-{counter}", item, "candidate")
        prompt = generation.get("prompt")
        if prompt is not None and store.prompt_version(take_id, 1) is None:
            store.conn.execute(
                "INSERT INTO prompt_versions (take_id, version, prompt, prompt_hash, fixes,"
                " created_at) VALUES (?, 1, ?, ?, '[]', ?)",
                (take_id, prompt, prompt_fix.sha256_text(prompt), at),
            )
            store.conn.commit()
        store.log_event("result_ingested", at=at, take_id=take_id, shot=data.get("shot"),
                        detail={"revision": revision})
    status = recompute_status(store, take_id, at=at)
    if status == "flagged":
        prompt_fix.on_flagged(store, take_id, rubric, at=at)
    return {"take_id": take_id, "status": status, "final": True}


def _insert_issue(store, take_id, issue_id, item, state) -> None:
    store.conn.execute(
        "INSERT INTO issues (issue_id, take_id, issue_type, state, frame_start, frame_end,"
        " t_start, t_end, value, threshold, model_note, evidence) VALUES (?, ?, ?, ?, ?, ?,"
        " ?, ?, ?, ?, ?, ?)",
        (
            issue_id,
            take_id,
            item.get("issue_type", ""),
            state,
            item.get("frame_start"),
            item.get("frame_end"),
            item.get("t_start"),
            item.get("t_end"),
            item.get("value"),
            item.get("threshold"),
            item.get("model_note", ""),
            json.dumps(item.get("evidence", [])),
        ),
    )
    store.conn.commit()


def main(argv=None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from rubric import rubric as rubric_loader  # noqa: E402

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", required=True, help="path to clearframe.db")
    parser.add_argument("--rubric", default=None, help="path to rubric.yaml")
    parser.add_argument("--queue", default="approved_generation_queue",
                        help="queue folder for approve-regen")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("load-result", help="load a result JSON file (manual-testing ingest)")
    p.add_argument("result", help="path to a clearframe.result/1 JSON file")

    p = sub.add_parser("show", help="print a take with its issues, versions, approvals, events")
    p.add_argument("--take-id", required=True)

    for name in ("confirm-note", "dismiss-note"):
        p = sub.add_parser(name, help=f"{name.replace('-', ' ')} a candidate note")
        p.add_argument("--issue-id", required=True)
        p.add_argument("--approver", required=True)

    p = sub.add_parser("accept-as-is", help="accept the take as is")
    p.add_argument("--take-id", required=True)
    p.add_argument("--approver", required=True)

    p = sub.add_parser("approve-regen", help="approve regeneration and write the queue file")
    p.add_argument("--take-id", required=True)
    p.add_argument("--prompt-version", required=True, type=int)
    p.add_argument("--prompt-hash", required=True)
    p.add_argument("--approver", required=True)

    p = sub.add_parser("link-retake", help="link an arrived retake to its approval")
    p.add_argument("--take-id", required=True)
    p.add_argument("--prompt-hash", required=True)

    args = parser.parse_args(argv)
    rubric = rubric_loader.load(args.rubric) if args.rubric else rubric_loader.load()

    try:
        with Store(args.db) as store:
            if args.command == "load-result":
                result = load_result(store, args.result, rubric)
            elif args.command == "show":
                take_id = args.take_id
                result = {
                    "take": dict(store.take(take_id)) if store.take(take_id) else None,
                    "issues": [dict(r) for r in store.conn.execute(
                        "SELECT * FROM issues WHERE take_id = ?", (take_id,))],
                    "prompt_versions": [dict(r) for r in store.conn.execute(
                        "SELECT * FROM prompt_versions WHERE take_id = ? ORDER BY version",
                        (take_id,))],
                    "approvals": [dict(r) for r in store.conn.execute(
                        "SELECT * FROM approvals WHERE take_id = ?", (take_id,))],
                    "events": store.events(take_id),
                }
            elif args.command == "confirm-note":
                result = confirm_note(store, args.issue_id, args.approver, rubric)
            elif args.command == "dismiss-note":
                result = dismiss_note(store, args.issue_id, args.approver, rubric)
            elif args.command == "accept-as-is":
                result = accept_as_is(store, args.take_id, args.approver)
            elif args.command == "approve-regen":
                result = approve_regeneration(
                    store, args.take_id, args.prompt_version, args.prompt_hash,
                    args.approver, args.queue, rubric)
            elif args.command == "link-retake":
                result = link_retake(store, args.take_id, args.prompt_hash)
    except ApprovalRefused as refused:
        print(f"refused: {refused.reason}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
