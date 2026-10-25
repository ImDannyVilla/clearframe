# Demo runbook

The GB10 session in order: set up once, then the 90-second demo. Commands come from the [GB10 cheat sheet](gb10-cheatsheet.md) and the [worker README](../worker/README.md); this page only puts them in order.

## 1. Set up (once, about 15 minutes)

**On the host.**

```bash
cd ~/clearframe/app && git pull
# Morgan's takes and their rows, if not staged yet (#23)
rsync -a --ignore-existing --exclude '._*' /media/$USER/SanDisk/clearframe-input/ ~/clearframe-input/
python3 ~/clearframe-input/staging/append_rows.py --dry-run && python3 ~/clearframe-input/staging/append_rows.py
# NemoClaw sometimes recreates its model server without our network; "already exists" is fine
docker network connect clearframe-net nemoclaw-vllm
```

**Check the inputs before anything runs.**

- `~/clearframe-input/shot_list.csv`: `expected_takes` for each demo shot must equal the number of originals you will copy into `renders/`. If it's higher, the shot card never posts. Check with `ls ~/clearframe-input/takes/4-[0-9].mp4 | wc -l` against shot 4's row.
- `~/clearframe-input/generation_log.csv`: `4-2r1`'s prompt must be the exact version 2 text from the contract, so its hash matches the approval.
- Never copy the 3840-wide review copies into `takes/` or `renders/`.

**Preflight**, in the worker's container. Every line should be PASS.

```bash
docker run --rm --network clearframe-net --env-file ~/clearframe-input/vision.env \
  -e VISION_URL=http://nemoclaw-vllm:8000/v1 -e PATH=/opt/ffmpeg:/usr/local/bin:/usr/bin:/bin \
  -v ~/clearframe/app:/app:ro -v ~/clearframe/results:/results -v ~/clearframe-input:/input:ro \
  -v ~/ffmpeg-7.0.2-arm64-static:/opt/ffmpeg:ro \
  python:3.12-slim python /app/worker/preflight.py
```

**Start the worker.**

```bash
docker rm -f worker 2>/dev/null
docker run -d --name worker --network clearframe-net --env-file ~/clearframe-input/vision.env \
  -e VISION_URL=http://nemoclaw-vllm:8000/v1 -e PATH=/opt/ffmpeg:/usr/local/bin:/usr/bin:/bin \
  -v ~/clearframe/app:/app:ro -v ~/clearframe/results:/results -v ~/clearframe-input:/input:ro \
  -v ~/ffmpeg-7.0.2-arm64-static:/opt/ffmpeg:ro \
  python:3.12-slim python /app/worker/main.py
```

**Inside the sandbox** (`nemoclaw clearframe connect`).

```bash
mkdir -p /sandbox/clearframe/state /sandbox/clearframe/approved_generation_queue
export SLACK_BOT_TOKEN=...   # from the password manager; never in git or on screen
export SLACK_CHANNEL=...     # the team channel's ID
nohup python3 /sandbox/clearframe/host/app/agent/poller.py --reset > /sandbox/clearframe/state/poller.log 2>&1 &
nohup python3 /sandbox/clearframe/host/app/evidence/server.py > /sandbox/clearframe/state/evidence.log 2>&1 &
```

`--reset` moves the old `clearframe.db` aside; it never deletes it. Old result files in `~/clearframe/results/` are ingested again, so move them aside on the host first if you want an empty first card.

**Forward the page** (on the host): `openshell forward start --background 8090 clearframe`, then open `http://127.0.0.1:8090` in the GB10's browser.

## 2. Dry run (5 minutes, before every rehearsal)

1. Copy one take in: `cp ~/clearframe-input/takes/6-1.mp4 ~/clearframe-input/renders/`.
2. `docker logs -f worker` shows `picked up`, then `final`.
3. `tail /sandbox/clearframe/state/poller.log` shows `ingested 6-1`.
4. `http://127.0.0.1:8090/take/6-1` opens.
5. Once the shot's expected takes are in, one card arrives in Slack.

## 3. The demo (90 seconds)

The full loop uses our backup Shot 4: `4-2` has a planted road-marking flaw, and `4-2r1` was generated from its exact version 2 prompt. Morgan's shots 5 and 6 are shown as the real-footage results (beat 7).

| Time | Beat | Action |
| --- | --- | --- |
| 0:00 | Takes land | `cp ~/clearframe-input/takes/4-[0-9].mp4 ~/clearframe-input/renders/`. The pattern copies originals only: not `4-2r1` or `4-2r2`. The worker and poller are already running. |
| 0:15 | Slack card | Show the card: take IDs, statuses, issue categories and the link. No prompt, frame or note. |
| 0:30 | Evidence page | Click the link: `/shot/4`, then `/take/4-2`. Show the note's frames and the brightness plot (code measured every frame). Pick the approver and press **Confirm note**. If the model's note is wrong, press **Dismiss note** instead: the person decides. |
| 0:45 | The fix | The page shows the prompt diff from version 1 to version 2 and the hash. Press **Approve version 2**. On the host: `cat` the queue file, or show it from the sandbox at `/sandbox/clearframe/approved_generation_queue/4-2-v2.json`. |
| 1:00 | The retake | Say it plainly: "this retake was generated in prep from that exact prompt." `cp ~/clearframe-input/takes/4-2r1.mp4 ~/clearframe-input/renders/`. |
| 1:15 | Re-review | `/take/4-2r1`: reviewed as a new take, "retake of 4-2", its own status as it comes. Scroll the audit trail. |
| 1:25 | Close | The numbers from the writeup, and: "No frames, prompts or brand assets leave the GB10. Slack receives only limited review metadata." |

## If something goes wrong

| Symptom | Fix |
| --- | --- |
| Preflight: model server unreachable | `docker network connect clearframe-net nemoclaw-vllm`, then preflight again |
| A take stays `review_pending` with "check error (road markings)" | The reviewer had no usable answer; check `docker logs worker` and the model server, then move that take's result file aside and copy the take in again |
| No Slack card | `expected_takes` is higher than the takes copied in, or `SLACK_BOT_TOKEN` / `SLACK_CHANNEL` aren't set in the poller's shell; the poller log says "not posted" |
| Approve refused: "no generation info" | #33 isn't merged or the GB10 hasn't pulled it; `git pull`, restart the poller with `--reset` |
| Page says "Stale or missing form token" | The page restarted; reload it before clicking |
| The page doesn't open | `openshell forward start --background 8090 clearframe` again; check `evidence.log` |
