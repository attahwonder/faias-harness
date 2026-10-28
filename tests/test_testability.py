"""The testability report: counts equal to the preflight, hashes, the lever table, the signature and no network."""
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from faias_harness import preflight, testability
from faias_harness.engine import runlog, sec001
from faias_harness.profile import load_profile
from tests.helpers import no_outside_network

REPO = Path(__file__).resolve().parents[1]
SRD1 = REPO / "targets" / "srd1" / "profile.yaml"
CHAT = REPO / "targets" / "examples" / "chat_only.yaml"


def preflight_counts(profile_path):
    with contextlib.redirect_stdout(io.StringIO()):
        return preflight.report(load_profile(profile_path), preflight.load_needs())


def run_main(*argv):
    out = io.StringIO()
    with contextlib.redirect_stdout(out), no_outside_network():
        rc = testability.main([str(a) for a in argv])
    return rc, out.getvalue()


def expected_levers(capabilities):
    """The lever table computed here from the needs file, without the module, so the test does not trust it."""
    needs = {tp: set(e["needed"]) for tp, e in yaml.safe_load(
        (REPO / "records" / "capability_needs.yaml").read_text(encoding="utf-8"))["needs"].items()}
    have = set(capabilities)
    table = {}
    for cap in ("conversation", "traces", "tool_routing", "state_read", "data_seeding", "config_read", "control"):
        if cap not in have:
            table[cap] = sorted(tp for tp, n in needs.items() if not n <= have and n <= have | {cap})
    return table


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def report(self, profile, name="out", *extra):
        rc, out = run_main("--profile", profile, "--out", self.dir / name, *extra)
        self.assertEqual(rc, 0, out)
        d = self.dir / name
        return json.loads((d / "testability.json").read_text(encoding="utf-8")), \
            (d / "testability.md").read_text(encoding="utf-8")

    def test_counts_equal_the_preflight_summary(self):
        for profile in (SRD1, CHAT):
            rep, _ = self.report(profile, profile.stem)
            pf = preflight_counts(profile)
            s = rep["summary"]
            self.assertEqual((s["full"], s["partly"], s["none"]), (pf["full"], pf["partly"], pf["none"]), profile)
            self.assertEqual(s["procedures"], 45)
            self.assertEqual(len(rep["rows"]), 45)
        rep, _ = self.report(SRD1, "srd1_again")
        self.assertEqual((rep["summary"]["full"], rep["summary"]["partly"], rep["summary"]["none"]), (45, 0, 0))

    def test_rows_copy_the_record_and_use_the_preflight_logic(self):
        rep, _ = self.report(CHAT)
        needs_file = yaml.safe_load((REPO / "records" / "capability_needs.yaml").read_text(encoding="utf-8"))["needs"]
        have = set(rep["profile"]["capabilities"])
        statuses = {"agreed": 0, "settled": 0, "open": 0}
        for r in rep["rows"]:
            rec = yaml.safe_load((REPO / "records" / f"{r['tp']}.yaml").read_text(encoding="utf-8"))
            for k in ("tp", "irq", "title", "star", "method_class"):
                self.assertEqual(r[k], rec[k], (r["tp"], k))
            self.assertEqual((r["result"], r["missing"]), preflight.judge(needs_file[r["tp"]]["needed"], have))
            self.assertEqual(r["lines"], needs_file[r["tp"]]["lines"])
            statuses[r["needs_status"]] += 1
        self.assertEqual(statuses, {"agreed": 26, "settled": 17, "open": 2})
        self.assertEqual({r["tp"] for r in rep["rows"] if r["needs_status"] == "open"}, set(preflight.open_entries()))

    def test_header_says_the_needs_are_a_draft_and_names_the_inputs(self):
        rep, md = self.report(CHAT)
        status = yaml.safe_load((REPO / "records" / "capability_needs.yaml").read_text(encoding="utf-8"))["status"]
        first = md.splitlines()[0]
        self.assertIn("No person has checked the capability needs", first)
        self.assertIn(f'"{status}"', first)
        self.assertEqual(rep["needs_draft"]["status_line"], status)
        self.assertTrue(rep["needs_draft"]["draft"])
        self.assertEqual(rep["profile"]["name"], "Example, chat only")
        self.assertEqual(rep["profile"]["sha256"], runlog.sha256_hex(CHAT.read_bytes()))
        self.assertEqual(rep["inputs"]["capability_needs"]["sha256"],
                         runlog.sha256_hex((REPO / "records" / "capability_needs.yaml").read_bytes()))
        self.assertEqual(len(rep["inputs"]["records"]), 45)
        for f in rep["inputs"]["records"]:
            self.assertEqual(f["sha256"], runlog.sha256_hex((REPO / f["file"]).read_bytes()))
        from faias_harness import __version__
        self.assertEqual(rep["harness_version"], __version__)
        self.assertEqual(rep["faias_version"], sec001.FAIAS_VERSION)
        self.assertTrue(rep["time_utc"].endswith("+00:00"))
        self.assertEqual(len(rep["not_shown"]), 4)
        self.assertIn("## What this report does not show", md)
        self.assertIn("| Signature | unsigned |", md)
        self.assertIsNone(rep["signature"])
        self.assertIn(runlog.sha256_hex((self.dir / "out" / "testability.json").read_bytes()), md)

    def test_a_profile_change_changes_the_profile_hash(self):
        copy = self.dir / "chat_copy.yaml"
        copy.write_bytes(CHAT.read_bytes())
        a, _ = self.report(copy, "a")
        copy.write_bytes(CHAT.read_bytes() + b"# one more comment line\n")
        b, _ = self.report(copy, "b")
        self.assertNotEqual(a["profile"]["sha256"], b["profile"]["sha256"])
        self.assertEqual(a["summary"], b["summary"])

    def test_lever_table_for_chat_only(self):
        rep, md = self.report(CHAT)
        expected = expected_levers(rep["profile"]["capabilities"])
        got = {lv["capability"]: lv["procedures"] for lv in rep["levers"]}
        self.assertEqual(got, expected)
        counts = [lv["to_full"] for lv in rep["levers"]]
        self.assertEqual(counts, sorted(counts, reverse=True))
        top = max(expected, key=lambda c: len(expected[c]))
        self.assertEqual(rep["levers"][0]["capability"], top)
        self.assertEqual(top, "config_read")     # computed above from the needs file, not assumed
        self.assertIn(f"| `{top}` | {len(expected[top])} |", md)

    def test_a_profile_with_every_capability_has_no_lever(self):
        rep, _ = self.report(SRD1)
        self.assertEqual(rep["levers"], [])

    def test_refuses_an_out_folder_in_records_or_the_package(self):
        for folder in ("records", "faias_harness"):
            target = REPO / folder / "testability_out_test"
            rc, out = run_main("--profile", CHAT, "--out", target)
            self.assertEqual(rc, 2)
            self.assertIn(f"inside {folder}/", out)
            self.assertFalse(target.exists())

    def test_refuses_a_folder_that_holds_a_report(self):
        self.report(CHAT)
        rc, out = run_main("--profile", CHAT, "--out", self.dir / "out")
        self.assertEqual(rc, 2)
        self.assertIn("holds a report file already", out)

    def test_refuses_an_invalid_profile(self):
        bad = self.dir / "bad.yaml"
        bad.write_text(yaml.safe_dump({"name": "x", "capabilities": ["telepathy"]}), encoding="utf-8")
        rc, out = run_main("--profile", bad, "--out", self.dir / "bad_out")
        self.assertEqual(rc, 2)
        self.assertIn("PROFILE INVALID", out)
        self.assertFalse((self.dir / "bad_out").exists())


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.priv, self.pub = runlog.make_key(self.dir / "keys", repo=REPO)
        rc, out = run_main("--profile", CHAT, "--out", self.dir / "rep", "--sign-key", self.priv)
        self.assertEqual(rc, 0, out)
        self.json = self.dir / "rep" / "testability.json"

    def tearDown(self):
        self.tmp.cleanup()

    def verify(self, *extra):
        return run_main("verify", "--report", self.json, "--pubkey", self.pub, *extra)

    def test_round_trip(self):
        self.assertTrue((self.dir / "rep" / "testability.json.sig").is_file())
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        rep = json.loads(self.json.read_text(encoding="utf-8"))
        self.assertEqual(rep["signature"], {"alg": "Ed25519", "file": "testability.json.sig",
                                            "covers": "testability.json"})
        md = (self.dir / "rep" / "testability.md").read_text(encoding="utf-8")
        self.assertNotIn("| Signature | unsigned |", md)
        self.assertIn("Ed25519, detached", md)

    def test_tampered_json_fails(self):
        data = self.json.read_bytes()
        self.json.write_bytes(data.replace(b'"full":5', b'"full":6', 1))
        self.assertNotEqual(self.json.read_bytes(), data)
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)

    def test_another_key_fails(self):
        _, other_pub = runlog.make_key(self.dir / "other_keys", repo=REPO)
        rc, out = run_main("verify", "--report", self.json, "--pubkey", other_pub)
        self.assertEqual(rc, 1)
        self.assertIn("does not match", out)

    def test_a_signed_report_with_a_wrong_result_fails_the_form_check(self):
        rep = json.loads(self.json.read_text(encoding="utf-8"))
        rep["rows"][0]["result"] = "full"
        self.json.write_bytes((runlog.canonical(rep) + "\n").encode("utf-8"))
        runlog.sign_log(self.json, self.priv)
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("malformed", out)

    def test_refuses_a_key_inside_the_repository(self):
        rc, out = run_main("--profile", CHAT, "--out", self.dir / "rep2", "--sign-key", REPO / "README.md")
        self.assertEqual(rc, 2)
        self.assertIn("inside the repository", out)
        self.assertFalse((self.dir / "rep2").exists())


class NoNetworkTests(unittest.TestCase):
    def test_the_module_imports_no_http_client(self):
        src = (REPO / "faias_harness" / "testability.py").read_text(encoding="utf-8")
        for name in ("httpx", "requests", "urllib", "http.client", "socket", "aiohttp", "egress", "adapters"):
            self.assertNotIn(f"import {name}", src)
            self.assertNotIn(f"from {name}", src)
            self.assertNotIn(f"from .{name}", src)
        code = ("import sys, faias_harness.testability; "
                "print(sorted(m for m in ('httpx', 'requests', 'urllib3', 'aiohttp', 'http.client', 'urllib.request', "
                "'faias_harness.egress', 'faias_harness.adapters') if m in sys.modules))")
        p = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "[]")


if __name__ == "__main__":
    unittest.main()
