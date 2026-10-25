"""The evidence page (#8): what it shows, what it sends to the four actions, and what it never writes.

The database is built from contracts/samples/ through the #7 ingest; the four
actions (#10) are a recorder, so no approval code is needed.
"""
import copy
import hashlib
import http.client
import json
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import db, ingest  # noqa: E402
from evidence import actions, server  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402

SAMPLE = json.loads((ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8"))
V2_PROMPT = SAMPLE["generation"]["prompt"] + (
    "\n\nMake sure: lane arrows point in the direction of travel and lane lines stay continuous.")
V2_HASH = "sha256:08e2b485acb99acac2ef6705b87465634da1c9a1c66d86c816d1ba1b07cf1ed6"


def result_for(take_id, measured=(), candidates=()):
    result = copy.deepcopy(SAMPLE)
    result.update(take_id=take_id, measured=list(measured), candidates=list(candidates),
                  brightness_series=f"frames/{take_id}/brightness.json")
    result["render"]["file"] = f"{take_id}.mp4"
    return result


FLICKER = {
    "issue_type": "exposure_flicker", "frame_start": 61, "frame_end": 63, "t_start": 2.542, "t_end": 2.625,
    "value": 18.4, "threshold": 8.0, "unit": "mean brightness change between consecutive frames, 0-255",
    "evidence": ["frames/4-5/000060.jpg", "frames/4-5/000061.jpg"],
}


class Recorder:
    """Stands in for #10's four actions: records each call, or refuses with a set reason."""

    def __init__(self):
        self.calls = []
        self.refuse = None

    def __call__(self, action, **fields):
        self.calls.append((action, fields))
        if self.refuse:
            raise actions.Refused(self.refuse)


def tree_digest(folder: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in folder.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(folder)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


class EvidenceCase(unittest.TestCase):
    """A running page on a database built by the #7 ingest; actions go to a recorder."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-evidence-"))
        self.results = self.tmp / "results"
        self.db_path = self.tmp / "state" / "clearframe.db"
        for take_id in ("4-1", "4-3", "4-4", "4-6"):
            self.write_result(result_for(take_id))
        self.write_result(SAMPLE)
        self.write_result(result_for("4-5", measured=[FLICKER]))
        conn = db.connect(self.db_path)
        ingest.run_pass(conn, self.results, rubric_loader.load(), lambda text: "ts", "http://127.0.0.1:8090")
        conn.close()

        self.recorder = Recorder()
        self.config = server.Config(db_path=self.db_path, results_dir=self.results, port=0,
                                    actions=self.recorder, token="test-token")
        self.httpd = server.serve(self.config)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_result(self, result):
        take_id = result["take_id"]
        frames = self.results / "frames" / take_id
        frames.mkdir(parents=True, exist_ok=True)
        series = [100.0] * 120
        if result["measured"]:
            series[61:64] = [118.4] * 3
        (frames / "brightness.json").write_text(json.dumps(series), encoding="utf-8")
        for item in result["measured"] + result["candidates"]:
            for path in item["evidence"]:
                (self.results / path).write_bytes(b"\xff\xd8\xff\xe0 fake jpeg")
        (self.results / f"{take_id}.json").write_text(json.dumps(result), encoding="utf-8")

    def request(self, method, path, form=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        body = urllib.parse.urlencode(form) if form is not None else None
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        conn.close()
        return response.status, dict(response.getheaders()), data.decode("utf-8", "replace")

    def get(self, path):
        status, _, text = self.request("GET", path)
        self.assertEqual(status, 200, path)
        return text

    def add_version_2(self):
        """What #10 will do: confirm the note, add version 2, recompute the status."""
        conn = db.connect(self.db_path)
        with conn:
            conn.execute("UPDATE issues SET state = 'confirmed', decided_by = 'Creative director', decided_at = ? "
                         "WHERE issue_id = '4-2-1'", (db.now(),))
            conn.execute("INSERT INTO prompt_versions VALUES ('4-2', 2, ?, ?, '[\"road_markings\"]', ?)",
                         (V2_PROMPT, V2_HASH, db.now()))
            db.recompute_status(conn, "4-2", "Creative director")
        conn.close()

    def post(self, action, **fields):
        return self.request("POST", f"/action/{action}", {"token": "test-token", **fields})


class EvidencePageTest(EvidenceCase):
    # The pages ----------------------------------------------------------------------------

    def test_shot_page_lists_every_take_with_its_status(self):
        text = self.get("/shot/4")
        for take_id in ("4-1", "4-2", "4-3", "4-4", "4-5", "4-6"):
            self.assertIn(f"href=\"/take/{take_id}\"", text)
        self.assertIn("needs confirmation", text)
        self.assertIn("status-flagged", text)
        self.assertIn("6 of 6 expected takes", text)
        self.assertIn("Shot 4", self.get("/"))

    def test_take_page_shows_the_candidate_note_and_its_buttons(self):
        text = self.get("/take/4-2")
        self.assertIn(SAMPLE["candidates"][0]["model_note"].replace("'", "&#x27;"), text)
        self.assertIn("frames 48 to 64 (2.000 s to 2.667 s)", text)
        for path in SAMPLE["candidates"][0]["evidence"]:
            self.assertIn(f"src=\"/results/{path}\"", text)
        self.assertIn("action=\"/action/confirm_note\"", text)
        self.assertIn("formaction=\"/action/dismiss_note\"", text)
        self.assertIn("name=\"issue_id\" value=\"4-2-1\"", text)
        self.assertIn("<option>Creative director</option>", text)
        self.assertIn("Accept as is", text)
        self.assertNotIn("action=\"/action/approve_regeneration\"", text)  # needs_confirmation: no approve yet
        self.assertIn("ee4aa7f4a4c28994d974159b541e8f8b372c29b03ade2ad7098b62c6ee08b89d", text)

    def test_measured_anomaly_shows_its_frames_and_the_plot(self):
        text = self.get("/take/4-5")
        self.assertIn("Measured 18.4 against a threshold of 8", text)
        self.assertIn("src=\"/results/frames/4-5/000060.jpg\"", text)
        self.assertIn("<svg role=\"img\"", text)
        self.assertIn("threshold 8", text)
        self.assertIn("The largest change is 18.4", text)

    def test_events_are_the_audit_trail_in_time_order(self):
        text = self.get("/take/4-2")
        trail = text[text.index("id=\"events\""):]
        positions = [trail.index(kind) for kind in ("take_seen", "result_ingested", "status_changed")]
        self.assertEqual(positions, sorted(positions))

    def test_prompt_diff_and_approve_carry_the_latest_version(self):
        self.add_version_2()
        text = self.get("/take/4-2")
        self.assertIn("Version 1 to version 2 (fixes: road markings)", text)
        self.assertIn("<ins class=\"add\">+ Make sure: lane arrows point in the direction of travel", text)
        self.assertIn(V2_HASH, text)
        approve = text[text.index("action=\"/action/approve_regeneration\""):]
        approve = approve[:approve.index("</form>")]
        self.assertIn("name=\"take_id\" value=\"4-2\"", approve)
        self.assertIn("name=\"prompt_version\" value=\"2\"", approve)
        self.assertIn(f"name=\"prompt_hash\" value=\"{V2_HASH}\"", approve)

    def test_model_words_are_escaped(self):
        conn = db.connect(self.db_path)
        with conn:
            conn.execute("UPDATE issues SET model_note = '<script>alert(1)</script>' WHERE issue_id = '4-2-1'")
        conn.close()
        text = self.get("/take/4-2")
        self.assertNotIn("<script>alert(1)</script>", text)
        self.assertIn("&lt;script&gt;", text)

    def test_unknown_pages_and_no_database(self):
        self.assertEqual(self.request("GET", "/take/9-9")[0], 404)
        self.assertEqual(self.request("GET", "/shot/9")[0], 404)
        self.db_path.rename(self.db_path.with_suffix(".aside"))
        self.assertIn("No database yet", self.get("/shot/4"))

    # The four actions ---------------------------------------------------------------------

    def test_confirm_sends_exactly_the_contract_fields(self):
        status, headers, _ = self.post("confirm_note", issue_id="4-2-1", approver="Creative director",
                                       extra="ignored")
        self.assertEqual(status, 303)
        self.assertTrue(headers["Location"].startswith("/take/4-2?done="))
        self.assertEqual(self.recorder.calls,
                         [("confirm_note", {"issue_id": "4-2-1", "approver": "Creative director"})])

    def test_approve_sends_version_and_hash(self):
        self.add_version_2()
        self.post("approve_regeneration", take_id="4-2", prompt_version="2", prompt_hash=V2_HASH,
                  approver="Creative director")
        self.assertEqual(self.recorder.calls, [("approve_regeneration", {
            "take_id": "4-2", "prompt_version": 2, "prompt_hash": V2_HASH, "approver": "Creative director"})])

    def test_a_refusal_shows_its_reason(self):
        self.recorder.refuse = "the prompt changed after the page showed it"
        status, headers, _ = self.post("accept_as_is", take_id="4-2", approver="Creative director")
        self.assertEqual(status, 303)
        location = headers["Location"]
        self.assertIn("refused=", location)
        self.assertIn("Refused: the prompt changed after the page showed it", self.get(location))

    def test_posts_need_the_form_token_and_the_loopback_host(self):
        status, _, _ = self.request("POST", "/action/confirm_note",
                                    {"issue_id": "4-2-1", "approver": "Creative director"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("POST", "/action/confirm_note",
                                    {"token": "test-token", "issue_id": "4-2-1", "approver": "Creative director"},
                                    host="evil.example:8090")
        self.assertEqual(status, 403)
        self.assertEqual(self.request("GET", "/shot/4", host="evil.example")[0], 403)
        self.assertEqual(self.post("drop_table", take_id="4-2")[0], 404)
        self.assertEqual(self.recorder.calls, [])

    def test_the_page_never_writes_sqlite_or_results(self):
        before = (tree_digest(self.results), self.db_path.read_bytes())
        for path in ("/", "/shot/4", "/take/4-2", "/take/4-5", "/results/frames/4-5/000060.jpg"):
            self.get(path)
        self.post("confirm_note", issue_id="4-2-1", approver="Creative director")
        self.assertEqual((tree_digest(self.results), self.db_path.read_bytes()), before)

    # Files --------------------------------------------------------------------------------

    def test_serves_only_evidence_jpegs_inside_results(self):
        status, headers, _ = self.request("GET", "/results/frames/4-5/000060.jpg")
        self.assertEqual((status, headers["Content-Type"]), (200, "image/jpeg"))
        for path in ("/results/4-2.json", "/results/frames/4-2/brightness.json",
                     "/results/../state/clearframe.db", "/results/%2e%2e/state/clearframe.db",
                     "/results/frames/4-5/missing.jpg"):
            self.assertEqual(self.request("GET", path)[0], 404, path)


class RealActionsTest(EvidenceCase):
    """The page driving #10's real `agent.approval` on the #7 ingest's database."""

    def setUp(self):
        super().setUp()
        self.queue = self.tmp / "approved_generation_queue"
        self.config.actions = actions.approval_actions(self.db_path, ROOT / "rubric" / "rubric.yaml", self.queue)

    def seed_generation(self):
        """#10 reads tool and model from its `generation` table; seed it as an ingest would."""
        from agent import approval
        store = approval.Store(self.db_path)
        store.conn.execute("INSERT INTO generation (take_id, tool, model, seed) VALUES (?, ?, ?, ?)",
                           ("4-2", SAMPLE["generation"]["tool"], SAMPLE["generation"]["model"],
                            json.dumps(SAMPLE["generation"]["seed"])))
        store.close()

    def test_confirm_on_the_page_flags_the_take_and_prompt_fix_adds_version_2(self):
        status, headers, _ = self.post("confirm_note", issue_id="4-2-1", approver="Creative director")
        self.assertEqual((status, headers["Location"].split("?")[0]), (303, "/take/4-2"))
        text = self.get(headers["Location"])
        self.assertIn("confirm note: done", text)
        self.assertIn("status-flagged", text)
        self.assertIn("Version 1 to version 2 (fixes: road markings)", text)
        self.assertIn(V2_HASH, text)
        self.assertIn("note_confirmed", text)

    def test_approve_on_the_page_writes_the_contract_queue_file(self):
        self.seed_generation()
        self.post("confirm_note", issue_id="4-2-1", approver="Creative director")
        status, headers, _ = self.post("approve_regeneration", take_id="4-2", prompt_version="2",
                                       prompt_hash=V2_HASH, approver="Creative director")
        self.assertIn("done=", headers["Location"])
        queued = json.loads((self.queue / "4-2-v2.json").read_text(encoding="utf-8"))
        expected = json.loads((ROOT / "contracts" / "samples" / "queue-4-2-v2.json").read_text(encoding="utf-8"))
        for key in ("schema", "request_id", "parent_take_id", "next_take_id", "shot", "prompt_version",
                    "prompt_hash", "prompt", "fixes", "generation"):
            self.assertEqual(queued[key], expected[key], key)
        text = self.get("/take/4-2")
        self.assertIn("approved for regeneration by Creative director", text)
        self.assertIn("queued as 4-2-v2.json, returns as 4-2r1", text)

    def test_a_stale_hash_is_refused_with_its_reason(self):
        self.seed_generation()
        self.post("confirm_note", issue_id="4-2-1", approver="Creative director")
        stale = "sha256:" + "0" * 64
        _, headers, _ = self.post("approve_regeneration", take_id="4-2", prompt_version="2",
                                  prompt_hash=stale, approver="Creative director")
        text = self.get(headers["Location"])
        self.assertIn("Refused:", text)
        self.assertIn("approval_refused", text)
        self.assertFalse((self.queue / "4-2-v2.json").exists())

    def test_an_unlisted_approver_is_refused(self):
        self.seed_generation()
        self.post("confirm_note", issue_id="4-2-1", approver="Creative director")
        _, headers, _ = self.post("approve_regeneration", take_id="4-2", prompt_version="2",
                                  prompt_hash=V2_HASH, approver="Somebody else")
        self.assertIn("Refused:", self.get(headers["Location"]))
        self.assertFalse((self.queue / "4-2-v2.json").exists())


class ActionsBridgeTest(unittest.TestCase):
    def test_fields_must_be_exactly_the_contract_fields(self):
        call = actions.approval_actions(Path("unused.db"), ROOT / "rubric" / "rubric.yaml", Path("unused"))
        for action, fields in (("confirm_note", {"issue_id": "4-2-1"}),
                               ("accept_as_is", {"take_id": "4-2", "approver": "x", "prompt_hash": "y"}),
                               ("delete_take", {"take_id": "4-2"})):
            with self.assertRaises(actions.Refused) as caught:
                call(action, **fields)
            self.assertIn("not a contract action", str(caught.exception))

    def test_the_four_actions_match_the_contract(self):
        contract = (ROOT / "contracts" / "README.md").read_text(encoding="utf-8")
        table = contract[contract.index("**The four actions.**"):contract.index("**Checks before a queue file")]
        self.assertIn("| Confirm note | `issue_id`, `approver` |", table)
        self.assertIn("| Dismiss note | `issue_id`, `approver` |", table)
        self.assertIn("| Accept as is | `take_id`, `approver` |", table)
        self.assertIn("| Approve regeneration | `take_id`, `prompt_version`, `prompt_hash`, `approver` |", table)
        self.assertEqual(actions.FIELDS["approve_regeneration"],
                         ("take_id", "prompt_version", "prompt_hash", "approver"))

    def test_the_server_refuses_to_listen_beyond_loopback(self):
        with self.assertRaises(SystemExit):
            server.serve(server.Config(host="0.0.0.0", port=0))


if __name__ == "__main__":
    unittest.main()
