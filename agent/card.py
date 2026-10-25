"""The Slack status card, built by code from the rubric's fixed templates. Standard library only.

Only the contract's card fields reach a card: shot, reviewed, expected_takes,
take IDs, status labels, counts by state and category, clean takes, the parent
of a retake and the evidence-page link. `build_shot_card` and
`build_retake_card` refuse any other field, so a prompt, a model note, a frame
or a file name can't be passed in by mistake.
"""
from __future__ import annotations

import re
import sqlite3

from agent import db

# The contract's card fields ("Slack card" -> "Fields").
SHOT_CARD_FIELDS = frozenset({"shot", "expected_takes", "takes", "link"})
TAKE_FIELDS = frozenset({"take_id", "status", "counts", "check_errors"})
RETAKE_CARD_FIELDS = frozenset({"take_id", "parent_take_id", "status", "counts", "check_errors", "link"})
# A count: (issue state, issue type) for every issue that isn't dismissed.
COUNTED_STATES = ("measured", "confirmed", "candidate")

TAKE_ID = re.compile(r"[0-9]+-[0-9]+(?:r[1-9][0-9]*)?")
# The evidence-page link: `EVIDENCE_BASE_URL` is configurable (agent/README.md),
# so any host is allowed, but the path must be a shot or take page.
LINK = re.compile(r"https?://[A-Za-z0-9.-]+:[0-9]+/(?:shot/[0-9]+|take/[0-9]+-[0-9]+(?:r[1-9][0-9]*)?)")


class CardError(ValueError):
    """A card was asked to carry something outside the contract's fields."""


def _only(fields: dict, allowed: frozenset, what: str) -> None:
    extra = set(fields) - allowed
    if extra:
        raise CardError(f"{what} may carry only {sorted(allowed)}, not {sorted(extra)}")


def _check_take_id(value) -> str:
    if not isinstance(value, str) or not TAKE_ID.fullmatch(value):
        raise CardError(f"not a take ID: {value!r}")
    return value


def _check_link(value) -> str:
    if not isinstance(value, str) or not LINK.fullmatch(value):
        raise CardError("the link must be an evidence-page link (base URL plus /shot/<n> or /take/<id>)")
    return value


def counts_text(rubric: dict, counts: list[tuple[str, str]]) -> str:
    """'1 candidate note (road markings)', in state-label order, then rubric order."""
    card = rubric["card"]
    parts = []
    for state, forms in card["state_labels"].items():
        for issue_type in rubric["fix_library"]:
            count = counts.count((state, issue_type))
            if count:
                label = forms["one"] if count == 1 else forms["many"]
                category = card["category_labels"][issue_type]
                parts.append(card["count"].format(count=count, state=label, category=category))
    return card["count_separator"].join(parts)


def status_text(rubric: dict, status: str, check_errors: list[str]) -> str:
    """The status label, or 'check error (category)' in its place when a check ended in error."""
    card = rubric["card"]
    if check_errors:
        categories = card["count_separator"].join(card["category_labels"][check] for check in check_errors)
        return card["check_error"].format(category=categories)
    return card["status_labels"][status]


def _take_parts(rubric: dict, take: dict) -> tuple[str, str, str]:
    _only(take, TAKE_FIELDS, "a take on the card")
    take_id = _check_take_id(take["take_id"])
    if take["status"] not in db.STATUSES:
        raise CardError(f"unknown status: {take['status']!r}")
    counts = list(take.get("counts", []))
    for state, issue_type in counts:
        if state not in COUNTED_STATES or issue_type not in rubric["card"]["category_labels"]:
            raise CardError(f"not a card count: {(state, issue_type)!r}")
    errors = list(take.get("check_errors", []))
    for check in errors:
        if check not in rubric["card"]["category_labels"]:
            raise CardError(f"not an issue type: {check!r}")
    return take_id, status_text(rubric, take["status"], errors), counts_text(rubric, counts)


