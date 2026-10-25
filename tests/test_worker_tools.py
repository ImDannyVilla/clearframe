"""The worker's run tools for #23: the preflight check and the findings report.

Clips are made with ffmpeg; result files are written by hand in the contract's
shape. No footage and no model.
"""
import copy
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rubric import rubric as rubric_loader  # noqa: E402
from vision import accuracy  # noqa: E402
from worker import preflight, report  # noqa: E402

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
HAVE_FFMPEG = shutil.which(FFMPEG) is not None and shutil.which(os.environ.get("FFPROBE", "ffprobe")) is not None
SAMPLE = json.loads((ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8"))
KEY = """take_id,issue_type,frame_start,frame_end,t_start,t_end,description
5-1,road_markings,76,119,,,lane lines skew
5-1,road_surface,0,119,,,road too glossy
5-2,unnatural_object,60,119,,,flowers look unnatural
6-1,none,,,,,
"""


class TimecodeTest(unittest.TestCase):
    def test_timecode_reads_like_a_creative_directors_note(self):
        self.assertEqual(report.timecode(102, 23.976), "00:00:04:06")
        self.assertEqual(report.timecode(82, 23.976), "00:00:03:10")
        self.assertEqual(report.timecode(0, 24), "00:00:00:00")
        self.assertEqual(report.timecode(24 * 3600 + 5, 24), "01:00:00:05")


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-report-"))
        self.results = self.tmp / "results"
        self.key_path = self.tmp / "answer_key.csv"
        self.key_path.write_text(KEY, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, take_id, candidates=(), series_peak=6.3, final=True):
        result = copy.deepcopy(SAMPLE)
        shot = int(take_id.split("-")[0])
        result.update(take_id=take_id, shot=shot, candidates=list(candidates), measured=[],
                      brightness_series=f"frames/{take_id}/brightness.json")
        result["render"].update(fps=23.976, frames=120)
        if not final:
            result["checks"] = {name: "running" for name in result["checks"]}
        series = [100.0] * 120
        series[50] = 100.0 + series_peak
        (self.results / "frames" / take_id).mkdir(parents=True, exist_ok=True)
        (self.results / result["brightness_series"]).write_text(json.dumps(series), encoding="utf-8")
        (self.results / f"{take_id}.json").write_text(json.dumps(result), encoding="utf-8")

    def note(self, start, end, text="the lane lines bend across the road"):
        return {"issue_type": "road_markings", "pass": "dense", "frame_start": start, "frame_end": end,
                "t_start": round(start / 23.976, 3), "t_end": round(end / 23.976, 3), "model_note": text,
                "evidence": []}

    def render(self):
        key = accuracy.load_answer_key(self.key_path)
        results = report.load_results(self.results, {5, 6})
        return report.render(self.results, results, key, rubric_loader.load())

    def test_scores_shots_5_and_6_against_the_key(self):
        self.write("5-1", [self.note(80, 110)])
        self.write("5-2")
        self.write("6-1")
        self.write("4-2", [self.note(48, 64)])  # another shot: left out
        text = self.render()
        self.assertNotIn("| 4-2 |", text)
        self.assertIn("candidate road_markings frames 80-110 (3.337-4.588 s, 00:00:03:08)", text)
        self.assertIn("6.3 at frame 50 vs 8", text)
        self.assertIn("- 5-1: caught road_markings 76-119; not checked: road_surface 0-119 (no enabled check)", text)
        self.assertIn("- 5-2: not checked: unnatural_object 60-119 (no enabled check)", text)
        self.assertIn("- 6-1: clean, as the key says", text)
        self.assertIn("note at 80-110: 'the lane lines bend across the road'", text)

    def test_a_miss_and_a_note_off_the_key(self):
        self.write("5-1", [self.note(10, 20)])
        self.write("6-1", [self.note(30, 40)])
        text = self.render()
        self.assertIn("MISSED road_markings 76-119", text)
        self.assertIn("off the key: candidate road_markings 10-20", text)
        self.assertIn("- 6-1: off the key: candidate road_markings 30-40", text)

    def test_only_final_results_are_reported(self):
        self.write("5-1", final=False)
        out = io.StringIO()
        with redirect_stdout(out):
            code = report.main(["--results", str(self.results), "--answer-key", str(self.key_path), "--shots", "5"])
        self.assertEqual(code, 1)


def make_clip(path: Path, size: str = "320x180") -> None:
    subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=gray:s={size}:r=24000/1001:d=1",
                    "-frames:v", "24", "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p", str(path)], check=True)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are needed for clip tests")
