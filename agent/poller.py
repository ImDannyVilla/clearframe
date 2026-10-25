#!/usr/bin/env python3
"""The recurring automation: poll results/, update SQLite, post due Slack cards.

This is #7's 5-second poller, the fallback for the OpenClaw recurring job; both
run the same pass (ingest -> status -> cards). Everything is configurable:

    python3 agent/poller.py --results <dir> --db <path> [--once] [--reset]
                            [--interval 5] [--slack-webhook URL]
                            [--base-url URL] [--rubric PATH]

Defaults match the contract: results in /sandbox/clearframe/host/results/ and
the database at /sandbox/clearframe/state/clearframe.db. --reset MOVES the
database aside with a timestamp, never deletes it (the contract's development
rerun). Without --slack-webhook, posting is a dry run that prints the card to
stdout and nothing leaves the box.
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import card, db, ingest  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402


def log(message: str) -> None:
    print(f"{db.now_iso()} {message}", flush=True)


def run_pass(
    conn,
    results_dir: str,
    transport,
    rubric: dict,
    base_url: str,
) -> tuple[list[str], list[tuple[str, str | int]]]:
    """One pass of the automation: ingest new revisions, then post due cards."""
    ingested = ingest.ingest_dir(conn, results_dir)
    posted = card.post_due_cards(conn, transport=transport, rubric=rubric, base_url=base_url)
    return ingested, posted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", default=db.DEFAULT_RESULTS_DIR,
                        help="the results folder the worker writes to")
    parser.add_argument("--db", default=db.DEFAULT_DB_PATH,
                        help="the SQLite file the sandbox app owns")
    parser.add_argument("--once", action="store_true",
                        help="run one pass and exit")
    parser.add_argument("--reset", action="store_true",
                        help="move the database aside (timestamped .bak) and rebuild")
    parser.add_argument("--interval", type=float, default=5.0,
                        help="seconds between passes (default 5)")
    parser.add_argument("--slack-webhook", default="",
                        help="Slack incoming-webhook URL; without it, cards print to stdout")
    parser.add_argument("--base-url", default=card.DEFAULT_BASE_URL,
                        help="the evidence page's base URL for card links")
    parser.add_argument("--rubric", default=str(card.RUBRIC_PATH),
                        help="rubric.yaml with the card templates and labels")
    args = parser.parse_args(argv)

    if args.reset:
        aside = db.reset(args.db)
        if aside is not None:
            log(f"automation: moved {args.db} aside to {aside}; rebuilding from results")
        else:
            log(f"automation: no database at {args.db}; starting fresh")

    transport = (
        card.webhook_transport(args.slack_webhook)
        if args.slack_webhook
        else card.dry_run_transport
    )
    rubric = rubric_loader.load(args.rubric)

    log(f"automation: watching {args.results} every {args.interval:g} s,"
        f" cards via {'slack webhook' if args.slack_webhook else 'dry run'}")
    while True:
        try:
            conn = db.connect(args.db)
            try:
                ingested, posted = run_pass(conn, args.results, transport, rubric, args.base_url)
            finally:
                conn.close()
            if ingested:
                log(f"automation: ingested new revision(s): {', '.join(ingested)}")
            for kind, what in posted:
                log(f"automation: posted {kind} card for {what}")
        except Exception:
            traceback.print_exc()
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
