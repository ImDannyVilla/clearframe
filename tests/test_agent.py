"""The status-card automation (#7): SQLite, ingest rules, the status rule and the Slack card.

Runs on contracts/samples/ and result files derived from them, so it needs no
footage, no model and no Slack.
"""
import copy
import json
import re
import shutil
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import card, db, ingest, slack  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402

CONTRACT = (ROOT / "contracts" / "README.md").read_text(encoding="utf-8")
SAMPLE = json.loads((ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8"))
BASE = "http://127.0.0.1:8090"
V1_HASH = "sha256:ee4aa7f4a4c28994d974159b541e8f8b372c29b03ade2ad7098b62c6ee08b89d"


def contract_section(heading):
    start = CONTRACT.index(f"## {heading}")
    end = CONTRACT.find("\n## ", start + 1)
    return CONTRACT[start:end if end != -1 else len(CONTRACT)]


def contract_columns(table):
    """Column names from the contract's table for `table`."""
    section = contract_section("SQLite tables")
    start = re.search(rf"^\*\*{table}\*\*", section, re.MULTILINE).end()
    columns = []
    for line in section[start:].splitlines()[1:]:
        if not line.startswith("|"):
            if columns:
                break
            continue
        first = line.strip("|").split("|")[0]
        columns += re.findall(r"`([a-z][a-z0-9_]*)`", first)
    return columns


CARD_SAMPLES = re.findall(r"```text\n(.*?)\n```", contract_section("Slack card"), re.DOTALL)


def clean_result(take_id, revision=2):
    """A final result for a take with nothing found, built from the contract sample."""
    result = copy.deepcopy(SAMPLE)
    shot, _, rest = take_id.partition("-")
    result.update(take_id=take_id, shot=int(shot), revision=revision, candidates=[], measured=[])
    result["parent_take_id"] = take_id.split("r")[0] if "r" in rest else None
    result["render"]["file"] = f"{take_id}.mp4"
    result["brightness_series"] = f"frames/{take_id}/brightness.json"
    return result


def flicker_result():
    result = clean_result("4-5")
    result["measured"] = [{
        "issue_type": "exposure_flicker", "frame_start": 61, "frame_end": 63, "t_start": 2.542, "t_end": 2.625,
        "value": 18.4, "threshold": 8.0, "unit": "mean brightness change between consecutive frames, 0-255",
        "evidence": ["frames/4-5/000060.jpg", "frames/4-5/000061.jpg"],
    }]
    return result


class Recorder:
    """A poster that records cards instead of sending them."""

    def __init__(self):
        self.cards = []

    def __call__(self, text):
        self.cards.append(text)
        return f"1730000000.{len(self.cards):06d}"


class AgentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-agent-"))
        self.results = self.tmp / "results"
        self.results.mkdir()
        self.conn = db.connect(self.tmp / "state" / "clearframe.db")
        self.rubric = rubric_loader.load()
        self.poster = Recorder()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, result, name=None):
        path = self.results / (name or f"{result['take_id']}.json")
        path.write_text(json.dumps(result), encoding="utf-8")

    def run_pass(self, poster=None):
        return ingest.run_pass(self.conn, self.results, self.rubric, poster or self.poster, BASE)

    def write_shot_4(self):
        for take_id in ("4-1", "4-3", "4-4", "4-6"):
            self.write(clean_result(take_id))
        self.write(SAMPLE)
        self.write(flicker_result())

    def status(self, take_id):
        return self.conn.execute("SELECT status FROM takes WHERE take_id = ?", (take_id,)).fetchone()["status"]

    def kinds(self, take_id):
        return [r["kind"] for r in self.conn.execute(
            "SELECT kind FROM events WHERE take_id = ? ORDER BY event_id", (take_id,))]


class TablesTest(AgentTestCase):
    def test_the_five_tables_have_the_contract_columns(self):
        for table in ("takes", "issues", "prompt_versions", "approvals", "events"):
            columns = [row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")]
            self.assertEqual(columns, contract_columns(table), table)

    def test_event_kinds_match_the_contract(self):
        listed = re.search(r"Event kinds: (.*)\.", contract_section("SQLite tables")).group(1)
        self.assertEqual(tuple(re.findall(r"`([a-z_]+)`", listed)), db.EVENT_KINDS)

    def test_unknown_event_kind_is_refused(self):
        with self.assertRaises(ValueError):
            db.log_event(self.conn, "automation", "card_edited")


class IngestTest(AgentTestCase):
    def test_the_sample_result_goes_in_once(self):
        self.write(SAMPLE)
        report = self.run_pass()
        self.assertEqual(report.ingested, ["4-2"])
        take = self.conn.execute("SELECT * FROM takes WHERE take_id = '4-2'").fetchone()
        self.assertIsNone(take["parent_take_id"])
        self.assertEqual((take["shot"], take["expected_takes"], take["result_revision"]), (4, 6, 2))
        self.assertEqual((take["fps"], take["frames"]), (24, 120))
        self.assertEqual(take["brightness_series"], "frames/4-2/brightness.json")
        self.assertEqual(take["status"], "needs_confirmation")

        issue = self.conn.execute("SELECT * FROM issues").fetchone()
        self.assertEqual(issue["issue_id"], "4-2-1")
        self.assertEqual((issue["issue_type"], issue["state"]), ("road_markings", "candidate"))
        self.assertEqual((issue["frame_start"], issue["frame_end"], issue["t_start"], issue["t_end"]),
                         (48, 64, 2.0, 2.667))
        self.assertEqual(issue["model_note"], SAMPLE["candidates"][0]["model_note"])
        self.assertIsNone(issue["value"])
        self.assertEqual(json.loads(issue["evidence"]), SAMPLE["candidates"][0]["evidence"])

        version = self.conn.execute("SELECT * FROM prompt_versions").fetchone()
        self.assertEqual((version["version"], version["prompt"]), (1, SAMPLE["generation"]["prompt"]))
        self.assertEqual(version["prompt_hash"], V1_HASH)
        self.assertEqual(version["fixes"], "[]")
        self.assertEqual(self.kinds("4-2"), ["take_seen", "result_ingested", "status_changed"])

        # Rule 1: the same revision again is skipped, and nothing is written twice.
        events = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(self.run_pass().ingested, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 1)

    def test_revision_1_then_final(self):
        first = copy.deepcopy(SAMPLE)
        first.update(revision=1, candidates=[], checks={"exposure_flicker": "running", "road_markings": "running"})
        self.write(first)
        self.assertEqual(self.run_pass().updated, ["4-2"])
        self.assertEqual(self.status("4-2"), "review_pending")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM prompt_versions").fetchone()[0], 0)

        self.write(SAMPLE)
        self.assertEqual(self.run_pass().ingested, ["4-2"])
        self.assertEqual(self.status("4-2"), "needs_confirmation")
        self.assertEqual(self.kinds("4-2"), ["take_seen", "result_ingested", "status_changed"])

    def test_an_older_revision_is_skipped(self):
        self.write(SAMPLE)
        self.run_pass()
        older = copy.deepcopy(SAMPLE)
        older.update(revision=1, candidates=[])
        self.write(older)
        self.run_pass()
        self.assertEqual(self.conn.execute("SELECT result_revision FROM takes").fetchone()[0], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0], 1)

    def test_tmp_and_foreign_files_are_skipped(self):
        self.write(SAMPLE, name="4-2.json.tmp")
        self.write({"hello": "world"}, name="notes.json")
        report = self.run_pass()
        self.assertEqual(report.ingested, [])
        self.assertEqual(report.skipped, ["notes.json: not a result file"])

    def test_a_check_error_keeps_the_take_pending(self):
        result = copy.deepcopy(SAMPLE)
        result.update(candidates=[], checks={"exposure_flicker": "done", "road_markings": "error"},
                      errors=["road_markings: reviewer not available"])
        self.write(result)
        self.run_pass()
        self.assertEqual(self.status("4-2"), "review_pending")
        self.assertEqual(db.errored_checks(self.conn, "4-2"), ["road_markings"])
        self.assertIn("check_error", self.kinds("4-2"))

    def test_measured_anomaly_flags_the_take(self):
        self.write(flicker_result())
        self.run_pass()
        self.assertEqual(self.status("4-5"), "flagged")
        issue = self.conn.execute("SELECT * FROM issues").fetchone()
        self.assertEqual((issue["state"], issue["value"], issue["threshold"]), ("measured", 18.4, 8.0))
        self.assertIsNone(issue["model_note"])

    def test_a_retake_is_linked_to_its_parent(self):
        self.write(clean_result("4-2r1"))
        self.run_pass()
        row = self.conn.execute("SELECT parent_take_id, status FROM takes WHERE take_id = '4-2r1'").fetchone()
        self.assertEqual((row["parent_take_id"], row["status"]), ("4-2", "clean"))


class StatusRuleTest(AgentTestCase):
    """The rule as #10's actions will drive it: change a row, then recompute."""

    def setUp(self):
        super().setUp()
        self.write(SAMPLE)
        self.run_pass()

    def decide_note(self, state):
        self.conn.execute("UPDATE issues SET state = ?, decided_by = 'Creative director', decided_at = ? "
                          "WHERE issue_id = '4-2-1'", (state, db.now()))
        return db.recompute_status(self.conn, "4-2", "Creative director")

    def test_confirming_a_note_flags_the_take(self):
        self.assertEqual(self.decide_note("confirmed"), ("needs_confirmation", "flagged"))
        detail = json.loads(self.conn.execute(
            "SELECT detail FROM events WHERE kind = 'status_changed' ORDER BY event_id DESC").fetchone()[0])
        self.assertEqual(detail, {"from": "needs_confirmation", "to": "flagged"})

    def test_dismissing_every_note_makes_it_clean(self):
        self.assertEqual(self.decide_note("dismissed"), ("needs_confirmation", "clean"))

    def test_a_decision_wins_over_issues(self):
        self.decide_note("confirmed")
        self.conn.execute("INSERT INTO approvals (take_id, decision, approver, decided_at) "
                          "VALUES ('4-2', 'accepted_as_is', 'Creative director', ?)", (db.now(),))
        self.assertEqual(db.recompute_status(self.conn, "4-2", "Creative director"), ("flagged", "accepted_as_is"))

    def test_no_change_logs_nothing(self):
        before = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertEqual(db.recompute_status(self.conn, "4-2", "automation"),
                         ("needs_confirmation", "needs_confirmation"))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)


