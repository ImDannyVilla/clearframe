# GB10 cheat sheet

Commands for setting up and running ClearFrame on the GB10. This sheet assumes you named the sandbox `clearframe` at onboarding. Command names differ between NemoClaw versions, so check `nemoclaw onboard --help` on the box first.

## Host network: Omni and the worker

Omni and the review worker run on the host, outside OpenShell, so an internal Docker network is the only thing keeping them offline. Run these on the GB10 host.

**Load what's on the stick.** It mounts at `/media/$USER/SanDisk`; check with `ls /media/$USER`.

```bash
docker load -i /media/$USER/SanDisk/vllm-0.20.0.tar
docker load -i /media/$USER/SanDisk/python-3.12-slim.tar
docker load -i /media/$USER/SanDisk/nemoclaw-vllm.tar
mkdir -p ~/.cache/huggingface && tar -xf /media/$USER/SanDisk/hf-models.tar -C ~/.cache/huggingface
tar -xJf /media/$USER/SanDisk/ffmpeg/ffmpeg-release-arm64-static.tar.xz -C ~
```

**Create the network.** `--internal` means no route to the internet.

```bash
docker network create --internal clearframe-net
```

**Start Omni.** Flags come from the model card's DGX Spark example, cut to our memory budget, with no audio and no `pip install`, since the network is offline.

```bash
docker run -d --name omni --network clearframe-net --gpus all --ipc=host --shm-size=16g \
  -e HF_HUB_OFFLINE=1 \
  -v ~/.cache/huggingface:/root/.cache/huggingface \
  -v ~/clearframe/results:/results:ro \
  --entrypoint vllm vllm/vllm-openai:v0.20.0 \
  serve nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4 \
  --served-model-name omni --port 8000 --trust-remote-code \
  --gpu-memory-utilization 0.35 --max-model-len 32768 --max-num-seqs 8 \
  --limit-mm-per-prompt '{"video": 1, "image": 8}' \
  --media-io-kwargs '{"video": {"fps": 4, "num_frames": 256}}' \
  --allowed-local-media-path /results \
  --reasoning-parser nemotron_v3
docker logs -f omni   # wait for "Application startup complete"
```

- **fps 4** gives Omni 20 frames of a 5-second take; the card uses 2. It's set per server, so to show every frame of a flagged moment, send frames as images or one grid.
- **Reasoning** is on by default. Turn it off per request with `"chat_template_kwargs": {"enable_thinking": false}`.
- **`--video-pruning-rate 0.5`** speeds things up by dropping repeated video tokens. Leave it off while you test accuracy.
- **If vLLM 0.20.0 won't start,** swap the image for `nvcr.io/nvidia/vllm:26.05.post1-py3`, which has vLLM 0.21.0.
- **Frames:** Omni sees results at `/results`, so the worker passes paths like `file:///results/frames/4-2/000048.jpg`.

**Start the worker** once #5's code exists. The script path is whatever Jason names it.

```bash
docker run -d --name worker --network clearframe-net \
  -e OMNI_URL=http://omni:8000/v1 \
  -e PATH=/opt/ffmpeg:/usr/local/bin:/usr/bin:/bin \
  -v ~/clearframe/app:/app:ro \
  -v ~/clearframe/results:/results \
  -v ~/clearframe-input:/input:ro \
  -v ~/ffmpeg-7.0.2-arm64-static:/opt/ffmpeg:ro \
  python:3.12-slim python /app/worker/main.py
```

The network has no PyPI, so the worker uses Python's standard library only, or wheels you bring in. After a `git pull`, run `docker restart worker`.

**Check it (smoke test 6).** The first command prints Omni's model list. The second must fail; if it succeeds, the network isn't internal.

```bash
docker run --rm --network clearframe-net python:3.12-slim python -c "import urllib.request as u; print(u.urlopen('http://omni:8000/v1/models').read()[:120])"
docker run --rm --network clearframe-net python:3.12-slim python -c "import urllib.request as u; u.urlopen('https://example.com', timeout=5)"
```

### Cosmos instead of Omni (the accuracy test)

Cosmos-Reason2-8B isn't on the stick, so download it on the GB10 before the wifi fills. It needs about 32 GB, and Qwen plus Omni already reserve about 96 of 128 GB, so it can't run next to Omni. It swaps in.

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

**Swap it in.** Stop Omni, then start Cosmos on the same network with the same image:

```bash
docker stop omni
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

- **Memory:** 0.30 is about 38 GB, above its 32 GB minimum. With Omni stopped, Qwen plus Cosmos is about 0.70.
- **fps 4** matches its model card's training setup.
- **Requests:** set `max_tokens` to 4096 so its reasoning isn't cut off, and read the answer that comes after the reasoning.
- **Pointing the worker at it:** use `http://cosmos:8000/v1` with model `cosmos`, run the same takes, and count catches and false notes.
- **Afterwards:** keep the winner and remove the other with `docker rm -f omni` or `docker rm -f cosmos`.

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

- [Nemotron 3 Nano Omni model card](https://huggingface.co/nvidia/Nemotron-3-Nano-Omni-30B-A3B-Reasoning-NVFP4): the DGX Spark `docker run` example and memory tips
- [Cosmos-Reason2-8B model card](https://huggingface.co/nvidia/Cosmos-Reason2-8B): memory minimum, fps 4, `max_tokens`
- [NemoClaw: choosing between the NemoClaw and OpenShell commands](https://docs.nvidia.com/nemoclaw/latest/reference/cli-selection-guide.html) and the [quickstart](https://www.mintlify.com/NVIDIA/NemoClaw/get-started/quickstart): `connect`, `openclaw tui`
- [OpenClaw skills](https://docs.openclaw.ai/skills) and [the NemoClaw workspace skill](https://www.skills.sh/nvidia/nemoclaw/nemoclaw-workspace): skill folders and the workspace path
- [OpenClaw cron commands](https://docs.openclaw.ai/tr/cli/cron) and [a cron guide](https://www.stack-junkie.com/blog/openclaw-cron-jobs-automation-guide): `cron add`, `--every`
