# worker

The host review worker (Jason, #5): takes in from `~/clearframe-input/`, every frame extracted with ffmpeg, flicker measured, and result files written exactly as `contracts/README.md` says. It runs in `python:3.12-slim` on `clearframe-net`, so it uses the standard library only.

| File | What |
| --- | --- |
| `main.py` | The poll loop and one take's review: revision 1, frames, checks, evidence, final revision |
| `flicker.py` | The flicker rule: a spike past `flicker_threshold` that returns within `flicker_return_frames` |
| `media.py` | ffprobe, the per-frame brightness series, frame extraction and road crops |
| `review.py` | The reviewer interface the worker calls, and the answers-file reviewer for tests |
| `takes.py` | Take IDs, `shot_list.csv` and `generation_log.csv` |

## Run it

On the GB10, with the cheat sheet's `docker run` (it runs `python /app/worker/main.py`). By hand:

```bash
CLEARFRAME_INPUT=~/clearframe-input CLEARFRAME_RESULTS=~/clearframe/results python3 worker/main.py
python3 worker/main.py --once   # review what's in renders/ now, then exit
```

| Variable | Default | What |
| --- | --- | --- |
| `CLEARFRAME_INPUT` | `/input` | Holds `renders/`, `shot_list.csv` and `generation_log.csv` |
| `CLEARFRAME_RESULTS` | `/results` | Result files and evidence frames |
| `CLEARFRAME_WORK` | the temp folder | Every extracted frame while a take is reviewed; removed after |
| `CLEARFRAME_RUBRIC` | `rubric/rubric.yaml` | Read again for every take |
| `POLL_SECONDS` | `2` | How often `renders/` is checked |
| `WORKER_PARALLEL` | `3` | Takes reviewed at once; they share one model server |
| `SPARSE_STEP` | fps ÷ 3 (8 at 24 fps) | Frames between sparse-pass samples |
| `DENSE_PAD` | `SPARSE_STEP` | Frames added on each side of a flagged moment |
| `ROAD_CROP` | `iw:ih/2:0:ih/2` | ffmpeg crop for road checks' dense windows: the lower half |
| `EVIDENCE_LIMIT` | `8` | Most evidence frames kept per anomaly or note |
| `VISION_ANSWERS` | unset | A canned answers file instead of the model, for tests and dry runs |
| `FFMPEG`, `FFPROBE` | from `PATH` | The binaries |

## How a take is reviewed

1. A render in `renders/` whose size held for one poll is picked up once. A take whose final result already exists is skipped, so a restart doesn't redo work.
2. Revision 1 goes out with every enabled check `running`.
3. ffmpeg decodes every frame. Code takes each frame's mean brightness (0–255) and writes `frames/<take_id>/brightness.json`.
4. `exposure_flicker` is measured on that series. Evidence is the baseline frame plus every frame off it.
5. Each noted check (`road_markings`, `extra_finger`) goes to the reviewer: a sparse pass over every `SPARSE_STEP`th frame, then dense windows of consecutive frames, cropped to the road for road checks, around every sparse note and every flicker spike. Dense notes replace the sparse note in their window; if the dense pass sees nothing there, the sparse note stays for a person to confirm or dismiss.
6. The final revision: every check `done` or `error`, with one fixed message per error (`road_markings: reviewer not available`), never model output.

Code sets every frame range, timestamp and evidence path. The reviewer only says which of the frames it was sent show the problem.

## The reviewer interface

Danny's `vision/reviewer.py` (#6) provides `review(check, pass_name, frames, context)`. The full signature is in `review.py`'s docstring. Until it exists, noted checks end in `error`, which keeps takes at `review_pending`; set `VISION_ANSWERS` for a dry run.
