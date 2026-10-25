#!/usr/bin/env python3
"""The host review worker (take-intake, #5). Standard library only.

Watches `<input>/renders/` and writes one result file per take to `<results>/`,
exactly as contracts/README.md "The result file" says:

1. A new render whose size has held for one poll is picked up once.
2. Revision 1 is written with every enabled check `running`.
3. ffmpeg decodes every frame. Code measures mean brightness on every frame
   and finds exposure flicker; the reviewer answers the noted checks on a sparse
   pass, then on dense windows (cropped to the road for road checks) around
   anything code flagged or the sparse pass noted.
4. Evidence frames and brightness.json go to `<results>/frames/<take_id>/`,
   then the final revision. Every JSON file is written .tmp, then renamed.

Settings come from the environment; the defaults match the cheat sheet's
`docker run`. Run with `--once` to review what's in renders/ and exit.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rubric import rubric as rubric_loader  # noqa: E402
from worker import flicker, media, review, takes  # noqa: E402

SCHEMA = "clearframe.result/1"
MEASURED_CHECKS = ("exposure_flicker",)
NOTED_CHECKS = ("road_markings", "extra_finger")
ROAD_CHECKS = ("road_markings",)

# The only error messages a result file carries: short, fixed, never model output.
ERR_RENDER = "render unreadable"
ERR_FRAMES = "frame extraction failed"
ERR_NO_REVIEWER = "reviewer not available"
ERR_REVIEWER = "reviewer failed"
ERR_UNKNOWN = "no worker check for this issue type"


@dataclass
class Config:
    input_dir: Path
    results_dir: Path
    work_dir: Path
    rubric_path: Path
    poll_seconds: float = 2.0
    parallel: int = 3
    sparse_step: int | None = None  # frames between sparse samples; default fps/3
    dense_pad: int | None = None  # frames added each side of a moment; default sparse_step
    road_crop: str = "iw:ih/2:0:ih/2"  # ffmpeg crop: the lower half, where the road is
    evidence_limit: int = 8
    commit: str | None = None

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get

        def optional_int(name: str) -> int | None:
            value = env(name)
            return int(value) if value else None

        return cls(
            input_dir=Path(env("CLEARFRAME_INPUT", "/input")),
            results_dir=Path(env("CLEARFRAME_RESULTS", "/results")),
            work_dir=Path(env("CLEARFRAME_WORK", str(Path(tempfile.gettempdir()) / "clearframe-work"))),
            rubric_path=Path(env("CLEARFRAME_RUBRIC", str(ROOT / "rubric" / "rubric.yaml"))),
            poll_seconds=float(env("POLL_SECONDS", "2")),
            parallel=int(env("WORKER_PARALLEL", "3")),
            sparse_step=optional_int("SPARSE_STEP"),
            dense_pad=optional_int("DENSE_PAD"),
            road_crop=env("ROAD_CROP", "iw:ih/2:0:ih/2"),
            evidence_limit=int(env("EVIDENCE_LIMIT", "8")),
            commit=env("WORKER_COMMIT") or git_commit(ROOT),
        )


def log(message: str) -> None:
    print(f"{now()} worker: {message}", flush=True)


def now() -> str:
    """ISO 8601 with the offset, as the contract says."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def git_commit(root: Path) -> str | None:
    """The short commit of the repo the worker runs from, read without the git binary."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref: "):
            return head[:7] or None
        ref = head[5:]
        loose = git / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()[:7]
        packed = git / "packed-refs"
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.endswith(" " + ref):
                return line.split(" ", 1)[0][:7]
    except OSError:
        pass
    return None


def write_json(path: Path, data) -> None:
    """Write `<name>.tmp` in the same folder, then rename it over `<name>`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def is_final(path: Path) -> bool:
    try:
        checks = json.loads(path.read_text(encoding="utf-8")).get("checks") or {}
    except (OSError, ValueError):
        return False
    return bool(checks) and all(state in ("done", "error") for state in checks.values())


