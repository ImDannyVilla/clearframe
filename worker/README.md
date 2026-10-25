# worker

The host review worker (Jason, #5): takes in from `~/clearframe-input/`, every frame extracted with ffmpeg, flicker measured, and result files written exactly as `contracts/README.md` says. It runs in `python:3.12-slim` on `clearframe-net`, so it uses the standard library only.

| File | What |
| --- | --- |
| `main.py` | The poll loop and one take's review: revision 1, frames, checks, evidence, final revision |
| `flicker.py` | The flicker rule: a spike past `flicker_threshold` that returns within `flicker_return_frames` |
| `media.py` | ffprobe, the per-frame brightness series, frame extraction and road crops |
| `review.py` | The bridge to Danny's reviewer (`vision.review.review_take`) and which model it uses |
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
| `EVIDENCE_LIMIT` | `8` | Most evidence frames kept per flicker spike |
| `VISION_URL`, `VISION_MODEL`, `VISION_API_KEY` | from `vision.env` | The model server, read by `vision.model.VisionClient` |
| `VISION_ANSWERS` | unset | A fake-model answers file (like `vision/samples/answers-4-2.json`) instead of the model, for dry runs |
| `FFMPEG`, `FFPROBE` | from `PATH` | The worker's binaries; `vision/` uses `ffmpeg` from `PATH` |

How the reviewer asks (frames, grid or video, thinking, votes, sparse and dense sizes) is set in `vision.review.Settings`, which #6 owns.

## How a take is reviewed

1. A render in `renders/` whose size held for one poll is picked up once. A take whose final result already exists is skipped, so a restart doesn't redo work.
2. Revision 1 goes out with every enabled check `running`.
3. ffmpeg decodes every frame. Code takes each frame's mean brightness (0–255) and writes `frames/<take_id>/brightness.json`.
4. `exposure_flicker` is measured on that series. Evidence is the baseline frame plus every frame off it.
5. The noted checks (`road_markings`, and `extra_finger` if enabled) go to Danny's `vision.review.review_take()` with every extracted frame and the flicker spikes as `flagged`. It runs the sparse pass, the dense windows around every sparse note and every spike, and the votes, and returns the candidates and the `vision` block in the contract's shape. The worker copies the evidence frames it names into `results/`.
6. The final revision: every check `done` or `error`, with one fixed message per error, never model output:
   - `reviewer not available`: no `VISION_URL` and `VISION_MODEL`, and no `VISION_ANSWERS`
   - `the reviewer gave no usable answer`: the model server failed, or its replies didn't parse
   - `reviewer failed`: anything else went wrong in the reviewer (the traceback goes to the worker's log)

Code sets every frame range, timestamp and evidence path, here and in `vision/`. The model only answers yes or no per frame.
