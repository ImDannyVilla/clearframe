# evidence

The evidence page (Jason, #8). It runs in the sandbox on port 8090, forwarded to `127.0.0.1:8090` on the GB10, reads SQLite and `results/`, and makes every write through the four actions in the contract.

| File | What |
| --- | --- |
| `server.py` | The page: `/shot/<shot>`, `/take/<take_id>`, evidence frames from `results/`, and the action buttons |
| `actions.py` | The bridge to the four actions (#10), with exactly the contract's fields |

## Run it (inside the sandbox)

```bash
nohup python3 /sandbox/clearframe/host/app/evidence/server.py > /sandbox/clearframe/state/evidence.log 2>&1 &
```

Then on the host: `openshell forward start --background 8090 clearframe`, and open `http://127.0.0.1:8090/shot/4` in the GB10's browser. Restart the page after a `git pull`.

| Variable | Default | What |
| --- | --- | --- |
| `CLEARFRAME_DB` | `/sandbox/clearframe/state/clearframe.db` | Opened read-only |
| `CLEARFRAME_RESULTS` | `/sandbox/clearframe/host/results` | Evidence frames and brightness series |
| `CLEARFRAME_RUBRIC` | `rubric/rubric.yaml` | `approvers` and the flicker threshold |
| `EVIDENCE_PORT` | `8090` | The contract's port |

## What `/take/<take_id>` shows

- The status, and which checks ended in error.
- Measured anomalies: frames, seconds, value against the threshold, evidence frames, and a two-panel plot (brightness per frame, and change per frame with the threshold line). The plot has a text summary for screen readers.
- Candidate notes: frames, seconds, the model's words (escaped, labelled as a candidate), evidence frames, and Confirm / Dismiss with an approver from the rubric's `approvers`.
- The prompt: version 1, or the diff from version 1 to the latest, with the latest hash.
- Approve (only when the take is `flagged`, no note waits and a version above 1 exists, carrying that version and hash) and Accept as is (from `flagged` or `needs_confirmation`). Recorded decisions are shown; they're final.
- The take's events in time order: the audit trail.

## The four actions (#10)

The page calls `agent.actions.confirm_note(issue_id=, approver=)`, `dismiss_note(issue_id=, approver=)`, `accept_as_is(take_id=, approver=)` and `approve_regeneration(take_id=, prompt_version=, prompt_hash=, approver=)`, with keyword arguments only. An action refuses by raising an exception; its message is shown on the page as the reason. Until `agent/actions.py` exists, every button shows "approval actions aren't installed yet (#10)" and the page is read-only.

## Safety

- It never writes SQLite (opened with `mode=ro`) or `results/`. A test checks both are byte-for-byte unchanged after every page and an action.
- It listens on the loopback address only and refuses to start on any other.
- GETs and POSTs need a loopback `Host` header, and POSTs carry a form token made fresh at each start, so another site open in the GB10's browser can't press the buttons. After a restart, reload the page before clicking.
- `/results/` serves only `.jpg` files inside `results/`; result files, brightness series and paths outside it are 404.