@dataclass
class Take:
    take_id: str
    shot: int
    parent_take_id: str | None
    render_path: Path
    config: Config
    result: dict = field(default_factory=dict)
    frames_dir: Path | None = None  # every extracted frame, outside results/
    frame_count: int = 0
    fps: float = 0

    @property
    def evidence_dir(self) -> Path:
        return self.config.results_dir / "frames" / self.take_id

    @property
    def result_path(self) -> Path:
        return self.config.results_dir / f"{self.take_id}.json"

    def write(self) -> None:
        self.result["written_at"] = now()
        write_json(self.result_path, self.result)

    def evidence(self, frames: list[int]) -> list[str]:
        """Copy the frames' JPEGs into results/ and return their paths relative to it."""
        assert self.frames_dir is not None
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for number in frames:
            name = f"{number:06d}.jpg"
            source = self.frames_dir / name
            if source.is_file():
                shutil.copyfile(source, self.evidence_dir / name)
                paths.append(f"frames/{self.take_id}/{name}")
        return paths


def review_take(take_id: str, render_path: Path, config: Config, reviewer_loader=review.load_reviewer) -> dict:
    """Review one take and write its revisions. Returns the final result."""
    parsed = takes.parse_take_id(take_id)
    if parsed is None:
        raise ValueError(f"not a take ID: {take_id}")
    shot, parent = parsed
    policy = rubric_loader.load(config.rubric_path)
    checks: list[str] = list(policy["checks"])

    shots = _read_optional(config.input_dir / "shot_list.csv", takes.read_shot_list)
    log_rows = _read_optional(config.input_dir / "generation_log.csv", takes.read_generation_log)
    shot_row = shots.get(shot, {})
    generation = log_rows.get(take_id)
    if generation is None:
        log(f"{take_id}: no row in generation_log.csv")
        generation = {"tool": None, "model": None, "seed": None, "prompt": ""}

    take = Take(take_id, shot, parent, render_path, config)
    take.result = {
        "schema": SCHEMA,
        "take_id": take_id,
        "parent_take_id": parent,
        "shot": shot,
        "expected_takes": shot_row.get("expected_takes"),
        "revision": 1,
        "written_at": None,
        "worker_commit": config.commit,
        "render": {"file": render_path.name, "sha256": None, "fps": None, "frames": None, "width": None, "height": None},
        "generation": generation,
        "checks": {check: "running" for check in checks},
        "measured": [],
        "candidates": [],
        "brightness_series": None,
        "vision": {"model": None, "sparse_frames_sent": 0, "dense_windows": 0, "dense_frames_sent": 0, "seconds": 0.0},
        "errors": [],
    }

    try:
        info = media.probe(render_path)
        take.result["render"].update(
            sha256=media.sha256_file(render_path),
            fps=info["fps"], frames=info["frames"], width=info["width"], height=info["height"],
        )
    except (media.MediaError, OSError):
        take.write()
        log(f"{take_id}: render unreadable")
        return _finish(take, {check: ERR_RENDER for check in checks})
    take.fps = info["fps"]
    take.write()  # revision 1: picked up, every check running
    log(f"{take_id}: picked up, checks {', '.join(checks)}")

    work = config.work_dir / take_id
    shutil.rmtree(work, ignore_errors=True)
    try:
        take.frames_dir = work / "frames"
        try:
            series = media.brightness_series(render_path, info["width"], info["height"])
            media.extract_frames(render_path, take.frames_dir)
        except (media.MediaError, OSError):
            return _finish(take, {check: ERR_FRAMES for check in checks})
        take.frame_count = len(series)
        take.result["render"]["frames"] = take.frame_count

        # The brightness series goes in before the JSON that points to it.
        series_path = take.evidence_dir / "brightness.json"
        write_json(series_path, series)
        take.result["brightness_series"] = f"frames/{take_id}/brightness.json"

        failed: dict[str, str] = {}
        spikes: list[flicker.Spike] = []
        if "exposure_flicker" in checks:
            spikes = flicker.detect(series, policy["flicker_threshold"], policy["flicker_return_frames"])
            for spike in spikes:
                take.result["measured"].append({
                    "issue_type": "exposure_flicker",
                    "frame_start": spike.frame_start,
                    "frame_end": spike.frame_end,
                    "t_start": flicker.seconds(spike.frame_start, take.fps),
                    "t_end": flicker.seconds(spike.frame_end, take.fps),
                    "value": spike.value,
                    "threshold": policy["flicker_threshold"],
                    "unit": flicker.UNIT,
                    "evidence": take.evidence(review.spread(spike.evidence_frames, config.evidence_limit)),
                })

        for check in checks:
            if check in MEASURED_CHECKS:
                continue
            if check not in NOTED_CHECKS:
                failed[check] = ERR_UNKNOWN
                continue
            error = _noted_check(take, check, spikes, shot_row, reviewer_loader)
            if error:
                failed[check] = error
        return _finish(take, failed)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _read_optional(path: Path, reader):
    try:
        return reader(path)
    except FileNotFoundError:
        log(f"missing {path.name}")
        return {}