class CardTest(AgentTestCase):
    def test_shot_card_matches_the_contract_sample_and_posts_once(self):
        self.write_shot_4()
        report = self.run_pass()
        self.assertEqual(report.cards, ["shot 4"])
        self.assertEqual(self.poster.cards, [CARD_SAMPLES[0]])
        posted = self.conn.execute("SELECT shot, detail FROM events WHERE kind = 'card_posted'").fetchall()
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["shot"], 4)
        self.assertEqual(json.loads(posted[0]["detail"]), {"card": "shot", "ts": "1730000000.000001"})
        self.run_pass()
        self.assertEqual(len(self.poster.cards), 1)

    def test_no_shot_card_until_every_expected_take_is_final(self):
        for take_id in ("4-1", "4-3", "4-4"):
            self.write(clean_result(take_id))
        self.write(SAMPLE)
        pending = flicker_result()
        pending.update(revision=1, checks={"exposure_flicker": "running", "road_markings": "running"})
        self.write(pending)
        self.run_pass()
        self.assertEqual(self.poster.cards, [])

    def test_retake_card_matches_the_contract_sample(self):
        self.write(clean_result("4-2r1"))
        self.assertEqual(self.run_pass().cards, ["retake 4-2r1"])
        self.assertEqual(self.poster.cards, [CARD_SAMPLES[1]])

    def test_a_check_error_shows_in_place_of_the_status(self):
        self.write_shot_4()
        broken = copy.deepcopy(SAMPLE)
        broken.update(candidates=[], checks={"exposure_flicker": "done", "road_markings": "error"})
        self.write(broken)
        self.run_pass()
        self.assertIn("4-2: check error (road markings)", self.poster.cards[0].splitlines())

    def test_unconfigured_slack_waits_then_posts(self):
        self.write(clean_result("4-2r1"))
        report = self.run_pass(poster=slack.unconfigured_poster)
        self.assertEqual((report.cards, report.unposted), ([], ["retake 4-2r1"]))
        self.assertEqual(self.run_pass().cards, ["retake 4-2r1"])

    def test_cards_never_carry_prompts_notes_or_files(self):
        self.write_shot_4()
        self.write(clean_result("4-2r1"))
        self.run_pass()
        text = "\n".join(self.poster.cards)
        for forbidden in (SAMPLE["generation"]["prompt"], SAMPLE["candidates"][0]["model_note"],
                          "frames/", ".jpg", ".mp4", "sha256:", "Creative director", "Vela"):
            self.assertNotIn(forbidden, text)

    def test_the_builder_refuses_fields_outside_the_contract(self):
        take = {"take_id": "4-2", "status": "clean", "counts": [], "check_errors": []}
        with self.assertRaises(card.CardError):
            card.build_shot_card(self.rubric, shot=4, expected_takes=6, takes=[take],
                                 link=f"{BASE}/shot/4", prompt="The Vela drives")
        with self.assertRaises(card.CardError):
            card.build_shot_card(self.rubric, shot=4, expected_takes=6, link=f"{BASE}/shot/4",
                                 takes=[{**take, "model_note": "arrow points at the camera"}])
        with self.assertRaises(card.CardError):
            card.build_retake_card(self.rubric, take_id="4-2r1", parent_take_id="4-2", status="clean",
                                   link=f"{BASE}/take/4-2r1", frames=["frames/4-2r1/000001.jpg"])

    def test_the_builder_refuses_bad_values(self):
        take = {"take_id": "4-2", "status": "clean"}
        for bad in ({**take, "take_id": "The Vela drives"}, {**take, "status": "great"},
                    {**take, "counts": [("candidate", "a free-text note")]}):
            with self.assertRaises(card.CardError):
                card.build_shot_card(self.rubric, shot=4, expected_takes=6, takes=[bad], link=f"{BASE}/shot/4")
        with self.assertRaises(card.CardError):
            card.build_shot_card(self.rubric, shot=4, expected_takes=6, takes=[take],
                                 link="https://example.com/shot/4")


