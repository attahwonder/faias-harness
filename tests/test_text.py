"""Text that the harness does not write itself.

`text.clean` replaces each lone surrogate of a reply with U+FFFD. The tests of each path that a reply takes are in
tests/test_external.py, test_e2e.py, test_gateway.py and test_srd1_tools.py; the test here is of the function alone.

`text.escape_streams` sets stdout and stderr to print a character that the stream cannot encode as an escape. Each
command of the harness calls it first. The tests here give each command that can print text from a file of the
operator a file that holds a lone surrogate (the JSON escape \\ud83d alone; in YAML, read with the pure-Python
loader; or a file name). Each test runs the command with a stdout that encodes as strict UTF-8
(tests/helpers.StrictStdout). The command prints the escape and stops with no traceback. One test runs a command in a
new process, with the real stdout. The test of the preflight (a TP-SEC-003 item ID) is in tests/test_sec003_run.py.

No test contacts a host outside the local machine.
"""
import contextlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from faias_harness import selftest, testability, yamlio
from faias_harness.emulators import mcp_tools
from faias_harness.engine import runlog, sec002_items
from tests.helpers import StrictStdout
from tests.test_sec002_run import item_file

REPO = Path(__file__).resolve().parents[1]
S = "\ud83d"                    # a lone surrogate: a high surrogate with no low half
ESCAPED = "\\ud83d"             # how a command prints it (errors="backslashreplace")
R = chr(0xFFFD)                 # U+FFFD, the Unicode replacement character


