"""rubric/rubric.yaml must agree with the contract, and its loader must refuse what it can't read.

The card wording is checked by rendering the contract's sample cards from the
templates, and the fix library by rebuilding the sample's prompt version 2.
"""
import hashlib
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rubric import rubric as rubric_loader  # noqa: E402

CONTRACT = (ROOT / "contracts" / "README.md").read_text(encoding="utf-8")


def load_sample(name):
    return json.loads((ROOT / "contracts" / "samples" / name).read_text(encoding="utf-8"))


def sha256_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def contract_section(heading):
    start = CONTRACT.index(f"## {heading}\n")
    end = CONTRACT.find("\n## ", start + 1)
    return CONTRACT[start:] if end == -1 else CONTRACT[start:end]


def table_rows(section):
    """Rows of every Markdown table in a section, as lists of cells, without headers."""
    rows = []
    for line in section.splitlines():
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def counts_text(card, issues):
    """The counts as a card builder would write them, from (state, issue_type) pairs."""
    parts = []
    for state, forms in card["state_labels"].items():
        for issue_type, category in card["category_labels"].items():
            count = issues.count((state, issue_type))
            if count:
                label = forms["one"] if count == 1 else forms["many"]
                parts.append(card["count"].format(count=count, state=label, category=category))
    return card["count_separator"].join(parts)


def shot_card_text(card, shot, expected_takes, takes, link):
    """takes: (take_id, status, issues) for every final original take, in take order."""
    shot_card = card["shot_card"]
    lines = [shot_card["header"].format(shot=shot, reviewed=len(takes), expected_takes=expected_takes)]
    for take_id, status, issues in takes:
        if status == "clean":
            continue
        counts = counts_text(card, issues)
        template = shot_card["take"] if counts else shot_card["take_no_counts"]
        lines.append(template.format(take_id=take_id, status=card["status_labels"][status], counts=counts))
    clean = [take_id for take_id, status, _ in takes if status == "clean"]
    if clean:
        lines.append(shot_card["clean"].format(clean_takes=shot_card["clean_separator"].join(clean)))
    else:
        lines.append(shot_card["clean_none"])
    lines.append(shot_card["link"].format(link=link))
    return "\n".join(lines)


class RubricMatchesContractTest(unittest.TestCase):
    def setUp(self):
        self.rubric = rubric_loader.load()
        self.card = self.rubric["card"]

    def test_enabled_checks(self):
        self.assertEqual(self.rubric["checks"], ["exposure_flicker", "road_markings"])

    def test_issue_types_and_category_labels_match_the_contract(self):
        rows = [row for row in table_rows(contract_section("Shared names")) if len(row) == 4]
        from_contract = {row[0].strip("`"): row[2] for row in rows if row[0].startswith("`")}
        self.assertEqual(tuple(from_contract), rubric_loader.ISSUE_TYPES)
        self.assertEqual(self.card["category_labels"], from_contract)
        self.assertEqual(tuple(self.rubric["fix_library"]), rubric_loader.ISSUE_TYPES)

    def test_status_and_state_labels_match_the_contract(self):
        labels = {}
        for value, label in (row for row in table_rows(contract_section("Slack card")) if len(row) == 2):
            match = re.fullmatch(r"(?:Issue state )?`(\w+)`", value)
            if match:
                labels[match.group(1)] = label
        for status, label in self.card["status_labels"].items():
            self.assertEqual(labels.pop(status), label)
        for state, forms in self.card["state_labels"].items():
            self.assertEqual(labels.pop(state), forms["one"])
        self.assertEqual(labels, {})

    def test_flicker_settings(self):
        self.assertIsInstance(self.rubric["flicker_threshold"], float)
        self.assertIsInstance(self.rubric["flicker_return_frames"], int)

    def test_sample_approver_is_allowed(self):
        approver = load_sample("queue-4-2-v2.json")["approval"]["approver"]
        self.assertIn(approver, self.rubric["approvers"])

    def test_approver_names_are_not_card_text(self):
        templates = json.dumps(self.card)
        for name in self.rubric["approvers"]:
            self.assertNotIn(name, templates)


class FixLibraryTest(unittest.TestCase):
    def setUp(self):
        self.rubric = rubric_loader.load()

    def test_lines_are_labeled_in_contract_order(self):
        labels = [label + ": " for label in self.rubric["fix_labels"].values()]
        self.assertEqual(labels, ["Don't include: ", "Add: ", "Make sure: "])
        for issue_type in self.rubric["fix_library"]:
            lines = rubric_loader.fix_lines(self.rubric, issue_type)
            self.assertTrue(lines)
            positions = [next(i for i, label in enumerate(labels) if line.startswith(label)) for line in lines]
            self.assertEqual(positions, sorted(set(positions)))

    def test_rebuilds_the_fixed_prompt_for_4_2(self):
        version_1 = load_sample("result-4-2.json")["generation"]["prompt"]
        queue = load_sample("queue-4-2-v2.json")
        self.assertEqual(queue["fixes"], ["road_markings"])
        version_2 = version_1 + "\n\n" + "\n".join(rubric_loader.fix_lines(self.rubric, "road_markings"))
        self.assertEqual(version_2, queue["prompt"])
        self.assertEqual(sha256_text(version_2), queue["prompt_hash"])
        self.assertIn(sha256_text(version_2), CONTRACT)


