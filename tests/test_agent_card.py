"""The Slack status card (#7): exact contract wording, post-once behaviour, the
contract's field list and nothing else, and a transport that never surprises.

Laptop tests: temp files and a stubbed urlopen, no network, no footage, no model.
"""
import io
import json
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import card, db, ingest  # noqa: E402
from rubric import rubric as rubric_loader  # noqa: E402

from agent_harness import CANDIDATE_ROAD, MEASURED_FLICKER, SAMPLE, write_final_take  # noqa: E402

RUBRIC = rubric_loader.load()

SAMPLE_SHOT_CARD = """ClearFrame · Shot 4 · 6 of 6 takes reviewed
4-2: needs confirmation · 1 candidate note (road markings)
4-5: flagged · 1 measured anomaly (exposure)
Clean: 4-1, 4-3, 4-4, 4-6
Evidence, on the GB10: http://127.0.0.1:8090/shot/4"""

SAMPLE_RETAKE_CARD = """ClearFrame · Retake 4-2r1 of 4-2 · clean
Evidence, on the GB10: http://127.0.0.1:8090/take/4-2r1"""

# contracts/README.md "Never on a card": prompts or diffs, the model's words,
# frames or thumbnails, brand, product or shot descriptions, approver names,
# and any path except the link.
NEVER_ON_A_CARD = [
    SAMPLE["generation"]["prompt"],
    SAMPLE["vision"]["model"],
    SAMPLE["generation"]["tool"],
    SAMPLE["generation"]["model"],
    "4-2.mp4",
    ".jpg",
    "frames/",
    "Creative director",
    "Vela",
    "Honda",
    "Blake",
    "golden hour",
    "approver",
]


class CardFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.results = self.base / "results"
        self.results.mkdir()
        self.conn = db.connect(self.base / "clearframe.db")
        self.addCleanup(self._close)
        self.posted = []

    def _close(self):
        self.conn.close()
        self.tmp.cleanup()

    def transport(self, text: str) -> dict:
        self.posted.append(text)
        return {"ts": f"ts-{len(self.posted)}"}

    def post(self):
        return card.post_due_cards(self.conn, transport=self.transport, rubric=RUBRIC)


class ShotCardWordingTest(CardFixture):
    def given_sample_shot(self):
        write_final_take(self.results, "4-1")
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        write_final_take(self.results, "4-3")
        write_final_take(self.results, "4-4")
        write_final_take(self.results, "4-5", measured=[MEASURED_FLICKER])
        write_final_take(self.results, "4-6")
        ingest.ingest_dir(self.conn, self.results)

    def test_renders_the_contracts_sample_card(self):
        self.given_sample_shot()
        payload = card.shot_payload(self.conn, 4, RUBRIC)
        self.assertEqual(card.render_shot_card(payload, RUBRIC), SAMPLE_SHOT_CARD)

    def test_renders_the_contracts_sample_retake_card(self):
        self.given_sample_shot()
        write_final_take(self.results, "4-2r1", parent_take_id="4-2")
        ingest.ingest_dir(self.conn, self.results)
        payload = card.retake_payload(self.conn, "4-2r1", RUBRIC)
        self.assertEqual(card.render_retake_card(payload, RUBRIC), SAMPLE_RETAKE_CARD)

    def test_counts_use_the_many_form_and_rubric_order(self):
        write_final_take(self.results, "4-1", expected_takes=1, shot=4,
                         candidates=[CANDIDATE_ROAD, dict(CANDIDATE_ROAD)],
                         measured=[MEASURED_FLICKER, dict(MEASURED_FLICKER)])
        ingest.ingest_dir(self.conn, self.results)
        payload = card.shot_payload(self.conn, 4, RUBRIC)
        counts = payload["takes"][0]["counts"]
        self.assertEqual(
            counts,
            "2 measured anomalies (exposure), 2 candidate notes (road markings)",
        )

    def test_a_check_error_replaces_the_status_label(self):
        write_final_take(self.results, "4-1")
        write_final_take(self.results, "4-2", checks={"exposure_flicker": "error",
                                                      "road_markings": "done"})
        write_final_take(self.results, "4-3")
        write_final_take(self.results, "4-4")
        write_final_take(self.results, "4-5")
        write_final_take(self.results, "4-6")
        ingest.ingest_dir(self.conn, self.results)
        payload = card.shot_payload(self.conn, 4, RUBRIC)
        self.assertIn({"take_id": "4-2", "status": "check error (exposure)",
                       "counts": ""}, payload["takes"])

    def test_two_check_errors_share_one_label(self):
        write_final_take(self.results, "4-1")
        write_final_take(self.results, "4-2", checks={"exposure_flicker": "error",
                                                      "road_markings": "error"})
        write_final_take(self.results, "4-3")
        write_final_take(self.results, "4-4")
        write_final_take(self.results, "4-5")
        write_final_take(self.results, "4-6")
        ingest.ingest_dir(self.conn, self.results)
        payload = card.shot_payload(self.conn, 4, RUBRIC)
        self.assertIn("check error (exposure, road markings)",
                      payload["takes"][0]["status"])


