"""The review worker (#5): flicker, take IDs and result files.

The clip tests make their own clips with ffmpeg and read model answers from a
sample file, so they need no footage and no model.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from worker import flicker, main, review, takes  # noqa: E402

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
HAVE_FFMPEG = shutil.which(FFMPEG) is not None and shutil.which(os.environ.get("FFPROBE", "ffprobe")) is not None
SAMPLE = json.loads((ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8"))
PROMPT = SAMPLE["generation"]["prompt"]


class FlickerRuleTest(unittest.TestCase):
    def test_a_spike_that_returns_is_flicker(self):
        series = [100.0] * 61 + [118.4] * 3 + [100.0] * 56
        spikes = flicker.detect(series, threshold=8.0, return_frames=6)
        self.assertEqual(len(spikes), 1)
        spike = spikes[0]
        self.assertEqual((spike.frame_start, spike.frame_end), (61, 63))
        self.assertEqual(spike.value, 18.4)
        self.assertEqual(spike.evidence_frames, [60, 61, 62, 63])

    def test_a_step_that_stays_is_not_flicker(self):
        series = [100.0] * 60 + [130.0] * 60
        self.assertEqual(flicker.detect(series, 8.0, 6), [])

    def test_a_spike_slower_than_return_frames_is_not_flicker(self):
        series = [100.0] * 30 + [130.0] * 7 + [100.0] * 30
        self.assertEqual(flicker.detect(series, 8.0, 6), [])
        self.assertEqual(len(flicker.detect(series, 8.0, 7)), 1)

    def test_a_slow_drift_into_shadow_is_not_flicker(self):
        series = [100.0 - 0.5 * i for i in range(120)]
        self.assertEqual(flicker.detect(series, 8.0, 6), [])

    def test_a_change_at_the_threshold_is_not_a_spike(self):
        series = [100.0] * 10 + [108.0] * 2 + [100.0] * 10
        self.assertEqual(flicker.detect(series, 8.0, 6), [])

    def test_a_dark_dip_counts_too(self):
        series = [100.0] * 10 + [80.0] + [100.0] * 10
        spikes = flicker.detect(series, 8.0, 6)
        self.assertEqual([(s.frame_start, s.frame_end) for s in spikes], [(10, 10)])

    def test_seconds_follow_the_contract(self):
        self.assertEqual(flicker.seconds(61, 24), 2.542)
        self.assertEqual(flicker.seconds(63, 24), 2.625)


class TakeIdTest(unittest.TestCase):
    def test_originals_and_retakes(self):
        self.assertEqual(takes.parse_take_id("4-2"), (4, None))
        self.assertEqual(takes.parse_take_id("4-2r1"), (4, "4-2"))
        self.assertEqual(takes.parse_take_id("4-2r2"), (4, "4-2r1"))

    def test_not_take_ids(self):
        for name in ("4", "4-2r0", "take4-2", "4-2 copy", "4-2r", ""):
            self.assertIsNone(takes.parse_take_id(name), name)


class ReviewHelpersTest(unittest.TestCase):
    def test_runs_split_on_gaps(self):
        self.assertEqual(review.runs_of([64, 48, 56, 100], 8), [[48, 56, 64], [100]])

    def test_window_stays_inside_the_take(self):
        self.assertEqual(review.window(2, 5, 8, 120), (0, 13))
        self.assertEqual(review.window(115, 118, 8, 120), (107, 119))

    def test_spread_keeps_both_ends(self):
        self.assertEqual(review.spread(list(range(20)), 3), [0, 10, 19])
        self.assertEqual(review.spread([1, 2], 8), [1, 2])


def make_clip(path: Path, vf: str | None = None, frames: int = 120) -> None:
    """A 320x180, 24 fps grey clip; `vf` adds a brightness change on chosen frames."""
    cmd = [
        FFMPEG, "-v", "error", "-y", "-f", "lavfi",
        "-i", f"color=c=gray:s=320x180:r=24:d={frames / 24}",
    ]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-frames:v", str(frames), "-c:v", "mpeg4", "-q:v", "2", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are needed for clip tests")
class WorkerClipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="clearframe-test-"))
        self.input = self.tmp / "input"
        self.renders = self.input / "renders"
        self.results = self.tmp / "results"
        self.renders.mkdir(parents=True)
        (self.input / "shot_list.csv").write_text(
            "shot,description,expected_takes\n4,The Vela on a city street with lane arrows,6\n",
            encoding="utf-8",
        )
        (self.input / "generation_log.csv").write_text(
            "take_id,shot,tool,model,seed,prompt\n"
            f"4-2,4,fal.ai,bytedance/seedance-2.0/text-to-video,18342,\"{PROMPT}\"\n"
            f"4-5,4,fal.ai,bytedance/seedance-2.0/text-to-video,,\"{PROMPT}\"\n"
            f"4-6,4,fal.ai,bytedance/seedance-2.0/text-to-video,,\"{PROMPT}\"\n"
            f"4-2r1,4,fal.ai,bytedance/seedance-2.0/text-to-video,,\"{PROMPT}\"\n",
            encoding="utf-8",
        )
        self.answers = self.tmp / "answers.json"
        self.answers.write_text(json.dumps({
            "model": "sample-answers",
            "4-2": {"road_markings": {
                "sparse": [{"frames": [48, 56, 64], "model_note": "The arrow points against traffic."}],
                "dense": [{"frames": list(range(50, 61)), "model_note": "The right-lane arrow points at the camera."}],
            }},
        }), encoding="utf-8")
        self.config = main.Config(
            input_dir=self.input,
            results_dir=self.results,
            work_dir=self.tmp / "work",
            rubric_path=ROOT / "rubric" / "rubric.yaml",
            commit="abc1234",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def review(self, take_id, vf=None, loader=None):
        clip = self.renders / f"{take_id}.mp4"
        make_clip(clip, vf)
        loader = loader or (lambda: review.answers_file_reviewer(self.answers))
        return main.review_take(take_id, clip, self.config, reviewer_loader=loader)

    def test_flicker_that_returns_is_a_measured_anomaly(self):
        result = self.review("4-5", "eq=brightness=0.3:enable='between(n,61,63)'")
        self.assertEqual(result["checks"], {"exposure_flicker": "done", "road_markings": "done"})
        self.assertEqual(len(result["measured"]), 1)
        anomaly = result["measured"][0]
        self.assertEqual(anomaly["issue_type"], "exposure_flicker")
        self.assertEqual((anomaly["frame_start"], anomaly["frame_end"]), (61, 63))
        self.assertEqual((anomaly["t_start"], anomaly["t_end"]), (2.542, 2.625))
        self.assertGreater(anomaly["value"], anomaly["threshold"])
        self.assertEqual(anomaly["threshold"], 8.0)
        self.assertEqual(anomaly["evidence"][0], "frames/4-5/000060.jpg")
        for path in anomaly["evidence"]:
            self.assertTrue((self.results / path).is_file(), path)
        # Code flagged a moment, so the reviewer got a dense window around it.
        self.assertEqual(result["vision"]["dense_windows"], 1)

    def test_a_step_that_stays_is_not_reported(self):
        result = self.review("4-6", "eq=brightness=0.3:enable='gte(n,60)'")
        self.assertEqual(result["measured"], [])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["vision"]["dense_windows"], 0)

    def test_road_notes_become_candidates_with_code_timestamps(self):
        result = self.review("4-2")
        self.assertEqual(result["measured"], [])
        self.assertEqual(len(result["candidates"]), 1)
        note = result["candidates"][0]
        self.assertEqual(note["issue_type"], "road_markings")
        self.assertEqual(note["pass"], "dense")
        self.assertEqual((note["frame_start"], note["frame_end"]), (50, 60))
        self.assertEqual((note["t_start"], note["t_end"]), (2.083, 2.5))
        self.assertEqual(note["model_note"], "The right-lane arrow points at the camera.")
        self.assertLessEqual(len(note["evidence"]), self.config.evidence_limit)
        for path in note["evidence"]:
            self.assertTrue((self.results / path).is_file(), path)
        vision = result["vision"]
        self.assertEqual(vision["model"], "sample-answers")
        self.assertEqual(vision["sparse_frames_sent"], 15)  # every 8th of 120 frames
        self.assertEqual(vision["dense_windows"], 1)
        self.assertEqual(vision["dense_frames_sent"], 33)  # frames 40 to 72: the note plus 8 each side

    def test_result_file_matches_the_contract_shape(self):
        result = self.review("4-2")
        on_disk = json.loads((self.results / "4-2.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, result)
        self.assertEqual(set(result), set(SAMPLE))
        self.assertEqual(set(result["render"]), set(SAMPLE["render"]))
        self.assertEqual(set(result["vision"]), set(SAMPLE["vision"]))
        self.assertEqual(result["schema"], "clearframe.result/1")
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["expected_takes"], 6)
        self.assertEqual(result["worker_commit"], "abc1234")
        self.assertEqual(result["render"]["frames"], 120)
        self.assertEqual(result["render"]["fps"], 24)
        self.assertTrue(result["render"]["sha256"].startswith("sha256:"))
        self.assertEqual(len(result["render"]["sha256"]), 7 + 64)
        self.assertEqual(result["generation"]["prompt"], PROMPT)
        self.assertEqual(result["generation"]["seed"], 18342)
        self.assertEqual(result["errors"], [])
        series = json.loads((self.results / result["brightness_series"]).read_text(encoding="utf-8"))
        self.assertEqual(len(series), 120)
        # Only evidence frames and the series are kept: no .tmp files, no full frame dump.
        self.assertEqual(list(self.results.rglob("*.tmp")), [])
        kept = {p.name for p in (self.results / "frames" / "4-2").iterdir()}
        self.assertLess(len(kept), 20)
        self.assertFalse((self.config.work_dir / "4-2").exists())

    def test_a_retake_names_its_parent(self):
        result = self.review("4-2r1")
        self.assertEqual(result["parent_take_id"], "4-2")
        self.assertEqual(result["candidates"], [])

    def test_no_reviewer_ends_the_noted_check_in_error(self):
        def unavailable():
            raise review.ReviewerUnavailable("reviewer not available")

        result = self.review("4-2", loader=unavailable)
        self.assertEqual(result["checks"], {"exposure_flicker": "done", "road_markings": "error"})
        self.assertEqual(result["errors"], ["road_markings: reviewer not available"])

    def test_a_reviewer_crash_is_a_fixed_error_not_model_output(self):
        def crashing():
            def review_fn(check, pass_name, frames, context):
                raise RuntimeError("model said something odd")
            return review_fn

        result = self.review("4-2", loader=crashing)
        self.assertEqual(result["errors"], ["road_markings: reviewer failed"])

    def test_revision_1_is_on_disk_while_checks_run(self):
        seen = {}
        canned = review.answers_file_reviewer(self.answers)

        def peeking():
            def review_fn(check, pass_name, frames, context):
                if pass_name == "sparse":
                    seen.update(json.loads((self.results / "4-2.json").read_text(encoding="utf-8")))
                    seen["tmp_left"] = list(self.results.glob("*.tmp"))
                return canned(check, pass_name, frames, context)
            return review_fn

        final = self.review("4-2", loader=peeking)
        self.assertEqual(seen["revision"], 1)
        self.assertEqual(set(seen["checks"].values()), {"running"})
        self.assertEqual(seen["tmp_left"], [])
        self.assertEqual(final["revision"], 2)

    def test_the_watcher_picks_each_take_up_once(self):
        os.environ["VISION_ANSWERS"] = str(self.answers)
        try:
            make_clip(self.renders / "4-2.mp4")
            make_clip(self.renders / "not-a-take.mp4")
            main.run(self.config, once=True)
            self.assertTrue(main.is_final(self.results / "4-2.json"))
            self.assertFalse((self.results / "not-a-take.json").exists())
            watcher = main.Watcher(self.config)
            watcher.ready()
            self.assertEqual(watcher.ready(), [])  # already final: not picked up again
        finally:
            del os.environ["VISION_ANSWERS"]


if __name__ == "__main__":
    unittest.main()
