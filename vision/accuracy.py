#!/usr/bin/env python3
"""The accuracy test for #6: review a shot's takes every way, then score against `answer_key.csv`.

Standard library only, so it runs where the worker runs. vision/README.md has the
GB10 command.

Usage: accuracy.py run --takes DIR --shot 4 --check road_markings --out DIR
           [--modes grid,frames,video] [--thinking off,on] [--runs 3] [--env-file vision.env]
       accuracy.py score [--answer-key vision/answer_key.csv] [--tolerance FRAMES] OUT_DIR_OR_FILES...

`run` writes one file per setup (mode, thinking, runs) to --out, rewritten after
each take, so a stopped run keeps what it finished. The files hold the model's
replies, so keep --out outside the repo. `score` prints a table to post on #6.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vision import frames as frame_tools  # noqa: E402
from vision import review  # noqa: E402
from vision.model import FakeModel, Model, ModelError, VisionClient  # noqa: E402

SCHEMA = "clearframe.accuracy/1"
ANSWER_KEY = Path(__file__).resolve().parent / "answer_key.csv"
KEY_COLUMNS = ("take_id", "issue_type", "frame_start", "frame_end", "t_start", "t_end", "description")
TAKE_FILE = re.compile(r"(\d+)-(\d+)(?:r(\d+))?\.mp4")
TAKE_ID = re.compile(r"\d+-\d+(?:r\d+)?")


class AccuracyError(RuntimeError):
    """A bad answer key, takes folder or argument."""


def take_order(take_id: str) -> tuple[int, int, int]:
    shot, rest = take_id.split("-", 1)
    take, _, retake = rest.partition("r")
    return int(shot), int(take), int(retake or 0)


def find_takes(folder: Path, shots: list[int], take_ids: list[str]) -> list[tuple[str, Path]]:
    takes = []
    for path in folder.iterdir():
        match = TAKE_FILE.fullmatch(path.name)
        if not match:
            continue
        take_id = path.stem
        if shots and int(match.group(1)) not in shots:
            continue
        if take_ids and take_id not in take_ids:
            continue
        takes.append((take_id, path))
    return sorted(takes, key=lambda take: take_order(take[0]))


def setup_name(check: str, settings: review.Settings) -> str:
    thinking = "on" if settings.thinking else "off"
    return f"{check}-{settings.mode}-thinking-{thinking}-runs{settings.runs}"


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def run(args: argparse.Namespace) -> int:
    model: Model = FakeModel.from_file(args.fake) if args.fake else VisionClient.from_env(args.env_file)
    takes = find_takes(args.takes, args.shot, args.take)
    if not takes:
        raise AccuracyError(f"no takes like 4-2.mp4 in {args.takes} for the shots and takes asked")
    base = review.Settings(
        runs=args.runs, min_agree=args.min_agree, sparse_frames=args.sparse_frames,
        dense_frames=args.dense_frames, max_images=args.max_images, parallel=args.parallel, strict=False,
    )
    setups = [replace(base, mode=mode, thinking=thinking).validate()
              for mode in args.modes for thinking in args.thinking]
    args.out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="clearframe-accuracy-") as scratch:
        extracted = {}
        for take_id, path in takes:
            fps, _ = frame_tools.probe(path)
            frames_dir = Path(scratch) / "frames" / take_id
            extracted[take_id] = (frames_dir, frame_tools.extract_frames(path, frames_dir), fps)
            print(f"{take_id}: {extracted[take_id][1]} frames at {fps:g} fps", flush=True)

        for settings in setups:
            name = setup_name(args.check, settings)
            record = {
                "schema": SCHEMA, "check": args.check, "model": model.name, "settings": asdict(settings),
                "started_at": now(), "batch_seconds": 0.0, "takes": [],
            }
            started = time.monotonic()
            for take_id, _ in takes:
                frames_dir, count, fps = extracted[take_id]
                result = review.review_take(
                    take_id, frames_dir, count, fps, [args.check], model, settings, workdir=Path(scratch) / "work"
                )
                checked = result.checks[args.check]
                record["takes"].append({
                    "take_id": take_id, "fps": fps, "frames": count, "seconds": result.seconds,
                    "requests": checked.requests, "replies": len(checked.replies),
                    "unanswered": checked.unanswered, "unparsed": checked.unparsed, "errors": checked.errors,
                    "sparse_notes": checked.sparse_notes, "candidates": checked.candidates,
                    "vision": result.vision, "reply_log": checked.replies,
                })
                record["batch_seconds"] = round(time.monotonic() - started, 1)
                write_json(args.out / f"{name}.json", record)
                spans = ", ".join(f"{note['frame_start']}-{note['frame_end']}" for note in checked.candidates)
                notes = len(checked.candidates)
                print(
                    f"{name} {take_id}: {notes} note{'' if notes == 1 else 's'}{f' ({spans})' if spans else ''}, "
                    f"{result.seconds} s, {checked.unparsed} of {len(checked.replies)} replies unparsed, "
                    f"{len(checked.errors)} errors",
                    flush=True,
                )
    print(f"Score it: python3 vision/accuracy.py score {args.out}")
    return 0


# Scoring ---------------------------------------------------------------------


@dataclass(frozen=True)
class Flaw:
    take_id: str
    issue_type: str
    frames: tuple[int, int] | None
    seconds: tuple[float, float] | None
    description: str

    def span(self, fps: float, frame_count: int) -> tuple[int, int]:
        """The flaw's frames; seconds become the frames they cover."""
        if self.frames is not None:
            return self.frames
        assert self.seconds is not None
        start, end = self.seconds
        return max(0, math.floor(start * fps)), min(frame_count - 1, math.ceil(end * fps))


