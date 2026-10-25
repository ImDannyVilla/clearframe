# ClearFrame

A local agent that runs your review checklist on every AI take before your creative director sees it, shows the evidence, drafts the fix, and keeps code red work on the box.

Built at the Dell x NVIDIA AI Hackathon, San Francisco, October 25, 2026, by Danny, Gael and Jason. Everything runs on one Dell Pro Max GB10; nothing calls a cloud model.

## The loop

1. Takes land in the render folder on the host.
2. The review worker extracts every frame. Code measures exposure flicker on every frame; a local vision model answers one question per check (road markings, hands) on sparse frames and on dense windows around anything code flags.
3. An OpenClaw automation in a NemoClaw sandbox writes the results to SQLite and posts a Slack card: take IDs, statuses, issue categories and a link. No frames, no prompts.
4. The creative director opens the evidence page on the GB10, confirms or dismisses each note, and approves the drafted prompt fix.
5. The approved prompt goes to a queue file for the studio's own generator. ClearFrame never generates video.
6. The returned take is reviewed as a new take, linked to its parent.

## Stack

| Piece | What | Where |
| --- | --- | --- |
| Agent model | Qwen3.6-35B-A3B (NVFP4) | NemoClaw's managed vLLM, reached at `inference.local` |
| Reviewer model | Nemotron 3 Nano Omni 30B-A3B (NVFP4); Cosmos-Reason2-8B as the challenger | vLLM 0.20.0 on the host, on the offline `clearframe-net` network |
| Measurer | ffmpeg and Python's standard library | The review worker container on `clearframe-net` |
| Agent runtime | OpenClaw in an OpenShell sandbox, set up by NemoClaw (Restricted policy plus Slack) | The sandbox |

Setup and run commands for the GB10 are in the [GB10 cheat sheet](docs/gb10-cheatsheet.md).

## Layout

| Folder | What goes there | Owner |
| --- | --- | --- |
| `contracts/` | [The contract](contracts/README.md) and sample files | Everyone; change only by pull request |
| `worker/` | Takes in, frames, flicker, result files | Jason |
| `vision/` | Reviewer questions, votes, clip vs grid, the accuracy test | Danny |
| `evidence/` | The evidence page | Jason |
| `agent/` | OpenClaw skills, the recurring job, SQLite, the Slack card, approval and the queue | Gael |
| `rubric/` | Checks, flicker threshold, fix library, approvers, card wording | Danny |
| `scripts/` | Smoke tests and the end-to-end script | Danny |
| `tests/` | Laptop tests; they make their own clips with ffmpeg | Everyone |
| `docs/` | The [GB10 cheat sheet](docs/gb10-cheatsheet.md) | Everyone |

## Rules

Agents and people follow [`AGENTS.md`](AGENTS.md). The checks enforce what they can:

- `make verify` must pass. CI runs it on every push and pull request.
- Run `scripts/install-hooks.sh` once per clone: the pre-commit hook scans for secrets and runs `make check`, and the pre-push hook runs `make verify`.
- Never commit video, frames, `.env`, tokens or the database. `.gitignore` blocks them, and `make check` fails if one gets in anyway.
- A change to the contract goes in a pull request that edits `contracts/README.md`, and a teammate looks before it merges.
- Small pull requests, merged often, with a normal merge so the history shows the day.
- Every non-obvious mistake becomes a lesson in `.agents/lessons/`, and a check when it can be one.
