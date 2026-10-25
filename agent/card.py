#!/usr/bin/env python3
"""The Slack status card (agent, #7). Standard library only.

contracts/README.md "Slack card": code builds every card from the rubric's
fixed templates, using only the contract's fields — shot, reviewed count, take
ID, status label, counts by issue state and type, clean takes, the retake's
parent, and the evidence-page link. No prompt, model text, frame or file name,
approver name or brand ever reaches the payload.

Payloads and rendered text are separate on purpose: a payload is the contract's
field list as data, the renderer fills the rubric's templates, and
tests/test_agent_card.py fails if anything outside the field list leaks in.

Posting goes through an injected transport. The default is a dry run that
prints the card; a Slack incoming-webhook transport exists for the sandbox.
Cards post once per shot (when every expected original take is final) and once
per retake (when its own result is final); each post logs card_posted.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import db, ingest, status  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402

DEFAULT_BASE_URL = "http://127.0.0.1:8090"
RUBRIC_PATH = ROOT / "rubric" / "rubric.yaml"

TAKE_ID = re.compile(r"^(\d+)-(\d+)(?:r(\d+))?$")


class CardError(RuntimeError):
    """A card could not be built or posted."""


def natural_key(take_id: str) -> tuple[int, int, int]:
    """Take order: 4-2 before 4-10, a retake right after its own original."""
    match = TAKE_ID.match(take_id)
    if match is None:
        return (0, 0, 0)
    shot, take, retake = match.groups()
    return (int(shot), int(take), int(retake) if retake else 0)


def rubric_issue_order(rubric: dict) -> list[str]:
    """Rubric order for the card's counts: the fix library's entry order."""
    categories = rubric["card"]["category_labels"]
    return [t for t in rubric["fix_library"] if t in categories]


def counts_for(conn: sqlite3.Connection, take_id: str, rubric: dict) -> str:
    """Counts by state and issue type, in state order then rubric order.

    Dismissed notes aren't counted. One count per state and type, as the
    rubric's count template says: "1 candidate note (road markings)".
    """
    card = rubric["card"]
    rows = conn.execute(
        "SELECT state, issue_type, COUNT(*) AS n FROM issues"
        " WHERE take_id = ? AND state IN ('measured', 'confirmed', 'candidate')"
        " GROUP BY state, issue_type",
        (take_id,),
    ).fetchall()
    by_state_type = {(row["state"], row["issue_type"]): row["n"] for row in rows}
    parts = []
    for state in card["state_labels"]:
        for issue_type in rubric_issue_order(rubric):
            n = by_state_type.get((state, issue_type))
            if n:
                forms = card["state_labels"][state]
                label = forms["one"] if n == 1 else forms["many"]
                parts.append(card["count"].format(
                    count=n, state=label, category=card["category_labels"][issue_type]
                ))
    return card["count_separator"].join(parts)


def display_status(take, rubric: dict) -> str:
    """The card's status field: a check error replaces the status label."""
    card = rubric["card"]
    checks = status.checks_of(take)
    errored = [c for c in rubric_issue_order(rubric) if checks.get(c) == "error"]
    errored += [c for c in checks if checks[c] == "error" and c not in errored]
    if errored:
        categories = card["count_separator"].join(
            card["category_labels"].get(c, c) for c in errored
        )
        return card["check_error"].format(category=categories)
    return card["status_labels"][take["status"]]


