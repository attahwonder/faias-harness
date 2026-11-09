"""Tests of the workflow file .github/workflows/tests.yml. They check the job timeout, the fixed package versions for
each Python, the job with inspect_ai and the concurrency group. They read the file only. No network."""
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

    def test_the_inspect_job_takes_the_steps_of_the_python_3_14_row_and_a_fixed_inspect_ai(self):
        job = self.doc["jobs"]["run-all-inspect"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertGreaterEqual(job["timeout-minutes"], 60)
        self.assertEqual(job["steps"][1]["with"], {"python-version": "3.14"})
        install = job["steps"][2]["run"]
        self.assertTrue(install.startswith("python -m pip install "), install)
        spec = install.removeprefix("python -m pip install ").split()
        self.assertTrue(all(re.fullmatch(r"[a-z_]+==\d+(\.\d+)+", s) for s in spec), spec)
        row = {e["python"]: e["packages"].split() for e in self.job["strategy"]["matrix"]["include"]}
        self.assertEqual(spec[:-1], row["3.14"])
        self.assertEqual(spec[-1], "inspect_ai==0.3.281")     # the version that README.md and AGENTS.md name
        # The checkout, the action that sets up Python, the FAIAS clone and the run are those of run-all.
        self.assertEqual(job["steps"][0], self.job["steps"][0])
        self.assertEqual(job["steps"][1]["uses"], self.job["steps"][1]["uses"])
        self.assertEqual(job["steps"][3:], self.job["steps"][3:])

    def test_a_new_run_on_the_same_ref_stops_the_older_one(self):
        self.assertEqual(self.doc.get("concurrency"), {"group": "tests-${{ github.ref }}", "cancel-in-progress": True})


if __name__ == "__main__":
    unittest.main()
