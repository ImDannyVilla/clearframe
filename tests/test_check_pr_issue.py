"""A pull request's description must name its issue, as AGENTS.md says."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_pr_issue.py"
sys.path.insert(0, str(ROOT))

from scripts import check_pr_issue  # noqa: E402


class IssuesNamedTest(unittest.TestCase):
    def test_closes_and_part_of(self):
        body = "Closes #4. What's left moves to #16.\n\nPart of #2."
        self.assertEqual(check_pr_issue.issues_named(body), [("closes", 4), ("part of", 2)])

    def test_case_and_colon_dont_matter(self):
        self.assertEqual(check_pr_issue.issues_named("closes: #7"), [("closes", 7)])
        self.assertEqual(check_pr_issue.issues_named("PART OF #10"), [("part of", 10)])

    def test_a_bare_mention_is_not_enough(self):
        for body in ("Refs #4.", "See #4 and #5.", "Closes 4", "", "Closes #4x"):
            with self.subTest(body=body):
                self.assertEqual(check_pr_issue.issues_named(body), [])


class ScriptTest(unittest.TestCase):
    def run_script(self, body):
        env = {**os.environ, "PR_BODY": body}
        return subprocess.run([sys.executable, str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_passes_with_an_issue(self):
        result = self.run_script("Closes #4.")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_fails_without_one(self):
        result = self.run_script("Refs #4.")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Closes #N", result.stderr)

    def test_an_empty_description_fails(self):
        # GitHub sends an empty body as an empty string.
        self.assertEqual(self.run_script("").returncode, 1)


if __name__ == "__main__":
    unittest.main()
