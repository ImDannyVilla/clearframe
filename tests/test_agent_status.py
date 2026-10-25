"""The status rule (#7): the contract's first match, recomputed and logged.

Laptop tests: temp SQLite files only, no footage, no model.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import db, status  # noqa: E402

from agent_harness import events_of  # noqa: E402


class StatusFixture(unittest.TestCase):
    """A take row plus issues and approvals, to exercise one rule at a time."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(Path(self.tmp.name) / "clearframe.db")
        self.addCleanup(self._close)

    def _close(self):
        self.conn.close()
        self.tmp.cleanup()

    def given_take(self, checks=None, state="review_pending"):
        self.conn.execute(
            "INSERT INTO takes (take_id, parent_take_id, shot, expected_takes, status,"
            " result_revision, checks, first_seen_at, updated_at)"
            " VALUES ('4-2', '', 4, 6, ?, 2, ?, '2026-10-25T12:00:00-07:00',"
            " '2026-10-25T12:00:00-07:00')",
            (state, json.dumps(checks if checks is not None else {})),
        )

    def given_issue(self, state, issue_type="road_markings"):
        count = self.conn.execute("SELECT COUNT(*) AS n FROM issues").fetchone()["n"]
        self.conn.execute(
            "INSERT INTO issues (issue_id, take_id, issue_type, state)"
            " VALUES (?, '4-2', ?, ?)",
            (f"4-2-{count + 1}", issue_type, state),
        )

    def given_approval(self, decision):
        self.conn.execute(
            "INSERT INTO approvals (take_id, decision, approver, decided_at)"
            " VALUES ('4-2', ?, 'Creative director', '2026-10-25T13:00:00-07:00')",
            (decision,),
        )

    def compute(self):
        return status.compute(self.conn, "4-2")


class FirstMatchRuleTest(StatusFixture):
    def test_running_check_wins_over_everything(self):
        self.given_take(checks={"exposure_flicker": "running", "road_markings": "done"})
        self.given_issue("candidate")
        self.given_approval("accepted_as_is")
        self.assertEqual(self.compute(), "review_pending")

    def test_errored_check_is_review_pending(self):
        self.given_take(checks={"exposure_flicker": "error", "road_markings": "done"})
        self.assertEqual(self.compute(), "review_pending")

    def test_decision_accepted_as_is(self):
        self.given_take()
        self.given_issue("candidate")
        self.given_approval("accepted_as_is")
        self.assertEqual(self.compute(), "accepted_as_is")

    def test_decision_approved_for_regeneration(self):
        self.given_take()
        self.given_issue("confirmed")
        self.given_approval("approved_for_regeneration")
        self.assertEqual(self.compute(), "approved_for_regeneration")

    def test_measured_anomaly_is_flagged(self):
        self.given_take()
        self.given_issue("measured", issue_type="exposure_flicker")
        self.assertEqual(self.compute(), "flagged")

    def test_confirmed_note_is_flagged(self):
        self.given_take()
        self.given_issue("confirmed")
        self.assertEqual(self.compute(), "flagged")

    def test_candidate_note_waits(self):
        self.given_take()
        self.given_issue("candidate")
        self.assertEqual(self.compute(), "needs_confirmation")

    def test_measured_beats_candidate(self):
        self.given_take()
        self.given_issue("candidate")
        self.given_issue("confirmed")
        self.assertEqual(self.compute(), "flagged")

    def test_dismissed_note_does_not_block_clean(self):
        self.given_take()
        self.given_issue("dismissed")
        self.assertEqual(self.compute(), "clean")

    def test_nothing_at_all_is_clean(self):
        self.given_take()
        self.assertEqual(self.compute(), "clean")

    def test_empty_checks_are_not_blocking(self):
        # A take that exists but carries no check states yet: rules 2-5 decide.
        self.given_take(checks={})
        self.assertEqual(self.compute(), "clean")

    def test_unknown_take_raises(self):
        with self.assertRaises(KeyError):
            status.compute(self.conn, "9-9")


class ApplyTest(StatusFixture):
    def test_no_event_when_nothing_changed(self):
        self.given_take(checks={"exposure_flicker": "running"}, state="review_pending")
        old, new = status.apply(self.conn, "4-2")
        self.assertEqual((old, new), ("review_pending", "review_pending"))
        self.assertEqual(events_of(self.conn, kind="status_changed"), [])
        self.conn.commit()

    def test_change_is_stored_and_logged(self):
        self.given_take(checks={"exposure_flicker": "running"}, state="review_pending")
        self.conn.execute(
            "UPDATE takes SET checks = ? WHERE take_id = '4-2'",
            (json.dumps({"exposure_flicker": "done"}),),
        )
        old, new = status.apply(self.conn, "4-2")
        self.assertEqual((old, new), ("review_pending", "clean"))
        row = db.take_row(self.conn, "4-2")
        self.assertEqual(row["status"], "clean")
        changes = events_of(self.conn, kind="status_changed")
        self.assertEqual(len(changes), 1)
        self.assertEqual(json.loads(changes[0]["detail"]), {"old": "review_pending", "new": "clean"})
        self.assertEqual(changes[0]["take_id"], "4-2")
        self.assertEqual(changes[0]["shot"], 4)

    def test_checks_argument_overrides_the_stored_ones(self):
        # The ingest pass passes the revision it just read, before storing it.
        self.given_take(checks={"exposure_flicker": "running"}, state="review_pending")
        old, new = status.apply(
            self.conn, "4-2", checks={"exposure_flicker": "done"}
        )
        self.assertEqual((old, new), ("review_pending", "clean"))


if __name__ == "__main__":
    unittest.main()
