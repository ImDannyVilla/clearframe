"""Ingest records the `generation` table (#7) so approval's queue file works (#10).

The bug from evidence/README.md: #7's ingest never populated #10's
`generation` table, so Approve was refused with "take … has no generation
info". These tests run the real ingest on the contract's sample result, then
drive agent/approval.py's real actions against the same SQLite file: temp
dirs only, no network, no model.
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))

from agent import db, ingest, slack  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402
import approval  # noqa: E402

SAMPLES = ROOT / "contracts" / "samples"
SAMPLE_RESULT = json.loads((SAMPLES / "result-4-2.json").read_text(encoding="utf-8"))
SAMPLE_QUEUE = json.loads((SAMPLES / "queue-4-2-v2.json").read_text(encoding="utf-8"))
RUBRIC = rubric_loader.load()
APPROVER = "Creative director"
HASH_V2 = "sha256:08e2b485acb99acac2ef6705b87465634da1c9a1c66d86c816d1ba1b07cf1ed6"
AT = SAMPLE_QUEUE["approval"]["approved_at"]  # the sample queue file's time
BASE = "http://127.0.0.1:8090"


class GenerationIngestTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-generation-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.results = self.tmp / "results"
        self.results.mkdir()
        self.db_path = self.tmp / "state" / "clearframe.db"
        self.conn = db.connect(self.db_path)
        self.addCleanup(self.conn.close)
        self.queue_dir = self.tmp / "approved_generation_queue"
        (self.results / "4-2.json").write_text(json.dumps(SAMPLE_RESULT), encoding="utf-8")

    def run_pass(self):
        return ingest.run_pass(self.conn, self.results, RUBRIC, slack.unconfigured_poster, BASE)

    def generation_row(self):
        return self.conn.execute("SELECT * FROM generation WHERE take_id = '4-2'").fetchone()

    def test_final_ingest_records_the_generation_block(self):
        report = self.run_pass()
        self.assertEqual(report.ingested, ["4-2"])
        row = self.generation_row()
        self.assertIsNotNone(row)
        self.assertEqual(row["tool"], SAMPLE_RESULT["generation"]["tool"])
        self.assertEqual(row["model"], SAMPLE_RESULT["generation"]["model"])
        self.assertEqual(json.loads(row["seed"]), SAMPLE_RESULT["generation"]["seed"])

    def test_approve_after_ingest_writes_the_sample_queue_file(self):
        self.run_pass()
        with approval.Store(self.db_path) as store:
            confirmed = approval.confirm_note(store, "4-2-1", APPROVER, RUBRIC, at=AT)
            self.assertEqual(confirmed["status"], "flagged")
            result = approval.approve_regeneration(
                store, "4-2", 2, HASH_V2, APPROVER, self.queue_dir, RUBRIC, at=AT)
        self.assertEqual(result["status"], "approved_for_regeneration")
        self.assertEqual(result["queue_file"], "4-2-v2.json")
        written = json.loads((self.queue_dir / "4-2-v2.json").read_text(encoding="utf-8"))
        self.assertEqual(written, SAMPLE_QUEUE)

    def test_approval_refused_without_the_generation_row(self):
        self.run_pass()
        # Simulate the old ingest: everything but the generation row.
        self.conn.execute("DELETE FROM generation WHERE take_id = '4-2'")
        self.conn.commit()
        self.assertIsNone(self.generation_row())
        with approval.Store(self.db_path) as store:
            approval.confirm_note(store, "4-2-1", APPROVER, RUBRIC, at=AT)
            with self.assertRaises(approval.ApprovalRefused) as caught:
                approval.approve_regeneration(
                    store, "4-2", 2, HASH_V2, APPROVER, self.queue_dir, RUBRIC, at=AT)
        self.assertEqual(caught.exception.reason, "take 4-2 has no generation info")
        self.assertFalse(list(self.queue_dir.glob("*")))

    def test_reingesting_the_same_revision_duplicates_nothing(self):
        self.run_pass()
        tables = ("takes", "issues", "prompt_versions", "generation", "events", "approvals")
        counts = lambda: {t: self.conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
                          for t in tables}  # noqa: E731
        before = counts()
        generation_before = self.generation_row()
        report = self.run_pass()
        self.assertEqual((report.ingested, report.updated), ([], []))
        self.assertEqual(counts(), before)
        row = self.generation_row()
        self.assertEqual(dict(row), dict(generation_before))


if __name__ == "__main__":
    unittest.main()
