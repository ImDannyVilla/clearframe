#!/usr/bin/env python3
"""The 5 s poller: runs the ingest pass in a loop, for when OpenClaw's recurring job
can't run well under a minute (smoke test 4). Standard library only.

    nohup python3 /sandbox/clearframe/host/app/agent/poller.py > /sandbox/clearframe/state/poller.log 2>&1 &

Settings are the ones `agent/ingest.py` reads, plus `POLL_SECONDS` (default 5).
The rubric is read again on every pass, so a `git pull` on the host takes effect
without a restart; code changes still need one.

`poller.py --reset` is the dev rerun from ingest rule 4: it moves the db aside
(never deletes it) so the next pass rebuilds it from the result files.
"""
from __future__ import annotations

import argparse
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


def move_db_aside(db_path: Path) -> Path | None:
    """Ingest rule 4, dev reruns: move `clearframe.db` aside, never delete it.

    Returns the path the db was moved to, or None when there is no db to move.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%S")
    aside = db_path.with_name(f"{db_path.name}.{stamp}.bak")
    counter = 0
    while aside.exists():
        counter += 1
        aside = db_path.with_name(f"{db_path.name}.{stamp}.{counter}.bak")
    db_path.rename(aside)
    return aside


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ingest pass every POLL_SECONDS (default 5 s).")
    parser.add_argument("--reset", action="store_true",
                        help="dev rerun (ingest rule 4): move the db aside and exit; never deletes it")
    args = parser.parse_args(argv)
    results_dir, db_path, rubric_path, base_url = ingest.settings_from_env()
    if args.reset:
        aside = move_db_aside(db_path)
        if aside is None:
            print(f"poller: no db at {db_path}; nothing to move", flush=True)
        else:
            print(f"poller: moved {db_path} aside to {aside}; the next pass rebuilds it from {results_dir}",
                  flush=True)
        return 0
    interval = float(os.environ.get("POLL_SECONDS", "5"))
    if slack.poster_from_env() is slack.unconfigured_poster:
        print("poller: Slack is not configured (SLACK_BOT_TOKEN and SLACK_CHANNEL); cards wait until it is",
              flush=True)
    print(f"poller: {results_dir} into {db_path} every {interval:g} s", flush=True)
    conn = db.connect(db_path)
    last = None
    while True:
        try:
            # The poster is chosen on every pass: cards go out on the first pass
            # after Slack is set, with no poller restart.
            poster = slack.poster_from_env()
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