def tool(name):
    """A script of tools/, loaded as a module."""
    spec = importlib.util.spec_from_file_location(f"faias_harness_tool_{name}", REPO / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class CleanTests(unittest.TestCase):
    def test_clean_replaces_each_lone_surrogate_at_any_depth_and_keeps_a_surrogate_pair(self):
        from faias_harness.text import clean
        pair = chr(0x1F600)     # one code point outside the basic plane: in JSON a surrogate pair, not a lone one
        value = {"a" + S: ["x" + S, {"b": "\udc00y"}, 3, None, True, 1.5, pair]}
        self.assertEqual(clean(value), {"a" + R: ["x" + R, {"b": R + "y"}, 3, None, True, 1.5, pair]})
        self.assertEqual(clean(json.loads('"\\ud83d\\ude00"')), pair)


class PrintedTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, main, argv):
        out = StrictStdout()
        with contextlib.redirect_stdout(out.stream):
            rc = main(argv)
        return rc, out.text()

    def labelled_log(self):
        """A file whose first line is a header with a label that holds the lone surrogate, as a log of an operator can
        hold it, and a signature file. It is not a signed run log, so verify fails."""
        log = self.dir / "run_log.jsonl"
        log.write_text(json.dumps({"label": f"a test label {S}"}) + "\n", encoding="ascii")
        sig = self.dir / "run_log.jsonl.sig"
        sig.write_text("{}\n", encoding="ascii")
        return log, sig

    def test_runlog_verify_rescore_and_stamp_print_the_label_with_the_escape(self):
        log, sig = self.labelled_log()
        for argv, code in ((["verify", "--log", str(log), "--sig", str(sig)], 1), (["rescore", "--log", str(log)], 2),
                           (["stamp", "--log", str(log)], 0)):
            with self.subTest(command=argv[0]):
                rc, out = self.run_main(runlog.main, argv)
                self.assertIn(f"label: a test label {ESCAPED}\n", out)
                self.assertEqual(rc, code, out)

    def test_runlog_verify_in_a_new_process_prints_the_escape_and_no_traceback(self):
        log, sig = self.labelled_log()
        p = subprocess.run([sys.executable, "-X", "utf8", "-m", "faias_harness.engine.runlog", "verify", "--log",
                            str(log), "--sig", str(sig)], cwd=REPO, capture_output=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertNotIn(b"Traceback", p.stderr, p.stderr)
        self.assertIn(f"label: a test label {ESCAPED}".encode("ascii"), p.stdout)
        self.assertEqual(p.returncode, 1, p.stderr)

    def test_selftest_rerun_prints_a_probe_id_with_the_escape(self):
        log = self.dir / "selftest_log.jsonl"
        lines = [{"schema": selftest.SCHEMA, "code_manifest": {"sha256": "0" * 64}, "environment": {}},
                 {"type": "probe", "id": f"S99{S}"}]
        log.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="ascii")
        rc, out = self.run_main(selftest.main, ["rerun", "--log", str(log)])
        self.assertIn(f"DIFFERENCE S99{ESCAPED}: this checkout has no such probe\n", out)
        self.assertEqual(rc, 1, out)

    def test_testability_verify_prints_a_row_with_the_escape(self):
        priv, pub = runlog.make_key(self.dir / "keys", repo=REPO)
        rep = {"schema": testability.SCHEMA, "needs_draft": {"status_line": "a draft"},
               "profile": {"capabilities": []},
               "rows": [{"tp": f"TP-TST-001{S}", "needed": ["conversation"], "result": "full", "missing": []}]}
        path = self.dir / "testability.json"
        path.write_text(json.dumps(rep) + "\n", encoding="ascii")
        runlog.sign_log(path, priv)
        rc, out = self.run_main(testability.main, ["verify", "--report", str(path), "--pubkey", str(pub)])
        self.assertIn(f"TP-TST-001{ESCAPED}: the result does not follow from its needs", out)
        self.assertEqual(rc, 1, out)

    def test_mcp_tools_serve_prints_a_tools_file_problem_with_the_escape(self):
        tools = self.dir / "tools.yaml"
        tools.write_text(f'tools:\n  - name: test_tool\n    "note{ESCAPED}": 1\n', encoding="ascii")
        with mock.patch.object(yamlio, "loader", lambda: yaml.SafeLoader):
            rc, out = self.run_main(mcp_tools.main, ["serve", "--tools", str(tools), "--ledger",
                                                     str(self.dir / "ledger.jsonl")])
        self.assertTrue(out.startswith("refused: "), out)
        self.assertIn(f"tool 1: unknown field(s) note{ESCAPED}", out)
        self.assertEqual(rc, 2)
        self.assertFalse((self.dir / "ledger.jsonl").exists())

    def test_sec002_items_check_prints_a_sequence_id_with_the_escape(self):
        data = item_file()
        seq = data["tracks"]["A"][0]
        seq["id"] = seq["id"] + S
        path = self.dir / "TP-SEC-002.json"
        path.write_text(json.dumps(data), encoding="ascii")
        rc, out = self.run_main(sec002_items.main, ["check", str(path)])
        self.assertIn(ESCAPED, out)
        self.assertNotIn(S, out)
        self.assertEqual(rc, 1, out)

    def test_sweep_barred_prints_a_file_name_with_the_escape(self):
        root = self.dir / "root"
        root.mkdir()
        name = f"notes{S}.txt" if os.name == "nt" else os.fsdecode(b"notes\xff.txt")
        try:
            (root / name).write_bytes(b"\xff\n")        # not UTF-8 text, so the sweep names the file
        except (OSError, UnicodeError) as e:
            self.skipTest(f"this file system takes no such file name ({type(e).__name__})")
        rc, out = self.run_main(tool("sweep_barred").main, ["--root", str(root)])
        shown = name.encode("utf-8", "backslashreplace").decode("ascii")
        self.assertIn(f"FAIL {shown}: not UTF-8 text\n", out)
        self.assertEqual(rc, 1)

    def test_check_records_prints_a_problem_with_the_escape(self):
        mod = tool("check_records")
        found = ([f"TP-TST-001 thresholds[0]: a problem {S}"], {"records": 1, "thresholds": 1, "firm": 0})
        with mock.patch.object(mod, "check", return_value=found):
            rc, out = self.run_main(mod.main, ["--faias", str(self.dir)])
        self.assertIn(f"a problem {ESCAPED}\n", out)
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