class PostOnceTest(CardFixture):
    def test_shot_card_posts_once_when_every_expected_take_is_final(self):
        for take_id in ("4-1", "4-2", "4-3", "4-4", "4-5"):
            write_final_take(self.results, take_id)
        ingest.ingest_dir(self.conn, self.results)
        self.assertEqual(self.post(), [])
        self.assertEqual(self.posted, [])

        write_final_take(self.results, "4-6")
        ingest.ingest_dir(self.conn, self.results)
        self.assertEqual(self.post(), [("shot", 4)])
        self.assertEqual(len(self.posted), 1)
        self.assertEqual(
            self.posted[0],
            "ClearFrame · Shot 4 · 6 of 6 takes reviewed\n"
            "Clean: 4-1, 4-2, 4-3, 4-4, 4-5, 4-6\n"
            "Evidence, on the GB10: http://127.0.0.1:8090/shot/4",
        )

        posted = db.events(self.conn, kind="card_posted")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["shot"], 4)
        self.assertEqual(posted[0]["take_id"], "")
        self.assertEqual(json.loads(posted[0]["detail"]), {"card": "shot", "ts": "ts-1"})

        # Any later pass, decision or retake never reposts the shot card.
        write_final_take(self.results, "4-2", revision=3, candidates=[CANDIDATE_ROAD])
        ingest.ingest_dir(self.conn, self.results)
        self.assertEqual(self.post(), [])
        self.assertEqual(len(db.events(self.conn, kind="card_posted")), 1)
        self.assertEqual(self.posted, [self.posted[0]])

    def test_retake_card_posts_when_its_own_result_is_final(self):
        write_final_take(self.results, "4-1")
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        write_final_take(self.results, "4-3")
        write_final_take(self.results, "4-4")
        write_final_take(self.results, "4-5")
        write_final_take(self.results, "4-6")
        ingest.ingest_dir(self.conn, self.results)
        self.assertEqual(self.post(), [("shot", 4)])

        # The retake lands with its pick-up revision: no card yet.
        write_final_take(self.results, "4-2r1", revision=1, parent_take_id="4-2",
                         checks={"exposure_flicker": "running",
                                 "road_markings": "running"})
        ingest.ingest_dir(self.conn, self.results)
        self.assertEqual(self.post(), [])

        write_final_take(self.results, "4-2r1", parent_take_id="4-2")
        ingest.ingest_dir(self.conn, self.results)
        self.assertEqual(self.post(), [("retake", "4-2r1")])
        self.assertEqual(self.posted[-1], SAMPLE_RETAKE_CARD)
        self.assertEqual(self.post(), [])

        posted = db.events(self.conn, kind="card_posted", take_id="4-2r1")
        self.assertEqual(len(posted), 1)
        self.assertEqual(json.loads(posted[0]["detail"]), {"card": "retake", "ts": "ts-2"})

    def test_a_take_reposted_after_reset_is_expected(self):
        # --reset moves the db aside; the cards post again into the fresh db.
        write_final_take(self.results, "4-1", expected_takes=2)
        write_final_take(self.results, "4-2", expected_takes=2)
        ingest.ingest_dir(self.conn, self.results)
        self.conn.close()
        aside = db.reset(self.base / "clearframe.db")
        self.assertTrue(aside.is_file())
        self.conn = db.connect(self.base / "clearframe.db")
        ingest.ingest_dir(self.conn, self.results)
        self.assertEqual(self.post(), [("shot", 4)])


