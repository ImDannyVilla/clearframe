---
date: 2026-10-25
issue: #1
area: gb10
severity: critical
---

# Re-onboarding with --recreate-sandbox fails once a host mount is added

## What happened
After a successful first onboard (no host mount), re-onboarding with
`nemoclaw onboard --fresh --name clearframe --recreate-sandbox --host-mount ~/clearframe:/sandbox/clearframe/host`
created the sandbox (CUDA proofs passed) but then failed:
"Native home/workspace restore failed: native restore could not remove replacement-only state at: /sandbox/clearframe".
The sandbox was left unregistered and recovery stayed blocked until it was destroyed.

## Root cause
The pre-recreate backup restore tries to remove replacement-only state under `/sandbox/clearframe`,
which is where the new read-only host mount lives. The restore cannot remove state there,
so registry publication fails after the sandbox itself was created successfully.

## Fix
`nemoclaw clearframe destroy --yes --keep-vllm`, then a fresh create without `--recreate-sandbox`:
`NEMOCLAW_PROVIDER=install-vllm nemoclaw onboard --fresh --name clearframe --host-mount ~/clearframe:/sandbox/clearframe/host`.
A fresh create has no backup to restore and completed in minutes. `--keep-vllm` avoids a model reload.
Slack was added afterwards with `nemoclaw clearframe channels add slack` (interactive, xoxb + xapp tokens), rebuild succeeded.

## Prevention
Declare every host mount on the FIRST onboard. If you must add one later,
destroy and create fresh instead of `--recreate-sandbox`; only recreate when the sandbox holds state you need.
Record the exact working command on issue #1 (done). No repo check applies: this is host setup, not repo code.
