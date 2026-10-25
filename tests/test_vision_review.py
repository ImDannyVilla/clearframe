"""The reviewer step: questions, the three ways of sending frames, votes and the two passes.

The clips are made here with ffmpeg and the model is a fake, so no footage or model is needed.
"""
import base64
import json
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vision import frames as frame_tools  # noqa: E402
from vision import review  # noqa: E402
from vision.model import FakeModel, ModelError, Reply, Request, Sampling, VisionClient  # noqa: E402

SAMPLE_ANSWERS = ROOT / "vision" / "samples" / "answers-4-2.json"
SAMPLE_RESULT = ROOT / "contracts" / "samples" / "result-4-2.json"

TMP = tempfile.TemporaryDirectory(prefix="clearframe-test-vision-")
FRAMES = Path(TMP.name) / "frames" / "4-2"


def make_clip(path, frames=120, fps=24, size="320x180"):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate={fps}",
         "-frames:v", str(frames), "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


def setUpModule():
    clip = Path(TMP.name) / "4-2.mp4"
    make_clip(clip)
    frame_tools.extract_frames(clip, FRAMES)


def tearDownModule():
    TMP.cleanup()


class ParseAnswersTest(unittest.TestCase):
    def test_one_line_per_frame(self):
        self.assertEqual(
            review.parse_answers("1: yes\n2: no - the arrow points back", 2),
            [("yes", ""), ("no", "the arrow points back")],
        )

    def test_thinking_and_markdown_are_ignored(self):
        text = "<think>Frame 1: no, maybe.</think>\nHere you go:\n- **1:** Yes\n* 2. no – wrong way\nFrame 3 (top left): yes"
        self.assertEqual(review.parse_answers(text, 3), [("yes", ""), ("no", "wrong way"), ("yes", "")])

    def test_anything_but_one_answer_per_frame_is_unparsed(self):
        for text in (
            "1: yes",                      # frame 2 missing
            "1: yes\n1: no\n2: yes",       # frame 1 twice
            "1: yes\n2: yes\n3: no",       # a frame that wasn't sent
            "<think>1: yes\n2: no",        # cut off while thinking
            "Yes, the arrows look right.",  # no per-frame answers
        ):
            with self.subTest(text=text):
                self.assertIsNone(review.parse_answers(text, 2))


class FramesChosenTest(unittest.TestCase):
    def test_sparse_frames_cover_the_take(self):
        frames = review.sparse_frames(120, 16)
        self.assertEqual(len(frames), 16)
        self.assertEqual((frames[0], frames[-1]), (0, 119))
        self.assertEqual(frames, sorted(set(frames)))
        self.assertEqual(review.sparse_frames(10, 16), list(range(10)))

    def test_a_window_is_consecutive_frames_around_the_moment(self):
        (window,) = review.dense_windows([(61, 63, None)], 120, 12)
        self.assertEqual(window.frames, list(range(window.frames[0], window.frames[0] + 12)))
        self.assertTrue(window.frames[0] <= 61 and 63 <= window.frames[-1])

    def test_windows_stay_inside_the_take(self):
        self.assertEqual(review.dense_windows([(0, 0, None)], 120, 12)[0].frames, list(range(12)))
        self.assertEqual(review.dense_windows([(119, 119, None)], 120, 12)[0].frames, list(range(108, 120)))

    def test_close_moments_share_a_window_and_far_ones_dont(self):
        close = review.dense_windows([(40, 40, 0), (48, 48, 1)], 120, 12)
        self.assertEqual([window.notes for window in close], [[0, 1]])
        far = review.dense_windows([(10, 10, None), (100, 100, 0)], 120, 12)
        self.assertEqual([window.notes for window in far], [[], [0]])

    def test_a_long_moment_gets_frames_spread_over_it(self):
        (window,) = review.dense_windows([(20, 60, None)], 120, 12)
        self.assertEqual(len(window.frames), 12)
        self.assertEqual((window.frames[0], window.frames[-1]), (20, 60))