def _pair(row: dict, first: str, second: str, kind: type, line: int) -> tuple | None:
    a, b = (row.get(first) or "").strip(), (row.get(second) or "").strip()
    if not a and not b:
        return None
    try:
        pair = (kind(a), kind(b))
    except ValueError:
        raise AccuracyError(f"answer key line {line}: {first} and {second} must both be numbers") from None
    if pair[1] < pair[0] or pair[0] < 0:
        raise AccuracyError(f"answer key line {line}: {second} is before {first}")
    return pair


def load_answer_key(path: Path) -> dict[str, list[Flaw]]:
    """Every take a person watched, with its flaws; a take with an `issue_type` of `none` has none."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in KEY_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise AccuracyError(f"answer key is missing columns: {', '.join(missing)}")
        key: dict[str, list[Flaw]] = {}
        for line, row in enumerate(reader, start=2):
            take_id = (row["take_id"] or "").strip()
            if not take_id:
                continue
            if not TAKE_ID.fullmatch(take_id):
                raise AccuracyError(f"answer key line {line}: not a take ID: {take_id}")
            issue_type = (row["issue_type"] or "").strip().lower()
            flaws = key.setdefault(take_id, [])
            if issue_type in ("", "none"):
                continue
            frames = _pair(row, "frame_start", "frame_end", int, line)
            seconds = _pair(row, "t_start", "t_end", float, line)
            if frames is None and seconds is None:
                raise AccuracyError(f"answer key line {line}: give frame_start and frame_end, or t_start and t_end")
            flaws.append(Flaw(take_id, issue_type, frames, seconds, (row["description"] or "").strip()))
    return key


def overlaps(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def iou(a: tuple[int, int], b: tuple[int, int]) -> float:
    shared = max(0, min(a[1], b[1]) - max(a[0], b[0]) + 1)
    return shared / ((a[1] - a[0] + 1) + (b[1] - b[0] + 1) - shared)


def score_record(record: dict, key: dict[str, list[Flaw]], tolerance: int = 0) -> dict:
    """One setup's numbers: catches, false notes, how well the frames line up, time and parse failures."""
    check = record["check"]
    scored = [take for take in record["takes"] if take["take_id"] in key]
    score = {
        "setup": setup_name(check, review.Settings(**record["settings"])),
        "takes": len(scored),
        "not_in_key": [take["take_id"] for take in record["takes"] if take["take_id"] not in key],
        "planted": 0, "caught": 0, "caught_sparse": 0, "false_on_clean": 0, "off_flaw": 0,
        "iou": [], "start_offset_s": [],
        "seconds_per_take": round(sum(take["seconds"] for take in scored) / len(scored), 1) if scored else None,
        "replies": sum(take["replies"] for take in scored),
        "unparsed": sum(take["unparsed"] for take in scored),
        "errors": sum(len(take["errors"]) for take in scored),
    }
    for take in scored:
        fps, count = take["fps"], take["frames"]
        flaws = [flaw.span(fps, count) for flaw in key[take["take_id"]] if flaw.issue_type == check]
        widened = [(start - tolerance, end + tolerance) for start, end in flaws]
        notes = [(note["frame_start"], note["frame_end"]) for note in take["candidates"]]
        sparse = [(note["frame_start"], note["frame_end"]) for note in take["sparse_notes"]]
        score["planted"] += len(flaws)
        for flaw, wide in zip(flaws, widened):
            hits = [note for note in notes if overlaps(note, wide)]
            if hits:
                best = max(hits, key=lambda note: iou(note, flaw))
                score["caught"] += 1
                score["iou"].append(iou(best, flaw))
                score["start_offset_s"].append(abs(best[0] - flaw[0]) / fps)
            if any(overlaps(note, wide) for note in sparse):
                score["caught_sparse"] += 1
        for note in notes:
            if not flaws:
                score["false_on_clean"] += 1
            elif not any(overlaps(note, wide) for wide in widened):
                score["off_flaw"] += 1
    score["iou"] = round(sum(score["iou"]) / len(score["iou"]), 2) if score["iou"] else None
    offsets = score["start_offset_s"]
    score["start_offset_s"] = round(sum(offsets) / len(offsets), 2) if offsets else None
    return score


