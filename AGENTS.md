# Agent and contributor instructions

## Project

ClearFrame is a local agent that reviews AI video takes on one Dell Pro Max GB10 before a creative director sees them. It was built on the day at the Dell x NVIDIA AI Hackathon, October 25, 2026. [`contracts/README.md`](contracts/README.md) is the source of truth for names, file shapes and rules; the GB10 commands are in [`docs/gb10-cheatsheet.md`](docs/gb10-cheatsheet.md).

## Before you change anything

1. Read `contracts/README.md` and the `README.md` of every folder you will edit.
2. Work from your GitHub issue and stay inside its folder:

   | Folder | Owner | Issues |
   | --- | --- | --- |
   | `worker/` | Jason | #5 |
   | `vision/` | Danny | #6 |
   | `evidence/` | Jason | #8 |
   | `agent/` | Gael | #7, #10 |
   | `rubric/` | Danny | #4, #16 |
   | `scripts/` | Danny | #3, #9 |
   | `contracts/` | Everyone, by pull request only | #2 |

3. Search `.agents/lessons/` for pitfalls in the area you are touching.

## Hard rules

The gates enforce what they can; the rest is on you.

- **Nothing leaves the box.** No code calls a cloud model or a cloud API. The sandbox reaches only Slack and the local model; the worker sits on an internal network with no internet route and reaches the model over it.
- **No footage, frames, database, `.env`, tokens or keys in git.** `make check` scans for credentials and for banned file types.
- **Models only raise candidate notes.** Code measures, sets every timestamp from frame positions, applies fixes from the fix library, and builds the Slack card from its template. No model text reaches Slack.
- **ClearFrame never generates video** and never queues a prompt without a person's approval on the evidence page.
- **Standard library only** in `worker/`, `agent/` and `evidence/`: the GB10's offline paths have no PyPI.
- **The contract changes only by pull request**, in the same pull request as its samples and tests, with a teammate's look before it merges.
- **Never weaken or skip a gate to make your change pass.** If a gate is wrong, fix it in its own commit and say why.
- **Morgan's clips are test assets** made for this demo. Never suggest a real brand is involved.

## Definition of done

- `make verify` passes. CI runs it on every push and pull request.
- A behavior change comes with a test in `tests/`. Laptop tests make their own clips with ffmpeg and read `contracts/samples/`; they need no footage and no model.
- The diff touches only your folder, unless your issue says otherwise.

## Workflow

- Run `scripts/install-hooks.sh` once per clone, on laptops and on the GB10. Pre-commit runs the staged secret scan and `make check`; pre-push runs `make verify`.
- One branch per issue (`<name>/<issue>-<slug>`), small pull requests, and a normal merge, not squash, so the history shows the day. You may merge your own pull request once `make verify` passes, except contract changes.
- Every pull request names its issue in its description: `Closes #N` when it finishes the issue, so GitHub links them and closes the issue on merge, or `Part of #N` when work remains. "Refs #N" or a bare mention doesn't link. CI fails a pull request without one (`scripts/check_pr_issue.py`). If work is left when an issue's pull request merges, move it to a new issue first.
- When you or an agent get something wrong in a way that wasn't obvious, write a lesson in `.agents/lessons/`. If it can be checked, add the check to `scripts/validate_repo.py` or a test, and note that in the lesson.

## Git

- Do not add co-author lines (such as `Co-Authored-By`) to commit messages or pull request bodies.