def _finish(take: Take, failed: dict[str, str]) -> dict:
    """Write the final revision: every check `done` or `error`."""
    for check in take.result["checks"]:
        take.result["checks"][check] = "error" if check in failed else "done"
    take.result["errors"] = [f"{check}: {message}" for check, message in failed.items()]
    take.result["revision"] += 1
    take.write()
    found = len(take.result["measured"]), len(take.result["candidates"])
    log(f"{take.take_id}: final, {found[0]} measured, {found[1]} candidate, {len(failed)} error")
    return take.result


def _noted_check(take: Take, check: str, spikes: list, shot_row: dict, reviewer_loader) -> str | None:
    """Ask the reviewer about one check; add candidate notes. Returns an error message or None."""
    config = take.config
    try:
        reviewer = reviewer_loader()
    except review.ReviewerUnavailable:
        return ERR_NO_REVIEWER
    assert take.frames_dir is not None
    step = config.sparse_step or max(1, round(take.fps / 3))
    pad = config.dense_pad if config.dense_pad is not None else step
    context = {
        "take_id": take.take_id,
        "shot": take.shot,
        "fps": take.fps,
        "description": shot_row.get("description", ""),
    }
    vision = take.result["vision"]
    started = time.monotonic()
    try:
        # Sparse pass over the whole take.
        sparse_frames = list(range(0, take.frame_count, step))
        answer = reviewer(check, "sparse", _frame_paths(take.frames_dir, sparse_frames), context)
        _note_model(vision, answer)
        vision["sparse_frames_sent"] += int(answer.get("frames_sent", len(sparse_frames)))
        sparse_moments = []
        for note in answer.get("notes", []):
            for run in review.runs_of(note["frames"], step):
                sparse_moments.append((run, note["model_note"]))

        # Dense windows around every sparse note and every measured spike.
        moments = [(run[0], run[-1]) for run, _ in sparse_moments]
        moments += [(s.frame_start, s.frame_end) for s in spikes]
        windows = _merge([review.window(a, b, pad, take.frame_count) for a, b in moments])
        found: list[dict] = []
        for first, last in windows:
            frames = list(range(first, last + 1))
            paths = _frame_paths(take.frames_dir, frames)
            if check in ROAD_CHECKS:
                crop_dir = take.frames_dir.parent / "crop" / f"{first:06d}"
                media.crop_frames(take.frames_dir, first, last, crop_dir, config.road_crop)
                paths = _frame_paths(crop_dir, frames)
            dense = reviewer(check, "dense", paths, context)
            _note_model(vision, dense)
            vision["dense_windows"] += 1
            vision["dense_frames_sent"] += int(dense.get("frames_sent", len(frames)))
            dense_notes = [
                (run, note["model_note"])
                for note in dense.get("notes", [])
                for run in review.runs_of(note["frames"], 2)
            ]
            for run, text in dense_notes:
                found.append(_candidate(take, check, "dense", run, text))
            if not dense_notes:
                # The dense pass saw nothing: keep the sparse notes in this window as
                # they were. A person confirms or dismisses them on the evidence page.
                for run, text in sparse_moments:
                    if first <= run[0] <= last:
                        found.append(_candidate(take, check, "sparse", run, text))
    except Exception:  # noqa: BLE001 - any reviewer failure ends the check in error
        traceback.print_exc()
        return ERR_REVIEWER
    finally:
        vision["seconds"] = round(vision["seconds"] + time.monotonic() - started, 1)

    seen = set()
    for item in sorted(found, key=lambda c: (c["frame_start"], c["frame_end"])):
        key = (item["frame_start"], item["frame_end"], item["model_note"])
        if key not in seen:
            seen.add(key)
            take.result["candidates"].append(item)
    return None