class CardWordingTest(unittest.TestCase):
    def setUp(self):
        self.card = rubric_loader.load()["card"]
        section = contract_section("Slack card")
        self.samples = re.findall(r"```text\n(.*?)\n```", section, re.DOTALL)
        self.assertEqual(len(self.samples), 2)

    def test_shot_card_matches_the_contract_sample(self):
        takes = [
            ("4-1", "clean", []),
            ("4-2", "needs_confirmation", [("candidate", "road_markings")]),
            ("4-3", "clean", []),
            ("4-4", "clean", []),
            ("4-5", "flagged", [("measured", "exposure_flicker")]),
            ("4-6", "clean", []),
        ]
        text = shot_card_text(self.card, 4, 6, takes, "http://127.0.0.1:8090/shot/4")
        self.assertEqual(text, self.samples[0])

    def test_retake_card_matches_the_contract_sample(self):
        retake_card = self.card["retake_card"]
        text = "\n".join([
            retake_card["header_no_counts"].format(
                take_id="4-2r1", parent_take_id="4-2", status=self.card["status_labels"]["clean"]
            ),
            retake_card["link"].format(link="http://127.0.0.1:8090/take/4-2r1"),
        ])
        self.assertEqual(text, self.samples[1])

    def test_counts_use_plurals_and_rubric_order(self):
        issues = [
            ("candidate", "road_markings"),
            ("measured", "exposure_flicker"),
            ("measured", "exposure_flicker"),
            ("dismissed", "road_markings"),
        ]
        self.assertEqual(
            counts_text(self.card, issues),
            "2 measured anomalies (exposure), 1 candidate note (road markings)",
        )

    def test_check_error_label(self):
        self.assertEqual(self.card["check_error"].format(category="exposure"), "check error (exposure)")

    def test_shot_card_with_no_clean_takes(self):
        takes = [("2-1", "flagged", [("confirmed", "extra_finger")])]
        text = shot_card_text(self.card, 2, 6, takes, "http://127.0.0.1:8090/shot/2")
        self.assertEqual(text.splitlines()[1:3], ["2-1: flagged · 1 confirmed note (extra finger)", "Clean: none"])


class LoaderTest(unittest.TestCase):
    def setUp(self):
        self.text = rubric_loader.RUBRIC.read_text(encoding="utf-8")

    def assertRefused(self, text, message):
        with self.assertRaises(rubric_loader.RubricError) as caught:
            rubric_loader.validate(rubric_loader.parse(text))
        self.assertIn(message, str(caught.exception))

    def test_parses_the_supported_yaml(self):
        parsed = rubric_loader.parse(
            "# comment\n"
            "a: 1\n"
            "b: 2.5  # trailing comment\n"
            'c: "Don\'t # \\"quoted\\""\n'
            "d:\n"
            "  - word_one\n"
            '  - "two"\n'
            "e:\n"
            "  f: true\n"
            "  g: null\n"
        )
        self.assertEqual(
            parsed,
            {"a": 1, "b": 2.5, "c": 'Don\'t # "quoted"', "d": ["word_one", "two"], "e": {"f": True, "g": None}},
        )

    def test_refuses_what_it_does_not_read(self):
        for text, message in (
            ("a:\n\t- b\n", "tabs"),
            ("a: 'b'\n", "double quotes"),
            ("a: yes\n", "double quotes"),
            ("a: two words\n", "double quotes"),
            ("a:\n   - b\n", "two spaces"),
            ("a: 1\n  b: 2\n", "unexpected indent"),
            ("a: 1\na: 2\n", "duplicate key"),
            ("a:\nb: 1\n", "has no value"),
            ('"a": 1\n', "lowercase key"),
        ):
            with self.subTest(text=text):
                with self.assertRaises(rubric_loader.RubricError) as caught:
                    rubric_loader.parse(text)
                self.assertIn(message, str(caught.exception))

    def test_refuses_an_unknown_check(self):
        self.assertRefused(self.text.replace("  - road_markings\n", "  - lens_flare\n"), "unknown issue type")

    def test_refuses_checks_out_of_rubric_order(self):
        text = self.text.replace("  - exposure_flicker\n  - road_markings\n", "  - road_markings\n  - exposure_flicker\n")
        self.assertRefused(text, "fix_library order")

    def test_refuses_fix_lines_out_of_label_order(self):
        old = '    dont_include: "flicker'
        start = self.text.index(old)
        end = self.text.index("\n  road_markings:")
        dont, make = self.text[start:end].split("\n")
        self.assertRefused(self.text[:start] + make + "\n" + dont + self.text[end:], "in that order")

    def test_refuses_a_card_field_outside_the_contract(self):
        text = self.text.replace("{take_id}: {status} · {counts}", "{take_id}: {status} · {prompt}")
        self.assertRefused(text, "not ['prompt']")

    def test_refuses_a_whole_number_threshold(self):
        self.assertRefused(re.sub(r"flicker_threshold: [0-9.]+", "flicker_threshold: 8", self.text), "flicker_threshold")

    def test_matches_pyyaml_when_installed(self):
        try:
            import yaml  # pyright: ignore[reportMissingModuleSource]
        except ImportError:
            self.skipTest("PyYAML is not installed")
        self.assertEqual(rubric_loader.parse(self.text), yaml.safe_load(self.text))


if __name__ == "__main__":
    unittest.main()
