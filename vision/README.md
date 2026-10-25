# vision

The reviewer step (Danny, #6): one question per check, votes across 3 or 4 runs, clip or grid for flagged moments, and the accuracy test against `answer_key.csv`. The worker calls it.

Standard library and ffmpeg only, because it runs inside the worker's `python:3.12-slim` container on `clearframe-net`.

| File | What |
| --- | --- |
| `review.py` | The questions, the three ways of sending frames, votes, the sparse and dense passes. `review_take()` is what the worker calls. |
| `model.py` | The model client (`VisionClient`, from `VISION_URL`, `VISION_API_KEY`, `VISION_MODEL`) and `FakeModel` for laptop tests. |
| `frames.py` | ffmpeg helpers: extract frames, crop to the road, build a 2×3 grid. |
| `accuracy.py` | The accuracy test: `run` reviews a shot's takes every way, `score` checks the runs against the answer key. |
| `answer_key.csv` | Every visible flaw in every take, written by a person. |
| `samples/answers-4-2.json` | Fake model answers matching `contracts/samples/result-4-2.json`, for laptop tests. |

## How it asks

- **One question per check.** The question text is #6's, word for word. After it, `review.py` adds one line so a frame without arrows or hands isn't noted ("If no arrow is visible in the car's lane in a frame, answer yes for that frame."), plus the reply format: one `<n>: yes` or `<n>: no - <a few words>` line per frame. A reply that doesn't give exactly one answer per frame counts as unparsed.
- **Three ways of sending frames** (`Settings.mode`):
  - `frames`: separate images, at most `max_images` per request.
  - `grid`: one image of 6 frames, 2 across by 3 down, left to right then down, cropped to the road.
  - `video`: the chosen frames sent as one video (base64 JPEGs joined by commas). The model sees exactly those frames, not a 2 fps sample of the take.
- **Sparse pass:** 16 frames spread over the whole take.
- **Dense pass:** 12 consecutive frames, cropped to the road, around every moment code flagged (`flagged=`) or the sparse pass noted. Where the dense pass finds the flaw, its note replaces the sparse one; where it doesn't, the sparse note stands.
- **Votes:** each request runs `runs` times (3), and a frame is noted only when `min_agree` runs (2) say so. Votes need randomness, so `Settings` refuses more than one run with thinking off and a temperature of 0 or `top_k` of 1.
- **Code sets the numbers.** Frame numbers come from the frames sent, never from the model, and seconds are frame ÷ fps, rounded to 3 decimals. The model's words go only into `model_note`, which the evidence page shows and Slack never does.

The defaults in `Settings` (grid, thinking off, 3 runs) stand until #6's decision is posted; then change them there.

## For the worker (#5)

```python
import sys
sys.path.insert(0, "/app")  # the repo root
from vision import review
from vision.model import VisionClient

result = review.review_take(
    take_id="4-2",
    frames_dir=frames_dir,       # every frame of the take as 000000.jpg onward
    frame_count=120,
    fps=24,
    checks=rubric_checks,        # the rubric's checks; code checks like exposure_flicker are skipped
    model=VisionClient.from_env(),
    flagged=[(61, 63)],          # code's measured anomalies, as (frame_start, frame_end)
)
result.candidates        # the result file's `candidates`, in the contract's shape
result.vision            # the result file's `vision` block
result.evidence_frames   # frame numbers to copy into results/frames/<take_id>/
result.checks["road_markings"].state  # "done" or "error"
result.errors            # fixed messages for checks in error, never model output
```

Laptop tests use `FakeModel.from_file(Path("vision/samples/answers-4-2.json"))` in place of `VisionClient`: it answers `no` for frames 48 to 64 with the sample's note, and `yes` everywhere else.

## The answer key

`answer_key.csv` has one row per flaw. Give frames (`frame_start`, `frame_end`) or seconds (`t_start`, `t_end`), whichever is easier while watching. A take you watched and found clean gets one row with `issue_type` set to `none`, so the scorer knows it was checked. Takes with no row aren't scored.

Use the contract's issue types (`road_markings`, `extra_finger`) where they fit, and a short lowercase name for Blake's other reasons, like `out_of_place_object` or `lighting`. `score` counts flaws by type, which is how #16 picks the second check.

```csv
take_id,issue_type,frame_start,frame_end,t_start,t_end,description
4-1,none,,,,,
4-2,road_markings,,,2.0,2.7,right-lane arrow points at the camera
```

## The accuracy test on the GB10

Run it where the worker runs, on `clearframe-net`, so it reaches Qwen the same way. The run files hold the model's replies; they go in `~/clearframe-input/accuracy/`, never in git.

```bash
mkdir -p ~/clearframe-input/accuracy
docker run --rm --network clearframe-net \
  --env-file ~/clearframe-input/vision.env \
  -e VISION_URL=http://nemoclaw-vllm:8000/v1 \
  -e PATH=/opt/ffmpeg:/usr/local/bin:/usr/bin:/bin \
  -v ~/clearframe/app:/app:ro \
  -v ~/clearframe-input:/input:ro \
  -v ~/clearframe-input/accuracy:/out \
  -v ~/ffmpeg-7.0.2-arm64-static:/opt/ffmpeg:ro \
  python:3.12-slim python /app/vision/accuracy.py run \
    --takes /input/takes --shot 4 --check road_markings --out /out \
    --modes grid,frames,video --thinking off,on --runs 3 --max-images 8

python3 ~/clearframe/app/vision/accuracy.py score \
  --answer-key ~/clearframe/app/vision/answer_key.csv ~/clearframe-input/accuracy
```

- **Set `--max-images`** to the image limit in `--limit-mm-per-prompt` from `docker inspect nemoclaw-vllm`. If it's 1, run only `--modes grid,video`.
- **Start small:** `--take 4-2 --modes grid --thinking off` takes a few minutes; the full set is 6 setups × 6 takes.
- **Dry run on a laptop**, no model: add `--fake vision/samples/answers-4-2.json` and point `--takes` at any folder of clips named like `4-2.mp4`.

`score` prints the answer-key counts by type and one row per setup: catches out of planted flaws, catches from the sparse pass alone, false notes on clean takes, notes off the flaw on flawed takes, overlap with the answer key's frames (IoU) and how far the start is off, seconds per take, unparsed replies and errors. Post the table on #6 with the decision.