class PayloadFieldsTest(CardFixture):
    def test_shot_payload_carries_only_the_contracts_fields(self):
        write_final_take(self.results, "4-1")
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        ingest.ingest_dir(self.conn, self.results)
        payload = card.shot_payload(self.conn, 4, RUBRIC)

        self.assertEqual(
            set(payload),
            {"shot", "reviewed", "expected_takes", "takes", "clean_takes", "link"},
        )
        self.assertEqual(payload["shot"], 4)
        self.assertEqual(payload["reviewed"], 2)
        self.assertEqual(payload["expected_takes"], 6)
        self.assertEqual(payload["link"], "http://127.0.0.1:8090/shot/4")
        for entry in payload["takes"]:
            self.assertEqual(set(entry), {"take_id", "status", "counts"})
        for take_id in payload["clean_takes"]:
            self.assertIsInstance(take_id, str)

    def test_retake_payload_carries_only_the_contracts_fields(self):
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        write_final_take(self.results, "4-2r1", parent_take_id="4-2")
        ingest.ingest_dir(self.conn, self.results)
        payload = card.retake_payload(self.conn, "4-2r1", RUBRIC)
        self.assertEqual(
            set(payload),
            {"take_id", "parent_take_id", "status", "counts", "link"},
        )
        self.assertEqual(payload["parent_take_id"], "4-2")
        self.assertEqual(payload["link"], "http://127.0.0.1:8090/take/4-2r1")

    def test_payload_fields_together_cover_the_contracts_card_fields(self):
        # The contract's field list: Shot, Reviewed, Take ID, Status, Counts by
        # category, Clean takes, Retake of, Link. Every key anywhere in a card
        # payload must be one of these (plus the header's expected_takes and
        # the takes list that holds the per-take fields) — anything new fails
        # this test, so nothing else can leak into a card payload.
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        write_final_take(self.results, "4-2r1", parent_take_id="4-2")
        ingest.ingest_dir(self.conn, self.results)
        payloads = [
            card.shot_payload(self.conn, 4, RUBRIC),
            card.retake_payload(self.conn, "4-2r1", RUBRIC),
        ]

        def keys(value):
            found = set()
            if isinstance(value, dict):
                for key, item in value.items():
                    found.add(key)
                    found |= keys(item)
            elif isinstance(value, list):
                for item in value:
                    found |= keys(item)
            return found

        all_keys = set().union(*(keys(payload) for payload in payloads))
        self.assertEqual(
            all_keys,
            {"shot", "reviewed", "expected_takes", "takes", "take_id", "status",
             "counts", "clean_takes", "parent_take_id", "link"},
        )

    def test_every_value_is_plain_data(self):
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        ingest.ingest_dir(self.conn, self.results)
        payload = card.shot_payload(self.conn, 4, RUBRIC)

        def check(value):
            if isinstance(value, dict):
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
            else:
                self.assertIsInstance(value, (str, int))

        check(payload)


class NeverOnACardTest(CardFixture):
    def test_nothing_but_the_contracts_fields_reaches_the_card_text(self):
        # The ingested result carries prompts, model names, evidence frames and
        # the render's file name; none of it may leak into any card.
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        ingest.ingest_dir(self.conn, self.results)
        text = card.render_shot_card(card.shot_payload(self.conn, 4, RUBRIC), RUBRIC)
        for banned in NEVER_ON_A_CARD:
            self.assertNotIn(banned, text)

    def test_every_card_ever_posted_stays_clean(self):
        write_final_take(self.results, "4-1")
        write_final_take(self.results, "4-2", candidates=[CANDIDATE_ROAD])
        write_final_take(self.results, "4-3")
        write_final_take(self.results, "4-4")
        write_final_take(self.results, "4-5")
        write_final_take(self.results, "4-6")
        write_final_take(self.results, "4-2r1", parent_take_id="4-2")
        ingest.ingest_dir(self.conn, self.results)
        self.post()
        self.assertTrue(self.posted)
        for text in self.posted:
            for banned in NEVER_ON_A_CARD:
                self.assertNotIn(banned, text)


class TransportTest(unittest.TestCase):
    def test_dry_run_prints_and_touches_nothing(self):
        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer):
            result = card.dry_run_transport("a card")
        self.assertEqual(result, {})
        self.assertEqual(buffer.getvalue(), "a card\n")

    def test_webhook_transport_posts_the_card_as_json(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'"1400.0001"'

        with mock.patch.object(urllib.request, "urlopen") as urlopen:
            urlopen.return_value = FakeResponse()
            transport = card.webhook_transport(
                "https://hooks.slack.invalid/services/T1/CLEARFRAME/test"
            )
            result = transport("a card")

        request = urlopen.call_args[0][0]
        self.assertEqual(json.loads(request.data.decode("utf-8")), {"text": "a card"})
        self.assertEqual(result, {"ts": "1400.0001"})

    def test_webhook_transport_surfaces_failures(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"ok"

        with mock.patch.object(urllib.request, "urlopen") as urlopen:
            urlopen.return_value = FakeResponse()
            transport = card.webhook_transport(
                "https://hooks.slack.invalid/services/T1/CLEARFRAME/test"
            )
            self.assertEqual(transport("a card"), {})


if __name__ == "__main__":
    unittest.main()
