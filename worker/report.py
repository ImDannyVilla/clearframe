#!/usr/bin/env python3
"""What the worker found, as a Markdown table to post on #23. Standard library only.

    python3 ~/clearframe/app/worker/report.py --results ~/clearframe/results --shots 5,6

For each take with a final result file: its checks, every measured anomaly and
candidate note with frames, seconds and timecode, the seconds the reviewer
took, and the peak frame-to-frame brightness jump against the flicker
threshold (#23's last box). Then each take is scored against
`vision/answer_key.csv`: a flaw is caught when a note or anomaly of the same
issue type overlaps its frames; a flaw whose type no enabled check covers is
"not checked"; a note that overlaps no flaw of its type is "off the key".

Timecode is HH:MM:SS:FF at the take's nominal rate (24 for 23.976), the way
creative directors write their notes: frame 102 is 00:00:04:06.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from rubric import rubric as rubric_loader  # noqa: E402
from vision import accuracy  # noqa: E402
from worker import takes  # noqa: E402


def timecode(frame: int, fps: float) -> str:
    """HH:MM:SS:FF, non-drop, at the nominal rate (round(fps))."""
    rate = max(1, round(fps))
    seconds, frames = divmod(frame, rate)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}:{frames:02d}"


def peak_jump(results_dir: Path, result: dict) -> tuple[float, int] | None:
    """(largest change between consecutive frames, the frame it lands on), from the brightness series."""
    relative = result.get("brightness_series")
    if not relative:
        return None
    try:
        series = json.loads((results_dir / relative).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    jumps = [(abs(series[i] - series[i - 1]), i) for i in range(1, len(series))]
    return max(jumps, key=lambda jump: jump[0]) if jumps else None  # ties: the first frame it happens on


def load_results(results_dir: Path, shots: set[int] | None) -> list[dict]:
    found = []
    for path in sorted(results_dir.glob("*.json")):
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(result, dict) or result.get("schema") != "clearframe.result/1":
            continue
        parsed = takes.parse_take_id(str(result.get("take_id", "")))
        if parsed is None or (shots and parsed[0] not in shots):
            continue
        found.append(result)
    return sorted(found, key=lambda r: accuracy.take_order(r["take_id"]))


def findings(result: dict) -> list[dict]:
    items = [{**m, "kind": "measured", "pass": "code"} for m in result.get("measured") or []]
    items += [{**c, "kind": "candidate"} for c in result.get("candidates") or []]
    return sorted(items, key=lambda item: (item["frame_start"], item["issue_type"]))


def score_take(result: dict, flaws: list, enabled: list[str]) -> dict:
    fps = result["render"].get("fps") or 24
    frame_count = result["render"].get("frames") or 0
    items = findings(result)
    caught, missed, unchecked = [], [], []
    for flaw in flaws:
        span = flaw.span(fps, frame_count)
        hit = any(item["issue_type"] == flaw.issue_type
                  and accuracy.overlaps((item["frame_start"], item["frame_end"]), span) for item in items)
        target = caught if hit else (missed if flaw.issue_type in enabled else unchecked)
        target.append((flaw, span))
    off_key = [item for item in items if not any(
        flaw.issue_type == item["issue_type"]
        and accuracy.overlaps((item["frame_start"], item["frame_end"]), flaw.span(fps, frame_count))
        for flaw in flaws)]
    return {"caught": caught, "missed": missed, "unchecked": unchecked, "off_key": off_key}


def render(results_dir: Path, results: list[dict], key: dict, policy: dict) -> str:
    threshold = policy["flicker_threshold"]
    enabled = list(policy["checks"])
    lines = ["| Take | Checks | Found | Reviewer s | Peak brightness jump |",
             "| --- | --- | --- | --- | --- |"]
    for result in results:
        fps = result["render"].get("fps") or 24
        checks = ", ".join(f"{name} {state}" for name, state in result["checks"].items())
        found = "; ".join(
            f"{item['kind']} {item['issue_type']} frames {item['frame_start']}-{item['frame_end']} "
            f"({item['t_start']:.3f}-{item['t_end']:.3f} s, {timecode(item['frame_start'], fps)})"
            for item in findings(result)) or "nothing"
        peak = peak_jump(results_dir, result)
        peak_text = (f"{peak[0]:.1f} at frame {peak[1]} vs {threshold:g}" if peak else "no series")
        lines.append(f"| {result['take_id']} | {checks} | {found} | {result['vision'].get('seconds', 0)} | {peak_text} |")
        for error in result.get("errors") or []:
            lines.append(f"| | error: {error} | | | |")

    lines += ["", "**Against `vision/answer_key.csv`**", ""]
    for result in results:
        take_id = result["take_id"]
        if take_id not in key:
            lines.append(f"- {take_id}: not in the answer key, not scored")
            continue
        score = score_take(result, key[take_id], enabled)
        parts = [f"caught {f.issue_type} {s[0]}-{s[1]}" for f, s in score["caught"]]
        parts += [f"MISSED {f.issue_type} {s[0]}-{s[1]}" for f, s in score["missed"]]
        parts += [f"not checked: {f.issue_type} {s[0]}-{s[1]} (no enabled check)" for f, s in score["unchecked"]]
        parts += [f"off the key: {i['kind']} {i['issue_type']} {i['frame_start']}-{i['frame_end']}"
                  for i in score["off_key"]]
        if not key[take_id] and not score["off_key"]:
            parts.append("clean, as the key says")
        lines.append(f"- {take_id}: " + "; ".join(parts))
        for item in result.get("candidates") or []:
            lines.append(f"  - note at {item['frame_start']}-{item['frame_end']}: {item['model_note']!r}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(description="The worker's findings as Markdown, for #23.")
    parser.add_argument("--results", default=env("CLEARFRAME_RESULTS", "/results"))
    parser.add_argument("--answer-key", default=str(accuracy.ANSWER_KEY))
    parser.add_argument("--rubric", default=env("CLEARFRAME_RUBRIC", str(ROOT / "rubric" / "rubric.yaml")))
    parser.add_argument("--shots", default="", help="comma-separated shots, like 5,6; default all")
    args = parser.parse_args(argv)
    shots = {int(s) for s in args.shots.split(",") if s.strip()} or None
    results_dir = Path(args.results)
    results = [r for r in load_results(results_dir, shots)
               if all(state in ("done", "error") for state in r["checks"].values())]
    if not results:
        print(f"report: no final result files in {results_dir}", file=sys.stderr)
        return 1
    print(render(results_dir, results, accuracy.load_answer_key(Path(args.answer_key)),
                 rubric_loader.load(args.rubric)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