class SettingsTest(unittest.TestCase):
    def test_votes_need_randomness(self):
        for settings in (
            review.Settings(runs=3, thinking=False, temperature=0.0),
            review.Settings(runs=3, thinking=False, top_k=1),
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()
        review.Settings(runs=3, thinking=True, temperature=0.0).validate()
        review.Settings(runs=1, min_agree=1, thinking=False, temperature=0.0).validate()

    def test_refuses_impossible_settings(self):
        for settings in (review.Settings(mode="clip"), review.Settings(runs=2, min_agree=3)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()

    def test_thinking_is_set_per_request(self):
        client = VisionClient("http://qwen:8000/v1", "qwen")
        request = Request(check="road_markings", frames=(0,), content=({"type": "text", "text": "hi"},))
        payload = client.payload(request, review.Settings(thinking=False).sampling())
        self.assertEqual(payload["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual((payload["model"], payload["top_k"]), ("qwen", 20))
        self.assertGreater(payload["temperature"], 0)
        self.assertEqual(client.url, "http://qwen:8000/v1/chat/completions")


class RequestsTest(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="clearframe-test-requests-")
        self.workdir = Path(self.work.name)
        self.frames = review.sparse_frames(120, 16)

    def tearDown(self):
        self.work.cleanup()

    def requests(self, mode, check="road_markings", cropped=False, frames=None):
        return review.build_requests(check, frames or self.frames, FRAMES, mode, cropped, 8, self.workdir)

    def parts(self, request, kind):
        return [part for part in request.content if part["type"] == kind]

    def decoded_size(self, request):
        url = self.parts(request, "image_url")[0]["image_url"]["url"]
        image = self.workdir / "decoded.jpg"
        image.write_bytes(base64.b64decode(url.split(",", 1)[1]))
        return frame_tools.image_size(image)

    def test_frames_go_as_separate_images_within_the_limit(self):
        requests = self.requests("frames")
        self.assertEqual([len(request.frames) for request in requests], [8, 8])
        self.assertEqual([frame for request in requests for frame in request.frames], self.frames)
        for request in requests:
            self.assertEqual(len(self.parts(request, "image_url")), 8)
            labels = [part["text"] for part in self.parts(request, "text") if part["text"].startswith("Frame ")]
            self.assertEqual(labels, [f"Frame {n}:" for n in range(1, 9)])
            self.assertIn("Reply with exactly 8 lines", request.content[-1]["text"])

    def test_a_grid_is_6_frames_2_across_3_down_cropped_to_the_road(self):
        requests = self.requests("grid")
        self.assertEqual([len(request.frames) for request in requests], [6, 6, 4])
        self.assertEqual(len(self.parts(requests[0], "image_url")), 1)
        # A 320x180 frame cropped to its lower 55% is 320x98; each cell is scaled to 640 wide.
        self.assertEqual(self.decoded_size(requests[0]), (2 * 640, 3 * 196))
        self.assertIn("cropped to the road", requests[0].content[0]["text"])
        self.assertIn("blank", requests[2].content[0]["text"])

    def test_a_hands_grid_keeps_whole_frames(self):
        (request,) = self.requests("grid", check="extra_finger", frames=self.frames[:6])
        self.assertEqual(self.decoded_size(request), (2 * 640, 3 * 360))

    def test_video_is_the_chosen_frames_as_jpegs(self):
        (request,) = self.requests("video")
        (video,) = self.parts(request, "video_url")
        prefix, frames = video["video_url"]["url"].split(",", 1)
        self.assertEqual(prefix, "data:video/jpeg;base64")
        jpegs = [base64.b64decode(frame) for frame in frames.split(",")]
        self.assertEqual(len(jpegs), 16)
        self.assertTrue(all(jpeg.startswith(b"\xff\xd8") for jpeg in jpegs))

    def test_dense_frames_are_cropped_to_the_road(self):
        (request,) = self.requests("frames", cropped=True, frames=list(range(60, 64)))
        self.assertEqual(self.decoded_size(request), (320, 98))


class ScriptedModel:
    """Says the flaw is in every frame on the first call for each set of frames, and never after."""

    name = "scripted"

    def __init__(self):
        self.seen = set()
        self.lock = threading.Lock()

    def ask(self, request: Request, sampling: Sampling) -> Reply:
        with self.lock:
            first = request.frames not in self.seen
            self.seen.add(request.frames)
        answer = "no - wrong way" if first else "yes"
        return Reply("\n".join(f"{n}: {answer}" for n in range(1, len(request.frames) + 1)), 0.0)


class GarbageModel:
    name = "garbage"

    def ask(self, request, sampling):
        return Reply("The arrows look fine to me.", 0.0)


class DownModel:
    name = "down"

    def ask(self, request, sampling):
        raise ModelError("model server unreachable or bad reply: refused")


class ReviewTakeTest(unittest.TestCase):
    def review(self, model, settings=None, checks=("exposure_flicker", "road_markings"), flagged=()):
        return review.review_take("4-2", FRAMES, 120, 24, checks, model, settings or review.Settings(), flagged)

    def test_the_sample_take_gives_one_dense_note_in_the_contract_shape(self):
        sample = json.loads(SAMPLE_RESULT.read_text(encoding="utf-8"))
        answers = json.loads(SAMPLE_ANSWERS.read_text(encoding="utf-8"))["road_markings"]
        result = self.review(FakeModel.from_file(SAMPLE_ANSWERS))

        self.assertEqual(list(result.checks), ["road_markings"])
        self.assertEqual(result.checks["road_markings"].state, "done")
        (note,) = result.candidates
        self.assertEqual(set(note), set(sample["candidates"][0]))
        self.assertEqual((note["issue_type"], note["pass"]), ("road_markings", "dense"))
        start, end = answers["flaw_frames"][0]
        self.assertTrue(start <= note["frame_start"] <= note["frame_end"] <= end)
        self.assertEqual(note["t_start"], round(note["frame_start"] / 24, 3))
        self.assertEqual(note["t_end"], round(note["frame_end"] / 24, 3))
        self.assertEqual(note["model_note"], answers["note"])
        self.assertTrue(all(re.fullmatch(r"frames/4-2/\d{6}\.jpg", path) for path in note["evidence"]))
        self.assertEqual(result.evidence_frames, [int(path[-10:-4]) for path in note["evidence"]])

        self.assertEqual(set(result.vision), set(sample["vision"]))
        self.assertEqual(result.vision["sparse_frames_sent"], 16)
        self.assertEqual((result.vision["dense_windows"], result.vision["dense_frames_sent"]), (1, 12))
        self.assertEqual(result.errors, [])

    def test_a_flag_from_code_gets_a_dense_window(self):
        result = self.review(FakeModel({}), flagged=[(100, 102)])
        self.assertEqual(result.candidates, [])
        self.assertEqual(result.vision["dense_windows"], 1)

    def test_a_note_needs_two_runs_to_agree(self):
        self.assertEqual(self.review(ScriptedModel()).candidates, [])
        one_vote = review.Settings(runs=3, min_agree=1)
        self.assertTrue(self.review(ScriptedModel(), one_vote).candidates)

    def test_no_usable_answer_fails_the_check_with_a_fixed_message(self):
        for model in (GarbageModel(), DownModel()):
            with self.subTest(model=model.name):
                result = self.review(model)
                self.assertEqual(result.checks["road_markings"].state, "error")
                self.assertEqual(result.candidates, [])
                self.assertEqual(result.errors, ["road_markings: the reviewer gave no usable answer"])

    def test_the_accuracy_test_counts_unparsed_replies_instead(self):
        result = self.review(GarbageModel(), review.Settings(strict=False))
        checked = result.checks["road_markings"]
        self.assertEqual(checked.state, "done")
        self.assertEqual(checked.unparsed, checked.requests * 3)


if __name__ == "__main__":
    unittest.main()
