#!/usr/bin/env python3
"""Fail if a pull request's description doesn't name its issue. Standard library only.

AGENTS.md: every pull request names its issue, `Closes #N` when it finishes the
issue or `Part of #N` when work remains. `Closes` links the pull request to the
issue on GitHub and closes it on merge; `Part of` leaves the issue open.

Usage: PR_BODY="..." check_pr_issue.py   (CI passes the description this way)
       check_pr_issue.py < body.md
"""
from __future__ import annotations

import os
import re
import sys

ISSUE_LINE = re.compile(r"\b(closes|part of):? #(\d+)\b", re.IGNORECASE)


def issues_named(body: str) -> list[tuple[str, int]]:
    """Every (`closes` or `part of`, issue number) the description names."""
    return [(verb.lower(), int(number)) for verb, number in ISSUE_LINE.findall(body)]


def main() -> int:
    body = os.environ["PR_BODY"] if "PR_BODY" in os.environ else sys.stdin.read()
    named = issues_named(body)
    if not named:
        print(
            "check_pr_issue: the description must name its issue: "
            "`Closes #N` when it finishes the issue, `Part of #N` when work remains",
            file=sys.stderr,
        )
        return 1
    print("check_pr_issue: " + ", ".join(f"{verb} #{number}" for verb, number in named))
    return 0


if __name__ == "__main__":
    sys.exit(main())
