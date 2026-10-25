#!/usr/bin/env python3
"""Check the worker's setup on the GB10 before a run (#23, smoke test 6). Standard library only.

Run it in the worker's own container, so it sees exactly what the worker sees:

    docker run --rm --network clearframe-net --env-file ~/clearframe-input/vision.env \
      -e VISION_URL=http://nemoclaw-vllm:8000/v1 -e PATH=/opt/ffmpeg:/usr/local/bin:/usr/bin:/bin \
      -v ~/clearframe/app:/app:ro -v ~/clearframe/results:/results -v ~/clearframe-input:/input:ro \
      -v ~/ffmpeg-7.0.2-arm64-static:/opt/ffmpeg:ro \
      python:3.12-slim python /app/worker/preflight.py

Each line is PASS, WARN or FAIL; the exit code is 1 if anything failed.

- ffmpeg and ffprobe run.
- The rubric loads, and its enabled checks are ones the worker runs.
- shot_list.csv and generation_log.csv read, and every take in takes/ and
  renders/ has a valid take ID, a generation-log row with a prompt, and a
  shot-list row. Review copies (wider than MAX_WIDTH, like Morgan's 3840-wide
  side-by-sides) fail in renders/ and warn in takes/.
- results/ is writable.
- The model server answers on VISION_URL with VISION_MODEL served.
- The internet is NOT reachable: the network must be internal.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rubric import rubric as rubric_loader  # noqa: E402
from worker import main as worker_main  # noqa: E402
from worker import media, takes  # noqa: E402

MAX_WIDTH = 2560  # wider than this is a review copy, not a take
INTERNET_URL = "https://example.com"


class Report:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def add(self, level: str, message: str) -> None:
        self.lines.append((level, message))
        print(f"{level:4}  {message}", flush=True)

    def ok(self, message: str) -> None:
        self.add("PASS", message)

    def warn(self, message: str) -> None:
        self.add("WARN", message)

    def fail(self, message: str) -> None:
        self.add("FAIL", message)

    @property
    def failed(self) -> bool:
        return any(level == "FAIL" for level, _ in self.lines)


def check_tools(report: Report) -> None:
    for name, binary in (("ffmpeg", media.ffmpeg()), ("ffprobe", media.ffprobe())):
        try:
            out = subprocess.run([binary, "-version"], capture_output=True, text=True, check=True).stdout
            report.ok(f"{name}: {out.splitlines()[0][:70]}")
        except (OSError, subprocess.CalledProcessError):
            report.fail(f"{name} doesn't run (is the static build mounted and on PATH?)")


def check_rubric(report: Report, path: Path) -> None:
    try:
        policy = rubric_loader.load(path)
    except (OSError, rubric_loader.RubricError) as error:
        report.fail(f"rubric: {error}")
        return
    checks = list(policy["checks"])
    unknown = [c for c in checks if c not in worker_main.MEASURED_CHECKS + worker_main.NOTED_CHECKS]
    if unknown:
        report.fail(f"rubric enables checks the worker can't run: {', '.join(unknown)}")
    else:
        report.ok(f"rubric: checks {', '.join(checks)}; flicker {policy['flicker_threshold']} "
                  f"within {policy['flicker_return_frames']} frames")


def check_inputs(report: Report, input_dir: Path) -> None:
    shots: dict = {}
    log: dict = {}
    for name, reader in (("shot_list.csv", takes.read_shot_list), ("generation_log.csv", takes.read_generation_log)):
        try:
            rows = reader(input_dir / name)
        except FileNotFoundError:
            report.fail(f"{name} is missing from {input_dir}")
            continue
        report.ok(f"{name}: {len(rows)} rows")
        if name == "shot_list.csv":
            shots = rows
        else:
            log = rows

    for folder, too_wide in (("takes", report.warn), ("renders", report.fail)):
        directory = input_dir / folder
        if not directory.is_dir():
            report.fail(f"{folder}/ is missing from {input_dir}")
            continue
        clips = sorted(p for p in directory.iterdir()
                       if p.suffix.lower() in takes.RENDER_SUFFIXES and not p.name.startswith("._"))
        report.ok(f"{folder}/: {len(clips)} clips")
        for clip in clips:
            check_clip(report, clip, folder, shots, log, too_wide)


def check_clip(report: Report, clip: Path, folder: str, shots: dict, log: dict, too_wide) -> None:
    take_id = clip.stem
    where = f"{folder}/{clip.name}"
    parsed = takes.parse_take_id(take_id)
    if parsed is None:
        report.fail(f"{where}: the file name isn't a take ID (like 5-1 or 4-2r1)")
        return
    shot, _ = parsed
    row = log.get(take_id)
    if row is None:
        report.fail(f"{where}: no row in generation_log.csv")
    elif not row["prompt"].strip():
        report.fail(f"{where}: its generation_log.csv row has no prompt")
    if shot not in shots:
        report.fail(f"{where}: shot {shot} isn't in shot_list.csv")
    try:
        info = media.probe(clip)
    except media.MediaError as error:
        report.fail(f"{where}: unreadable ({error})")
        return
    size = f"{info['width']}x{info['height']} at {info['fps']} fps, {info['frames']} frames"
    if info["width"] > MAX_WIDTH:
        too_wide(f"{where}: {size}, wider than {MAX_WIDTH}: a review copy, not a take")
    else:
        report.ok(f"{where}: {size}")


def check_results(report: Report, results_dir: Path) -> None:
    probe = results_dir / ".preflight.tmp"  # .tmp: the sandbox's ingest skips it
    try:
        results_dir.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        report.ok(f"results/ is writable: {results_dir}")
    except OSError as error:
        report.fail(f"results/ isn't writable: {results_dir} ({error.strerror})")


def check_model(report: Report, url: str | None, model: str | None, key: str | None, timeout: float) -> None:
    if not url or not model:
        report.fail("VISION_URL and VISION_MODEL must be set (from vision.env)")
        return
    request = urllib.request.Request(url.rstrip("/") + "/models")
    if key:
        request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            served = [m.get("id") for m in json.loads(response.read()).get("data", [])]
    except urllib.error.HTTPError as error:
        report.fail(f"model server at {url} answered {error.code} (is VISION_API_KEY right?)")
        return
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as error:
        reason = getattr(error, "reason", error)
        report.fail(f"model server at {url} unreachable ({reason}). If nemoclaw-vllm was recreated, "
                    "reattach it: docker network connect clearframe-net nemoclaw-vllm")
        return
    if model in served:
        report.ok(f"model server at {url} serves {model}")
    else:
        report.fail(f"model server at {url} serves {', '.join(map(str, served)) or 'nothing'}, not {model}")


def check_no_internet(report: Report, url: str, timeout: float) -> None:
    try:
        with urllib.request.urlopen(url, timeout=timeout):
            pass
    except (urllib.error.URLError, TimeoutError, OSError):
        report.ok(f"no route to the internet ({url} unreachable)")
        return
    report.fail(f"{url} is reachable: the worker's network isn't internal (use --network clearframe-net)")


def run(args: argparse.Namespace) -> Report:
    report = Report()
    check_tools(report)
    check_rubric(report, Path(args.rubric))
    check_inputs(report, Path(args.input))
    check_results(report, Path(args.results))
    if not args.skip_network:
        check_model(report, os.environ.get("VISION_URL"), os.environ.get("VISION_MODEL"),
                    os.environ.get("VISION_API_KEY"), args.timeout)
        check_no_internet(report, args.internet_url, args.timeout)
    passed = sum(1 for level, _ in report.lines if level == "PASS")
    warned = sum(1 for level, _ in report.lines if level == "WARN")
    failed = sum(1 for level, _ in report.lines if level == "FAIL")
    print(f"preflight: {passed} pass, {warned} warn, {failed} fail", flush=True)
    return report


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description="Check the worker's setup before a run.")
    parser.add_argument("--input", default=env("CLEARFRAME_INPUT", "/input"))
    parser.add_argument("--results", default=env("CLEARFRAME_RESULTS", "/results"))
    parser.add_argument("--rubric", default=env("CLEARFRAME_RUBRIC", str(ROOT / "rubric" / "rubric.yaml")))
    parser.add_argument("--internet-url", default=INTERNET_URL)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--skip-network", action="store_true", help="skip the model and internet checks")
    return 1 if run(parser.parse_args(argv)).failed else 0


if __name__ == "__main__":
    sys.exit(main())