def shot_payload(
    conn: sqlite3.Connection,
    shot: int,
    rubric: dict | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> dict:
    """The shot card's payload: only the contract's fields, as plain data."""
    rubric = rubric or rubric_loader.load()
    takes = conn.execute(
        "SELECT * FROM takes WHERE shot = ? AND parent_take_id = ''", (shot,)
    ).fetchall()
    takes = sorted(takes, key=lambda t: natural_key(t["take_id"]))
    entries = []
    clean_takes = []
    for take in takes:
        if take["status"] == "clean":
            clean_takes.append(take["take_id"])
            continue
        entries.append({
            "take_id": take["take_id"],
            "status": display_status(take, rubric),
            "counts": counts_for(conn, take["take_id"], rubric),
        })
    reviewed = sum(
        1 for take in takes if ingest.is_final(status.checks_of(take))
    )
    expected = max((take["expected_takes"] for take in takes), default=0)
    return {
        "shot": shot,
        "reviewed": reviewed,
        "expected_takes": expected,
        "takes": entries,
        "clean_takes": clean_takes,
        "link": f"{base_url}/shot/{shot}",
    }


def retake_payload(
    conn: sqlite3.Connection,
    take_id: str,
    rubric: dict | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> dict:
    """The retake card's payload: take, parent, status, counts, link — nothing else."""
    rubric = rubric or rubric_loader.load()
    take = db.take_row(conn, take_id)
    if take is None or not take["parent_take_id"]:
        raise CardError(f"not a retake take: {take_id}")
    return {
        "take_id": take_id,
        "parent_take_id": take["parent_take_id"],
        "status": display_status(take, rubric),
        "counts": counts_for(conn, take_id, rubric),
        "link": f"{base_url}/take/{take_id}",
    }


def render_shot_card(payload: dict, rubric: dict) -> str:
    templates = rubric["card"]["shot_card"]
    lines = [templates["header"].format(
        shot=payload["shot"], reviewed=payload["reviewed"],
        expected_takes=payload["expected_takes"],
    )]
    for take in payload["takes"]:
        if take["counts"]:
            lines.append(templates["take"].format(**take))
        else:
            lines.append(templates["take_no_counts"].format(**take))
    if payload["clean_takes"]:
        lines.append(templates["clean"].format(
            clean_takes=templates["clean_separator"].join(payload["clean_takes"])
        ))
    else:
        lines.append(templates["clean_none"])
    lines.append(templates["link"].format(link=payload["link"]))
    return "\n".join(lines)


def render_retake_card(payload: dict, rubric: dict) -> str:
    templates = rubric["card"]["retake_card"]
    if payload["counts"]:
        lines = [templates["header"].format(**payload)]
    else:
        lines = [templates["header_no_counts"].format(**payload)]
    lines.append(templates["link"].format(link=payload["link"]))
    return "\n".join(lines)


def dry_run_transport(text: str) -> dict:
    """The default transport: print the card, touch no network, claim no ts."""
    print(text)
    return {}


def webhook_transport(webhook_url: str):
    """A Slack incoming-webhook transport; the only one that leaves the box."""

    def post(text: str) -> dict:
        body = json.dumps({"text": text}).encode("utf-8")
        request = urllib.request.Request(
            webhook_url, data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                reply = response.read().decode("utf-8", "replace").strip()
        except urllib.error.URLError as error:
            raise CardError(f"slack webhook failed: {error}") from error
        ts = reply.strip('"')
        return {"ts": ts} if ts and ts != "ok" else {}

    return post


def _card_posted(
    conn: sqlite3.Connection, take_id: str | None = None, shot: int | None = None
) -> bool:
    sql = "SELECT 1 FROM events WHERE kind = 'card_posted'"
    args: list = []
    if take_id is not None:
        sql += " AND take_id = ?"
        args.append(take_id)
    if shot is not None:
        sql += " AND shot = ? AND take_id = ''"
        args.append(shot)
    return conn.execute(sql + " LIMIT 1", args).fetchone() is not None


def post_due_cards(
    conn: sqlite3.Connection,
    transport=None,
    rubric: dict | None = None,
    base_url: str = DEFAULT_BASE_URL,
    actor: str = "automation",
) -> list[tuple[str, str | int]]:
    """Post every card that is due (ingest rule 5), each once, and log card_posted."""
    rubric = rubric or rubric_loader.load(RUBRIC_PATH)
    transport = transport or dry_run_transport
    posted = []

    shots = conn.execute(
        "SELECT DISTINCT shot FROM takes WHERE parent_take_id = '' ORDER BY shot"
    ).fetchall()
    for row in shots:
        shot = row["shot"]
        takes = conn.execute(
            "SELECT * FROM takes WHERE shot = ? AND parent_take_id = ''", (shot,)
        ).fetchall()
        expected = max((take["expected_takes"] for take in takes), default=0)
        reviewed = sum(1 for take in takes if ingest.is_final(status.checks_of(take)))
        if reviewed < expected or _card_posted(conn, shot=shot):
            continue  # post once, when every expected original take is final
        payload = shot_payload(conn, shot, rubric, base_url)
        response = transport(render_shot_card(payload, rubric)) or {}
        detail = {"card": "shot"}
        detail.update({k: v for k, v in response.items() if v is not None})
        db.log_event(conn, "card_posted", actor=actor, take_id="", shot=shot, detail=detail)
        posted.append(("shot", shot))

    retakes = conn.execute(
        "SELECT take_id, shot FROM takes WHERE parent_take_id <> ''"
        " ORDER BY take_id"
    ).fetchall()
    for row in retakes:
        take_id = row["take_id"]
        take = db.take_row(conn, take_id)
        if not ingest.is_final(status.checks_of(take)) or _card_posted(conn, take_id=take_id):
            continue  # post once, when the retake's own result is final
        payload = retake_payload(conn, take_id, rubric, base_url)
        response = transport(render_retake_card(payload, rubric)) or {}
        detail = {"card": "retake"}
        detail.update({k: v for k, v in response.items() if v is not None})
        db.log_event(conn, "card_posted", actor=actor, take_id=take_id,
                     shot=row["shot"], detail=detail)
        posted.append(("retake", take_id))

    conn.commit()
    return posted
