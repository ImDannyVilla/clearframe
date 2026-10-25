"""#7 checklist item 5: what may and may not reach a Slack card.

The builder-level refusals (extra fields, bad values) and the basic forbidden
strings are tests.test_agent.CardTest; this file holds the payload test the
checklist asks for - a full take flow through the #7 database with decisions
recorded, then every card field scanned so a prompt, a model note, a frame
path, a file name, an approver name, a hash or any off-contract content fails
the test - plus the evidence-link rule (the one path a card may carry).

Runs on contracts/samples/ and temp dirs: no footage, no model, no network.
"""
import copy
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import card, db, ingest  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402

SAMPLE = json.loads((ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8"))
BASE = "http://127.0.0.1:8090"
APPROVER = "Creative director"


class Recorder:
    def __init__(self):
        self.cards = []

    def __call__(self, text):
        self.cards.append(text)
        return f"1730000000.{len(self.cards):06d}"


def flicker_result():
    result = copy.deepcopy(SAMPLE)
    result.update(take_id="4-5", revision=2, candidates=[])
    result["parent_take_id"] = None
    result["render"]["file"] = "4-5.mp4"
    result["brightness_series"] = "frames/4-5/brightness.json"
    result["measured"] = [{
        "issue_type": "exposure_flicker", "frame_start": 61, "frame_end": 63,
        "t_start": 2.542, "t_end": 2.625, "value": 18.4, "threshold": 8.0,
        "unit": "mean brightness change between consecutive frames, 0-255",
        "evidence": ["frames/4-5/000060.jpg", "frames/4-5/000061.jpg"],
    }]
    return result


class NeverOnACardTest(unittest.TestCase):
    """The contract's 'Never on a card' list, checked against real card payloads
    built from a database that has seen a take, a decision and a retake."""

    FORBIDDEN = (
        SAMPLE["generation"]["prompt"],          # the prompt
        SAMPLE["candidates"][0]["model_note"],   # the model's words
        SAMPLE["generation"]["tool"],            # generation tool name
        SAMPLE["generation"]["model"],           # generation model name
        SAMPLE["vision"]["model"],               # reviewer model name
        SAMPLE["render"]["sha256"],              # the render hash
        "sha256:",                               # any hash
        "frames/",                               # frame paths
        ".jpg", ".mp4", ".json",                 # file names
        "Vela",                                  # brand or product names
        APPROVER,                                # approver names
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-cards-"))
        self.results = self.tmp / "results"
        self.results.mkdir()
        self.conn = db.connect(self.tmp / "state" / "clearframe.db")
        self.rubric = rubric_loader.load()
        self.poster = Recorder()
        self.addCleanup(self.close)

    def close(self):
        if self.conn is not None:
            self.conn.close()
            self.conn = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, result):
        (self.results / f"{result['take_id']}.json").write_text(json.dumps(result), encoding="utf-8")

    def run_pass(self):
        return ingest.run_pass(self.conn, self.results, self.rubric, self.poster, BASE)

    def test_no_forbidden_content_in_any_card_payload_after_decisions(self):
        for take_id in ("4-1", "4-3", "4-4", "4-6"):
            result = copy.deepcopy(SAMPLE)
            result.update(take_id=take_id, revision=2, candidates=[], measured=[])
            result["parent_take_id"] = None
            result["render"]["file"] = f"{take_id}.mp4"
            result["brightness_series"] = f"frames/{take_id}/brightness.json"
            self.write(result)
        self.write(SAMPLE)          # 4-2: one waiting candidate note
        self.write(flicker_result())  # 4-5: one measured anomaly
        retake = copy.deepcopy(SAMPLE)
        retake.update(take_id="4-2r1", revision=2, candidates=[], measured=[])
        retake["parent_take_id"] = "4-2"
        retake["render"]["file"] = "4-2r1.mp4"
        retake["brightness_series"] = "frames/4-2r1/brightness.json"
        self.write(retake)
        self.run_pass()

        # Decisions land in the db: an approver on the confirmed issue and on
        # the accepted take, both with names the card must never carry.
        with self.conn:
            self.conn.execute("UPDATE issues SET state = 'confirmed', decided_by = ?, decided_at = ? "
                              "WHERE issue_id = '4-2-1'", (APPROVER, db.now()))
            db.recompute_status(self.conn, "4-2", APPROVER)
            self.conn.execute("INSERT INTO approvals (take_id, decision, approver, decided_at) "
                              "VALUES ('4-5', 'accepted_as_is', ?, ?)", (APPROVER, db.now()))
            db.recompute_status(self.conn, "4-5", APPROVER)
        self.assertEqual(len(self.poster.cards), 2)  # shot 4 + retake 4-2r1

        payloads = self.poster.cards + [
            card.shot_card_from_db(self.conn, self.rubric, 4, BASE),
            card.retake_card_from_db(self.conn, self.rubric, "4-2r1", BASE),
        ]
        self.assertTrue(payloads)
        for text in payloads:
            for forbidden in self.FORBIDDEN:
                self.assertNotIn(forbidden, text)
        # The decisions did shape the card, so the test isn't vacuous.
        shot_card = payloads[-2]
        self.assertIn("4-2: flagged · 1 confirmed note (road markings)", shot_card)
        self.assertIn("4-5: accepted as is · 1 measured anomaly (exposure)", shot_card)
        self.assertNotIn("4-2r1", shot_card.splitlines()[1])  # retakes aren't shot-card takes

    def test_the_card_carries_only_the_contract_fields(self):
        self.write(SAMPLE)
        self.run_pass()
        shot_card = card.shot_card_from_db(self.conn, self.rubric, 4, BASE)
        lines = shot_card.splitlines()
        self.assertEqual(lines[0], "ClearFrame · Shot 4 · 1 of 6 takes reviewed")
        self.assertEqual(lines[1], "4-2: needs confirmation · 1 candidate note (road markings)")
        self.assertEqual(lines[2], "Clean: none")
        self.assertEqual(lines[3], f"Evidence, on the GB10: {BASE}/shot/4")
        self.assertEqual(len(lines), 4)  # no section the contract doesn't list


class BuilderRefusalTest(unittest.TestCase):
    """Fields the contract doesn't list must be refused, including whole result
    rows passed in by mistake. (Overlaps tests.test_agent.CardTest only on the
    principle; the payloads here are the ones that flow from a result file.)"""

    def setUp(self):
        self.rubric = rubric_loader.load()
        self.take = {"take_id": "4-2", "status": "needs_confirmation",
                     "counts": [("candidate", "road_markings")], "check_errors": []}

    def test_a_result_file_row_is_not_a_take_field_set(self):
        row = {**SAMPLE["candidates"][0], "take_id": "4-2", "status": "needs_confirmation"}
        with self.assertRaises(card.CardError):
            card.build_shot_card(self.rubric, shot=4, expected_takes=6, takes=[row],
                                 link=f"{BASE}/shot/4")

    def test_approver_names_and_evidence_lists_are_refused(self):
        with self.assertRaises(card.CardError):
            card.build_shot_card(self.rubric, shot=4, expected_takes=6,
                                 takes=[{**self.take, "approver": APPROVER}], link=f"{BASE}/shot/4")
        with self.assertRaises(card.CardError):
            card.build_shot_card(self.rubric, shot=4, expected_takes=6,
                                 takes=[{**self.take, "evidence": ["frames/4-2/000048.jpg"]}],
                                 link=f"{BASE}/shot/4")
        with self.assertRaises(card.CardError):
            card.build_retake_card(self.rubric, take_id="4-2r1", parent_take_id="4-2",
                                   status="clean", link=f"{BASE}/take/4-2r1",
                                   decided_by=APPROVER)

    def test_prompts_and_model_words_are_refused_as_fields(self):
        with self.assertRaises(card.CardError):
            card.build_retake_card(self.rubric, take_id="4-2r1", parent_take_id="4-2",
                                   status="needs_confirmation", link=f"{BASE}/take/4-2r1",
                                   counts=[], check_errors=[], prompt=SAMPLE["generation"]["prompt"])
        with self.assertRaises(card.CardError):
            card.build_retake_card(self.rubric, take_id="4-2r1", parent_take_id="4-2",
                                   status="needs_confirmation", link=f"{BASE}/take/4-2r1",
                                   model_note=SAMPLE["candidates"][0]["model_note"])


class CardLinkTest(unittest.TestCase):
    """The link is the one path a card may carry: an evidence-page URL built
    from the configurable base URL, and nothing else."""

    def setUp(self):
        self.rubric = rubric_loader.load()
        self.take = {"take_id": "4-2", "status": "clean", "counts": [], "check_errors": []}

    def shot_card(self, link):
        return card.build_shot_card(self.rubric, shot=4, expected_takes=6,
                                    takes=[dict(self.take)], link=link)

    def test_the_base_url_is_configurable(self):
        # agent/README.md documents EVIDENCE_BASE_URL; any host must build.
        text = self.shot_card("http://gb10-studio:8090/shot/4")
        self.assertIn("Evidence, on the GB10: http://gb10-studio:8090/shot/4", text)

    def test_anything_but_an_evidence_page_is_refused(self):
        for bad in ("/shot/4",
                    "http://127.0.0.1:8090",
                    "http://127.0.0.1:8090/shot/4/extra",
                    "http://127.0.0.1:8090/take/4-2/",
                    "http://user:pw@127.0.0.1:8090/shot/4",
                    "ftp://127.0.0.1:8090/shot/4"):
            with self.assertRaises(card.CardError, msg=bad):
                self.shot_card(bad)


if __name__ == "__main__":
    unittest.main()
