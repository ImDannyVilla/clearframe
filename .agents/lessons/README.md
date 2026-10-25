# Lessons

Mistakes, surprises and their fixes, kept so nobody (person or agent) repeats them.

## When to write one

- A task failed for a reason that wasn't obvious.
- An agent confidently did the wrong thing and a person caught it.
- A check let a bug through. These are the most valuable.
- Something on the GB10, NemoClaw, OpenShell, OpenClaw or vLLM needed a workaround.

## File name

`<YYYY-MM-DD>_<short-slug>.md`

## Template

```markdown
---
date: <YYYY-MM-DD>
issue: <#N or PR link, if any>
area: worker | vision | agent | evidence | contract | gb10 | other
severity: critical | important | minor
---

# <Short title>

## What happened
## Root cause
## Fix
## Prevention
<!-- Which check, test or AGENTS.md rule now stops it from happening again.
     A lesson without a Prevention change is unfinished. -->
```

## Use

- Before starting a task, search this folder for the area you're touching.
- When a theme repeats, turn it into a check in `scripts/validate_repo.py` or a test, and say so in the lesson.