class SlackPosterTest(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.reply = {"ok": True, "ts": "1730000000.000100"}
        original = urllib.request.urlopen

        def fake_urlopen(request, timeout=None):
            self.sent.append(request)

            class Response:
                def __enter__(inner):
                    return inner

                def __exit__(inner, *args):
                    return False

                def read(inner):
                    return json.dumps(self.reply).encode()

            return Response()

        urllib.request.urlopen = fake_urlopen
        self.addCleanup(setattr, urllib.request, "urlopen", original)

    def test_posts_only_the_channel_and_text(self):
        post = slack.slack_poster("test-token", "C0123")
        self.assertEqual(post("ClearFrame · Shot 4"), "1730000000.000100")
        request = self.sent[0]
        self.assertEqual(request.full_url, slack.SLACK_POST_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(json.loads(request.data),
                         {"channel": "C0123", "text": "ClearFrame · Shot 4", "unfurl_links": False})

    def test_a_refusal_raises_without_the_card_text(self):
        self.reply = {"ok": False, "error": "channel_not_found"}
        with self.assertRaises(slack.SlackError) as caught:
            slack.slack_poster("test-token", "C0123")("ClearFrame · Shot 4")
        self.assertEqual(str(caught.exception), "Slack refused the post: channel_not_found")


if __name__ == "__main__":
    unittest.main()
