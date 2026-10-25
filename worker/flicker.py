"""Exposure flicker, measured by code on every frame. Standard library only.

The contract's rule: flicker is a brightness spike that crosses the rubric's
threshold and comes back within `flicker_return_frames` frames. A change that
stays (a cut, the car driving into shadow) is not flicker and is not reported.

How it's measured, frame by frame:
1. A spike starts at frame i when |b[i] - b[i-1]| is above the threshold.
   Frame i-1 is the baseline.
2. It has come back at the first frame j, at most `return_frames` frames after
   i, whose brightness is within the threshold of the baseline again.
3. Frames i to j-1 are the anomaly. Its value is the largest change between
   consecutive frames from i to j, in the contract's unit.
4. No return in time means the change stayed: nothing is reported.
"""
from __future__ import annotations

from dataclasses import dataclass

UNIT = "mean brightness change between consecutive frames, 0-255"


@dataclass(frozen=True)
class Spike:
    frame_start: int  # first frame off the baseline
    frame_end: int  # last frame off the baseline
    baseline_frame: int  # the frame before the spike
    return_frame: int  # the first frame back at the baseline
    value: float  # largest consecutive-frame change, 0-255

    @property
    def evidence_frames(self) -> list[int]:
        """The baseline frame and every frame off it, as in the contract's sample."""
        return list(range(self.baseline_frame, self.frame_end + 1))


def detect(series: list[float], threshold: float, return_frames: int) -> list[Spike]:
    spikes: list[Spike] = []
    count = len(series)
    i = 1
    while i < count:
        baseline = series[i - 1]
        if abs(series[i] - baseline) <= threshold:
            i += 1
            continue
        back = None
        for j in range(i + 1, min(count, i + return_frames + 1)):
            if abs(series[j] - baseline) <= threshold:
                back = j
                break
        if back is None:
            # The change stayed, or the take ended before it could come back.
            i += 1
            continue
        value = max(abs(series[k] - series[k - 1]) for k in range(i, back + 1))
        spikes.append(Spike(i, back - 1, i - 1, back, round(value, 1)))
        i = back + 1
    return spikes


def seconds(frame: int, fps: float) -> float:
    """The contract's rule: seconds = frame / fps, rounded to 3 decimals."""
    return round(frame / fps, 3)
