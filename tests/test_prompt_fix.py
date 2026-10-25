"""prompt-fix: the sample prompts hash as the contract says, and the fix
versions are built exactly as contracts/README.md describes.

No footage, no model, no network: a temp sqlite file stands in for the sandbox
state, and the rubric is the repo's own.
"""
import hashlib
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

PROMPT_V1_4_2 = (
    "The Vela drives down a city street at golden hour, tracking shot, "
    "lane arrows painted on the road."
)
PROMPT_V2_4_2 = (
    "The Vela drives down a city street at golden hour, tracking shot, "
    "lane arrows painted on the road.\n\n"
    "Make sure: lane arrows point in the direction of travel and lane lines "
    "stay continuous."
)
HASH_V1 = "sha256:ee4aa7f4a4c28994d974159b541e8f8b372c29b03ade2ad7098b62c6ee08b89d"
HASH_V2 = "sha256:08e2b485acb99acac2ef6705b87465634da1c9a1c66d86c816d1ba1b07cf1ed6"

# 2-3's version 1 (a sample value like every prompt; the fix line is the contract's).
PROMPT_V1_2_3 = (
    "The Vela waits at a crossing at golden hour, close-up on the driver's "
    "hands resting on the wheel."
)
PROMPT_V2_2_3 = (
    PROMPT_V1_2_3
    + "\n\nMake sure: every hand has exactly five fingers in every frame."
)

AT = "2026-10-25T13:41:07-07:00"


def sha256_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class HashTest(unittest.TestCase):
    def test_sample_v1_prompt_hashes_to_contract_hash(self):
        self.assertEqual(prompt_fix.sha256_text(PROMPT_V1_4_2), HASH_V1)

    def test_sample_v2_prompt_hashes_to_contract_hash(self):
        self.assertEqual(prompt_fix.sha256_text(PROMPT_V2_4_2), HASH_V2)


class BuildVersionTest(unittest.TestCase):
    def test_4_2_version_2_matches_the_contract_text(self):
        built = prompt_fix.build_version(PROMPT_V1_4_2, RUBRIC, ["road_markings"])
        self.assertEqual(built, (PROMPT_V2_4_2, ["road_markings"]))
        self.assertFalse(PROMPT_V2_4_2.endswith("\n"))

    def test_2_3_version_2_appends_the_extra_finger_fix(self):
        built = prompt_fix.build_version(PROMPT_V1_2_3, RUBRIC, ["extra_finger"])
        self.assertEqual(built, (PROMPT_V2_2_3, ["extra_finger"]))
        self.assertEqual(prompt_fix.sha256_text(built[0]),
                         sha256_text(PROMPT_V2_2_3))

    def test_fix_lines_follow_rubric_order_once_per_type(self):
        built = prompt_fix.build_version(
            PROMPT_V1_4_2, RUBRIC, ["road_markings", "exposure_flicker", "road_markings"]
        )
        text, fixes = built
        self.assertEqual(
            text,
            PROMPT_V1_4_2
            + "\n\n"
            + "Don't include: flicker, strobing or sudden jumps in brightness.\n"
            + "Make sure: exposure stays steady from frame to frame.\n"
            + "Make sure: lane arrows point in the direction of travel and lane "
            "lines stay continuous.",
        )
        self.assertEqual(fixes, ["exposure_flicker", "road_markings"])

    def test_line_already_in_version_1_is_skipped(self):
        base = PROMPT_V1_4_2 + "\n\nMake sure: lane arrows point in the direction of travel and lane lines stay continuous."
        self.assertIsNone(prompt_fix.build_version(base, RUBRIC, ["road_markings"]))

    def test_only_missing_lines_are_added(self):
        base = PROMPT_V1_4_2 + "\nMake sure: exposure stays steady from frame to frame."
        built = prompt_fix.build_version(base, RUBRIC, ["exposure_flicker"])
        self.assertEqual(
            built,
            (
                base
                + "\n\nDon't include: flicker, strobing or sudden jumps in brightness.",
                ["exposure_flicker"],
            ),
        )

    def test_no_issue_types_means_no_version(self):
        self.assertIsNone(prompt_fix.build_version(PROMPT_V1_4_2, RUBRIC, []))


