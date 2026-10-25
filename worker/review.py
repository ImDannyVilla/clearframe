"""The worker's bridge to the reviewer step, Danny's `vision/` (#6).

`vision.review.review_take()` asks one question per noted check on a sparse pass,
then on dense windows (cropped to the road for road checks) around every moment
code flagged or the sparse pass noted, with votes. Code there sets every frame
number and time. The worker passes it the extracted frames and its flicker
spikes, then copies the returned evidence frames into `results/`.

Which model answers:
- `VISION_ANSWERS=<file.json>`: `vision.model.FakeModel` reads canned answers,
  for laptop tests and dry runs (`vision/samples/answers-4-2.json` is one).
- Otherwise `vision.model.VisionClient` from `VISION_URL`, `VISION_MODEL` and
  `VISION_API_KEY`, which the cheat sheet's `docker run` loads from `vision.env`.
"""
from __future__ import annotations

import os
from pathlib import Path

from vision import review as vision_review
from vision.model import FakeModel, Model, ModelError, VisionClient


class ReviewerUnavailable(RuntimeError):
    """No model is configured. The message is fixed text."""


def load_model() -> Model:
    answers = os.environ.get("VISION_ANSWERS")
    if answers:
        return FakeModel.from_file(Path(answers))
    try:
        return VisionClient.from_env()
    except ModelError:
        raise ReviewerUnavailable("reviewer not available") from None


def review_take(
    take_id: str,
    frames_dir: Path,
    frame_count: int,
    fps: float,
    checks: list[str],
    model: Model,
    flagged: list[tuple[int, int]],
    workdir: Path,
) -> vision_review.Review:
    """Danny's reviewer, with the defaults #6 decides in `vision.review.Settings`."""
    return vision_review.review_take(
        take_id=take_id,
        frames_dir=frames_dir,
        frame_count=frame_count,
        fps=fps,
        checks=checks,
        model=model,
        flagged=flagged,
        workdir=workdir,
    )
