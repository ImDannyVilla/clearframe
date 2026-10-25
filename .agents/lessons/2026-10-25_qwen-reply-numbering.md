---
date: 2026-10-25
issue: "#6"
area: vision
severity: important
---

# Qwen's replies didn't parse: frames numbered from 0, numbers left off, thinking cut off

## What happened

The first live run of `vision/accuracy.py` against Qwen3.6 on `nemoclaw-vllm` (one generated clip, all six setups) had 12 of 36 replies unparsed. Video mode with thinking off lost all 3: Qwen answered `0: yes` to `15: yes` for 16 frames. Separate images with thinking off lost 3 of 6: lines came back as `. no - ...` with no number. With thinking on, most failed replies were empty after about 3 minutes.

## Root cause

The prompt described the format as `<frame number>: yes`, with no example and no "start at 1", and the parser wanted exactly 1 to N. An empty reply with thinking on most likely means the thinking used up `max_tokens` (8192), but the run file didn't record `finish_reason`, so it looked like garbage.

## Fix

- The prompt says "1 to N" and ends with a short example reply for that question.
- `parse_answers` reads 0 to N-1 as 1 to N, and reads a reply with no numbers in order, only when it has exactly one answer per frame.
- Each logged reply keeps the server's `finish_reason`, and `accuracy.py run` prints how many replies were cut off at `max_tokens`. `--max-tokens` sets the limit.

After the fix, the same run with thinking off parsed 24 of 24.

## Prevention

`tests/test_vision_review.py` covers both reply shapes, the example in the prompt and the cut-off log. Run a one-take live check (`--take 4-2 --thinking off`) after any prompt change, before the full set.