class StoreCase(unittest.TestCase):
    """A temp sqlite file with one take, seeded per test."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "clearframe.db"
        self.store = approval.Store(self.db)
        self.addCleanup(self.store.close)

    def seed_take(self, take_id="4-2", prompt=PROMPT_V1_4_2, status="review_pending",
                  issues=()):
        self.store.conn.execute(
            "INSERT INTO takes (take_id, shot, expected_takes, status, result_revision,"
            " first_seen_at, updated_at) VALUES (?, 4, 6, ?, 2, ?, ?)",
            (take_id, status, AT, AT),
        )
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


class OnFlaggedTest(StoreCase):
    def test_measured_issue_adds_version_2_with_event(self):
        self.seed_take(status="flagged", issues=[("exposure_flicker", "measured")])
        row = prompt_fix.on_flagged(self.store, "4-2", RUBRIC, at=AT)
        self.assertEqual(row["version"], 2)
        self.assertEqual(
            row["prompt"],
            PROMPT_V1_4_2
            + "\n\n"
            + "Don't include: flicker, strobing or sudden jumps in brightness.\n"
            + "Make sure: exposure stays steady from frame to frame.",
        )
        self.assertEqual(row["fixes"], ["exposure_flicker"])
        stored = self.store.prompt_version("4-2", 2)
        self.assertEqual(stored["prompt_hash"], row["prompt_hash"])
        (event,) = [e for e in self.store.events("4-2") if e["kind"] == "prompt_version_added"]
        self.assertEqual(event["actor"], "prompt-fix")
        self.assertEqual(json.loads(event["detail"]), {"version": 2, "fixes": ["exposure_flicker"]})

    def test_confirmed_note_later_adds_another_version_built_from_version_1(self):
        self.seed_take(
            status="flagged",
            issues=[("exposure_flicker", "measured"), ("road_markings", "candidate")],
        )
        prompt_fix.on_flagged(self.store, "4-2", RUBRIC, at=AT)
        approval.confirm_note(self.store, "4-2-2", "Creative director", RUBRIC, at=AT)
        latest = self.store.latest_prompt_version("4-2")
        self.assertEqual(latest["version"], 3)
        # Built the same way from version 1: every measured or confirmed type,
        # once each, in rubric order.
        self.assertEqual(
            latest["prompt"],
            PROMPT_V1_4_2
            + "\n\n"
            + "Don't include: flicker, strobing or sudden jumps in brightness.\n"
            + "Make sure: exposure stays steady from frame to frame.\n"
            + "Make sure: lane arrows point in the direction of travel and lane "
            "lines stay continuous.",
        )
        self.assertEqual(json.loads(latest["fixes"]), ["exposure_flicker", "road_markings"])
        versions = [r["version"] for r in self.store.conn.execute(
            "SELECT version FROM prompt_versions WHERE take_id = '4-2'")]
        self.assertEqual(versions, [1, 2, 3])

    def test_no_new_line_means_no_new_version(self):
        self.seed_take(
            status="flagged",
            prompt=PROMPT_V2_4_2,
            issues=[("road_markings", "confirmed")],
        )
        self.assertIsNone(prompt_fix.on_flagged(self.store, "4-2", RUBRIC, at=AT))
        self.assertIsNone(self.store.prompt_version("4-2", 2))

    def test_not_flagged_means_no_version(self):
        self.seed_take(status="needs_confirmation", issues=[("road_markings", "candidate")])
        self.assertIsNone(prompt_fix.on_flagged(self.store, "4-2", RUBRIC, at=AT))


if __name__ == "__main__":
    unittest.main()
