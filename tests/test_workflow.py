"""Tests of the workflow file .github/workflows/tests.yml. They check the job timeout, the fixed package versions for
each Python, and the concurrency group. They read the file only. No network."""
import re
import unittest
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOW = REPO / ".github" / "workflows" / "tests.yml"
PACKAGES = ["cryptography", "httpx", "pyyaml"]


class WorkflowTests(unittest.TestCase):

    def setUp(self):
        self.doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.job = self.doc["jobs"]["run-all"]

    def test_the_job_may_run_for_60_minutes(self):
        self.assertGreaterEqual(self.job["timeout-minutes"], 60)

    def test_each_python_installs_the_three_packages_at_fixed_versions(self):
        matrix = self.job["strategy"]["matrix"]
        specs = {e["python"]: e["packages"].split() for e in matrix.get("include", [])}
        self.assertEqual(sorted(specs), sorted(matrix["python"]))
        for python, spec in specs.items():
            with self.subTest(python=python):
                self.assertEqual(sorted(s.partition("==")[0] for s in spec), PACKAGES)
                self.assertTrue(all(re.fullmatch(r"[a-z]+==\d+(\.\d+)+", s) for s in spec), spec)
        install = [s["run"] for s in self.job["steps"] if "pip install" in s.get("run", "")]
        self.assertEqual(install, ["python -m pip install ${{ matrix.packages }}"])

    def test_a_new_run_on_the_same_ref_stops_the_older_one(self):
        self.assertEqual(self.doc.get("concurrency"), {"group": "tests-${{ github.ref }}", "cancel-in-progress": True})


if __name__ == "__main__":
    unittest.main()
