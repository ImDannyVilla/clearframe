#!/usr/bin/env python3
"""Fail if a tracked or staged file contains a credential. Standard library only.

Usage: scan_secrets.py --all     scan every tracked file in the working tree
       scan_secrets.py --staged  scan what is about to be committed
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Each pattern is assembled from pieces so this file never matches itself.
PATTERNS = (
    ("AWS access key", re.compile("A" + "KIA" + r"[0-9A-Z]{16}")),
    ("GitHub token", re.compile("g" + "h" + r"[pousr]_[A-Za-z0-9]{30,}")),
    ("GitHub fine-grained token", re.compile("github" + "_pat_" + r"[A-Za-z0-9_]{30,}")),
    ("Slack token", re.compile("xo" + "x" + r"[abprs]-[A-Za-z0-9-]{10,}")),
    ("Slack app token", re.compile("xa" + "pp-" + r"[0-9]-[A-Za-z0-9-]{10,}")),
    ("NVIDIA NGC key", re.compile("nv" + "api-" + r"[A-Za-z0-9_-]{20,}")),
    ("Hugging Face token", re.compile("h" + "f_" + r"[A-Za-z0-9]{30,}")),
    ("private key", re.compile("-----BEGIN " + r"(?:[A-Z]+ )*PRIVATE KEY-----")),
)


def git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True).stdout


def staged_files() -> list[tuple[str, bytes]]:
    names = git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").decode().split("\0")
    return [(name, git("show", f":{name}")) for name in names if name]


def tracked_files() -> list[tuple[str, bytes]]:
    files = []
    for name in git("ls-files", "-z").decode().split("\0"):
        path = ROOT / name
        if name and path.is_file():
            files.append((name, path.read_bytes()))
    return files


def scan(files: list[tuple[str, bytes]]) -> list[str]:
    findings = []
    for name, content in files:
        if b"\0" in content:
            continue  # binary file
        text = content.decode("utf-8", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{name}:{number}: looks like a {label}")
    return findings


def main(argv: list[str]) -> int:
    if argv == ["--all"]:
        files = tracked_files()
    elif argv == ["--staged"]:
        files = staged_files()
    else:
        print(__doc__, file=sys.stderr)
        return 2
    findings = scan(files)
    for finding in findings:
        print(f"scan_secrets: {finding}", file=sys.stderr)
    if findings:
        print("scan_secrets: remove the credential and rotate it. Tokens never go in git.", file=sys.stderr)
        return 1
    print(f"scan_secrets: {len(files)} files clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
