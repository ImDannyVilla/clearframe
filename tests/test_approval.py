"""The four actions, the approval checks, the queue file and retake linking.

All contract rules come from contracts/README.md "Approval and the queue file".
Tests run on a temp sqlite file and a temp queue folder: no network, no model,
no footage.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))

from rubric import rubric as rubric_loader  # noqa: E402
import approval  # noqa: E402
import prompt_fix  # noqa: E402

RUBRIC = rubric_loader.load()
APPROVER = "Creative director"

PROMPT_V1 = (
    "The Vela drives down a city street at golden hour, tracking shot, "
    "lane arrows painted on the road."
)
PROMPT_V2 = (
    "The Vela drives down a city street at golden hour, tracking shot, "
    "lane arrows painted on the road.\n\n"
    "Make sure: lane arrows point in the direction of travel and lane lines "
    "stay continuous."
)
HASH_V2 = "sha256:08e2b485acb99acac2ef6705b87465634da1c9a1c66d86c816d1ba1b07cf1ed6"
GEN = {"tool": "fal.ai", "model": "bytedance/seedance-2.0/text-to-video", "seed": 18342}
AT = "2026-10-25T13:41:07-07:00"
SAMPLE_DIR = ROOT / "contracts" / "samples"


def load_sample(name):
    return json.loads((SAMPLE_DIR / name).read_text(encoding="utf-8"))


class ApprovalCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = approval.Store(Path(self.tmp.name) / "clearframe.db")
        self.addCleanup(self.store.close)
        self.queue_dir = Path(self.tmp.name) / "approved_generation_queue"

    def seed_take(self, take_id="4-2", status="needs_confirmation", prompt=PROMPT_V1,
                  generation=GEN, issues=(), revision=2, shot=4):
        """A take with generation info, prompt version 1 and issue rows."""
        self.store.conn.execute(
            "INSERT INTO takes (take_id, shot, expected_takes, status, result_revision,"
            " first_seen_at, updated_at) VALUES (?, ?, 6, ?, ?, ?, ?)",
            (take_id, shot, status, revision, AT, AT),
        )
        if generation is not None:
            self.store.conn.execute(
                "INSERT INTO generation (take_id, tool, model, seed) VALUES (?, ?, ?, ?)",
                (take_id, generation["tool"], generation["model"], json.dumps(generation["seed"])),
            )
        if prompt is not None:
            self.store.conn.execute(
                "INSERT INTO prompt_versions (take_id, version, prompt, prompt_hash, fixes,"
                " created_at) VALUES (?, 1, ?, ?, '[]', ?)",
                (take_id, prompt, prompt_fix.sha256_text(prompt), AT),
            )
        for i, (issue_type, state) in enumerate(issues, start=1):
            self.store.conn.execute(
                "INSERT INTO issues (issue_id, take_id, issue_type, state) VALUES (?, ?, ?, ?)",
                (f"{take_id}-{i}", take_id, issue_type, state),
            )
        self.store.conn.commit()

    def flag_4_2(self):
        """Seed 4-2 with a waiting note, confirm it, and return the fresh store row."""
        self.seed_take(issues=[("road_markings", "candidate")])
        approval.confirm_note(self.store, "4-2-1", APPROVER, RUBRIC, at=AT)
        return self.store.latest_prompt_version("4-2")

    def approve_4_2(self):
        return approval.approve_regeneration(
            self.store, "4-2", 2, HASH_V2, APPROVER, self.queue_dir, RUBRIC, at=AT
        )

    def last_event(self, kind):
        events = [e for e in self.store.events(kinds=[kind])]
        return events[-1] if events else None


class ConfirmDismissTest(ApprovalCase):
    def test_confirm_note_writes_state_and_events_and_prompt_fix(self):
        self.seed_take(issues=[("road_markings", "candidate")])
        result = approval.confirm_note(self.store, "4-2-1", APPROVER, RUBRIC, at=AT)
        self.assertEqual(result, {"issue_id": "4-2-1", "state": "confirmed", "status": "flagged"})
        issue = self.store.issue("4-2-1")
        self.assertEqual(issue["state"], "confirmed")
        self.assertEqual(issue["decided_by"], APPROVER)
        self.assertEqual(issue["decided_at"], AT)
        take = self.store.take("4-2")
        self.assertEqual(take["status"], "flagged")
        kinds = [e["kind"] for e in self.store.events("4-2")]
        self.assertEqual(
            kinds, ["note_confirmed", "status_changed", "prompt_version_added"]
        )
        latest = self.store.latest_prompt_version("4-2")
        self.assertEqual(latest["version"], 2)
        self.assertEqual(latest["prompt_hash"], HASH_V2)

    def test_confirm_note_refused_when_not_candidate(self):
        self.seed_take(issues=[("road_markings", "confirmed")])
        with self.assertRaises(approval.ApprovalRefused) as caught:
            approval.confirm_note(self.store, "4-2-1", APPROVER, RUBRIC, at=AT)
        self.assertIn("not candidate", caught.exception.reason)
        event = self.last_event("approval_refused")
        self.assertEqual(event["actor"], APPROVER)
        detail = json.loads(event["detail"])
        self.assertEqual(detail["action"], "confirm_note")
        self.assertIn("not candidate", detail["reason"])
        self.assertEqual(self.store.issue("4-2-1")["state"], "confirmed")

    def test_dismiss_note_marks_dismissed_and_recomputes_clean(self):
        self.seed_take(issues=[("road_markings", "candidate")])
        result = approval.dismiss_note(self.store, "4-2-1", APPROVER, RUBRIC, at=AT)
        self.assertEqual(result["state"], "dismissed")
        self.assertEqual(result["status"], "clean")
        self.assertEqual(self.store.issue("4-2-1")["state"], "dismissed")
        self.assertEqual(
            [e["kind"] for e in self.store.events("4-2")], ["note_dismissed", "status_changed"]
        )


class AcceptAsIsTest(ApprovalCase):
    def test_accept_from_flagged_writes_approval_row_and_event(self):
        self.flag_4_2()
        result = approval.accept_as_is(self.store, "4-2", APPROVER, at=AT)
        self.assertEqual(result["status"], "accepted_as_is")
        row = self.store.conn.execute(
            "SELECT * FROM approvals WHERE take_id = '4-2'").fetchone()
        self.assertEqual(row["decision"], "accepted_as_is")
        self.assertIsNone(row["prompt_version"])
        self.assertEqual(row["prompt_hash"], "")
        self.assertEqual(row["queue_file"], "")
        self.assertEqual(row["next_take_id"], "")
        self.assertEqual(row["approver"], APPROVER)
        self.assertEqual(
            [e["kind"] for e in self.store.events("4-2")][-2:],
            ["accepted_as_is", "status_changed"],
        )

    def test_accept_from_needs_confirmation_is_allowed(self):
        self.seed_take(issues=[("road_markings", "candidate")])
        result = approval.accept_as_is(self.store, "4-2", APPROVER, at=AT)
        self.assertEqual(result["status"], "accepted_as_is")

    def test_accept_refused_from_clean(self):
        self.seed_take(status="clean")
        with self.assertRaises(approval.ApprovalRefused) as caught:
            approval.accept_as_is(self.store, "4-2", APPROVER, at=AT)
        self.assertIn("not flagged or needs_confirmation", caught.exception.reason)
        event = self.last_event("approval_refused")
        self.assertEqual(json.loads(event["detail"])["action"], "accept_as_is")


class ApproveRegenerationTest(ApprovalCase):
    def test_full_path_writes_sample_shaped_queue_file(self):
        latest = self.flag_4_2()
        self.assertEqual(latest["prompt_hash"], HASH_V2)
        result = self.approve_4_2()
        self.assertEqual(result["next_take_id"], "4-2r1")
        self.assertEqual(result["queue_file"], "4-2-v2.json")
        self.assertEqual(result["status"], "approved_for_regeneration")
        queue_file = self.queue_dir / "4-2-v2.json"
        self.assertTrue(queue_file.is_file())
        written = json.loads(queue_file.read_text(encoding="utf-8"))
        sample = load_sample("queue-4-2-v2.json")
        for key in ("schema", "request_id", "parent_take_id", "next_take_id", "shot",
                    "prompt_version", "prompt_hash", "prompt", "fixes", "generation"):
            self.assertEqual(written[key], sample[key], key)
        self.assertEqual(written["approval"]["approval_id"], 1)
        self.assertEqual(written["approval"]["approver"], sample["approval"]["approver"])
        # Seedance 2.0 on fal doesn't take a seed: the retake gets a new one.
        self.assertIsNone(written["generation"]["seed"])
        kinds = [e["kind"] for e in self.store.events("4-2")]
        self.assertEqual(kinds[-3:], ["approved", "queued", "status_changed"])
        row = self.store.conn.execute(
            "SELECT * FROM approvals WHERE take_id = '4-2'").fetchone()
        self.assertEqual(row["queue_file"], "4-2-v2.json")
        self.assertEqual(row["next_take_id"], "4-2r1")

    def test_refused_unknown_approver(self):
        self.flag_4_2()
        with self.assertRaises(approval.ApprovalRefused) as caught:
            approval.approve_regeneration(
                self.store, "4-2", 2, HASH_V2, "Intern", self.queue_dir, RUBRIC, at=AT)
        self.assertIn("not listed under approvers", caught.exception.reason)
        event = self.last_event("approval_refused")
        self.assertEqual(event["actor"], "Intern")
        self.assertEqual(
            json.loads(event["detail"]),
            {"action": "approve_regeneration", "reason": caught.exception.reason},
        )

    def test_refused_while_a_note_waits(self):
        self.seed_take(
            status="flagged",
            issues=[("road_markings", "confirmed"), ("extra_finger", "candidate")],
        )
        approval.prompt_fix.on_flagged(self.store, "4-2", RUBRIC, at=AT)
        with self.assertRaises(approval.ApprovalRefused) as caught:
            self.approve_4_2()
        self.assertIn("still waiting", caught.exception.reason)
        self.assertFalse(list(self.queue_dir.glob("*")))

    def test_refused_when_not_flagged(self):
        self.seed_take(issues=[("road_markings", "candidate")])
        with self.assertRaises(approval.ApprovalRefused) as caught:
            self.approve_4_2()
        self.assertIn("not flagged", caught.exception.reason)

    def test_refused_without_a_version_above_1(self):
        self.seed_take(status="flagged", issues=[("road_markings", "confirmed")])
        with self.assertRaises(approval.ApprovalRefused) as caught:
            approval.approve_regeneration(
                self.store, "4-2", 1, prompt_fix.sha256_text(PROMPT_V1),
                APPROVER, self.queue_dir, RUBRIC, at=AT)
        self.assertIn("no prompt version above 1", caught.exception.reason)

    def test_refused_stale_version(self):
        self.flag_4_2()
        with self.assertRaises(approval.ApprovalRefused) as caught:
            approval.approve_regeneration(
                self.store, "4-2", 1, HASH_V2, APPROVER, self.queue_dir, RUBRIC, at=AT)
        self.assertIn("not the latest version", caught.exception.reason)

    def test_refused_stale_hash(self):
        self.flag_4_2()
        stale = prompt_fix.sha256_text(PROMPT_V2 + " ")
        with self.assertRaises(approval.ApprovalRefused) as caught:
            approval.approve_regeneration(
                self.store, "4-2", 2, stale, APPROVER, self.queue_dir, RUBRIC, at=AT)
        self.assertIn("does not match the stored hash", caught.exception.reason)

    def test_refused_when_stored_text_no_longer_matches_its_hash(self):
        self.flag_4_2()
        self.store.conn.execute(
            "UPDATE prompt_versions SET prompt = ? WHERE take_id = '4-2' AND version = 2",
            (PROMPT_V2 + "\nMake sure: the car stays on the road.",),
        )
        self.store.conn.commit()
        with self.assertRaises(approval.ApprovalRefused) as caught:
            self.approve_4_2()
        self.assertIn("no longer hashes", caught.exception.reason)

    def test_queue_file_is_atomic_and_never_rewritten(self):
        self.flag_4_2()
        self.approve_4_2()
        self.assertEqual([p.name for p in self.queue_dir.iterdir()], ["4-2-v2.json"])
        before = (self.queue_dir / "4-2-v2.json").read_bytes()
        # A later action on another take must not touch the queued file.
        self.seed_take(take_id="2-3", shot=2, status="flagged",
                       issues=[("extra_finger", "confirmed")], prompt=None)
        self.store.log_event("card_posted", at=AT, take_id="2-3", shot=2, detail={"ts": "1.2"})
        self.assertEqual((self.queue_dir / "4-2-v2.json").read_bytes(), before)
        self.assertEqual([p.name for p in self.queue_dir.iterdir()], ["4-2-v2.json"])

    def test_seed_repeats_only_when_the_tool_takes_one(self):
        self.flag_4_2()
        self.store.conn.execute(
            "UPDATE generation SET tool = 'local-cli', model = 'stub-video', seed = '7'"
            " WHERE take_id = '4-2'"
        )
        self.store.conn.commit()
        self.approve_4_2()
        written = json.loads((self.queue_dir / "4-2-v2.json").read_text(encoding="utf-8"))
        self.assertEqual(written["generation"],
                         {"tool": "local-cli", "model": "stub-video", "seed": 7})


class RetakeTest(ApprovalCase):
    def approve_flagged_retake(self, take_id, prompt, shot=4):
        """A retake that arrived, failed a different check, got fixed and approved."""
        exposure_fix = (
            "\n\nDon't include: flicker, strobing or sudden jumps in brightness.\n"
            "Make sure: exposure stays steady from frame to frame."
        )
        fixed = prompt + exposure_fix
        self.seed_take(take_id=take_id, status="flagged", shot=shot, prompt=prompt,
                       issues=[("exposure_flicker", "measured")])
        approval.prompt_fix.on_flagged(self.store, take_id, RUBRIC, at=AT)
        latest = self.store.latest_prompt_version(take_id)
        self.assertEqual(latest["prompt"], fixed)
        return approval.approve_regeneration(
            self.store, take_id, 2, latest["prompt_hash"], APPROVER,
            self.queue_dir, RUBRIC, at=AT)

    def test_retake_of_4_2_is_4_2r1_and_retake_of_that_is_4_2r2(self):
        self.flag_4_2()
        first = self.approve_4_2()
        self.assertEqual(first["next_take_id"], "4-2r1")
        second = self.approve_flagged_retake("4-2r1", PROMPT_V2)
        self.assertEqual(second["next_take_id"], "4-2r2")
        written = json.loads((self.queue_dir / "4-2r1-v2.json").read_text(encoding="utf-8"))
        self.assertEqual(written["parent_take_id"], "4-2r1")
        self.assertEqual(written["next_take_id"], "4-2r2")

    def test_link_retake_logs_match_with_the_approval_hash(self):
        self.flag_4_2()
        self.approve_4_2()
        self.seed_take(take_id="4-2r1", status="review_pending", prompt=None, issues=())
        linked = approval.link_retake(self.store, "4-2r1", HASH_V2, at=AT)
        self.assertEqual(linked["parent_take_id"], "4-2")
        self.assertTrue(linked["prompt_hash_match"])
        self.assertEqual(self.store.take("4-2r1")["parent_take_id"], "4-2")
        event = self.last_event("retake_linked")
        detail = json.loads(event["detail"])
        self.assertEqual(detail["parent_take_id"], "4-2")
        self.assertTrue(detail["prompt_hash_match"])

    def test_link_retake_logs_mismatch_too(self):
        self.flag_4_2()
        self.approve_4_2()
        self.seed_take(take_id="4-2r1", status="review_pending", prompt=None, issues=())
        linked = approval.link_retake(
            self.store, "4-2r1", prompt_fix.sha256_text("a different prompt"), at=AT)
        self.assertFalse(linked["prompt_hash_match"])
        self.assertFalse(json.loads(self.last_event("retake_linked")["detail"])["prompt_hash_match"])

    def test_link_retake_without_approval_returns_none(self):
        self.seed_take(take_id="4-2r1", status="review_pending", prompt=None, issues=())
        self.assertIsNone(approval.link_retake(self.store, "4-2r1", HASH_V2, at=AT))


class LoadResultTest(ApprovalCase):
    def test_loading_the_sample_result_walks_the_whole_flow(self):
        result = approval.load_result(
            self.store, SAMPLE_DIR / "result-4-2.json", RUBRIC, at=AT)
        self.assertEqual(result, {"take_id": "4-2", "status": "needs_confirmation", "final": True})
        self.assertEqual(
            [e["kind"] for e in self.store.events("4-2")],
            ["take_seen", "result_ingested", "status_changed"],
        )
        version_1 = self.store.prompt_version("4-2", 1)
        self.assertEqual(
            version_1["prompt_hash"],
            "sha256:ee4aa7f4a4c28994d974159b541e8f8b372c29b03ade2ad7098b62c6ee08b89d",
        )
        issue = self.store.issue("4-2-1")
        self.assertEqual((issue["issue_type"], issue["state"]), ("road_markings", "candidate"))
        approval.confirm_note(self.store, "4-2-1", APPROVER, RUBRIC, at=AT)
        self.approve_4_2()
        written = json.loads((self.queue_dir / "4-2-v2.json").read_text(encoding="utf-8"))
        sample = load_sample("queue-4-2-v2.json")
        self.assertEqual(written["prompt"], sample["prompt"])
        self.assertEqual(written["prompt_hash"], sample["prompt_hash"])

    def test_events_never_carry_prompt_text(self):
        self.flag_4_2()
        self.approve_4_2()
        for event in self.store.events():
            self.assertNotIn(PROMPT_V2, event["detail"])
            self.assertNotIn(PROMPT_V1, event["detail"])


if __name__ == "__main__":
    unittest.main()
