"""The agent's database (#7): the contract's five tables, events, and reset.

Laptop tests: temp files only, no footage, no model.
"""
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import db  # noqa: E402

TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")

CONTRACT_COLUMNS = {
    "takes": [
        "take_id", "parent_take_id", "shot", "expected_takes", "status",
        "result_revision", "render_sha256", "fps", "frames",
        "brightness_series", "first_seen_at", "updated_at",
    ],
    "issues": [
        "issue_id", "take_id", "issue_type", "state", "frame_start", "frame_end",
        "t_start", "t_end", "value", "threshold", "model_note", "evidence",
        "decided_by", "decided_at",
    ],
    "prompt_versions": [
        "take_id", "version", "prompt", "prompt_hash", "fixes", "created_at",
    ],
    "approvals": [
        "approval_id", "take_id", "decision", "prompt_version", "prompt_hash",
        "approver", "decided_at", "queue_file", "next_take_id",
    ],
    "events": [
        "event_id", "at", "actor", "kind", "take_id", "shot", "detail",
    ],
}


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state" / "clearframe.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self._close)

    def _close(self):
        self.conn.close()
        self.tmp.cleanup()

    def tables(self):
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        return [row["name"] for row in rows]

    def test_the_five_contract_tables(self):
        self.assertEqual(
            self.tables(),
            ["approvals", "events", "issues", "prompt_versions", "takes"],
        )

    def test_columns_match_the_contract(self):
        for table, columns in CONTRACT_COLUMNS.items():
            with self.subTest(table=table):
                found = [row["name"] for row in self.conn.execute(
                    f"PRAGMA table_info({table})"  # noqa: S608 - test-only, fixed names
                )]
                for column in columns:
                    self.assertIn(column, found)

    def test_connect_creates_parent_dirs(self):
        self.assertTrue(self.db_path.is_file())


class EventLogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "clearframe.db")
        self.addCleanup(self._close)

    def _close(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_events_append_in_order_with_a_counter(self):
        first = db.log_event(self.conn, "take_seen", take_id="4-2", shot=4,
                             detail={"revision": 1})
        second = db.log_event(self.conn, "result_ingested", take_id="4-2", shot=4,
                              detail={"revision": 2})
        self.assertLess(first, second)
        rows = db.events(self.conn)
        self.assertEqual([row["kind"] for row in rows], ["take_seen", "result_ingested"])
        self.assertEqual(rows[0]["take_id"], "4-2")
        self.assertEqual(rows[0]["shot"], 4)
        self.assertEqual(rows[0]["detail"], '{"revision": 1}')

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            db.log_event(self.conn, "bogus_kind")

    def test_the_contracts_event_kinds_are_all_accepted(self):
        for kind in db.EVENT_KINDS:
            with self.subTest(kind=kind):
                db.log_event(self.conn, kind)
        self.assertEqual(len(db.events(self.conn)), len(db.EVENT_KINDS))

    def test_now_iso_matches_the_contract_shape(self):
        self.assertRegex(db.now_iso(), TIME_RE)


class ResetTest(unittest.TestCase):
    def test_reset_moves_the_db_aside_with_a_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clearframe.db"
            conn = db.connect(path)
            conn.close()
            self.assertTrue(path.exists())

            aside = db.reset(path)
            self.assertIsNotNone(aside)
            self.assertFalse(path.exists())
            self.assertTrue(aside.is_file())
            self.assertRegex(aside.name, r"^clearframe\.db\.\d{8}T\d{6}\.bak$")
            self.assertEqual(aside.parent, path.parent)

            # The move is a rename: the data survives next to the live path.
            check = db.connect(aside)
            self.assertEqual(check.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'takes'"
            ).fetchone()["name"], "takes")
            check.close()

    def test_reset_twice_keeps_both_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "clearframe.db"
            db.connect(path).close()
            first = db.reset(path)
            db.connect(path).close()
            second = db.reset(path)
            self.assertNotEqual(first, second)
            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            self.assertFalse(path.exists())

    def test_reset_of_a_missing_db_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(db.reset(Path(tmp) / "clearframe.db"))


if __name__ == "__main__":
    unittest.main()
