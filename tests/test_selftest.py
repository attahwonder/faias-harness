"""The containment self-test (faias_harness/selftest): the guard, the static scan, the probes, the log, rerun and the
preflight line. No test contacts a host outside the local machine; every probe target is a reserved name or a
documentation address.
"""
import contextlib
import io
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from faias_harness import preflight, selftest
from faias_harness.egress import EgressClient
from faias_harness.engine import runlog, spike
from faias_harness.selftest import guard as guard_mod
from faias_harness.selftest import probes as probes_mod
from faias_harness.selftest import scan as scan_mod
from faias_harness.selftest.guard import Guard, GuardTrip
from tests.helpers import no_outside_network

REPO = Path(__file__).resolve().parents[1]
SUMMARY = re.compile(r"paths probed: (\d+), refused: (\d+), guard trips: (\d+), static scan: (\d+) unexpected, "
                     r"result: (PASS|FAIL)")
TEST_NETS = [ipaddress.ip_network(n) for n in probes_mod.TEST_NETS]


def call(fn, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = fn(argv)
    return rc, buf.getvalue()


def copy_code(dst):
    """The files that the scan reads and that the log check needs, in a new folder."""
    for r in scan_mod.ROOTS:
        shutil.copytree(REPO / r, dst / r, ignore=shutil.ignore_patterns("__pycache__"))
    for f in scan_mod.EXTRA_FILES + (selftest.RECORD_FILE,):
        (dst / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / f, dst / f)
    return dst


def write_log(path, recs):
    path.write_bytes(b"".join(runlog.canonical(r).encode("utf-8") + b"\n" for r in recs))
    return path


class GuardTests(unittest.TestCase):
    def test_outside_calls_are_recorded_and_refused_and_loopback_passes(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        with Guard() as g:
            s = socket.socket()
            self.addCleanup(s.close)
            with self.assertRaises(GuardTrip):
                s.connect(("203.0.113.1", 443))
            with self.assertRaises(GuardTrip):
                socket.getaddrinfo("x.harness-selftest.invalid", 443)
            with self.assertRaises(GuardTrip):
                socket.create_connection(("198.51.100.1", 80), timeout=1)
            c = socket.socket()
            self.addCleanup(c.close)
            c.connect(server.getsockname())          # loopback passes
            ok = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True)
            with self.assertRaises(GuardTrip):
                subprocess.run(["git", "push"], env={"SELFTEST_ONLY": "not recorded"}, capture_output=True)
        self.assertEqual(ok.returncode, 0)
        self.assertRegex(ok.stdout.strip(), r"^[0-9a-f]{40}$")
        self.assertEqual(g.trips, [
            {"kind": "connect", "host": "203.0.113.1", "port": 443},
            {"kind": "getaddrinfo", "host": "x.harness-selftest.invalid", "port": 443},
            {"kind": "create_connection", "host": "198.51.100.1", "port": 80},
            {"kind": "process", "argv": ["git", "push"]},
        ])
        self.assertEqual(g.calls, [{"argv": ["git", "-C", str(REPO), "rev-parse", "HEAD"], "allowed": True},
                                   {"argv": ["git", "push"], "allowed": False}])
        self.assertNotIn("not recorded", json.dumps(g.trips + g.calls))
        self.assertEqual((socket.getaddrinfo.__module__, subprocess.Popen.__module__), ("socket", "subprocess"))

    def test_the_argv_rule(self):
        allowed = [["git", "-C", "x", "rev-parse", "HEAD"], [r"C:\Program Files\Git\cmd\git.EXE", "rev-parse", "--short", "HEAD"],
                   ["/usr/bin/git", "remote", "get-url", "origin"], ["git", "status", "--porcelain"]]
        refused = [["git", "push"], ["git", "-C", "x", "push"], ["git", "remote", "add", "o", "u"], "git status --porcelain",
                   ["curl", "-C", "x", "rev-parse"], [], ["python", "-m", "pip"]]
        for a in allowed:
            self.assertTrue(guard_mod.argv_allowed(a), a)
        for a in refused:
            self.assertFalse(guard_mod.argv_allowed(a), a)


class ScanTests(unittest.TestCase):
    def test_the_checkout_matches_the_declared_list(self):
        r = scan_mod.scan(REPO)
        self.assertEqual((r["unexpected"], r["stale"]), ([], []))
        self.assertEqual(len(r["found"]), len(scan_mod.DECLARED))
        self.assertIn("tests/", r["excluded"])
        self.assertFalse(any(s["file"].startswith("tests/") for s in r["found"]))
        self.assertEqual((r["publication"]["imports"], r["publication"]["process_words"]), ([], []))
        self.assertEqual(r["manifest"]["count"], len(scan_mod.scanned_files(REPO)))

    def test_a_new_socket_is_reported_and_a_removed_site_is_stale(self):
        with tempfile.TemporaryDirectory() as t:
            root = copy_code(Path(t))
            before = scan_mod.code_manifest(root)["sha256"]
            mod = root / "faias_harness" / "capabilities.py"
            mod.write_text(mod.read_text(encoding="utf-8") + '\nimport socket; socket.socket().connect(("192.0.2.1", 1))\n',
                           encoding="utf-8")
            eg = root / "faias_harness" / "egress.py"
            eg.write_text(eg.read_text(encoding="utf-8").replace("httpx.Client(", "httpx_Client("), encoding="utf-8")
            r = scan_mod.scan(root)
            self.assertNotEqual(r["manifest"]["sha256"], before)
        got = sorted((s["file"], s["kind"], s["name"]) for s in r["unexpected"])
        self.assertEqual(got, [("faias_harness/capabilities.py", "call", "socket.socket"),
                               ("faias_harness/capabilities.py", "import", "socket")])
        self.assertEqual([(s["file"], s["name"]) for s in r["stale"]], [("faias_harness/egress.py", "httpx.Client")])


class SelftestRunTests(unittest.TestCase):
    """One signed self-test of this checkout, shared by the tests of this class."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.priv, cls.pub = runlog.make_key(cls.dir / "keys", repo=REPO)
        with no_outside_network():
            cls.rc, cls.out = call(selftest.main, ["--out", str(cls.dir / "st"), "--sign-key", str(cls.priv)])
        cls.log = cls.dir / "st" / selftest.LOG_NAME
        cls.recs = runlog.read_log(cls.log)
        cls.header, cls.summary = cls.recs[0], cls.recs[-1]
        cls.probes = {r["id"]: r for r in cls.recs if r.get("type") == "probe"}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_command_passes_and_prints_its_summary(self):
        self.assertEqual(self.rc, 0, self.out)
        m = SUMMARY.search(self.out)
        self.assertTrue(m, self.out)
        n = len(probes_mod.PROBES)
        self.assertEqual((int(m.group(1)), int(m.group(3)), int(m.group(4)), m.group(5)), (n, 0, 0, "PASS"))
        self.assertEqual(int(m.group(2)), self.summary["refused"])
        self.assertEqual(list(self.probes), [p.id for p in probes_mod.PROBES])
        self.assertTrue(all(r["result"] == "PASS" for r in self.probes.values()))

    def test_every_target_is_reserved_documentation_or_loopback(self):
        parts = [self.header["scope"]["probe_plan"]] + [{k: r[k] for k in ("target", "observed", "trips")}
                                                       for r in self.probes.values()]
        text = json.dumps(parts)
        hosts = [urlsplit(u).hostname for u in re.findall(r"[a-z][a-z0-9+.-]*://[^\s\"',;)]+", text)]
        self.assertGreater(len(hosts), 10)
        for h in hosts:
            with self.subTest(host=h):
                ok = h.endswith("." + probes_mod.SUFFIX) or h in ("127.0.0.1", "::1")
                if not ok:
                    ip = ipaddress.ip_address(h)
                    ok = any(ip in n for n in TEST_NETS)
                self.assertTrue(ok)
        for ip in re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text):
            with self.subTest(ip=ip):
                a = ipaddress.ip_address(ip)
                self.assertTrue(a.is_loopback or ip == "0.0.0.0" or any(a in n for n in TEST_NETS))
        self.assertNotIn("openrouter", text.lower())

    def test_s5_token_and_s14_engine(self):
        s5 = self.probes["S5"]["observed"]
        self.assertEqual((s5["token_sent_in"], s5["token_in_log"]), (["body", "header", "query"], False))
        s14 = self.probes["S14"]
        self.assertEqual(s14["observed"]["engine"], "inspect_ai" if spike.inspect_available() else "direct")
        self.assertEqual((s14["counts"]["guard_trips"], s14["observed"]["all_loopback"]), (0, True))

    def test_verify_and_one_changed_byte(self):
        sig = self.log.with_name(self.log.name + ".sig")
        rc, out = call(runlog.main, ["verify", "--log", str(self.log), "--sig", str(sig), "--pubkey", str(self.pub)])
        self.assertEqual(rc, 0, out)
        self.assertIn("PASS", out)
        rc, out = call(runlog.main, ["verify", "--log", str(self.log), "--sig", str(sig), "--no-repo"])
        self.assertEqual(rc, 0, out)
        bad = self.dir / "byte" / selftest.LOG_NAME
        bad.parent.mkdir()
        data = bytearray(self.log.read_bytes())
        data[len(data) // 2] ^= 0x01
        bad.write_bytes(bytes(data))
        rc, out = call(runlog.main, ["verify", "--log", str(bad), "--sig", str(sig), "--pubkey", str(self.pub)])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)

    def test_the_form_check_finds_an_early_probe_and_a_changed_manifest(self):
        self.assertEqual(runlog.check_log(self.log), [])
        recs = json.loads(json.dumps(self.recs))
        recs[1]["t_ns"] = recs[0]["lock"]["time_ns"]
        problems = runlog.check_log(write_log(self.dir / "early.jsonl", recs))
        self.assertIn("probe S1: time is not later than the lock time", problems)
        recs = json.loads(json.dumps(self.recs))
        files = recs[0]["code_manifest"]["files"]
        files["faias_harness/egress.py"] = "0" * 64
        recs[0]["code_manifest"]["sha256"] = runlog.sha256_hex(json.dumps(files, sort_keys=True, separators=(",", ":")))
        problems = runlog.check_log(write_log(self.dir / "manifest.jsonl", recs))
        self.assertEqual(problems, ["header: the lock hash does not match the fixed parts of the header"])
        recs[0]["code_manifest"]["sha256"] = "1" * 64
        problems = runlog.check_log(write_log(self.dir / "manifest2.jsonl", recs))
        self.assertIn("header: the code manifest hash does not match its files", problems)
        recs = json.loads(json.dumps(self.recs))
        recs[-1]["result"] = "FAIL"
        problems = runlog.check_log(write_log(self.dir / "summary.jsonl", recs))
        self.assertIn("summary: the result does not follow from the probes, the guard trips and the scan", problems)

    def test_rerun_finds_no_difference(self):
        with no_outside_network():
            rc, out = call(selftest.main, ["rerun", "--log", str(self.log)])
        self.assertEqual(rc, 0, out)
        self.assertIn("differences: 0", out)
        self.assertIn("code manifest: matches the log", out)

    def test_rerun_finds_one_edited_result(self):
        with no_outside_network():
            path, _, _ = selftest.run(self.dir / "small", probes=probes_mod.PROBES[:2])
        recs = runlog.read_log(path)
        recs[2]["result"] = "FAIL"
        edited = write_log(self.dir / "small_edited.jsonl", recs)
        with no_outside_network():
            rc, out = call(selftest.main, ["rerun", "--log", str(edited)])
        self.assertEqual(rc, 1, out)
        self.assertIn("differences: 1", out)
        self.assertIn("DIFFERENCE S2 result", out)

    def test_header_procedure_thresholds_and_label(self):
        rec = selftest.load_record(REPO)
        h = self.header
        self.assertEqual((h["procedure"]["tp"], h["procedure"]["star"]), ("TP-AGT-014", "★2"))
        self.assertEqual(h["procedure"]["star"], rec["star"])
        self.assertEqual(sorted(h["thresholds"]), ["M1", "M2", "M3", "M4", "M5", "M6"])
        for m, t in h["thresholds"].items():
            self.assertTrue(t["basis"] and t["basis_source"], m)
        self.assertEqual(h["label"], "containment self-test of the harness: client-level and process-level checks, "
                                     "not a network sandbox; not a finding about any target")
        self.assertEqual(h["lock"]["hash"], runlog.sha256_hex(runlog.canonical(selftest.lock_fixed(h))))
        self.assertEqual(h["code"]["scorer_file"], selftest.PROBES_FILE)

    def test_steps_and_measures(self):
        """T5: not_in_run names steps 3 to 7, M2, M5 and M6 with their lines; the summary gives M1, M3 and M4."""
        nir = self.header["not_in_run"]
        text = " | ".join(f"{x['what']}: {x['why']} {x['lines']}" for x in nir)
        for what in ("step 3", "step 4", "steps 5 and 6", "step 7", "M2", "M5", "M6"):
            self.assertIn(what, text)
        self.assertTrue(all(x["lines"] for x in nir if x["what"].startswith(("step", "measure"))))
        self.assertIn("not applicable", next(x["why"] for x in nir if "M5" in x["what"]))
        self.assertIn("not built", next(x["why"] for x in nir if x["what"] == "measure M6"))
        m = self.summary["measures"]
        self.assertEqual((m["M1"]["rate"], m["M1"]["basis"]), (1.0, "harness-made attempts"))
        self.assertEqual((m["M1"]["attempts"], m["M1"]["refused"]), (self.summary["attempts"], self.summary["refused"]))
        self.assertEqual(m["M3"]["value"], "at the attempt, within the run, for the client path; no detector outside "
                                           "the client")
        self.assertEqual(m["M4"]["value"], "shared run folder; absent for the agent")

    def test_attestation(self):
        att = REPO / selftest.ATTESTATION_FILE
        self.assertEqual(self.header["attestation"]["sha256"], runlog.sha256_hex(att.read_bytes()))
        self.assertIn(selftest.DRAFT_MARK, att.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as t:
            repo = Path(t)
            with self.assertRaises(selftest.SelftestRefused) as cm:
                selftest.run(repo / "out", repo=repo)
            self.assertIn("does not exist", str(cm.exception))
            (repo / "docs").mkdir()
            (repo / selftest.ATTESTATION_FILE).write_text(" \n", encoding="utf-8")
            with self.assertRaises(selftest.SelftestRefused) as cm:
                selftest.run(repo / "out", repo=repo)
            self.assertIn("is empty", str(cm.exception))
            self.assertFalse((repo / "out").exists())

    def test_preflight_line_and_the_gate(self):
        args = ["--profile", "targets/srd1/profile.yaml", "--settings", "targets/srd1/settings.yaml"]
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict("os.environ", {"FAIAS_HARNESS_RUNS": empty}):
            rc, out = call(preflight.main, args)
            self.assertEqual(rc, 0, out)
            self.assertIn("Isolation self-test: none found", out)
            rc, out = call(preflight.main, args + ["--selftest-log", str(self.log.parent)])
        line = next(x for x in out.splitlines() if x.startswith("Isolation self-test:"))
        self.assertEqual(line, f"Isolation self-test: PASS at {self.header['lock']['time']}, commit "
                               f"{self.header['code']['git_commit'] or 'not recorded'}, code manifest matches: yes")
        with mock.patch.dict("os.environ", {"FAIAS_HARNESS_RUNS": str(self.dir)}):
            status = selftest.current()
        self.assertEqual((status["result"], status["verified"], status["manifest_matches"]), ("PASS", True, True))
        with tempfile.TemporaryDirectory() as t:
            root = copy_code(Path(t))
            eg = root / "faias_harness" / "egress.py"
            data = bytearray(eg.read_bytes())
            data[-2] ^= 0x01
            eg.write_bytes(bytes(data))
            changed = selftest.current(repo=root, selftest_log=self.log)
        self.assertFalse(changed["manifest_matches"])
        self.assertTrue(selftest.preflight_line(changed).endswith("code manifest matches: no"))
        self.assertEqual(selftest.gate_problems("TP-SEC-001", None), [])
        self.assertEqual(selftest.gate_problems("TP-AGT-012", status), [])
        self.assertTrue(selftest.gate_problems("TP-AGT-012", None))
        self.assertTrue(any("does not match" in p for p in selftest.gate_problems("TP-DAT-009", changed)))


class SeededFaultTests(unittest.TestCase):
    def test_an_allow_list_that_lets_everything_through_fails_s1(self):
        with tempfile.TemporaryDirectory() as t, no_outside_network(), \
                mock.patch.object(probes_mod, "PROBES", probes_mod.PROBES[:1]), \
                mock.patch.object(EgressClient, "_allowed", lambda self, scheme, host, port: True):
            rc, out = call(selftest.main, ["--out", str(Path(t) / "fault")])
            recs = runlog.read_log(Path(t) / "fault" / selftest.LOG_NAME)
        self.assertEqual(rc, 1, out)
        self.assertIn("result: FAIL", out)
        s1 = recs[1]
        self.assertEqual((s1["id"], s1["result"], s1["observed"]["transport_reached"]), ("S1", "FAIL", 1))
        self.assertEqual(recs[-1]["result"], "FAIL")


if __name__ == "__main__":
    unittest.main()
