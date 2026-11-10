"""The check that the run engine inspect_ai can run an eval (spike.inspect_problem, preflight._inspect_missing).

The import function is injected, so no test needs inspect_ai. The preflight uses the check before --start, an
external run and a bench cell. inspect_available() does not change, so the tests and the self-test still choose the
engine by it.
"""
import unittest
from unittest import mock

from faias_harness import preflight
from faias_harness.engine import spike

PINS = "install inspect_ai 0.3.281 with agent-client-protocol 0.12.1 (README.md)"


class InspectProblemTests(unittest.TestCase):
    def check(self, import_module, available=True):
        with mock.patch.object(spike, "inspect_available", return_value=available):
            return spike.inspect_problem(import_module)

    def test_inspect_ai_not_found(self):
        self.assertEqual(self.check(None, available=False), "the run engine inspect_ai is not installed")

    def test_the_module_of_the_eval_path_loads(self):
        seen = []
        self.assertIsNone(self.check(seen.append))
        self.assertEqual(seen, ["inspect_ai.agent._acp.server"])

    def test_a_module_that_the_eval_path_needs_is_not_found(self):
        """The case of agent-client-protocol 1.0.0, which has no module acp.helpers."""
        def imp(name):
            raise ModuleNotFoundError("No module named 'acp.helpers'", name="acp.helpers")
        self.assertEqual(self.check(imp), "the run engine inspect_ai is installed but cannot run an eval: the module "
                                          f"acp.helpers is not found; {PINS}")

    def test_an_import_that_fails_in_another_way(self):
        def imp(name):
            raise ImportError("cannot import name 'x'")
        self.assertEqual(self.check(imp), f"the run engine inspect_ai is installed but cannot run an eval (ImportError); "
                                          f"{PINS}")

    def test_a_moved_private_module_gives_no_refusal(self):
        """A later inspect_ai that moves its private module: the check cannot test the eval path, so it does not
        refuse a working install."""
        for missing in ("inspect_ai.agent._acp.server", "inspect_ai.agent._acp", "inspect_ai.agent"):
            def imp(name, m=missing):
                raise ModuleNotFoundError(f"No module named {m!r}", name=m)
            with self.subTest(missing=missing):
                self.assertIsNone(self.check(imp))

    def test_the_preflight_check_returns_it(self):
        with mock.patch.object(spike, "inspect_problem", return_value="a reason"):
            self.assertEqual(preflight._inspect_missing(), "a reason")


if __name__ == "__main__":
    unittest.main()
