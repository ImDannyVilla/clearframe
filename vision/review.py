"""The reviewer step (Danny, #6). The worker (#5) calls `review_take()`. Standard library only.

One yes-or-no question per check, never "anything wrong?". Each question is asked
about sparse frames over the whole take, then about dense windows of consecutive
frames, cropped to the road, around every moment code flagged or the sparse pass
noted. Each request runs `runs` times; a frame is noted only when at least
`min_agree` runs agree. Code, not the model, sets every frame number and time.

Frames go to the model one of three ways (`Settings.mode`):

- `frames`: separate images, up to `max_images` per request
- `grid`: one image of up to 6 frames, 2 across by 3 down, cropped to the road
- `video`: one video built from the chosen frames, sent as base64 JPEGs joined by
  commas, so the model sees those frames and not a 2 fps sample of the take
"""
from __future__ import annotations

import hashlib
import re
import tempfile
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import frames as frame_tools
from .model import Model, ModelError, Reply, Request, Sampling

Crop = frame_tools.Crop

# The road in a tracking shot of the car: the lower 55% of the frame.
ROAD_CROP: Crop = (0.0, 0.45, 1.0, 0.55)


@dataclass(frozen=True)
class Question:
    text: str
    # Said after the question, so frames without the thing asked about aren't noted.
    not_visible: str
    # The answer that means the flaw is there.
    flaw_answer: str
    # What a dense pass or a grid crops to, and how the prompt says so; None keeps whole frames.
    crop: Crop | None
    crop_note: str = ""


# One question per noted check, keyed by the contract's issue type. The question
# text is #6's, word for word; `not_visible` is ours.
QUESTIONS = {
    "road_markings": Question(
        text="Do the arrows in the car's lane point in its direction of travel? Answer yes or no for each frame.",
        not_visible="If no arrow is visible in the car's lane in a frame, answer yes for that frame.",
        flaw_answer="no",
        crop=ROAD_CROP,
        crop_note="Each frame is cropped to the road.",
    ),
    "extra_finger": Question(
        text="Does each hand have exactly five fingers in every frame?",
        not_visible="If no hand is visible in a frame, answer yes for that frame.",
        flaw_answer="no",
        crop=None,
    ),
}

MODES = ("frames", "grid", "video")
PASSES = ("sparse", "dense")

# Qwen3's recommended sampling for each thinking mode. Check the Qwen3.6 model card.
THINKING_SAMPLING = {True: (0.6, 0.95, 8192), False: (0.7, 0.8, 1024)}

MAX_NOTE = 300
MAX_EVIDENCE = 6


