"""The 5-second poller (#7): one pass ingests and posts due cards, --reset moves
the database aside and rebuilds, and the dry run never touches the network.

Laptop tests: temp files only, stdout captured, no footage, no model.
"""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import db, poller  # noqa: E402

from agent_harness import write_final_take  # noqa: E402


class PollerFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.results = self.base / "results"
        self.results.mkdir()
        self.db_path = self.base / "state" / "clearframe.db"
        self.addCleanup(self.tmp.cleanup)

    def run_poller(self, *extra) -> str:
        buffer = io.StringIO()
        argv = ["--results", str(self.results), "--db", str(self.db_path),
                "--once", *extra]
        with contextlib.redirect_stdout(buffer):
            code = poller.main(argv)
        self.assertEqual(code, 0)
        return buffer.getvalue()


class OnceTest(PollerFixture):
    def test_one_pass_ingests_results_into_the_configured_db(self):
        write_final_take(self.results, "4-2")
        out = self.run_poller()
        self.assertIn("watching", out)

        conn = db.connect(self.db_path)
        try:
            take = db.take_row(conn, "4-2")
            self.assertIsNotNone(take)
            self.assertEqual(take["status"], "clean")
        finally:
            conn.close()

    def test_a_completed_shot_posts_its_card_to_stdout(self):
        for take_id in ("4-1", "4-2", "4-3", "4-4", "4-5", "4-6"):
            write_final_take(self.results, take_id)
        out = self.run_poller()
        self.assertIn("ClearFrame · Shot 4 · 6 of 6 takes reviewed", out)
        self.assertIn("posted shot card for 4", out)

        conn = db.connect(self.db_path)
        try:
            posted = db.events(conn, kind="card_posted")
            self.assertEqual(len(posted), 1)
            self.assertEqual(posted[0]["shot"], 4)
        finally:
            conn.close()

    def test_the_poller_defaults_match_the_contract(self):
        self.assertEqual(db.DEFAULT_RESULTS_DIR, "/sandbox/clearframe/host/results")
        self.assertEqual(db.DEFAULT_DB_PATH, "/sandbox/clearframe/state/clearframe.db")


class ResetTest(PollerFixture):
    def test_reset_moves_the_db_aside_and_rebuilds_from_results(self):
        write_final_take(self.results, "4-2")
        self.run_poller()
        self.assertTrue(self.db_path.exists())

        out = self.run_poller("--reset")
        self.assertIn("moved", out)
        self.assertFalse(any("Traceback" in line for line in out.splitlines()))

        backups = sorted(self.db_path.parent.glob("clearframe.db.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].is_file())

        # The fresh database rebuilt the same state from the result files.
        conn = db.connect(self.db_path)
        try:
            take = db.take_row(conn, "4-2")
            self.assertIsNotNone(take)
            seen = db.events(conn, kind="take_seen", take_id="4-2")
            self.assertEqual(len(seen), 1)
        finally:
            conn.close()

    def test_reset_without_a_db_is_a_fresh_start(self):
        out = self.run_poller("--reset")
        self.assertIn("starting fresh", out)
        self.assertTrue(self.db_path.exists())


if __name__ == "__main__":
    unittest.main()
