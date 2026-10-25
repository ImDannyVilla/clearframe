# GB10 cheat sheet

Commands for setting up and running ClearFrame on the GB10. This sheet assumes you named the sandbox `clearframe` at onboarding. Command names differ between NemoClaw versions, so check `nemoclaw onboard --help` on the box first.

Qwen3.6 is the only model. NemoClaw's model server (container `nemoclaw-vllm`) runs it for the agent and for the vision checks.

## Host network: the worker and its route to Qwen

The review worker runs on the host, outside OpenShell, so an internal Docker network is the only thing keeping it offline. It reaches Qwen because we add NemoClaw's model server to that same network. Run these on the GB10 host.

**Load what's on the stick.** It mounts at `/media/$USER/SanDisk`; check with `ls /media/$USER`. The vLLM 0.20.0 image is only for Cosmos later.

```bash
docker load -i /media/$USER/SanDisk/nemoclaw-vllm.tar
docker load -i /media/$USER/SanDisk/python-3.12-slim.tar
docker load -i /media/$USER/SanDisk/vllm-0.20.0.tar
mkdir -p ~/.cache/huggingface && tar -xf /media/$USER/SanDisk/hf-models.tar -C ~/.cache/huggingface
tar -xJf /media/$USER/SanDisk/ffmpeg/ffmpeg-release-arm64-static.tar.xz -C ~
```

**Create the network.** `--internal` means no route to the internet.

```bash
docker network create --internal clearframe-net
```

**Check how NemoClaw started Qwen.**

```bash
docker inspect nemoclaw-vllm --format '{{json .Args}}'
```

- **`--language-model-only`** means text only, so images won't work. Use the fallback below.
- **`--limit-mm-per-prompt`** caps images per request. If it's one, send a grid or frames as one video.
- **The access key:** find the one NemoClaw set, in that output or in NemoClaw's config. Put it in `~/clearframe-input/vision.env` as `VISION_API_KEY=...`, with the served model name from `/v1/models` as `VISION_MODEL=...`. That file never goes in git.

**Give the worker a route.** The worker then calls `http://nemoclaw-vllm:8000/v1` with that key. If NemoClaw restarts its server, run this again.

```bash
docker network connect clearframe-net nemoclaw-vllm
```

**How to send frames to it**

- **Base64:** NemoClaw's server can't read our files, so send frames as base64 (`data:image/jpeg;base64,...`).
- **Qwen samples video at 2 fps by default.** To show every frame of a moment, send frames as images, as one grid, or as one video built from the frames you choose (`data:video/jpeg;base64,...`, frames joined by commas).
- **Thinking is on by default.** Turn it off per request with `"chat_template_kwargs": {"enable_thinking": false}`.
- **The agent and the vision checks share this server,** so measure seconds per take with a whole batch of takes.

**Fallback: run Qwen yourselves.** Use this only if NemoClaw's server won't take images. Start Qwen with our flags, then onboard NemoClaw with `NEMOCLAW_PROVIDER=vllm` to attach to it; check `nemoclaw onboard --help` for how it finds the server. Point `VISION_URL` at `http://qwen:8000/v1`.

```bash
docker run -d --name qwen --gpus all --ipc=host --shm-size=16g -p 127.0.0.1:8000:8000 \
  -e HF_HUB_OFFLINE=1 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v ~/clearframe/results:/results:ro \
  --entrypoint vllm nvcr.io/nvidia/vllm:26.05.post1-py3 \
  serve nvidia/Qwen3.6-35B-A3B-NVFP4 \
  --served-model-name qwen --port 8000 \
  --gpu-memory-utilization 0.40 --max-model-len 65536 --max-num-seqs 8 \
  --limit-mm-per-prompt '{"video": 1, "image": 8}' \
  --allowed-local-media-path /results \
  --reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder
docker network connect clearframe-net qwen
```

With this fallback, Qwen can also read frames as `file:///results/...`.

**Start the worker** once #5's code exists. The script path is whatever Jason names it.

