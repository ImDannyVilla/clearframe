---
date: 2026-10-25
issue: "#2"
area: other
severity: minor
---

# Pull requests didn't link their issues

## What happened

PR #14 (rubric, #4) said "Refs #4" and PR #15 (contract) mentioned #4 and #5 in passing. Neither showed up as linked on its issue, so the issues didn't show their pull requests and wouldn't close on merge.

## Root cause

GitHub links a pull request to an issue only through a closing keyword (`Closes #N`) or the Development sidebar. "Refs #N" and bare mentions only add a timeline entry. AGENTS.md said to work from your issue but not to name it in the pull request, and nothing checked.

A closing keyword also closes the issue on merge, which is why an agent may avoid it when work remains.

## Fix

PR #14 now says `Closes #4`, with the leftover work moved to #16 first.

## Prevention

- AGENTS.md's Workflow section: every pull request names its issue, `Closes #N` or `Part of #N`.
- CI runs `scripts/check_pr_issue.py` on every pull request and fails one whose description names no issue, rerunning when the description is edited. `tests/test_check_pr_issue.py` covers it.
