---
name: status-card
description: Ingest new ClearFrame review results into SQLite and post any due Slack status card. Run on ClearFrame's recurring schedule, or when asked to check for new takes.
---

# status-card

Run exactly this command, once, inside the sandbox:

```bash
python3 /sandbox/clearframe/host/app/agent/ingest.py
```

It reads the worker's result files from `/sandbox/clearframe/host/results/`, writes them into `/sandbox/clearframe/state/clearframe.db`, recomputes every take's status, and posts each Slack card that is due. The code builds and posts the card itself, from the rubric's fixed template.

Rules:

- Do not write, rewrite, summarise or add to the card. Do not post anything to Slack yourself.
- Do not read result files, frames, prompts or the database to describe them. The command's one-line output is the whole report.
- If the command fails, report its last line and stop. Do not retry with other commands or edit any file.
- Never delete or move `clearframe.db`.
