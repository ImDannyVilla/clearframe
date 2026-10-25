#!/usr/bin/env python3
"""The 5 s poller: runs the ingest pass in a loop, for when OpenClaw's recurring job
can't run well under a minute (smoke test 4). Standard library only.

    nohup python3 /sandbox/clearframe/host/app/agent/poller.py > /sandbox/clearframe/state/poller.log 2>&1 &

Settings are the ones `agent/ingest.py` reads, plus `POLL_SECONDS` (default 5).
The rubric is read again on every pass, so a `git pull` on the host takes effect
without a restart; code changes still need one.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import db, ingest, slack  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402


def main() -> int:
    results_dir, db_path, rubric_path, base_url = ingest.settings_from_env()
    interval = float(os.environ.get("POLL_SECONDS", "5"))
    poster = slack.poster_from_env()
    if poster is slack.unconfigured_poster:
        print("poller: Slack is not configured (SLACK_BOT_TOKEN and SLACK_CHANNEL); cards wait until it is",
              flush=True)
    print(f"poller: {results_dir} into {db_path} every {interval:g} s", flush=True)
    conn = db.connect(db_path)
    last = None
    while True:
        try:
            report = ingest.run_pass(conn, results_dir, rubric_loader.load(rubric_path), poster, base_url)
            summary = ingest.describe(report)
            if summary != last:  # don't repeat the same line every 5 s
                print(f"{db.now()} poller: {summary}", flush=True)
                last = summary
        except Exception:  # noqa: BLE001 - one bad pass must not stop the poller
            traceback.print_exc()
        time.sleep(interval)


if __name__ == "__main__":
    sys.exit(main())
