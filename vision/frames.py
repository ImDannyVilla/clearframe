"""Frames for the reviewer, made with ffmpeg. Standard library only, like the worker that calls it.

Frame numbers start at 0, and a take's frames are `<frames_dir>/<six-digit frame>.jpg`,
the same names the contract gives evidence frames.
"""
from __future__ import annotations

import base64
import subprocess
from fractions import Fraction
from pathlib import Path

# A crop is (x, y, width, height) as fractions of the frame.
Crop = tuple[float, float, float, float]

# The grid the issue asks for: 6 frames, 2 across by 3 down, left to right then down.
GRID_COLUMNS = 2
GRID_ROWS = 3
GRID_FRAMES = GRID_COLUMNS * GRID_ROWS
GRID_CELL_WIDTH = 640


class FrameError(RuntimeError):
    """ffmpeg or ffprobe failed, or a frame is missing."""


def frame_name(frame: int) -> str:
    return f"{frame:06d}.jpg"


def frame_path(frames_dir: Path, frame: int) -> Path:
    path = frames_dir / frame_name(frame)
    if not path.is_file():
        raise FrameError(f"missing frame {frame}: {path}")
    return path


def _run(command: list[str]) -> str:
    try:
        done = subprocess.run(command, capture_output=True, text=True, check=False)
    except FileNotFoundError:
        raise FrameError(f"{command[0]} is not on PATH") from None
    if done.returncode != 0:
        raise FrameError(f"{command[0]} failed: {done.stderr.strip()[-500:]}")
    return done.stdout


def probe(video: Path) -> tuple[float, int]:
    """(fps, frame count) of a video, counting the frames by decoding them."""
    out = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", "stream=r_frame_rate,nb_read_frames", "-of", "csv=p=0", str(video),
    ])
    rate, count = out.strip().split(",")[:2]
    return float(Fraction(rate)), int(count)


def extract_frames(video: Path, frames_dir: Path) -> int:
    """Write every frame of a video as `<frames_dir>/000000.jpg` onward; return the count."""
    frames_dir.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(video),
        "-fps_mode", "passthrough", "-start_number", "0", "-q:v", "2",
        str(frames_dir / "%06d.jpg"),
    ])
    return len(list(frames_dir.glob("*.jpg")))


def _crop_filter(crop: Crop) -> str:
    x, y, w, h = crop
    even = "trunc({}/2)*2"
    return "crop={}:{}:{}:{}".format(
        even.format(f"iw*{w}"), even.format(f"ih*{h}"), even.format(f"iw*{x}"), even.format(f"ih*{y}")
    )


def crop_frames(frames_dir: Path, frames: list[int], crop: Crop, out_dir: Path) -> list[Path]:
    """Crop each frame to `crop`; return the cropped images, in order."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for frame in frames:
        out = out_dir / frame_name(frame)
        if not out.is_file():
            _run([
                "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(frame_path(frames_dir, frame)),
                "-vf", _crop_filter(crop), "-q:v", "2", str(out),
            ])
        paths.append(out)
    return paths


def grid(images: list[Path], out: Path, crop: Crop | None = None, cell_width: int = GRID_CELL_WIDTH) -> Path:
    """Tile up to 6 images, 2 across by 3 down, left to right then down; empty cells stay black."""
    if not 1 <= len(images) <= GRID_FRAMES:
        raise FrameError(f"a grid holds 1 to {GRID_FRAMES} frames, not {len(images)}")
    out.parent.mkdir(parents=True, exist_ok=True)
    listing = out.with_suffix(".ffconcat")
    listing.write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{path.resolve()}'\n" for path in images),
        encoding="utf-8",
    )
    filters = ([_crop_filter(crop)] if crop else []) + [
        f"scale={cell_width}:-2",
        f"tile={GRID_COLUMNS}x{GRID_ROWS}",
    ]
    _run([
        "ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
        "-vf", ",".join(filters), "-frames:v", "1", "-q:v", "2", str(out),
    ])
    listing.unlink()
    return out


def image_size(image: Path) -> tuple[int, int]:
    width, height = _run([
        "ffprobe", "-v", "error", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(image),
    ]).strip().split(",")[:2]
    return int(width), int(height)


def b64(image: Path) -> str:
    return base64.b64encode(image.read_bytes()).decode("ascii")
