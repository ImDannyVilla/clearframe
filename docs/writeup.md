# ClearFrame: writeup draft

Draft for #12. Lines marked **TODO** take a number from the GB10 run; everything else comes from the issues and pull requests. Before submitting, decide how to name our agency contact: the repo uses the stand-in "Morgan".

## The problem

An independent agency's AI studio makes animatics with AI before shooting the real ad. Each shot gets 2 to 10 renders, and a creative director watches every one. Takes are rejected for:
- road markings that are wrong
- objects that don't belong
- lighting that looks artificial

Vision tools see only every Nth frame. A lot of the work is "code red": it can't touch a cloud service, so those projects get no review tools at all.

## What ClearFrame does

A local agent on one Dell Pro Max GB10 runs the review checklist on every take before the creative director sees it, then waits for a person:

1. A take lands in the render folder. The host review worker extracts every frame with ffmpeg.
2. Code measures every frame's brightness and reports exposure flicker: a jump past the rubric's threshold that comes back within 16 frames. Nothing measurable is left to every Nth frame.
3. Qwen3.6 answers one yes-or-no question per check, on sparse frames and then dense windows around anything code or the sparse pass flagged. Each request runs three times, and a note stands only when two agree. Its findings are candidate notes, never verdicts. Code sets every frame, timestamp and evidence path.
4. Inside a NemoClaw sandbox, the automation writes results to SQLite and posts a Slack card built from a fixed template: take IDs, statuses, issue categories and a link. No prompt, frame or model text reaches Slack.
5. On a local evidence page, the creative director sees the frames, the brightness plot and the model's words, and confirms or dismisses each note.
6. A confirmed issue gets a prompt fix from the rubric's fix library, in the creative directors' own format ("Make sure: …"), shown as an exact diff with its hash. No model writes the fix.
7. Approval is bound to the take, prompt version, hash, approver and time. The queue writer refuses it if the prompt changed. The approved prompt goes to a queue folder for the studio's own generator; ClearFrame never generates video.
8. The returned take is reviewed as a new take, linked to its parent. Every step is in the audit trail.

## What runs where

| Piece | Where | Reaches |
| --- | --- | --- |
| Qwen3.6-35B-A3B NVFP4, one model for the agent and the vision checks | NemoClaw's managed vLLM on the host (vLLM 26.05.post1, `--gpu-memory-utilization 0.4`) | — |
| Review worker: ffmpeg and Python's standard library | Docker on the host, on `clearframe-net`, an internal network | Qwen only; no internet (checked by `worker/preflight.py`) |
| Automation, SQLite, Slack card, approval, queue, evidence page | The NemoClaw sandbox (OpenShell, Restricted policy plus Slack); `results/` mounted read-only | Slack and the local model only |
| Evidence page | Port 8090 in the sandbox, forwarded to the GB10's loopback | The GB10's own screen |

Versions: NemoClaw 0.0.137, OpenShell 0.0.116, OpenClaw 2026.9.2, Docker 29.6.2.

## The numbers

- Seconds per take reviewed, with a batch running: **TODO** (from `worker/report.py` on shots 5 and 6)
- Seconds from approval to the queue file: **TODO**
- GPU memory with the agent and the reviews on one model: **TODO** (`nvidia-smi`)
- A real Slack card's payload, checked for metadata only: **TODO**
- Flicker check: the threshold was tuned on our own backup clip, which dims from about 97 to 33 three times, for 16, 6 and 15 frames. The largest jump was 33.8; the clean clip's was 1.0. Morgan's takes peak at 6.3 against the 8.0 threshold, so the check stays quiet on them.

## What we found about the model

The sample is small: three of Morgan's takes and three of our backups. We report it as it came.

- **Asked about one frame, Qwen can't judge road markings.**
  - On our backup take, no setup caught the planted wrong arrow. Grid's one "catch" was Qwen answering "no" for frames with no arrow at all; the prompt now offers "none".
  - On Morgan's takes, asked "do the lane lines look like a real road?", it said no on 35 of 36 answers, clean take included.
- **Compared with the source footage, it can.** Shown the source frame and the take frame side by side and asked whether the take adds, removes, bends or moves any road markings, Qwen flagged all four frames in 5-1's flaw range (frames 82-112) 3 of 3 times. With the two-of-three vote, it made no false note on the clean take 6-1. It also flagged a crosswalk smear in 5-2 that the reviewer hadn't noted. Each request took about 3 to 9 seconds.
- **Unnatural objects aren't caught.** Asked whether flowers look fake, it said no on every crop of 5-2, including the frame Morgan noted.
- **Thinking on isn't usable yet.** 8 of 12 replies were cut off at 8,192 tokens, at over five minutes a take.
- Live run on shots 5 and 6 with the shipped settings (grid, thinking off, three votes): **TODO** (paste `worker/report.py`'s table)

This is why every model finding is a candidate note that a person confirms. The side-by-side result is the next step: give the worker each take's source and compare, rather than judge a frame alone.

## What we left out

- Compare-with-source review in the worker. Tested ad hoc on the GB10, not wired in.
- A second noted check (glossy road surface, unnatural objects): no question we tried caught them reliably.
- Lighting, physics, shadow and palette checks.
- Video generation on the box, editing, Frame.io.
- Cosmos-Reason2 as a second voter. Not downloaded on the event's wifi.
- Two loose ends in the automation: a take flagged only by flicker gets no prompt fix, and a returning retake isn't yet checked against its approval's prompt hash.
- A login for the evidence page. Today, physical access to the GB10 is the login.

## The privacy claim

No frames, prompts or brand assets leave the GB10. Slack receives only limited review metadata. OpenShell locks down the sandbox; the host worker is kept offline separately, by its internal Docker network.
