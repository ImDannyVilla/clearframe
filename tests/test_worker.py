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

from vision.model import FakeModel, ModelError  # noqa: E402
from worker import flicker, main, review, takes  # noqa: E402

FFMPEG = os.environ.get("FFMPEG", "ffmpeg")
HAVE_FFMPEG = shutil.which(FFMPEG) is not None and shutil.which(os.environ.get("FFPROBE", "ffprobe")) is not None
SAMPLE = json.loads((ROOT / "contracts" / "samples" / "result-4-2.json").read_text(encoding="utf-8"))
PROMPT = SAMPLE["generation"]["prompt"]
# Danny's fake model answers: the road-marking flaw on frames 48 to 64.
ANSWERS_4_2 = ROOT / "vision" / "samples" / "answers-4-2.json"


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


class SpreadTest(unittest.TestCase):
    def test_spread_keeps_both_ends(self):
        self.assertEqual(main.spread(list(range(20)), 3), [0, 10, 19])
        self.assertEqual(main.spread([1, 2], 8), [1, 2])


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


class PeekingModel:
    """Danny's FakeModel, plus a look at results/ during the first request."""

    def __init__(self, inner, result_path: Path):
        self.inner = inner
        self.name = inner.name
        self.result_path = result_path
        self.seen: dict = {}

    def ask(self, request, sampling):
        if not self.seen:
            self.seen = json.loads(self.result_path.read_text(encoding="utf-8"))
            self.seen["tmp_left"] = list(self.result_path.parent.glob("*.tmp"))
        return self.inner.ask(request, sampling)


class FailingModel:
    name = "failing"

    def __init__(self, error: Exception):
        self.error = error

    def ask(self, request, sampling):
        raise self.error


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
        # A model that sees no flaw anywhere, for takes with no planted road flaw.
        self.no_flaws = self.tmp / "no-flaws.json"
        self.no_flaws.write_text("{}", encoding="utf-8")
        self.config = main.Config(
            input_dir=self.input,
            results_dir=self.results,
            work_dir=self.tmp / "work",
            rubric_path=ROOT / "rubric" / "rubric.yaml",
            commit="abc1234",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def review(self, take_id, vf=None, model=None):
        clip = self.renders / f"{take_id}.mp4"
        make_clip(clip, vf)
        model = model or FakeModel.from_file(ANSWERS_4_2)
        return main.review_take(take_id, clip, self.config, model_loader=lambda: model)

    def test_flicker_that_returns_is_a_measured_anomaly(self):
        result = self.review("4-5", "eq=brightness=0.3:enable='between(n,61,63)'",
                             FakeModel.from_file(self.no_flaws))
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
        self.assertEqual(result["candidates"], [])

    def test_a_step_that_stays_is_not_reported(self):
        result = self.review("4-6", "eq=brightness=0.3:enable='gte(n,60)'", FakeModel.from_file(self.no_flaws))
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
        # The fake model sees the flaw on frames 48 to 64; the note stays inside them.
        self.assertGreaterEqual(note["frame_start"], 48)
        self.assertLessEqual(note["frame_end"], 64)
        self.assertEqual(note["t_start"], round(note["frame_start"] / 24, 3))
        self.assertEqual(note["t_end"], round(note["frame_end"] / 24, 3))
        self.assertEqual(note["model_note"], json.loads(ANSWERS_4_2.read_text())["road_markings"]["note"])
        self.assertTrue(note["evidence"])
        for path in note["evidence"]:
            self.assertTrue((self.results / path).is_file(), path)
        vision = result["vision"]
        self.assertEqual(vision["model"], "fake:answers-4-2.json")
        self.assertEqual(vision["sparse_frames_sent"], 16)
        self.assertEqual(vision["dense_windows"], 1)
        self.assertEqual(vision["dense_frames_sent"], 12)

    def test_result_file_matches_the_contract_shape(self):
        result = self.review("4-2")
        on_disk = json.loads((self.results / "4-2.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk, result)
        self.assertEqual(set(result), set(SAMPLE))
        self.assertEqual(set(result["render"]), set(SAMPLE["render"]))
        self.assertEqual(set(result["vision"]), set(SAMPLE["vision"]))
        self.assertEqual(set(result["candidates"][0]), set(SAMPLE["candidates"][0]))
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
        result = self.review("4-2r1", model=FakeModel.from_file(self.no_flaws))
        self.assertEqual(result["parent_take_id"], "4-2")
        self.assertEqual(result["candidates"], [])

    def test_no_model_configured_ends_the_noted_check_in_error(self):
        clip = self.renders / "4-2.mp4"
        make_clip(clip)

        def unavailable():
            raise review.ReviewerUnavailable("reviewer not available")

        result = main.review_take("4-2", clip, self.config, model_loader=unavailable)
        self.assertEqual(result["checks"], {"exposure_flicker": "done", "road_markings": "error"})
        self.assertEqual(result["errors"], ["road_markings: reviewer not available"])

    def test_a_model_server_failure_is_a_fixed_error(self):
        result = self.review("4-2", model=FailingModel(ModelError("model server answered 500: boom")))
        self.assertEqual(result["checks"]["road_markings"], "error")
        self.assertEqual(result["errors"], ["road_markings: the reviewer gave no usable answer"])
        self.assertEqual(result["candidates"], [])

    def test_a_reviewer_crash_is_a_fixed_error_not_model_output(self):
        result = self.review("4-2", model=FailingModel(RuntimeError("model said something odd")))
        self.assertEqual(result["errors"], ["road_markings: reviewer failed"])

    def test_revision_1_is_on_disk_while_checks_run(self):
        model = PeekingModel(FakeModel.from_file(ANSWERS_4_2), self.results / "4-2.json")
        final = self.review("4-2", model=model)
        self.assertEqual(model.seen["revision"], 1)
        self.assertEqual(set(model.seen["checks"].values()), {"running"})
        self.assertEqual(model.seen["tmp_left"], [])
        self.assertEqual(final["revision"], 2)

    def test_the_watcher_picks_each_take_up_once(self):
        os.environ["VISION_ANSWERS"] = str(ANSWERS_4_2)
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
