"""ffmpeg and ffprobe calls. Standard library only.

`FFMPEG` and `FFPROBE` override the binaries; otherwise they come from PATH
(the cheat sheet mounts the static build at /opt/ffmpeg and puts it on PATH).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from fractions import Fraction
from pathlib import Path


class MediaError(RuntimeError):
    """ffmpeg or ffprobe failed, or the render isn't a readable video."""


def ffmpeg() -> str:
    return os.environ.get("FFMPEG", "ffmpeg")


def ffprobe() -> str:
    return os.environ.get("FFPROBE", "ffprobe")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(cmd, capture_output=True, check=False)
    except OSError as error:
        raise MediaError(f"could not run {cmd[0]}: {error}") from None
    if proc.returncode != 0:
        tail = proc.stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise MediaError(f"{Path(cmd[0]).name} exited {proc.returncode}: {tail[0]}")
    return proc


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def probe(path: Path) -> dict:
    """fps, width, height and packet count of the first video stream.

    The packet count is only a first guess for revision 1; the final revision
    counts the frames actually decoded.
    """
    proc = _run([
        ffprobe(), "-v", "error", "-select_streams", "v:0", "-count_packets",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_read_packets",
        "-of", "json", str(path),
    ])
    streams = json.loads(proc.stdout or b"{}").get("streams") or []
    if not streams:
        raise MediaError("no video stream")
    stream = streams[0]
    rate = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        try:
            candidate = Fraction(stream.get(key, "0/0"))
        except (ValueError, ZeroDivisionError):
            continue
        if candidate > 0:
            rate = candidate
            break
    if rate is None:
        raise MediaError("unknown frame rate")
    fps: float | int = float(rate)
    if abs(fps - round(fps)) < 1e-6:
        fps = int(round(fps))
    else:
        fps = round(fps, 3)
    packets = str(stream.get("nb_read_packets", ""))
    return {
        "fps": fps,
        "width": int(stream["width"]),
        "height": int(stream["height"]),
        "frames": int(packets) if packets.isdigit() else None,
    }


def brightness_series(path: Path, width: int, height: int) -> list[float]:
    """Mean brightness (0-255) of every decoded frame, in order. Nothing is skipped."""
    frame_size = width * height
    cmd = [
        ffmpeg(), "-v", "error", "-i", str(path), "-map", "0:v:0",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "gray", "-",
    ]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError as error:
        raise MediaError(f"could not run {cmd[0]}: {error}") from None
    assert proc.stdout is not None
    series: list[float] = []
    while True:
        frame = proc.stdout.read(frame_size)
        if len(frame) < frame_size:
            break
        series.append(round(sum(frame) / frame_size, 2))
    _, stderr = proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode(errors="replace").strip().splitlines()[-1:] or [""]
        raise MediaError(f"ffmpeg exited {proc.returncode}: {tail[0]}")
    if not series:
        raise MediaError("no frames decoded")
    return series


def extract_frames(path: Path, out_dir: Path) -> int:
    """Every frame as out_dir/<six-digit frame number>.jpg, starting at 000000."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _run([
        ffmpeg(), "-v", "error", "-i", str(path), "-map", "0:v:0",
        "-fps_mode", "passthrough", "-q:v", "2", "-start_number", "0",
        str(out_dir / "%06d.jpg"),
    ])
    return len(list(out_dir.glob("*.jpg")))


def crop_frames(src_dir: Path, first: int, last: int, out_dir: Path, crop: str) -> None:
    """Crop frames first..last from src_dir into out_dir, keeping their frame numbers."""
    out_dir.mkdir(parents=True, exist_ok=True)
    _run([
        ffmpeg(), "-v", "error", "-y", "-start_number", str(first),
        "-i", str(src_dir / "%06d.jpg"), "-frames:v", str(last - first + 1),
        "-vf", f"crop={crop}", "-q:v", "2", "-start_number", str(first),
        str(out_dir / "%06d.jpg"),
    ])
