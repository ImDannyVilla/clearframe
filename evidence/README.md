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
| `CLEARFRAME_QUEUE` | `/sandbox/clearframe/approved_generation_queue` | Where #10 writes approved queue files |
| `EVIDENCE_PORT` | `8090` | The contract's port |

## What `/take/<take_id>` shows

- The status, and which checks ended in error.
- Measured anomalies: frames, seconds, value against the threshold, evidence frames, and a two-panel plot (brightness per frame, and change per frame with the threshold line). The plot has a text summary for screen readers.
- Candidate notes: frames, seconds, the model's words (escaped, labelled as a candidate), evidence frames, and Confirm / Dismiss with an approver from the rubric's `approvers`.
- The prompt: version 1, or the diff from version 1 to the latest, with the latest hash.
- Approve (only when the take is `flagged`, no note waits and a version above 1 exists, carrying that version and hash) and Accept as is (from `flagged` or `needs_confirmation`). Recorded decisions are shown; they're final.
- The take's events in time order: the audit trail.

## The four actions (#10)

`actions.py` takes exactly the contract's fields for each button and calls Gael's `agent/approval.py`: `confirm_note`, `dismiss_note`, `accept_as_is` and `approve_regeneration`. Each call opens its own `approval.Store` on the database and passes the rubric, so confirming a note runs prompt-fix and approve checks `approvers`. It also passes the queue folder (`CLEARFRAME_QUEUE`, default `/sandbox/clearframe/approved_generation_queue`). An `ApprovalRefused` comes back to the page as `Refused: <reason>`; #10 has already logged it as `approval_refused`. Any other error shows a fixed message, and the traceback goes to the page's log.

**Approve needs the `generation` table.** #10 reads the queue file's tool, model and seed from its own `generation` table, which the #7 ingest doesn't fill yet. Until it does, Approve is refused with "take … has no generation info". See the PR for the fix.

## Safety

- It never writes SQLite (opened with `mode=ro`) or `results/`. A test checks both are byte-for-byte unchanged after every page and an action.
- It listens on the loopback address only and refuses to start on any other.
- GETs and POSTs need a loopback `Host` header, and POSTs carry a form token made fresh at each start, so another site open in the GB10's browser can't press the buttons. After a restart, reload the page before clicking.
- `/results/` serves only `.jpg` files inside `results/`; result files, brightness series and paths outside it are 404.