def _note_model(vision: dict, answer: dict) -> None:
    if answer.get("model") and not vision["model"]:
        vision["model"] = answer["model"]


def _frame_paths(directory: Path, frames: list[int]) -> list[tuple[int, Path]]:
    return [(number, directory / f"{number:06d}.jpg") for number in frames]


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for first, last in sorted(windows):
        if merged and first <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], last)
        else:
            merged.append([first, last])
    return [(a, b) for a, b in merged]


def _candidate(take: Take, check: str, pass_name: str, run: list[int], text: str) -> dict:
    return {
        "issue_type": check,
        "pass": pass_name,
        "frame_start": run[0],
        "frame_end": run[-1],
        "t_start": flicker.seconds(run[0], take.fps),
        "t_end": flicker.seconds(run[-1], take.fps),
        "model_note": text,
        "evidence": take.evidence(review.spread(run, take.config.evidence_limit)),
    }


class Watcher:
    """Polls renders/ and hands each take to the pool once."""

    def __init__(self, config: Config):
        self.config = config
        self.renders = config.input_dir / "renders"
        self.sizes: dict[str, tuple[int, float]] = {}
        self.claimed: set[str] = set()

    def ready(self) -> list[tuple[str, Path]]:
        found = []
        if not self.renders.is_dir():
            return found
        for path in sorted(self.renders.iterdir()):
            take_id = path.stem
            if path.suffix.lower() not in takes.RENDER_SUFFIXES or take_id in self.claimed:
                continue
            if takes.parse_take_id(take_id) is None:
                log(f"skipping {path.name}: the file name isn't a take ID")
                self.claimed.add(take_id)
                continue
            if is_final(self.config.results_dir / f"{take_id}.json"):
                self.claimed.add(take_id)  # reviewed before a restart
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            size = (stat.st_size, stat.st_mtime)
            if self.sizes.get(take_id) != size:
                self.sizes[take_id] = size  # still arriving, or just arrived
                continue
            self.claimed.add(take_id)
            found.append((take_id, path))
        return found


def _safe_review(take_id: str, path: Path, config: Config) -> None:
    try:
        review_take(take_id, path, config)
    except Exception:  # noqa: BLE001 - one bad take must not stop the worker
        traceback.print_exc()
        log(f"{take_id}: review failed")


def run(config: Config, once: bool = False) -> None:
    watcher = Watcher(config)
    config.results_dir.mkdir(parents=True, exist_ok=True)
    log(f"watching {watcher.renders} every {config.poll_seconds:g} s, results in {config.results_dir}")
    with ThreadPoolExecutor(max_workers=max(1, config.parallel)) as pool:
        while True:
            if once:
                # The first look only records sizes; the second picks up what held still.
                watcher.ready()
            ready = watcher.ready()
            futures = [pool.submit(_safe_review, take_id, path, config) for take_id, path in ready]
            if once:
                for future in futures:
                    future.result()
                return
            time.sleep(config.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--once", action="store_true", help="review what's in renders/ now, then exit")
    args = parser.parse_args(argv)
    run(Config.from_env(), once=args.once)
    return 0


if __name__ == "__main__":
    sys.exit(main())
