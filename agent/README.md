# agent

Everything inside the NemoClaw sandbox: the OpenClaw skills in `skills/` (`status-card`, `prompt-fix`), the recurring job, SQLite, the Slack card, approval and the queue. Standard library only.

#7 (the automation, SQLite and the status card) is in; #10 (prompt-fix, approval and the queue) builds on `db.py`.

| File | What |
| --- | --- |
| `db.py` | The five tables, `log_event`, and the status rule (`recompute_status`). The ingest, the four actions (#10) and the evidence page (#8) all use it. |
| `ingest.py` | One pass: `results/*.json` into SQLite by the contract's ingest rules 1 to 5, then any due card. Run it from OpenClaw's recurring job. |
| `poller.py` | The same pass every 5 s, for when the recurring job can't run well under a minute. |
| `card.py` | The shot and retake cards, from `rubric.yaml`'s templates. Refuses any field the contract doesn't list. |
| `slack.py` | Delivers a finished card: Slack's `chat.postMessage`, a print for dry runs, or nothing until Slack is set. |
| `skills/status-card/SKILL.md` | The OpenClaw skill: run `ingest.py` once, post nothing itself. |

## Run it (inside the sandbox)

```bash
mkdir -p /sandbox/clearframe/state /sandbox/clearframe/approved_generation_queue
export SLACK_BOT_TOKEN=...   # the bot token, from the password manager; never in git
export SLACK_CHANNEL=...     # the team channel's ID
nohup python3 /sandbox/clearframe/host/app/agent/poller.py > /sandbox/clearframe/state/poller.log 2>&1 &
```

Or one pass, as OpenClaw's recurring job runs it: `python3 /sandbox/clearframe/host/app/agent/ingest.py`.

| Variable | Default | What |
| --- | --- | --- |
| `CLEARFRAME_RESULTS` | `/sandbox/clearframe/host/results` | The worker's result files, mounted read-only |
| `CLEARFRAME_DB` | `/sandbox/clearframe/state/clearframe.db` | SQLite; tables are created on first run |
| `CLEARFRAME_RUBRIC` | `rubric/rubric.yaml` in this repo | Card templates and labels |
| `EVIDENCE_BASE_URL` | `http://127.0.0.1:8090` | The link on every card |
| `SLACK_BOT_TOKEN`, `SLACK_CHANNEL` | unset | Both set: cards go to Slack |
| `CLEARFRAME_SLACK` | unset | `print`: print cards and count them as posted, for a dry run |
| `POLL_SECONDS` | `5` | The poller's interval |

With no Slack settings, nothing is posted and the cards wait: they go out on the first pass after Slack is set.

## For #10 and #8

```python
from agent import db
conn = db.connect()                                  # or db.connect_readonly() for the evidence page
with conn:                                           # one transaction per action
    conn.execute("UPDATE issues SET state = 'confirmed', decided_by = ?, decided_at = ? WHERE issue_id = ?",
                 (approver, db.now(), issue_id))
    db.log_event(conn, approver, "note_confirmed", take_id, shot, {"issue_id": issue_id})
    old, new = db.recompute_status(conn, take_id, actor=approver)   # logs status_changed
```

- **Status:** `compute_status` follows the contract's rule. The tables hold no check states, so "a check is still running or ended in error" is read from the events: no `result_ingested` yet, or a `check_error`.
- **Empty values** ("empty for originals", "empty for notes") are NULL.
- **Issue IDs** number measured anomalies first, then candidate notes, in result-file order: `4-2-1`, `4-2-2`.
- **Event `detail`** stays small JSON with no prompt text and no model notes. `card_posted` carries `{"card": "shot" | "retake", "ts": ...}`.
