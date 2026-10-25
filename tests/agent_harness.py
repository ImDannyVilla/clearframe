"""Shared helpers for the agent tests (#7). Not a test module itself.

Builds result files on top of the contract sample, so laptop tests need no
footage and no model.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SAMPLE = json.loads(
    (ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8")
)

# The contract states version 1's hash; the sample prompt is that version.
PROMPT_V1 = SAMPLE["generation"]["prompt"]
PROMPT_V1_HASH = "sha256:ee4aa7f4a4c28994d974159b541e8f8b372c29b03ade2ad7098b62c6ee08b89d"

DONE_CHECKS = {"exposure_flicker": "done", "road_markings": "done"}
RUNNING_CHECKS = {"exposure_flicker": "running", "road_markings": "running"}

MEASURED_FLICKER = {
    "issue_type": "exposure_flicker",
    "frame_start": 61,
    "frame_end": 63,
    "t_start": 2.542,
    "t_end": 2.625,
    "value": 18.4,
    "threshold": 8.0,
    "unit": "mean brightness change between consecutive frames, 0-255",
    "evidence": [
        "frames/4-5/000060.jpg",
        "frames/4-5/000061.jpg",
        "frames/4-5/000062.jpg",
        "frames/4-5/000063.jpg",
    ],
}

CANDIDATE_ROAD = {
    "issue_type": "road_markings",
    "pass": "sparse",
    "frame_start": 48,
    "frame_end": 64,
    "t_start": 2.0,
    "t_end": 2.667,
    "model_note": "The arrow in the right lane points toward the camera, against the direction of travel.",
    "evidence": ["frames/4-2/000048.jpg", "frames/4-2/000056.jpg", "frames/4-2/000064.jpg"],
}


def make_result(
    take_id: str,
    revision: int = 2,
    checks: dict | None = None,
    measured: list | None = None,
    candidates: list | None = None,
    shot: int = 4,
    expected_takes: int = 6,
    parent_take_id: str | None = None,
    prompt: str = PROMPT_V1,
) -> dict:
    """A result file shaped like the contract sample, named by its take ID."""
    result = copy.deepcopy(SAMPLE)
    result["take_id"] = take_id
    result["parent_take_id"] = parent_take_id
    result["shot"] = shot
    result["expected_takes"] = expected_takes
    result["revision"] = revision
    result["checks"] = dict(checks) if checks is not None else dict(DONE_CHECKS)
    result["measured"] = copy.deepcopy(measured) if measured is not None else []
    result["candidates"] = copy.deepcopy(candidates) if candidates is not None else []
    result["generation"]["prompt"] = prompt
    result["brightness_series"] = f"frames/{take_id}/brightness.json"
    return result


def write_result(results_dir: Path, result: dict) -> Path:
    """Write <take_id>.json the way the worker does: .tmp first, then rename."""
    path = results_dir / f"{result['take_id']}.json"
    tmp = results_dir / f"{result['take_id']}.json.tmp"
    tmp.write_text(json.dumps(result), encoding="utf-8")
    tmp.rename(path)
    return path


def write_final_take(results_dir: Path, take_id: str, **overrides) -> Path:
    return write_result(results_dir, make_result(take_id, **overrides))


def write_pickup(results_dir: Path, take_id: str, **overrides) -> Path:
    """Revision 1 as the worker writes it at pick-up: every check running."""
    overrides.setdefault("checks", RUNNING_CHECKS)
    return write_result(results_dir, make_result(take_id, revision=1, **overrides))


def events_of(conn, kind: str | None = None, take_id: str | None = None) -> list[dict]:
    from agent import db

    return [dict(row) for row in db.events(conn, kind=kind, take_id=take_id)]
