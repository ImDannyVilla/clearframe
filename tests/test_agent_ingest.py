"""Ingest rules 1-4 (#7): .tmp and old revisions skipped, takes, issues, prompt
version 1 written once per take, result_ingested and check_error logged, status
recomputed after every ingest.

Laptop tests: result files built from contracts/samples/, no footage, no model.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import db, ingest  # noqa: E402

from agent_harness import (  # noqa: E402
    CANDIDATE_ROAD,
    MEASURED_FLICKER,
    PROMPT_V1_HASH,
    RUNNING_CHECKS,
    events_of,
    make_result,
    write_final_take,
    write_pickup,
    write_result,
)


class IngestFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.results = self.base / "results"
        self.results.mkdir()
        self.conn = db.connect(self.base / "state" / "clearframe.db")
        self.addCleanup(self._close)

    def _close(self):
        self.conn.close()
        self.tmp.cleanup()

    def take(self, take_id="4-2"):
        return db.take_row(self.conn, take_id)

    def ingest(self):
        return ingest.ingest_dir(self.conn, self.results)


class PickupTest(IngestFixture):
    def test_new_take_starts_at_review_pending_with_take_seen(self):
        write_pickup(self.results, "4-2")
        self.assertEqual(self.ingest(), ["4-2"])

        take = self.take()
        self.assertEqual(take["status"], "review_pending")
        self.assertEqual(take["result_revision"], 1)
        self.assertEqual(json.loads(take["checks"]), RUNNING_CHECKS)
        self.assertEqual(take["parent_take_id"], "")
        self.assertEqual(take["shot"], 4)
        self.assertEqual(take["expected_takes"], 6)
        self.assertEqual(take["brightness_series"], "frames/4-2/brightness.json")

        seen = events_of(self.conn, kind="take_seen", take_id="4-2")
        self.assertEqual(len(seen), 1)
        self.assertEqual(json.loads(seen[0]["detail"]), {"revision": 1})
        self.assertEqual(seen[0]["actor"], "automation")
        self.assertEqual(events_of(self.conn, kind="result_ingested"), [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) AS n FROM issues").fetchone()["n"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM prompt_versions").fetchone()["n"], 0
        )

    def test_pickup_then_final_revision_is_one_take(self):
        write_pickup(self.results, "4-2")
        self.ingest()
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        self.assertEqual(self.ingest(), ["4-2"])

        take = self.take()
        self.assertEqual(take["result_revision"], 2)
        self.assertEqual(take["status"], "needs_confirmation")
        # take_seen fired once, for the new take ID.
        self.assertEqual(len(events_of(self.conn, kind="take_seen", take_id="4-2")), 1)


class SkipRulesTest(IngestFixture):
    def test_tmp_files_are_never_read(self):
        path = self.results / "4-2.json.tmp"
        path.write_text(json.dumps(make_result("4-2")), encoding="utf-8")
        self.assertEqual(self.ingest(), [])
        self.assertIsNone(self.take())

    def test_old_revisions_are_skipped(self):
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        self.ingest()
        first_seen = len(events_of(self.conn))
        # The worker rewrote history: revision 1 is not above result_revision 2.
        write_pickup(self.results, "4-2")
        self.assertEqual(self.ingest(), [])
        take = self.take()
        self.assertEqual(take["result_revision"], 2)
        self.assertEqual(len(events_of(self.conn)), first_seen)

    def test_same_revision_again_is_skipped(self):
        write_final_take(self.results, "4-2")
        self.ingest()
        count = len(events_of(self.conn))
        write_final_take(self.results, "4-2")
        self.assertEqual(self.ingest(), [])
        self.assertEqual(len(events_of(self.conn)), count)

    def test_a_file_that_is_not_a_result_is_skipped(self):
        (self.results / "notes.json").write_text('{"schema": "something.else/1"}',
                                                encoding="utf-8")
        (self.results / "broken.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(self.ingest(), [])


class FinalRevisionTest(IngestFixture):
    def test_sample_result_writes_issue_and_prompt_version_1(self):
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        self.ingest()

        issue = self.conn.execute("SELECT * FROM issues WHERE take_id = '4-2'").fetchone()
        self.assertEqual(issue["issue_id"], "4-2-1")
        self.assertEqual(issue["issue_type"], "road_markings")
        self.assertEqual(issue["state"], "candidate")
        self.assertEqual(issue["frame_start"], 48)
        self.assertEqual(issue["frame_end"], 64)
        self.assertEqual(issue["t_start"], 2.0)
        self.assertEqual(issue["t_end"], 2.667)
        self.assertIsNone(issue["value"])
        self.assertIsNone(issue["threshold"])
        self.assertEqual(issue["model_note"], CANDIDATE_ROAD["model_note"])
        self.assertEqual(json.loads(issue["evidence"]), CANDIDATE_ROAD["evidence"])

        version = self.conn.execute(
            "SELECT * FROM prompt_versions WHERE take_id = '4-2'"
        ).fetchone()
        self.assertEqual(version["version"], 1)
        self.assertEqual(version["prompt_hash"], PROMPT_V1_HASH)
        self.assertEqual(json.loads(version["fixes"]), [])
        self.assertTrue(version["created_at"])

        ingested = events_of(self.conn, kind="result_ingested", take_id="4-2")
        self.assertEqual(len(ingested), 1)
        self.assertEqual(json.loads(ingested[0]["detail"]), {"revision": 2})
        added = events_of(self.conn, kind="prompt_version_added", take_id="4-2")
        self.assertEqual(len(added), 1)

    def test_measured_anomaly_is_flagged_with_its_numbers(self):
        write_final_take(self.results, "4-5", measured=[MEASURED_FLICKER])
        self.ingest()
        take = self.take("4-5")
        self.assertEqual(take["status"], "flagged")

        issue = self.conn.execute("SELECT * FROM issues WHERE take_id = '4-5'").fetchone()
        self.assertEqual(issue["state"], "measured")
        self.assertEqual(issue["issue_type"], "exposure_flicker")
        self.assertEqual(issue["value"], 18.4)
        self.assertEqual(issue["threshold"], 8.0)
        self.assertEqual(issue["model_note"], "")

        changes = events_of(self.conn, kind="status_changed", take_id="4-5")
        self.assertEqual(json.loads(changes[-1]["detail"]),
                         {"old": "review_pending", "new": "flagged"})

    def test_issues_and_prompt_version_are_written_once_per_take(self):
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        self.ingest()
        # A later final revision (a worker rewrite) changes nothing already written.
        write_final_take(self.results, "4-2", revision=3, candidates=[CANDIDATE_ROAD])
        self.ingest()

        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM issues WHERE take_id = '4-2'")
            .fetchone()["n"],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM prompt_versions WHERE take_id = '4-2'")
            .fetchone()["n"],
            1,
        )
        ingested = events_of(self.conn, kind="result_ingested", take_id="4-2")
        self.assertEqual([json.loads(row["detail"])["revision"] for row in ingested], [2, 3])
        take = self.take()
        self.assertEqual(take["result_revision"], 3)
        self.assertEqual(take["status"], "needs_confirmation")

    def test_ingesting_the_same_directory_again_changes_nothing(self):
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        self.ingest()
        before = len(events_of(self.conn))
        self.assertEqual(self.ingest(), [])
        self.assertEqual(len(events_of(self.conn)), before)


class CheckErrorTest(IngestFixture):
    def test_each_failed_check_logs_check_error(self):
        write_final_take(self.results, "4-2", checks={"exposure_flicker": "error",
                                                      "road_markings": "done"})
        self.ingest()

        errors = events_of(self.conn, kind="check_error", take_id="4-2")
        self.assertEqual([json.loads(row["detail"])["check"] for row in errors],
                         ["exposure_flicker"])
        # A check in error keeps the take in review_pending, and the card shows it.
        self.assertEqual(self.take()["status"], "review_pending")
        changes = events_of(self.conn, kind="status_changed", take_id="4-2")
        self.assertEqual(changes, [])

    def test_two_failed_checks_log_two_events(self):
        write_final_take(self.results, "4-2", checks={"exposure_flicker": "error",
                                                      "road_markings": "error"})
        self.ingest()
        errors = events_of(self.conn, kind="check_error", take_id="4-2")
        self.assertEqual(len(errors), 2)


class RetakeTest(IngestFixture):
    def test_retake_arrives_as_its_own_take_linked_to_its_parent(self):
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        write_final_take(self.results, "4-2r1", parent_take_id="4-2")
        self.assertEqual(self.ingest(), ["4-2", "4-2r1"])

        retake = self.take("4-2r1")
        self.assertEqual(retake["parent_take_id"], "4-2")
        self.assertEqual(retake["status"], "clean")
        # The retake gets its own prompt version 1, its own issue namespace.
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS n FROM prompt_versions WHERE take_id = '4-2r1'"
            ).fetchone()["n"],
            1,
        )
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS n FROM issues WHERE take_id = '4-2r1'")
            .fetchone()["n"],
            0,
        )
        self.assertNotEqual(self.take("4-2")["status"], retake["status"])


if __name__ == "__main__":
    unittest.main()