class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-preflight-"))
        self.input = self.tmp / "input"
        for folder in ("takes", "renders"):
            (self.input / folder).mkdir(parents=True)
        (self.input / "shot_list.csv").write_text(
            "shot,description,expected_takes\n5,a city street,2\n6,a city street,1\n", encoding="utf-8")
        (self.input / "generation_log.csv").write_text(
            "take_id,shot,tool,model,seed,prompt\n5-1,5,tool,model,,\"A city street.\"\n6-1,6,tool,model,,\n",
            encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_preflight(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = preflight.main(["--input", str(self.input), "--results", str(self.tmp / "results"),
                                   "--skip-network"])
        return code, out.getvalue()

    def test_a_good_setup_passes(self):
        make_clip(self.input / "takes" / "5-1.mp4")
        code, text = self.run_preflight()
        self.assertEqual(code, 0, text)
        self.assertIn("PASS  takes/5-1.mp4: 320x180 at 23.976 fps, 24 frames", text)
        self.assertIn("PASS  results/ is writable", text)
        self.assertIn("rubric: checks exposure_flicker, road_markings", text)

    def test_problems_with_takes_fail(self):
        make_clip(self.input / "takes" / "6-1.mp4")  # its log row has no prompt
        make_clip(self.input / "takes" / "7-1.mp4")  # no log row, no shot-list row
        make_clip(self.input / "takes" / "review copy.mp4")
        make_clip(self.input / "takes" / "._5-1.mp4")  # macOS resource fork: ignored
        make_clip(self.input / "renders" / "5-1.mp4", size="3840x180")  # a review copy in renders/
        code, text = self.run_preflight()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  takes/6-1.mp4: its generation_log.csv row has no prompt", text)
        self.assertIn("FAIL  takes/7-1.mp4: no row in generation_log.csv", text)
        self.assertIn("FAIL  takes/7-1.mp4: shot 7 isn't in shot_list.csv", text)
        self.assertIn("FAIL  takes/review copy.mp4: the file name isn't a take ID", text)
        self.assertNotIn("._5-1", text)
        self.assertIn("FAIL  renders/5-1.mp4: 3840x180", text)
        self.assertIn("a review copy, not a take", text)

    def test_missing_files_fail(self):
        (self.input / "shot_list.csv").unlink()
        code, text = self.run_preflight()
        self.assertEqual(code, 1)
        self.assertIn("FAIL  shot_list.csv is missing", text)

    def test_network_checks(self):
        result = preflight.Report()
        with redirect_stdout(io.StringIO()):
            preflight.check_model(result, "http://127.0.0.1:9/v1", "qwen", "", timeout=1)
            preflight.check_model(result, None, None, None, timeout=1)
            preflight.check_no_internet(result, "http://127.0.0.1:9/", timeout=1)
        self.assertEqual([level for level, _ in result.lines], ["FAIL", "FAIL", "PASS"])
        self.assertIn("docker network connect clearframe-net nemoclaw-vllm", result.lines[0][1])


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are needed for clip tests")
class FrameRateTest(unittest.TestCase):
    """Morgan's takes run at 23.976 fps: the worker keeps that rate and seconds stay frame / fps."""

    def test_a_23_976_fps_take_end_to_end(self):
        from vision.model import FakeModel
        from worker import main as worker_main

        tmp = Path(tempfile.mkdtemp(prefix="clearframe-fps-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        inp = tmp / "input"
        (inp / "renders").mkdir(parents=True)
        (inp / "shot_list.csv").write_text("shot,description,expected_takes\n5,a city street,2\n", encoding="utf-8")
        (inp / "generation_log.csv").write_text("take_id,shot,tool,model,seed,prompt\n5-1,5,t,m,,\"A street.\"\n",
                                                encoding="utf-8")
        clip = inp / "renders" / "5-1.mp4"
        subprocess.run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x180:r=24000/1001:d=6",
                        "-vf", "eq=brightness=0.3:enable='between(n,101,103)'", "-frames:v", "120",
                        "-c:v", "mpeg4", "-q:v", "2", str(clip)], check=True)
        answers = tmp / "none.json"
        answers.write_text("{}", encoding="utf-8")
        config = worker_main.Config(input_dir=inp, results_dir=tmp / "results", work_dir=tmp / "work",
                                    rubric_path=ROOT / "rubric" / "rubric.yaml", commit="test")
        with redirect_stdout(io.StringIO()):
            result = worker_main.review_take("5-1", clip, config,
                                             model_loader=lambda: FakeModel.from_file(answers))
        self.assertEqual(result["render"]["fps"], 23.976)
        self.assertEqual(result["expected_takes"], 2)
        anomaly = result["measured"][0]
        self.assertEqual((anomaly["frame_start"], anomaly["frame_end"]), (101, 103))
        self.assertEqual((anomaly["t_start"], anomaly["t_end"]), (round(101 / 23.976, 3), round(103 / 23.976, 3)))
        self.assertEqual(report.timecode(anomaly["frame_start"], 23.976), "00:00:04:05")


if __name__ == "__main__":
    unittest.main()
