#!/usr/bin/env python3
"""The status-card automation (#7): result files into SQLite, statuses, and the Slack cards.

Ingest rules, from contracts/README.md "SQLite tables":

1. Read `results/*.json`. Skip `.tmp` files and any file whose `revision`
   isn't above `takes.result_revision`.
2. A new take ID: insert it as `review_pending` and log `take_seen`.
3. The first final revision: insert its issues and prompt version 1, log
   `result_ingested` (and `check_error` for each check that ended in error),
   then recompute the status. A take's issues are written once.
4. Rerunning a take during development means moving `clearframe.db` aside and
   letting this rebuild it from the result files.
5. Post a shot's card once, when every expected original take has a final
   result. Post a retake's card when its own result is final. Each post logs
   `card_posted`.

`python3 agent/ingest.py` runs one pass and exits, for OpenClaw's recurring job.
`agent/poller.py` runs it every 5 s instead.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import card, db, slack  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402

ACTOR = "automation"
RESULT_SCHEMA = "clearframe.result/1"
DEFAULT_RESULTS = Path("/sandbox/clearframe/host/results")
DEFAULT_BASE_URL = "http://127.0.0.1:8090"


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def is_final(result: dict) -> bool:
    checks = result.get("checks") or {}
    return bool(checks) and all(state in ("done", "error") for state in checks.values())


@dataclass
class PassReport:
    ingested: list[str] = field(default_factory=list)  # take IDs whose final result went in
    updated: list[str] = field(default_factory=list)  # take IDs with a newer non-final revision
    skipped: list[str] = field(default_factory=list)  # files not read, with why
    cards: list[str] = field(default_factory=list)  # "shot 4", "retake 4-2r1"
    unposted: list[str] = field(default_factory=list)  # cards ready but not delivered


def read_result(path: Path) -> dict | None:
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(result, dict) or result.get("schema") != RESULT_SCHEMA:
        return None
    if result.get("take_id") != path.stem or not isinstance(result.get("revision"), int):
        return None
    return result


def ingest_result(conn: sqlite3.Connection, result: dict) -> str:
    """Apply rules 1 to 3 to one result. Returns 'ingested', 'updated' or 'skipped'."""
    take_id = result["take_id"]
    render = result.get("render") or {}
    row = conn.execute("SELECT result_revision FROM takes WHERE take_id = ?", (take_id,)).fetchone()
    if row is not None and result["revision"] <= row["result_revision"]:
        return "skipped"
    at = db.now()
    if row is None:
        conn.execute(
            "INSERT INTO takes (take_id, parent_take_id, shot, expected_takes, status, result_revision, "
            "render_sha256, fps, frames, brightness_series, first_seen_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'review_pending', ?, ?, ?, ?, ?, ?, ?)",
            (take_id, result.get("parent_take_id"), result["shot"], result.get("expected_takes"),
             result["revision"], render.get("sha256"), render.get("fps"), render.get("frames"),
             result.get("brightness_series"), at, at),
        )
        db.log_event(conn, ACTOR, "take_seen", take_id, result["shot"], {"revision": result["revision"]}, at)
    else:
        conn.execute(
            "UPDATE takes SET result_revision = ?, expected_takes = COALESCE(?, expected_takes), "
            "render_sha256 = COALESCE(?, render_sha256), fps = COALESCE(?, fps), "
            "frames = COALESCE(?, frames), brightness_series = COALESCE(?, brightness_series), "
            "updated_at = ? WHERE take_id = ?",
            (result["revision"], result.get("expected_takes"), render.get("sha256"), render.get("fps"),
             render.get("frames"), result.get("brightness_series"), at, take_id),
        )

    outcome = "updated"
    if is_final(result) and not db.has_event(conn, "result_ingested", take_id=take_id):
        _write_issues(conn, take_id, result)
        prompt = (result.get("generation") or {}).get("prompt") or ""
        conn.execute(
            "INSERT OR IGNORE INTO prompt_versions (take_id, version, prompt, prompt_hash, fixes, created_at) "
            "VALUES (?, 1, ?, ?, '[]', ?)",
            (take_id, prompt, sha256_text(prompt), at),
        )
        db.log_event(conn, ACTOR, "result_ingested", take_id, result["shot"], {
            "revision": result["revision"],
            "measured": len(result.get("measured") or []),
            "candidates": len(result.get("candidates") or []),
        }, at)
        for check, state in (result.get("checks") or {}).items():
            if state == "error":
                db.log_event(conn, ACTOR, "check_error", take_id, result["shot"], {"check": check}, at)
        outcome = "ingested"
    db.recompute_status(conn, take_id, ACTOR)
    return outcome


def _write_issues(conn: sqlite3.Connection, take_id: str, result: dict) -> None:
    """Measured anomalies first, then candidate notes, numbered 4-2-1, 4-2-2, ..."""
    rows = []
    for item in result.get("measured") or []:
        rows.append((item, "measured", item.get("value"), item.get("threshold"), None))
    for item in result.get("candidates") or []:
        rows.append((item, "candidate", None, None, item.get("model_note") or ""))
    for number, (item, state, value, threshold, note) in enumerate(rows, start=1):
        conn.execute(
            "INSERT INTO issues (issue_id, take_id, issue_type, state, frame_start, frame_end, t_start, t_end, "
            "value, threshold, model_note, evidence) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"{take_id}-{number}", take_id, item["issue_type"], state, item["frame_start"], item["frame_end"],
             item["t_start"], item["t_end"], value, threshold, note, json.dumps(item.get("evidence") or [])),
        )


def _final_originals(conn: sqlite3.Connection, shot: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM takes t WHERE t.shot = ? AND t.parent_take_id IS NULL AND EXISTS "
        "(SELECT 1 FROM events e WHERE e.kind = 'result_ingested' AND e.take_id = t.take_id)",
        (shot,),
    ).fetchone()["n"]


def post_cards(conn: sqlite3.Connection, rubric: dict, poster: slack.Poster, base_url: str,
               report: PassReport) -> None:
    """Rule 5: each shot card and retake card goes out once."""
    for row in conn.execute(
        "SELECT shot, MAX(expected_takes) AS expected FROM takes WHERE parent_take_id IS NULL GROUP BY shot ORDER BY shot"
    ).fetchall():
        shot, expected = row["shot"], row["expected"]
        if not expected or _final_originals(conn, shot) < expected:
            continue
        if db.has_event(conn, "card_posted", shot=shot, detail_match={"card": "shot"}):
            continue
        _post(conn, poster, card.shot_card_from_db(conn, rubric, shot, base_url),
              {"card": "shot"}, None, shot, f"shot {shot}", report)

    retakes = conn.execute(
        "SELECT t.take_id, t.shot FROM takes t WHERE t.parent_take_id IS NOT NULL AND EXISTS "
        "(SELECT 1 FROM events e WHERE e.kind = 'result_ingested' AND e.take_id = t.take_id)"
    ).fetchall()
    for row in sorted(retakes, key=lambda r: card.take_sort_key(r["take_id"])):
        if db.has_event(conn, "card_posted", take_id=row["take_id"], detail_match={"card": "retake"}):
            continue
        _post(conn, poster, card.retake_card_from_db(conn, rubric, row["take_id"], base_url),
              {"card": "retake"}, row["take_id"], row["shot"], f"retake {row['take_id']}", report)


def _post(conn, poster, text, detail, take_id, shot, name, report) -> None:
    try:
        ts = poster(text)
    except slack.SlackError as error:
        print(f"ingest: {name} card not posted: {error}", file=sys.stderr, flush=True)
        report.unposted.append(name)
        return
    if ts is None:
        report.unposted.append(name)
        return
    db.log_event(conn, ACTOR, "card_posted", take_id, shot, {**detail, "ts": ts})
    conn.commit()
    report.cards.append(name)


def run_pass(conn: sqlite3.Connection, results_dir: Path, rubric: dict, poster: slack.Poster,
             base_url: str = DEFAULT_BASE_URL) -> PassReport:
    """One pass over results/: ingest every new revision, then post any card that's due."""
    report = PassReport()
    if results_dir.is_dir():
        for path in sorted(results_dir.glob("*.json")):  # `.json.tmp` files don't match
            result = read_result(path)
            if result is None:
                report.skipped.append(f"{path.name}: not a result file")
                continue
            try:
                with conn:  # one transaction per take
                    outcome = ingest_result(conn, result)
            except (KeyError, TypeError, sqlite3.Error) as error:
                report.skipped.append(f"{path.name}: {type(error).__name__}")
                continue
            if outcome == "ingested":
                report.ingested.append(result["take_id"])
            elif outcome == "updated":
                report.updated.append(result["take_id"])
    post_cards(conn, rubric, poster, base_url, report)
    return report