```bash
docker run -d --name worker --network clearframe-net \
  --env-file ~/clearframe-input/vision.env \
  -e VISION_URL=http://nemoclaw-vllm:8000/v1 \
  -e PATH=/opt/ffmpeg:/usr/local/bin:/usr/bin:/bin \
  -v ~/clearframe/app:/app:ro \
  -v ~/clearframe/results:/results \
  -v ~/clearframe-input:/input:ro \
  -v ~/ffmpeg-7.0.2-arm64-static:/opt/ffmpeg:ro \
  python:3.12-slim python /app/worker/main.py
```

The network has no PyPI, so the worker uses Python's standard library only, or wheels you bring in. After a `git pull`, run `docker restart worker`.

**Check it (smoke test 6).** The first command prints Qwen's model list through the worker's network. The second must fail; if it succeeds, the network isn't internal.

```bash
docker run --rm --network clearframe-net --env-file ~/clearframe-input/vision.env python:3.12-slim python -c "import os, urllib.request as u; r = u.Request('http://nemoclaw-vllm:8000/v1/models', headers={'Authorization': 'Bearer ' + os.environ['VISION_API_KEY']}); print(u.urlopen(r).read()[:200])"
docker run --rm --network clearframe-net python:3.12-slim python -c "import urllib.request as u; u.urlopen('https://example.com', timeout=5)"
```

### Cosmos-Reason2 later (challenger or second voter)

Cosmos-Reason2-8B isn't on the stick, so download it on the GB10. Without Omni there's room for it next to Qwen: about 0.40 plus 0.30 of memory.

**Download it (17.5 GB, once).** First click Agree on its Hugging Face page, with the account whose token you use. DGX OS blocks system-wide pip installs, so use a small venv:

```bash
python3 -m venv ~/hf && ~/hf/bin/pip install -q huggingface_hub
~/hf/bin/hf auth login
~/hf/bin/hf download nvidia/Cosmos-Reason2-8B
```

If `python3 -m venv` fails, run the same two `hf` commands through the vLLM image instead. The token and the weights land in the same host cache. Revoke the token after the event.

```bash
docker run --rm -it -v ~/.cache/huggingface:/root/.cache/huggingface --entrypoint hf vllm/vllm-openai:v0.20.0 auth login
docker run --rm -v ~/.cache/huggingface:/root/.cache/huggingface --entrypoint hf vllm/vllm-openai:v0.20.0 download nvidia/Cosmos-Reason2-8B
```

**Start it next to Qwen**, on the same network:

```bash
docker run -d --name cosmos --network clearframe-net --gpus all --ipc=host --shm-size=16g \
  -e HF_HUB_OFFLINE=1 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v ~/clearframe/results:/results:ro \
  --entrypoint vllm vllm/vllm-openai:v0.20.0 \
  serve nvidia/Cosmos-Reason2-8B \
  --served-model-name cosmos --port 8000 \
  --gpu-memory-utilization 0.30 --max-model-len 32768 --max-num-seqs 8 \
  --limit-mm-per-prompt '{"video": 1, "image": 8}' \
  --media-io-kwargs '{"video": {"fps": 4, "num_frames": 256}}' \
  --allowed-local-media-path /results
docker logs -f cosmos
```

- **Memory:** 0.30 is about 38 GB, above its 32 GB minimum. Qwen plus Cosmos is about 0.70.
- **fps 4** matches its model card's training setup.
- **Requests:** set `max_tokens` to 4096 so its reasoning isn't cut off, and read the answer that comes after the reasoning.
- **Pointing the worker at it:** use `VISION_URL=http://cosmos:8000/v1` with `VISION_MODEL=cosmos`, run the same takes, and count catches and false notes. Keep the winner, or use both as voters.

## Skills

OpenClaw only loads skills from its workspace folder, so link ours in from the read-only mount.

**Get a shell in the sandbox** (from the host). From there, `openclaw tui` opens the agent's chat view.

```bash
nemoclaw clearframe connect
```

**Where ours live:** `agent/skills/status-card/` and `agent/skills/prompt-fix/` in the repo, which is `/sandbox/clearframe/host/app/` inside the sandbox. Each skill is a folder with a `SKILL.md`: YAML frontmatter with a lowercase, hyphenated `name` that matches the folder, then the instructions.

**Link them in** (inside the sandbox):

