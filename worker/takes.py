"""Take IDs and the producer's two input files. Standard library only.

Take IDs follow the contract: `<shot>-<take>`, plus `r<n>` for a retake. The
first retake of 4-2 is 4-2r1 (parent 4-2); a retake of that is 4-2r2 (parent 4-2r1).
"""
from __future__ import annotations

import csv
import re
from pathlib import Path

TAKE_ID = re.compile(r"(?P<shot>[0-9]+)-(?P<take>[0-9]+)(?:r(?P<retake>[1-9][0-9]*))?")

# Render file types the worker picks up. The file name (without suffix) is the take ID.
RENDER_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")


def parse_take_id(take_id: str) -> tuple[int, str | None] | None:
    """(shot, parent_take_id) for a valid take ID, or None."""
    match = TAKE_ID.fullmatch(take_id)
    if not match:
        return None
    shot = int(match["shot"])
    retake = match["retake"]
    if retake is None:
        return shot, None
    original = f"{match['shot']}-{match['take']}"
    number = int(retake)
    return shot, original if number == 1 else f"{original}r{number - 1}"


def _rows(path: Path) -> list[dict[str, str]]:
    # utf-8-sig: a CSV saved from a spreadsheet often starts with a byte-order mark.
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return [
            {(key or "").strip(): (value if value is not None else "") for key, value in row.items()}
            for row in csv.DictReader(handle)
        ]


def _blank_to_none(value: str) -> str | None:
    value = value.strip()
    return value or None


def _seed(value: str) -> int | str | None:
    value = value.strip()
    if not value:
        return None
    return int(value) if re.fullmatch(r"-?[0-9]+", value) else value


def read_shot_list(path: Path) -> dict[int, dict]:
    """shot -> {"description", "expected_takes"} from shot_list.csv."""
    shots: dict[int, dict] = {}
    for row in _rows(path):
        shot = row.get("shot", "").strip()
        expected = row.get("expected_takes", "").strip()
        if not shot.isdigit() or not expected.isdigit():
            continue
        shots[int(shot)] = {
            "description": row.get("description", "").strip(),
            "expected_takes": int(expected),
        }
    return shots


def read_generation_log(path: Path) -> dict[str, dict]:
    """take_id -> the result file's `generation` block, from generation_log.csv.

    The prompt is kept byte for byte: its hash becomes prompt version 1's hash.
    """
    log: dict[str, dict] = {}
    for row in _rows(path):
        take_id = row.get("take_id", "").strip()
        if not take_id:
            continue
        log[take_id] = {
            "tool": _blank_to_none(row.get("tool", "")),
            "model": _blank_to_none(row.get("model", "")),
            "seed": _seed(row.get("seed", "")),
            "prompt": row.get("prompt", ""),
        }
    return log