def settings_from_env() -> tuple[Path, Path, Path, str]:
    env = os.environ.get
    return (
        Path(env("CLEARFRAME_RESULTS", str(DEFAULT_RESULTS))),
        Path(env("CLEARFRAME_DB", str(db.DEFAULT_DB))),
        Path(env("CLEARFRAME_RUBRIC", str(ROOT / "rubric" / "rubric.yaml"))),
        env("EVIDENCE_BASE_URL", DEFAULT_BASE_URL),
    )


def describe(report: PassReport) -> str:
    parts = []
    if report.ingested:
        parts.append("ingested " + ", ".join(report.ingested))
    if report.updated:
        parts.append("updated " + ", ".join(report.updated))
    if report.cards:
        parts.append("posted " + ", ".join(report.cards))
    if report.unposted:
        parts.append("not posted (Slack not configured or refused) " + ", ".join(report.unposted))
    if report.skipped:
        parts.append("skipped " + "; ".join(report.skipped))
    return "; ".join(parts) or "nothing new"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One ingest pass: result files into SQLite, then due cards.")
    parser.parse_args(argv)
    results_dir, db_path, rubric_path, base_url = settings_from_env()
    conn = db.connect(db_path)
    try:
        report = run_pass(conn, results_dir, rubric_loader.load(rubric_path), slack.poster_from_env(), base_url)
    finally:
        conn.close()
    print(f"{db.now()} ingest: {describe(report)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