@dataclass(frozen=True)
class Settings:
    """How the reviewer asks. The defaults stand until #6's decision is posted."""

    mode: str = "grid"
    thinking: bool = False
    runs: int = 3
    min_agree: int = 2
    temperature: float | None = None  # None: the recommended value for the thinking mode
    top_p: float | None = None
    top_k: int = 20
    max_tokens: int | None = None
    sparse_frames: int = 16
    dense_frames: int = 12
    max_images: int = 8  # images per request, from the server's --limit-mm-per-prompt
    parallel: int = 4  # requests in flight at once
    # True for the worker: a request with no usable answer fails the check. The
    # accuracy test sets False and counts it instead.
    strict: bool = True

    def sampling(self) -> Sampling:
        temperature, top_p, max_tokens = THINKING_SAMPLING[self.thinking]
        return Sampling(
            thinking=self.thinking,
            temperature=temperature if self.temperature is None else self.temperature,
            top_p=top_p if self.top_p is None else self.top_p,
            top_k=self.top_k,
            max_tokens=max_tokens if self.max_tokens is None else self.max_tokens,
        )

    def validate(self) -> "Settings":
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}, not {self.mode}")
        if self.runs < 1 or not 1 <= self.min_agree <= self.runs:
            raise ValueError("runs must be 1 or more, and min_agree between 1 and runs")
        sampling = self.sampling()
        if self.runs > 1 and not sampling.thinking and (sampling.temperature <= 0 or sampling.top_k <= 1):
            raise ValueError("votes need randomness: thinking on, or temperature above 0 with top_k above 1")
        for name in ("sparse_frames", "dense_frames", "max_images", "parallel"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be 1 or more")
        return self


class ReviewError(RuntimeError):
    """A check couldn't finish. Its message is fixed text, never model output."""


# Parsing -----------------------------------------------------------------

THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
ANSWER_LINE = re.compile(
    r"(?:frame\s*)?(\d+)(?:\s*\([^)]*\))?\s*[:.)\]-]\s*(yes|no)\b[\s.,;:!\-–—]*(.*)", re.IGNORECASE
)


def parse_answers(text: str, count: int) -> list[tuple[str, str]] | None:
    """[(yes or no, the model's words)] for frames 1 to `count`, or None if the reply doesn't say exactly that."""
    text = THINK.sub("", text)
    if "<think>" in text.lower() and "</think>" not in text.lower():
        return None  # cut off while thinking
    if "</think>" in text.lower():
        text = re.split(r"</think>", text, flags=re.IGNORECASE)[-1]
    answers: dict[int, tuple[str, str]] = {}
    for raw in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•>]\s+)?", "", raw).replace("**", "").replace("`", "").strip()
        match = ANSWER_LINE.fullmatch(line)
        if not match:
            continue
        number = int(match.group(1))
        if number in answers:
            return None
        answers[number] = (match.group(2).lower(), match.group(3).strip())
    if sorted(answers) != list(range(1, count + 1)):
        return None
    return [answers[number] for number in range(1, count + 1)]


# Sampling frames -----------------------------------------------------------


def sparse_frames(frame_count: int, count: int) -> list[int]:
    """`count` frames spread evenly over the take, first and last included."""
    if frame_count <= count:
        return list(range(frame_count))
    if count == 1:
        return [frame_count // 2]
    return sorted({round(i * (frame_count - 1) / (count - 1)) for i in range(count)})


@dataclass
class Window:
    frames: list[int]
    # Indexes of the sparse notes this window takes a closer look at.
    notes: list[int] = field(default_factory=list)


def dense_windows(moments: Iterable[tuple[int, int, int | None]], frame_count: int, size: int) -> list[Window]:
    """Windows of `size` consecutive frames around each moment (start, end, sparse note index or None).

    Moments closer than `size` frames share a window. A moment longer than `size`
    gets `size` frames spread over it instead.
    """
    merged: list[list] = []
    for start, end, note in sorted(moments, key=lambda moment: (moment[0], moment[1])):
        start, end = max(0, start), min(frame_count - 1, end)
        if merged and start <= merged[-1][1] + size:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end, []])
        if note is not None:
            merged[-1][2].append(note)
    windows = []
    for start, end, notes in merged:
        if end - start + 1 <= size:
            first = (start + end + 1) // 2 - size // 2
            first = min(max(0, first), max(0, frame_count - size))
            frames = list(range(first, min(frame_count, first + size)))
        else:
            frames = sorted({round(start + i * (end - start) / (size - 1)) for i in range(size)})
        windows.append(Window(frames=frames, notes=notes))
    return windows


# Requests ------------------------------------------------------------------


def _prompt(question: Question, mode: str, count: int, cropped: bool) -> tuple[str, str]:
    crop = f" {question.crop_note}" if cropped and question.crop_note else ""
    if mode == "grid":
        blank = " Cells after the last frame are blank." if count < frame_tools.GRID_FRAMES else ""
        intro = (
            f"This image is a grid of {count} frames from one AI-generated video take, "
            f"{frame_tools.GRID_COLUMNS} across and {frame_tools.GRID_ROWS} down, numbered 1 to {count} "
            f"left to right, then top to bottom.{blank}{crop}"
        )
    elif mode == "video":
        intro = f"This video is {count} frames from one AI-generated video take, in order, numbered 1 to {count}.{crop}"
    else:
        intro = f"These are {count} frames from one AI-generated video take, in order, numbered 1 to {count}.{crop}"
    ask = (
        f"{question.text} {question.not_visible}\n"
        f"Reply with exactly {count} lines, one per frame, in order, and nothing else. "
        "Write each line as `<frame number>: yes` or `<frame number>: no - <a few words on what is wrong>`."
    )
    return intro, ask


def _image(path: Path) -> dict:
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + frame_tools.b64(path)}}


def build_requests(
    check: str, frames: list[int], frames_dir: Path, mode: str, cropped: bool, max_images: int, workdir: Path
) -> list[Request]:
    """The requests that ask `check`'s question about `frames`. A grid is always cropped."""
    question = QUESTIONS[check]
    crop = question.crop if (cropped or mode == "grid") else None
    if mode == "grid":
        size = frame_tools.GRID_FRAMES
    elif mode == "video":
        size = len(frames)
    else:
        size = max_images
    requests = []
    for at in range(0, len(frames), size):
        chunk = frames[at:at + size]
        intro, ask = _prompt(question, mode, len(chunk), crop is not None)
        if mode == "grid":
            originals = [frame_tools.frame_path(frames_dir, frame) for frame in chunk]
            # Named by its frames, so grids from different passes never share a file.
            digest = hashlib.sha256(",".join(map(str, chunk)).encode()).hexdigest()[:12]
            image = frame_tools.grid(originals, workdir / f"grid-{check}-{digest}.jpg", crop)
            parts = [_image(image)]
        else:
            if crop:
                images = frame_tools.crop_frames(frames_dir, chunk, crop, workdir / f"crop-{check}")
            else:
                images = [frame_tools.frame_path(frames_dir, frame) for frame in chunk]
            if mode == "video":
                url = "data:video/jpeg;base64," + ",".join(frame_tools.b64(path) for path in images)
                parts = [{"type": "video_url", "video_url": {"url": url}}]
            else:
                parts = []
                for number, path in enumerate(images, start=1):
                    parts += [{"type": "text", "text": f"Frame {number}:"}, _image(path)]
        content = [{"type": "text", "text": intro}, *parts, {"type": "text", "text": ask}]
        requests.append(Request(check=check, frames=tuple(chunk), content=tuple(content)))
    return requests


# One pass --------------------------------------------------------------------


@dataclass
class PassResult:
    frames: list[int]
    votes: dict[int, int]
    reasons: dict[int, str]
    requests: int = 0
    unanswered: int = 0  # requests with fewer than min_agree usable replies
    unparsed: int = 0
    errors: list[str] = field(default_factory=list)
    replies: list[dict] = field(default_factory=list)


def _ask(model: Model, request: Request, sampling: Sampling) -> Reply | ModelError:
    for attempt in (1, 2):
        try:
            return model.ask(request, sampling)
        except ModelError as error:
            if attempt == 2:
                return error
    raise AssertionError("unreachable")


def run_pass(
    model: Model, settings: Settings, check: str, frames: list[int], frames_dir: Path,
    cropped: bool, workdir: Path, pass_name: str,
) -> PassResult:
    """Ask `check`'s question about `frames`, `runs` times, and count the votes."""
    question = QUESTIONS[check]
    requests = build_requests(check, frames, frames_dir, settings.mode, cropped, settings.max_images, workdir)
    sampling = settings.sampling()
    jobs = [(request, run) for request in requests for run in range(1, settings.runs + 1)]
    with ThreadPoolExecutor(max_workers=settings.parallel) as pool:
        replies = list(pool.map(lambda job: _ask(model, job[0], sampling), jobs))

    result = PassResult(frames=sorted(frames), votes=dict.fromkeys(frames, 0), reasons={}, requests=len(requests))
    usable = dict.fromkeys(range(len(requests)), 0)
    for index, ((request, run), reply) in enumerate(zip(jobs, replies)):
        number = index // settings.runs
        record = {"pass": pass_name, "frames": list(request.frames), "run": run}
        if isinstance(reply, ModelError):
            result.errors.append(str(reply))
            result.replies.append({**record, "error": str(reply)})
            continue
        answers = parse_answers(reply.text, len(request.frames))
        result.replies.append({**record, "seconds": reply.seconds, "parsed": answers is not None, "text": reply.text})
        if answers is None:
            result.unparsed += 1
            continue
        usable[number] += 1
        for frame, (answer, words) in zip(request.frames, answers):
            if answer == question.flaw_answer:
                result.votes[frame] += 1
                if words and frame not in result.reasons:
                    result.reasons[frame] = words[:MAX_NOTE]
    result.unanswered = sum(1 for count in usable.values() if count < settings.min_agree)
    return result


def _evidence(frames: list[int]) -> list[int]:
    if len(frames) <= MAX_EVIDENCE:
        return frames
    return [frames[round(i * (len(frames) - 1) / (MAX_EVIDENCE - 1))] for i in range(MAX_EVIDENCE)]


def notes_from(result: PassResult, check: str, pass_name: str, min_agree: int, take_id: str, fps: float) -> list[dict]:
    """Candidate notes in the contract's shape: runs of noted frames next to each other in the pass."""
    groups: list[list[int]] = []
    previous = None
    for position, frame in enumerate(result.frames):
        if result.votes.get(frame, 0) >= min_agree:
            if groups and previous == position - 1:
                groups[-1].append(frame)
            else:
                groups.append([frame])
            previous = position
    notes = []
    for group in groups:
        words = next((result.reasons[frame] for frame in group if frame in result.reasons), "")
        notes.append({
            "issue_type": check,
            "pass": pass_name,
            "frame_start": group[0],
            "frame_end": group[-1],
            "t_start": round(group[0] / fps, 3),
            "t_end": round(group[-1] / fps, 3),
            "model_note": words,
            "evidence": [f"frames/{take_id}/{frame_tools.frame_name(frame)}" for frame in _evidence(group)],
        })
    return notes


# One take ----------------------------------------------------------------------


@dataclass
class CheckReview:
    check: str
    state: str  # done or error, as the result file's checks
    sparse_notes: list[dict]
    candidates: list[dict]
    sparse_frames_sent: int
    dense_windows: int
    dense_frames_sent: int
    requests: int
    unanswered: int
    unparsed: int
    errors: list[str]
    replies: list[dict]


@dataclass
class Review:
    take_id: str
    model: str
    checks: dict[str, CheckReview]
    seconds: float

    @property
    def candidates(self) -> list[dict]:
        """Every check's notes, for the result file's `candidates`, in frame order."""
        notes = [note for review in self.checks.values() for note in review.candidates]
        return sorted(notes, key=lambda note: (note["frame_start"], note["issue_type"]))

    @property
    def vision(self) -> dict:
        """The result file's `vision` block."""
        reviews = self.checks.values()
        return {
            "model": self.model,
            "sparse_frames_sent": sum(review.sparse_frames_sent for review in reviews),
            "dense_windows": sum(review.dense_windows for review in reviews),
            "dense_frames_sent": sum(review.dense_frames_sent for review in reviews),
            "seconds": self.seconds,
        }

    @property
    def evidence_frames(self) -> list[int]:
        """The frame numbers the candidates point to, for the worker to keep in `results/`."""
        names = {path.rsplit("/", 1)[-1] for note in self.candidates for path in note["evidence"]}
        return sorted(int(name.split(".")[0]) for name in names)

    @property
    def errors(self) -> list[str]:
        return [f"{review.check}: the reviewer gave no usable answer" for review in self.checks.values()
                if review.state == "error"]


def review_check(
    take_id: str, check: str, frames_dir: Path, frame_count: int, fps: float, model: Model,
    settings: Settings, flagged: list[tuple[int, int]], workdir: Path,
) -> CheckReview:
    sparse_list = sparse_frames(frame_count, settings.sparse_frames)
    sparse = run_pass(model, settings, check, sparse_list, frames_dir, False, workdir, "sparse")
    sparse_notes = notes_from(sparse, check, "sparse", settings.min_agree, take_id, fps)

    moments = [(start, end, None) for start, end in flagged]
    moments += [(note["frame_start"], note["frame_end"], index) for index, note in enumerate(sparse_notes)]
    windows = dense_windows(moments, frame_count, settings.dense_frames)
    candidates, passes = [], [sparse]
    for window in windows:
        dense = run_pass(model, settings, check, window.frames, frames_dir, True, workdir, "dense")
        passes.append(dense)
        found = notes_from(dense, check, "dense", settings.min_agree, take_id, fps)
        # The closer look decides where it found something; otherwise the sparse note stands.
        candidates += found or [sparse_notes[index] for index in window.notes]

    unanswered = sum(result.unanswered for result in passes)
    return CheckReview(
        check=check,
        state="error" if settings.strict and unanswered else "done",
        sparse_notes=sparse_notes,
        candidates=[] if settings.strict and unanswered else candidates,
        sparse_frames_sent=len(sparse_list),
        dense_windows=len(windows),
        dense_frames_sent=sum(len(window.frames) for window in windows),
        requests=sum(result.requests for result in passes),
        unanswered=unanswered,
        unparsed=sum(result.unparsed for result in passes),
        errors=[error for result in passes for error in result.errors],
        replies=[reply for result in passes for reply in result.replies],
    )


def review_take(
    take_id: str,
    frames_dir: Path,
    frame_count: int,
    fps: float,
    checks: Iterable[str],
    model: Model,
    settings: Settings | None = None,
    flagged: Iterable[tuple[int, int]] = (),
    workdir: Path | None = None,
) -> Review:
    """Review one take's frames for every noted check in `checks`; code checks are skipped.

    `frames_dir` holds every frame as `000000.jpg` onward; `flagged` is code's
    measured anomalies as (frame_start, frame_end), which get dense windows too.
    """
    settings = (settings or Settings()).validate()
    flagged = [(int(start), int(end)) for start, end in flagged]
    started = time.monotonic()
    reviews = {}
    with tempfile.TemporaryDirectory(prefix="clearframe-vision-") as scratch:
        root = Path(workdir) if workdir else Path(scratch)
        for check in checks:
            if check in QUESTIONS:
                reviews[check] = review_check(
                    take_id, check, Path(frames_dir), frame_count, fps, model, settings, flagged, root / take_id
                )
    return Review(take_id=take_id, model=model.name, checks=reviews, seconds=round(time.monotonic() - started, 1))
