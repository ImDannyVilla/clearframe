"""The accuracy test: reading the answer key, scoring runs against it, and the run-then-score command."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vision import accuracy  # noqa: E402

SAMPLE_ANSWERS = ROOT / "vision" / "samples" / "answers-4-2.json"
HEADER = ",".join(accuracy.KEY_COLUMNS)


def make_clip(path, frames=120, fps=24):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate={fps}",
         "-frames:v", str(frames), "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


class AnswerKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="clearframe-test-key-")
        self.path = Path(self.tmp.name) / "answer_key.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, *rows, header=HEADER):
        self.path.write_text("\n".join([header, *rows]) + "\n", encoding="utf-8")
        return accuracy.load_answer_key(self.path)

    def test_frames_seconds_and_clean_takes(self):
        key = self.load(
            "4-1,none,,,,,",
            "4-2,road_markings,48,64,,,right-lane arrow points back",
            "4-3,Out_Of_Place_Object,,,1.5,2.0,a cone in the sky",
        )
        self.assertEqual(key["4-1"], [])
        self.assertEqual(key["4-2"][0].frames, (48, 64))
        self.assertEqual(key["4-3"][0].issue_type, "out_of_place_object")
        self.assertEqual(key["4-3"][0].span(fps=24, frame_count=120), (36, 48))

    def test_the_answer_key_in_the_repo_loads(self):
        key = accuracy.load_answer_key(accuracy.ANSWER_KEY)
        for take_id, flaws in key.items():
            with self.subTest(take_id=take_id):
                self.assertRegex(take_id, accuracy.TAKE_ID)
                for flaw in flaws:
                    self.assertTrue(flaw.frames or flaw.seconds)

    def test_refuses_rows_it_cant_score(self):
        for rows, message in (
            (["4-2,road_markings,,,,,"], "give frame_start"),
            (["4-2,road_markings,64,48,,,"], "before"),
            (["4-2,road_markings,a,b,,,"], "numbers"),
            (["take 2,road_markings,1,2,,,"], "not a take ID"),
        ):
            with self.subTest(rows=rows), self.assertRaises(accuracy.AccuracyError) as caught:
                self.load(*rows)
            self.assertIn(message, str(caught.exception))
        with self.assertRaises(accuracy.AccuracyError):
            self.load(header="take_id,issue_type")


def take(take_id, candidates, sparse=None, seconds=10.0, replies=6, unparsed=0, errors=()):
    return {
        "take_id": take_id, "fps": 24, "frames": 120, "seconds": seconds, "replies": replies,
        "unparsed": unparsed, "errors": list(errors),
        "candidates": [{"frame_start": a, "frame_end": b} for a, b in candidates],
        "sparse_notes": [{"frame_start": a, "frame_end": b} for a, b in (candidates if sparse is None else sparse)],
    }


class ScoreTest(unittest.TestCase):
    def setUp(self):
        flaw = accuracy.Flaw
        self.key = {
            "4-1": [],
            "4-2": [flaw("4-2", "road_markings", (48, 64), None, "")],
            "4-3": [flaw("4-3", "road_markings", (10, 20), None, ""), flaw("4-3", "lighting", (0, 5), None, "")],
        }
        self.record = {
            "check": "road_markings",
            "settings": {"mode": "grid", "thinking": False, "runs": 3},
            "takes": [
                take("4-1", [(90, 95)], seconds=8.0),                 # a false note on a clean take
                take("4-2", [(50, 60), (100, 110)], sparse=[]),       # a catch and an off-flaw note
                take("4-3", [], seconds=12.0, unparsed=2, errors=["refused"]),  # a miss
                take("4-4", [(1, 2)]),                                # not in the key
            ],
        }

    def test_scores(self):
        score = accuracy.score_record(self.record, self.key)
        self.assertEqual(score["setup"], "road_markings-grid-thinking-off-runs3")
        self.assertEqual((score["takes"], score["not_in_key"]), (3, ["4-4"]))
        self.assertEqual((score["caught"], score["planted"], score["caught_sparse"]), (1, 2, 0))
        self.assertEqual((score["false_on_clean"], score["off_flaw"]), (1, 1))
        self.assertEqual(score["iou"], round(11 / 17, 2))
        self.assertEqual(score["start_offset_s"], round(2 / 24, 2))
        self.assertEqual(score["seconds_per_take"], 10.0)
        self.assertEqual((score["unparsed"], score["replies"], score["errors"]), (2, 18, 1))

    def test_tolerance_widens_the_flaw(self):
        self.record["takes"][2]["candidates"] = [{"frame_start": 22, "frame_end": 24}]
        self.assertEqual(accuracy.score_record(self.record, self.key)["caught"], 1)
        self.assertEqual(accuracy.score_record(self.record, self.key, tolerance=2)["caught"], 2)

    def test_table_and_counts(self):
        text = accuracy.table([accuracy.score_record(self.record, self.key)])
        self.assertIn("| road_markings-grid-thinking-off-runs3 | 1 of 2 | 0 of 2 | 1 | 1 | 0.65 | 0.08 s |", text)
        self.assertEqual(
            accuracy.key_summary(self.key),
            "Answer key: 3 takes, 1 with no flaw. Flaws by type: road_markings 2, lighting 1.",
        )


class RunThenScoreTest(unittest.TestCase):
    def test_a_dry_run_with_the_fake_model(self):
        with tempfile.TemporaryDirectory(prefix="clearframe-test-accuracy-") as tmp:
            takes, out = Path(tmp) / "takes", Path(tmp) / "out"
            takes.mkdir()
            for take_id in ("4-1", "4-2", "2-1"):
                make_clip(takes / f"{take_id}.mp4")
            (takes / "notes.txt").write_text("not a take", encoding="utf-8")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = accuracy.main([
                    "run", "--takes", str(takes), "--shot", "4", "--modes", "grid,frames", "--thinking", "off",
                    "--fake", str(SAMPLE_ANSWERS), "--parallel", "2", "--out", str(out),
                ])
            self.assertEqual(code, 0, printed.getvalue())
            files = sorted(path.name for path in out.glob("*.json"))
            self.assertEqual(files, [
                "road_markings-frames-thinking-off-runs3.json", "road_markings-grid-thinking-off-runs3.json",
            ])
            record = json.loads((out / files[1]).read_text(encoding="utf-8"))
            self.assertEqual([t["take_id"] for t in record["takes"]], ["4-1", "4-2"])
            self.assertEqual(record["schema"], accuracy.SCHEMA)

            key = Path(tmp) / "answer_key.csv"
            key.write_text(f"{HEADER}\n4-1,none,,,,,\n4-2,road_markings,,,2.0,2.7,arrow\n", encoding="utf-8")
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                code = accuracy.main(["score", "--answer-key", str(key), str(out)])
            self.assertEqual(code, 0)
            # The fake answers the same for every take, so 4-1 gets a false note.
            self.assertIn("| road_markings-grid-thinking-off-runs3 | 1 of 1 | 1 of 1 | 1 | 0 |", printed.getvalue())

    def test_no_takes_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()) as err:
            code = accuracy.main(["run", "--takes", tmp, "--fake", str(SAMPLE_ANSWERS), "--out", tmp])
        self.assertEqual(code, 1)
        self.assertIn("no takes", err.getvalue())


if __name__ == "__main__":
    unittest.main()
