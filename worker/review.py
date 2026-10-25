"""How the worker asks the reviewer model, and how its answers become candidate notes.

The worker never talks to the model itself. It calls a reviewer with one check,
one pass and an ordered list of frames, and gets back the frames the model
flagged. Code then sets every frame range, timestamp and evidence path.

The reviewer interface (Danny's `vision/`, #6, implements it):

    review(check, pass_name, frames, context) -> Answer

- check: an issue type, such as "road_markings"
- pass_name: "sparse" (the whole take) or "dense" (consecutive frames)
- frames: [(frame_number, Path to a JPEG), ...], in frame order. For road checks
  the dense frames are cropped to the road.
- context: {"take_id", "shot", "fps", "description"}
- Answer: {"notes": [{"frames": [frame numbers from `frames`], "model_note": str}],
           "model": str, "frames_sent": int}

Two reviewers are built in:
- `VISION_ANSWERS=<file.json>` reads canned answers, for laptop tests:
  {"<take_id>": {"<check>": {"sparse": [note, ...], "dense": [note, ...]}}}
  A dense note counts only if one of its frames was in the window sent.
- Otherwise `vision.reviewer.review` from the repo, once #6 lands. Until then a
  noted check ends in `error`, which keeps the take at review_pending.
"""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Callable

Reviewer = Callable[[str, str, list, dict], dict]


class ReviewerUnavailable(RuntimeError):
    pass


def answers_file_reviewer(path: Path) -> Reviewer:
    answers = json.loads(path.read_text(encoding="utf-8"))

    def review(check: str, pass_name: str, frames: list, context: dict) -> dict:
        sent = {number for number, _ in frames}
        notes = answers.get(context["take_id"], {}).get(check, {}).get(pass_name, [])
        kept = []
        for note in notes:
            hit = [n for n in note["frames"] if n in sent]
            if hit:
                kept.append({"frames": hit, "model_note": note["model_note"]})
        return {"notes": kept, "model": answers.get("model", "answers-file"), "frames_sent": len(frames)}

    return review


def load_reviewer() -> Reviewer:
    answers = os.environ.get("VISION_ANSWERS")
    if answers:
        return answers_file_reviewer(Path(answers))
    try:
        module = importlib.import_module("vision.reviewer")
    except ImportError:
        raise ReviewerUnavailable("reviewer not available") from None
    review = getattr(module, "review", None)
    if not callable(review):
        raise ReviewerUnavailable("reviewer not available")
    return review


def runs_of(frames: list[int], max_gap: int) -> list[list[int]]:
    """Split sorted frame numbers into runs whose neighbours are at most max_gap apart."""
    runs: list[list[int]] = []
    for number in sorted(set(frames)):
        if runs and number - runs[-1][-1] <= max_gap:
            runs[-1].append(number)
        else:
            runs.append([number])
    return runs


def window(start: int, end: int, pad: int, frame_count: int) -> tuple[int, int]:
    """Consecutive frames from start-pad to end+pad, inside the take."""
    return max(0, start - pad), min(frame_count - 1, end + pad)


def spread(frames: list[int], limit: int) -> list[int]:
    """At most `limit` frames, evenly spread and always keeping the first and last."""
    if len(frames) <= limit:
        return list(frames)
    if limit == 1:
        return [frames[0]]
    step = (len(frames) - 1) / (limit - 1)
    return sorted({frames[round(i * step)] for i in range(limit)})