def _cell(value, suffix: str = "") -> str:
    return "–" if value is None else f"{value}{suffix}"


def table(scores: list[dict]) -> str:
    rows = [
        "| Setup | Caught | Sparse pass alone | False notes, clean takes | Off-flaw notes "
        "| Overlap (IoU) | Start off by | s per take | Unparsed replies | Errors |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    ranked = sorted(scores, key=lambda s: (-s["caught"], s["false_on_clean"] + s["off_flaw"], s["setup"]))
    for s in ranked:
        rows.append(
            f"| {s['setup']} | {s['caught']} of {s['planted']} | {s['caught_sparse']} of {s['planted']} "
            f"| {s['false_on_clean']} | {s['off_flaw']} | {_cell(s['iou'])} | {_cell(s['start_offset_s'], ' s')} "
            f"| {_cell(s['seconds_per_take'])} | {s['unparsed']} of {s['replies']} | {s['errors']} |"
        )
    return "\n".join(rows)


def key_summary(key: dict[str, list[Flaw]]) -> str:
    counts: dict[str, int] = {}
    for flaws in key.values():
        for flaw in flaws:
            counts[flaw.issue_type] = counts.get(flaw.issue_type, 0) + 1
    by_type = ", ".join(f"{name} {count}" for name, count in sorted(counts.items(), key=lambda item: -item[1]))
    clean = sum(1 for flaws in key.values() if not flaws)
    return f"Answer key: {len(key)} takes, {clean} with no flaw. Flaws by type: {by_type or 'none yet'}."


def load_records(paths: list[Path]) -> list[dict]:
    files = []
    for path in paths:
        files += sorted(path.glob("*.json")) if path.is_dir() else [path]
    records = []
    for file in files:
        data = json.loads(file.read_text(encoding="utf-8"))
        if data.get("schema") == SCHEMA:
            records.append(data)
    if not records:
        raise AccuracyError("no accuracy runs found in " + ", ".join(str(path) for path in paths))
    return records


def score(args: argparse.Namespace) -> int:
    key = load_answer_key(args.answer_key)
    scores = [score_record(record, key, args.tolerance) for record in load_records(args.runs)]
    print(key_summary(key))
    not_in_key = sorted({take for s in scores for take in s["not_in_key"]}, key=take_order)
    if not_in_key:
        print(f"Not in the answer key, so not scored: {', '.join(not_in_key)}")
    print()
    print(table(scores))
    return 0


def _list(choices: tuple[str, ...]):
    def parse(text: str) -> list[str]:
        values = [value.strip() for value in text.split(",") if value.strip()]
        bad = [value for value in values if value not in choices]
        if bad or not values:
            raise argparse.ArgumentTypeError(f"choose from {', '.join(choices)}")
        return values
    return parse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="review the takes every way asked and save the replies")
    run_cmd.add_argument("--takes", type=Path, required=True, help="folder of takes named like 4-2.mp4")
    run_cmd.add_argument("--shot", type=int, action="append", default=[], help="only this shot (repeatable)")
    run_cmd.add_argument("--take", action="append", default=[], help="only this take ID (repeatable)")
    run_cmd.add_argument("--check", choices=sorted(review.QUESTIONS), default="road_markings")
    run_cmd.add_argument("--modes", type=_list(review.MODES), default=list(review.MODES))
    run_cmd.add_argument("--thinking", type=_list(("off", "on")), default=["off", "on"])
    run_cmd.add_argument("--runs", type=int, default=3)
    run_cmd.add_argument("--min-agree", type=int, default=2)
    run_cmd.add_argument("--sparse-frames", type=int, default=review.Settings.sparse_frames)
    run_cmd.add_argument("--dense-frames", type=int, default=review.Settings.dense_frames)
    run_cmd.add_argument("--max-images", type=int, default=review.Settings.max_images,
                         help="images per request, from the server's --limit-mm-per-prompt")
    run_cmd.add_argument("--parallel", type=int, default=review.Settings.parallel, help="requests in flight at once")
    run_cmd.add_argument("--env-file", type=Path, help="vision.env with VISION_URL, VISION_API_KEY, VISION_MODEL")
    run_cmd.add_argument("--fake", type=Path, help="answer from a sample file instead of the model, for a dry run")
    run_cmd.add_argument("--out", type=Path, required=True, help="where the run files go; keep it out of the repo")

    score_cmd = commands.add_parser("score", help="score saved runs against the answer key")
    score_cmd.add_argument("runs", type=Path, nargs="+", help="run files, or folders of them")
    score_cmd.add_argument("--answer-key", type=Path, default=ANSWER_KEY)
    score_cmd.add_argument("--tolerance", type=int, default=0, help="frames a note may miss a flaw by and still catch it")

    args = parser.parse_args(argv)
    if args.command == "run":
        args.thinking = [value == "on" for value in args.thinking]
    try:
        return run(args) if args.command == "run" else score(args)
    except (AccuracyError, ModelError, frame_tools.FrameError, ValueError, OSError) as error:
        print(f"accuracy: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
