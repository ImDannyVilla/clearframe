# agent

Everything inside the NemoClaw sandbox (Gael, #7 and #10): the OpenClaw skills in `skills/` (`status-card`, `prompt-fix`), the recurring job, SQLite, the Slack card, approval and the queue. Standard library only.

## The automation (#7)

`poller.py` is the 5-second poller, the fallback for the OpenClaw recurring job; both run the same pass: ingest result files, recompute statuses, post due Slack cards.

```bash
python3 agent/poller.py --results /sandbox/clearframe/host/results \
                        --db /sandbox/clearframe/state/clearframe.db \
                        [--once] [--reset] [--interval 5] \
                        [--slack-webhook URL] [--base-url URL] [--rubric PATH]
```

Both paths default to the contract's. `--reset` MOVES the database aside with a timestamp (`clearframe.db.<stamp>.bak`) and rebuilds it from the result files — the contract's development rerun; it never deletes. Without `--slack-webhook`, posting is a dry run that prints the card to stdout and nothing leaves the box.

The modules, in dependency order:

- `db.py` — the SQLite file and the contract's five tables (`takes`, `issues`, `prompt_versions`, `approvals`, `events`), the append-only event log, and `reset()`. One operational column sits next to the contract's `takes` columns: `checks` holds the latest ingested revision's check states as small JSON, so the status rule's first match can be recomputed after a decision without re-reading the results mount. No row is ever deleted.
- `status.py` — a take's status from the contract's first-match rule (running/errored check → decision → measured/confirmed → candidate → clean). `status.apply()` stores the result and logs `status_changed` only when it actually changed; the ingest and every decision call it.
- `ingest.py` — the contract's ingest rules 1–4: skip `.tmp` files and revisions not above `takes.result_revision`; a new take ID starts at `review_pending` with `take_seen`; the first final revision writes the take's issues and prompt version 1 (once each), logs `result_ingested` and `check_error` per failed check, then recomputes the status. Retakes arrive as new takes linked by `parent_take_id`.
- `card.py` — the Slack status card. Payloads carry only the contract's card fields (shot, reviewed, take ID, status label, counts by state and category, clean takes, the retake's parent, the link); the renderer fills the rubric's templates, so all wording lives in `rubric/rubric.yaml`. A check in error replaces the status label. Shot cards post once when every expected original take is final; retake cards once when the retake's own result is final; each post logs `card_posted`. Posting goes through an injected transport — dry run by default, a Slack incoming webhook only when configured.
- `poller.py` — the CLI loop above.

#10 (prompt-fix, approval and the queue) builds on `db.py` and `status.py`: the evidence page's four actions record decisions and notes, call `status.apply()`, and the queue writer emits `approved_generation_queue/<take_id>-v<n>.json`.

Tests: `tests/test_agent_*.py` with shared fixtures in `tests/agent_harness.py` run on `contracts/samples/` — no footage, no model, no network.
