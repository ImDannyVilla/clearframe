"""#7 checklist proofs that tests/test_agent.py doesn't already carry: the
configured paths and defaults, the db's first-run behaviour, the retake half of
ingest rule 5, the dev rerun (rule 4: move the db aside, never delete it), the
poller picking up Slack settings without a restart, and the status rule on the
decision path through agent/approval.py.

Everything runs on contracts/samples/ and temp dirs: no footage, no model,
no network.
"""
import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agent"))

from agent import db, ingest, poller, slack  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402
import approval  # noqa: E402

CONTRACT = (ROOT / "contracts" / "README.md").read_text(encoding="utf-8")
SAMPLE = json.loads((ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8"))
BASE = "http://127.0.0.1:8090"
APPROVER = "Creative director"


def clean_result(take_id, revision=2):
    """A final result with nothing found, derived from the contract sample."""
    result = copy.deepcopy(SAMPLE)
    shot, _, rest = take_id.partition("-")
    result.update(take_id=take_id, shot=int(shot), revision=revision, candidates=[], measured=[])
    result["parent_take_id"] = take_id.split("r")[0] if "r" in rest else None
    result["render"]["file"] = f"{take_id}.mp4"
    result["brightness_series"] = f"frames/{take_id}/brightness.json"
    return result


class Recorder:
    """A poster that records cards instead of sending them."""

    def __init__(self):
        self.cards = []

    def __call__(self, text):
        self.cards.append(text)
        return f"1730000000.{len(self.cards):06d}"


class RulesCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-rules-"))
        self.results = self.tmp / "results"
        self.results.mkdir()
        self.db_path = self.tmp / "state" / "clearframe.db"
        self.conn = db.connect(self.db_path)
        self.rubric = rubric_loader.load()
        self.poster = Recorder()

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, result, name=None):
        (self.results / (name or f"{result['take_id']}.json")).write_text(
            json.dumps(result), encoding="utf-8")

    def run_pass(self, poster=None):
        return ingest.run_pass(self.conn, self.results, self.rubric, poster or self.poster, BASE)


class SettingsTest(RulesCase):
    """Checklist 1 and 2: where the automation reads from and writes to."""

    def test_defaults_match_the_contract_paths(self):
        self.assertIn("`~/clearframe/results/` (host)", CONTRACT)
        self.assertIn("/sandbox/clearframe/state/clearframe.db", CONTRACT)
        with mock.patch.dict(os.environ, {}, clear=True):
            results, db_path, rubric_path, base_url = ingest.settings_from_env()
        self.assertEqual(results, Path("/sandbox/clearframe/host/results"))
        self.assertEqual(db_path, Path("/sandbox/clearframe/state/clearframe.db"))
        self.assertEqual(db.DEFAULT_DB, Path("/sandbox/clearframe/state/clearframe.db"))
        self.assertEqual(ingest.DEFAULT_RESULTS, Path("/sandbox/clearframe/host/results"))
        self.assertEqual(rubric_path, ROOT / "rubric" / "rubric.yaml")
        self.assertEqual(base_url, "http://127.0.0.1:8090")

    def test_every_path_is_configurable_by_environment(self):
        env = {
            "CLEARFRAME_RESULTS": str(self.tmp / "elsewhere" / "results"),
            "CLEARFRAME_DB": str(self.tmp / "elsewhere" / "clearframe.db"),
            "CLEARFRAME_RUBRIC": str(self.tmp / "elsewhere" / "rubric.yaml"),
            "EVIDENCE_BASE_URL": "http://gb10-box:8090",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            results, db_path, rubric_path, base_url = ingest.settings_from_env()
        self.assertEqual((results, db_path, rubric_path, base_url),
                         (Path(env["CLEARFRAME_RESULTS"]), Path(env["CLEARFRAME_DB"]),
                          Path(env["CLEARFRAME_RUBRIC"]), env["EVIDENCE_BASE_URL"]))

    def test_connect_creates_the_folder_and_five_tables_on_first_run(self):
        fresh = self.tmp / "brand" / "new" / "clearframe.db"
        self.assertFalse(fresh.exists())
        conn = db.connect(fresh)
        try:
            tables = {row["name"] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'")}
            # The five contract tables, created on first run. (#10 may add its
            # own, so this is a subset check, not an exact one.)
            self.assertLessEqual({"takes", "issues", "prompt_versions", "approvals", "events"}, tables)
        finally:
            conn.close()

    def test_the_poster_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIs(slack.poster_from_env(), slack.unconfigured_poster)
        with mock.patch.dict(os.environ, {"CLEARFRAME_SLACK": "print"}, clear=True):
            self.assertIs(slack.poster_from_env(), slack.print_poster)
        with mock.patch.dict(os.environ, {"SLACK_BOT_TOKEN": "t", "SLACK_CHANNEL": "C1"}, clear=True):
            poster = slack.poster_from_env()
        self.assertTrue(callable(poster))
        self.assertIsNot(poster, slack.print_poster)
        self.assertIsNot(poster, slack.unconfigured_poster)


class RetakeCardRuleTest(RulesCase):
    """Ingest rule 5's retake half: the card waits for the retake's own final
    result, posts once, and each post is a card_posted event. (The shot-card
    half is tests.test_agent.CardTest.)"""

    def test_no_retake_card_until_its_own_result_is_final_then_once(self):
        running = clean_result("4-2r1", revision=1)
        running["checks"] = {"exposure_flicker": "running", "road_markings": "running"}
        self.write(running)
        report = self.run_pass()
        self.assertEqual((report.cards, report.unposted), ([], []))
        self.assertEqual(self.poster.cards, [])
        self.assertFalse(db.has_event(self.conn, "card_posted", take_id="4-2r1"))

        self.write(clean_result("4-2r1"))  # revision 2, every check done
        report = self.run_pass()
        self.assertEqual(report.cards, ["retake 4-2r1"])
        self.assertEqual(len(self.poster.cards), 1)
        posted = self.conn.execute(
            "SELECT kind, shot, detail FROM events WHERE take_id = '4-2r1' AND kind = 'card_posted'"
        ).fetchall()
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["shot"], 4)
        detail = json.loads(posted[0]["detail"])
        self.assertEqual(detail["card"], "retake")
        self.assertIn("ts", detail)

        # The retake card is not posted again on later passes.
        self.assertEqual(self.run_pass().cards, [])
        self.assertEqual(len(self.poster.cards), 1)


class ResetTest(RulesCase):
    """Ingest rule 4, the dev rerun: the db is MOVED aside, never deleted."""

    def seed_db(self):
        self.write(SAMPLE)
        self.run_pass()
        self.conn.close()
        self.conn = db.connect(self.db_path)

    def test_move_db_aside_keeps_every_row(self):
        self.seed_db()
        events_before = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.assertGreater(events_before, 0)
        aside = poller.move_db_aside(self.db_path)
        try:
            self.assertIsNotNone(aside)
            self.assertFalse(self.db_path.exists())          # moved away...
            self.assertTrue(aside.is_file())                 # ...not gone
            self.assertNotEqual(aside, self.db_path)
            check = db.connect(aside)                        # the copy is intact
            try:
                self.assertEqual(check.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                                 events_before)
            finally:
                check.close()
            # The automation rebuilds from the result files on the next pass.
            rebuilt = db.connect(self.db_path)
            try:
                report = ingest.run_pass(rebuilt, self.results, self.rubric, self.poster, BASE)
                self.assertEqual(report.ingested, ["4-2"])
                self.assertEqual(rebuilt.execute("SELECT COUNT(*) FROM events").fetchone()[0],
                                 events_before)
            finally:
                rebuilt.close()
        finally:
            self.conn = db.connect(self.db_path)  # tearDown closes it

    def test_move_db_aside_without_a_db_is_a_no_op(self):
        missing = self.tmp / "no-such-folder" / "clearframe.db"
        self.assertIsNone(poller.move_db_aside(missing))
        self.assertFalse(missing.parent.exists())

    def test_poller_reset_moves_the_configured_db_and_exits(self):
        self.seed_db()
        env = {"CLEARFRAME_DB": str(self.db_path), "CLEARFRAME_RESULTS": str(self.results)}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out):
            self.assertEqual(poller.main(["--reset"]), 0)
        self.assertFalse(self.db_path.exists())
        aside = list((self.tmp / "state").glob("clearframe.db.*.bak"))
        self.assertEqual(len(aside), 1, out.getvalue())
        self.assertIn("moved", out.getvalue())


class PollerPosterTest(RulesCase):
    """The poller re-reads the Slack settings every pass: cards go out on the
    first pass after Slack is set, without a restart (agent/README.md)."""

    def test_a_card_waits_then_posts_once_slack_is_configured(self):
        self.write(clean_result("4-2r1"))
        env = {"CLEARFRAME_DB": str(self.db_path), "CLEARFRAME_RESULTS": str(self.results),
               "POLL_SECONDS": "5"}
        calls = []

        def fake_sleep(_seconds):
            calls.append(1)
            if len(calls) == 1:
                os.environ["CLEARFRAME_SLACK"] = "print"  # Slack "set" mid-run
            else:
                raise KeyboardInterrupt  # stop the loop after the second pass

        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch("time.sleep", fake_sleep), redirect_stdout(out):
            with self.assertRaises(KeyboardInterrupt):
                poller.main([])
        text = out.getvalue()
        self.assertIn("Slack is not configured", text)
        self.assertIn("not posted", text)
        self.assertIn("CLEARFRAME_SLACK=print", text)  # the card itself was printed
        self.assertIn("posted retake 4-2r1", text)
        self.assertTrue(db.has_event(self.conn, "card_posted", take_id="4-2r1"))


class StatusAfterDecisionTest(unittest.TestCase):
    """Checklist 4 on the decision path: agent/approval.py recomputes the status
    after every action, logs status_changed exactly when it moved, and nothing
    when it didn't. (After-ingest and db-level no-op coverage lives in
    tests.test_agent; action sequences in tests.test_approval.)"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-decide-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.store = approval.Store(self.tmp / "clearframe.db")
        self.addCleanup(self.store.close)
        self.rubric = rubric_loader.load()

    def status_changed_events(self):
        return [e for e in self.store.events("4-2", kinds=["status_changed"])]

    def test_every_action_recomputes_and_logs_only_real_changes(self):
        approval.load_result(self.store, ROOT / "contracts" / "samples" / "result-4-2.json",
                             self.rubric)
        # Ingest-side: the sample lands on needs_confirmation with one change logged.
        self.assertEqual(self.store.take("4-2")["status"], "needs_confirmation")
        self.assertEqual(len(self.status_changed_events()), 1)

        approval.confirm_note(self.store, "4-2-1", APPROVER, self.rubric)
        self.assertEqual(self.store.take("4-2")["status"], "flagged")
        changes = self.status_changed_events()
        self.assertEqual(len(changes), 2)
        self.assertEqual(json.loads(changes[-1]["detail"]),
                         {"old": "needs_confirmation", "new": "flagged"})

        # A recompute that changes nothing logs nothing.
        approval.recompute_status(self.store, "4-2")
        self.assertEqual(len(self.status_changed_events()), 2)

        approval.accept_as_is(self.store, "4-2", APPROVER)
        self.assertEqual(self.store.take("4-2")["status"], "accepted_as_is")
        changes = self.status_changed_events()
        self.assertEqual(len(changes), 3)
        self.assertEqual(json.loads(changes[-1]["detail"]),
                         {"old": "flagged", "new": "accepted_as_is"})


if __name__ == "__main__":
    unittest.main()
