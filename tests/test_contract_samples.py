"""The contract's sample files must agree with each other and with the contract text.

Other tests build on these samples, so a drift here would hide real bugs.
"""
import hashlib
import json
import unittest
from pathlib import Path

CONTRACTS = Path(__file__).resolve().parent.parent / "contracts"


def load(name):
    return json.loads((CONTRACTS / "samples" / name).read_text(encoding="utf-8"))


def sha256_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


class ResultSampleTest(unittest.TestCase):
    def setUp(self):
        self.result = load("result-4-2.json")

    def test_schema(self):
        self.assertEqual(self.result["schema"], "clearframe.result/1")

    def test_seconds_come_from_frame_positions(self):
        fps = self.result["render"]["fps"]
        for item in self.result["measured"] + self.result["candidates"]:
            self.assertEqual(item["t_start"], round(item["frame_start"] / fps, 3))
            self.assertEqual(item["t_end"], round(item["frame_end"] / fps, 3))

    def test_final_revision_has_no_running_checks(self):
        for state in self.result["checks"].values():
            self.assertIn(state, ("done", "error"))


class QueueSampleTest(unittest.TestCase):
    def setUp(self):
        self.result = load("result-4-2.json")
        self.queue = load("queue-4-2-v2.json")

    def test_schema(self):
        self.assertEqual(self.queue["schema"], "clearframe.queue/1")

    def test_hash_matches_prompt(self):
        self.assertEqual(self.queue["prompt_hash"], sha256_text(self.queue["prompt"]))

    def test_prompt_is_version_1_then_a_blank_line_then_the_fix(self):
        version_1 = self.result["generation"]["prompt"]
        self.assertTrue(self.queue["prompt"].startswith(version_1 + "\n\n"))
        self.assertFalse(self.queue["prompt"].endswith("\n"))

    def test_ids_follow_the_naming_rules(self):
        parent = self.queue["parent_take_id"]
        self.assertEqual(self.queue["request_id"], f"{parent}-v{self.queue['prompt_version']}")
        self.assertEqual(self.queue["next_take_id"], f"{parent}r1")

    def test_contract_text_states_the_sample_hashes(self):
        contract = (CONTRACTS / "README.md").read_text(encoding="utf-8")
        self.assertIn(sha256_text(self.result["generation"]["prompt"]), contract)
        self.assertIn(self.queue["prompt_hash"], contract)


if __name__ == "__main__":
    unittest.main()