def build_shot_card(rubric: dict, **fields) -> str:
    """The shot card. `takes` lists every final original take, each as TAKE_FIELDS, in take order."""
    _only(fields, SHOT_CARD_FIELDS, "the shot card")
    card = rubric["card"]
    shot_card = card["shot_card"]
    shot, expected, takes = int(fields["shot"]), int(fields["expected_takes"]), list(fields["takes"])
    lines = [shot_card["header"].format(shot=shot, reviewed=len(takes), expected_takes=expected)]
    clean = []
    for take in takes:
        take_id, status, counts = _take_parts(rubric, take)
        if take["status"] == "clean" and not take.get("check_errors"):
            clean.append(take_id)
            continue
        template = shot_card["take"] if counts else shot_card["take_no_counts"]
        lines.append(template.format(take_id=take_id, status=status, counts=counts))
    if clean:
        lines.append(shot_card["clean"].format(clean_takes=shot_card["clean_separator"].join(clean)))
    else:
        lines.append(shot_card["clean_none"])
    lines.append(shot_card["link"].format(link=_check_link(fields["link"])))
    return "\n".join(lines)


def build_retake_card(rubric: dict, **fields) -> str:
    """The retake card, posted once when a retake's result is final."""
    _only(fields, RETAKE_CARD_FIELDS, "the retake card")
    retake_card = rubric["card"]["retake_card"]
    take = {key: fields[key] for key in ("take_id", "status", "counts", "check_errors") if key in fields}
    take_id, status, counts = _take_parts(rubric, take)
    parent = _check_take_id(fields["parent_take_id"])
    template = retake_card["header"] if counts else retake_card["header_no_counts"]
    header = template.format(take_id=take_id, parent_take_id=parent, status=status, counts=counts)
    return "\n".join([header, retake_card["link"].format(link=_check_link(fields["link"]))])


# Reading the fields from SQLite ---------------------------------------------------------------

def take_sort_key(take_id: str) -> tuple[int, int, int]:
    """4-2 before 4-10, and 4-2r1 after 4-2."""
    shot, rest = take_id.split("-", 1)
    take, _, retake = rest.partition("r")
    return int(shot), int(take), int(retake or 0)


def take_fields(conn: sqlite3.Connection, take_id: str) -> dict:
    """One take's card fields: take ID, status, counts by state and type, and errored checks."""
    row = conn.execute("SELECT status FROM takes WHERE take_id = ?", (take_id,)).fetchone()
    counts = [
        (issue["state"], issue["issue_type"])
        for issue in conn.execute(
            "SELECT state, issue_type FROM issues WHERE take_id = ? AND state != 'dismissed' ORDER BY issue_id",
            (take_id,),
        )
    ]
    return {"take_id": take_id, "status": row["status"], "counts": counts,
            "check_errors": db.errored_checks(conn, take_id)}


def shot_card_from_db(conn: sqlite3.Connection, rubric: dict, shot: int, base_url: str) -> str:
    final = [
        row["take_id"]
        for row in conn.execute(
            "SELECT t.take_id FROM takes t WHERE t.shot = ? AND t.parent_take_id IS NULL AND EXISTS "
            "(SELECT 1 FROM events e WHERE e.kind = 'result_ingested' AND e.take_id = t.take_id)",
            (shot,),
        )
    ]
    expected = conn.execute(
        "SELECT MAX(expected_takes) AS n FROM takes WHERE shot = ? AND parent_take_id IS NULL", (shot,)
    ).fetchone()["n"]
    takes = [take_fields(conn, take_id) for take_id in sorted(final, key=take_sort_key)]
    return build_shot_card(rubric, shot=shot, expected_takes=expected, takes=takes,
                           link=f"{base_url.rstrip('/')}/shot/{shot}")


def retake_card_from_db(conn: sqlite3.Connection, rubric: dict, take_id: str, base_url: str) -> str:
    parent = conn.execute("SELECT parent_take_id FROM takes WHERE take_id = ?", (take_id,)).fetchone()
    return build_retake_card(rubric, parent_take_id=parent["parent_take_id"],
                             link=f"{base_url.rstrip('/')}/take/{take_id}", **take_fields(conn, take_id))