```bash
mkdir -p /sandbox/.openclaw/workspace/skills
ln -s /sandbox/clearframe/host/app/agent/skills/status-card /sandbox/.openclaw/workspace/skills/status-card
ln -s /sandbox/clearframe/host/app/agent/skills/prompt-fix /sandbox/.openclaw/workspace/skills/prompt-fix
openclaw skills list
```

- **Both should show as eligible.** If they don't, copy instead of linking (`cp -r`), and copy again after every pull.
- **A workspace skill wins** over any other skill with the same name.
- **take-intake isn't an OpenClaw skill.** It's the worker on the host.

## SQLite and the recurring job

The database is one file the sandbox app owns, `/sandbox/clearframe/state/clearframe.db`. Nothing on the host writes it.

**Create the folders** (inside the sandbox, once). The code creates the tables itself on first run.

```bash
mkdir -p /sandbox/clearframe/state /sandbox/clearframe/approved_generation_queue
```

**Look inside.** These use Python's built-in sqlite3, in case the `sqlite3` command isn't installed. The first lists every take's status; the second shows the last 10 events.

```bash
python3 -c "import sqlite3; c=sqlite3.connect('/sandbox/clearframe/state/clearframe.db'); print(c.execute('select take_id, status from takes order by take_id').fetchall())"
python3 -c "import sqlite3; c=sqlite3.connect('/sandbox/clearframe/state/clearframe.db'); [print(r) for r in c.execute('select at, kind, take_id from events order by event_id desc limit 10')]"
```

**Start fresh during development.** Move the file aside; never delete it.

```bash
mv /sandbox/clearframe/state/clearframe.db /sandbox/clearframe/state/clearframe.db.$(date +%H%M)
```

**The recurring job**

- **OpenClaw schedules jobs with `openclaw cron`:** `add`, `list`, `run <job-id>` and `remove`. Guides show `--every "1m"` for an interval; check the exact flags with `openclaw cron add --help`.
- **Smoke test 4 decides.** If the shortest interval isn't well under a minute, run the 5-second poller in the sandbox instead. The file name is whatever #7 builds.

```bash
nohup python3 /sandbox/clearframe/host/app/agent/poller.py > /sandbox/clearframe/state/poller.log 2>&1 &
```

## Evidence page

The page runs inside the sandbox on port 8090, and an OpenShell forward puts it on the GB10's own loopback address. By design, no other machine can reach it.

**Test the forward before the page exists** (inside the sandbox):

```bash
cd /tmp && python3 -m http.server 8090 --bind 127.0.0.1
```

**Forward it** (on the host):

```bash
openshell forward start --background 8090 clearframe
```

Then open `http://127.0.0.1:8090` in the GB10's browser. A file listing means the forward works, which is half of smoke test 5. Stop the test server with Ctrl-C.

**Run the real page** (inside the sandbox). The file name is whatever #8 builds; restart it after every pull.

```bash
nohup python3 /sandbox/clearframe/host/app/evidence/server.py > /sandbox/clearframe/state/evidence.log 2>&1 &
```

Its pages: `http://127.0.0.1:8090/shot/4` and `http://127.0.0.1:8090/take/4-2`.

## Sources

- [Qwen3.6-35B-A3B model card](https://huggingface.co/Qwen/Qwen3.6-35B-A3B): image and video input, the 2 fps default, thinking mode, vLLM flags
- [Cosmos-Reason2-8B model card](https://huggingface.co/nvidia/Cosmos-Reason2-8B): memory minimum, fps 4, `max_tokens`
- [vLLM: multimodal inputs](https://docs.vllm.ai/en/latest/features/multimodal_inputs.html): images per request, base64 frames as a video
- [NemoClaw: choosing between the NemoClaw and OpenShell commands](https://docs.nvidia.com/nemoclaw/latest/reference/cli-selection-guide.html) and the [quickstart](https://www.mintlify.com/NVIDIA/NemoClaw/get-started/quickstart): `connect`, `openclaw tui`
- [OpenClaw skills](https://docs.openclaw.ai/skills) and [the NemoClaw workspace skill](https://www.skills.sh/nvidia/nemoclaw/nemoclaw-workspace): skill folders and the workspace path
- [OpenClaw cron commands](https://docs.openclaw.ai/tr/cli/cron) and [a cron guide](https://www.stack-junkie.com/blog/openclaw-cron-jobs-automation-guide): `cron add`, `--every`
