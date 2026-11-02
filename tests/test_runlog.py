"""S5: the run log header, the lock, the Ed25519 signature and its verification."""
import contextlib
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from faias_harness.engine import runlog, sec001
from faias_harness.settings import from_raw
from tests.helpers import make_settings, raw_settings

REPO = Path(__file__).resolve().parents[1]


def build_log(path, settings=None, lock_ns=1_000, run_ns=2_000, with_summary=True, mutate=None, request=None,
              summary=None):
    s = settings or make_settings()
    if request is None:     # the text of C1-01 in the set of the settings, as a real run sends it
        request = next(i.text for i in sec001.load_items(s.item_set)[0] if i.id == "C1-01")
    h = runlog.build_header(s, allow_hosts=["openrouter.ai", "127.0.0.1:9"], system_prompt_sha256="p" * 64,
                            confirmation={"typed": "yes", "time": "2026-01-01T00:00:00Z"}, probes=None,
                            engine="direct")
    runlog.lock_header(h, lock_ns)
    if mutate:
        mutate(h)
    log = runlog.RunLog(path)
    log.write(h)
    log.write({"type": "run", "item": "C1-01", "class": "C1", "run": 1, "t_ns": run_ns, "request": request, "reply": "p",
               "score": {"success": False, "kind": "override", "evidence": ""}, "served_host": "ExampleHost",
               "served_model": "m/x"})
    if with_summary:
        log.write(summary or {"type": "summary", "status": "complete"})
    log.close()
    return h


class HeaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_header_holds_everything_the_plan_lists(self):
        h = build_log(self.dir / "log.jsonl")
        self.assertEqual(h["type"], "header")
        p = h["procedure"]
        self.assertEqual((p["tp"], p["star"]), ("TP-SEC-001", "★" + "4"))
        self.assertEqual(h["model"]["pinned_id"], "example-vendor/example-model")
        self.assertEqual(h["routing"]["only"], ["examplehost"])
        for m, basis in (("M1", "cited"), ("M2", "cited"), ("M3", "cited"), ("M4", "reviewer")):
            self.assertEqual(h["thresholds"][m]["basis"], basis)
            self.assertIsNotNone(h["thresholds"][m]["value"])
            self.assertTrue(h["thresholds"][m]["basis_source"])
        self.assertEqual(h["thresholds"]["M4"]["value"], 0.5)
        self.assertEqual(h["thresholds"]["M4"]["value_reason"], "test value for the unit tests")
        self.assertEqual(h["settings_hash"], make_settings().digest())
        self.assertEqual(h["label"], "engine spike, not a finding")
        self.assertEqual(h["corpus"]["per_class"], {"C1": 5, "C2": 5, "C3": 5, "C4": 5})
        self.assertEqual(h["corpus"]["runs_per_item"], 3)
        self.assertIn("not a network sandbox", h["egress"]["limit"])
        self.assertTrue(any("C-10" in x for x in h["not_in_run"]))
        self.assertTrue(any("config_read" in x and "M1 to M4" in x for x in h["not_in_run"]))
        self.assertTrue(any("clean-baseline" in x for x in h["not_in_run"]))   # the spike set has no baseline arm
        self.assertEqual(h["owner_confirmation"]["typed"], "yes")
        self.assertIn("served_host", h["host_per_call"])
        self.assertEqual(h["lock"]["time_ns"], 1_000)
        self.assertEqual(len(h["lock"]["hash"]), 64)

    def test_the_lock_hash_changes_when_a_threshold_or_the_settings_change(self):
        a = build_log(self.dir / "a.jsonl")
        b = build_log(self.dir / "b.jsonl", settings=make_settings(model_id="other/model"))
        c = build_log(self.dir / "c.jsonl", mutate=lambda h: None)
        self.assertNotEqual(a["lock"]["hash"], b["lock"]["hash"])
        self.assertEqual(a["lock"]["hash"], c["lock"]["hash"])

    def test_the_header_is_the_first_line_and_the_form_check_passes(self):
        build_log(self.dir / "log.jsonl")
        self.assertEqual(runlog.check_log(self.dir / "log.jsonl"), [])
        self.assertEqual(runlog.read_log(self.dir / "log.jsonl")[0]["type"], "header")

    def test_the_form_check_catches_a_lock_that_is_not_earlier_than_the_first_item(self):
        build_log(self.dir / "log.jsonl", lock_ns=5_000, run_ns=5_000)
        self.assertTrue(any("not later than the lock time" in p for p in runlog.check_log(self.dir / "log.jsonl")))
        build_log(self.dir / "log.jsonl", lock_ns=9_000, run_ns=5_000)
        self.assertTrue(runlog.check_log(self.dir / "log.jsonl"))

    def test_the_form_check_catches_missing_header_parts(self):
        def drop_reason(h):
            h["thresholds"]["M4"]["value_reason"] = None

        def drop_basis(h):
            h["thresholds"]["M2"]["basis"] = None

        def wrong_star(h):
            h["procedure"]["star"] = "★" + "5"

        for mutate, text in ((drop_reason, "no reason"), (drop_basis, "no basis"), (wrong_star, "star label")):
            build_log(self.dir / "log.jsonl", mutate=mutate)
            self.assertTrue(any(text in p for p in runlog.check_log(self.dir / "log.jsonl")), text)
        build_log(self.dir / "log.jsonl", with_summary=False)
        self.assertTrue(any("summary" in p for p in runlog.check_log(self.dir / "log.jsonl")))


class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.priv, self.pub = runlog.make_key(self.dir / "keys", repo=REPO)
        self.log = self.dir / "run_log.jsonl"
        build_log(self.log)
        self.sig = runlog.sign_log(self.log, self.priv)

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_key_file_and_the_signature_file_give_the_same_fingerprint(self):
        """README, "The run-signing key of the maintainer". The key file of make-key is a PEM file of 113 bytes.
        The field public_key_pem of a signature file holds the same text. The README command prints its SHA-256."""
        key = self.pub.read_bytes()
        self.assertEqual(len(key), 113)
        sig = json.loads(self.sig.read_text(encoding="utf-8"))
        self.assertEqual(sig["public_key_pem"].encode("ascii"), key)
        readme = (REPO / "README.md").read_text(encoding="utf-8")
        self.assertRegex(readme, r"fingerprint of the maintainer's run-signing public key file is "
                                 r"`(?:[0-9a-f]{8} ){7}[0-9a-f]{8}`")      # groups: the sweep refuses 40 hex in a row
        code = re.search(r'^py -3\.14 -c "(import hashlib[^"]+)" <public key file>$', readme, re.M).group(1)
        out = subprocess.run([sys.executable, "-c", code, str(self.pub)], capture_output=True, text=True, check=True)
        digest = runlog.sha256_hex(key)
        self.assertEqual(out.stdout.strip(), " ".join(digest[i:i + 8] for i in range(0, 64, 8)))

    def test_the_signature_verifies(self):
        ok, msg = runlog.verify_log(self.log, self.sig)
        self.assertTrue(ok, msg)
        self.assertIn("not pinned", msg)
        ok, msg = runlog.verify_log(self.log, self.sig, self.pub)
        self.assertTrue(ok, msg)
        self.assertIn("pinned public key", msg)

    def test_one_changed_byte_fails_verification(self):
        data = bytearray(self.log.read_bytes())
        for pos in (0, len(data) // 2, len(data) - 2):
            changed = bytearray(data)
            changed[pos] ^= 0x01
            self.log.write_bytes(bytes(changed))
            ok, msg = runlog.verify_log(self.log, self.sig)
            self.assertFalse(ok, pos)
            self.assertIn("does not match", msg)
        self.log.write_bytes(bytes(data))
        self.assertTrue(runlog.verify_log(self.log, self.sig)[0])

    def test_one_added_or_removed_byte_fails(self):
        data = self.log.read_bytes()
        for changed in (data + b" ", data[:-1]):
            self.log.write_bytes(changed)
            self.assertFalse(runlog.verify_log(self.log, self.sig)[0])

    def test_a_changed_signature_or_a_different_key_fails(self):
        import base64
        import json
        s = json.loads(self.sig.read_bytes())
        raw = bytearray(base64.b64decode(s["signature_b64"]))
        raw[0] ^= 0x01
        s["signature_b64"] = base64.b64encode(bytes(raw)).decode("ascii")
        bad = self.dir / "bad.sig"
        bad.write_bytes(json.dumps(s).encode("utf-8"))
        self.assertFalse(runlog.verify_log(self.log, bad)[0])
        _, other_pub = runlog.make_key(self.dir / "other", repo=REPO)
        ok, msg = runlog.verify_log(self.log, self.sig, other_pub)      # the embedded key is not trusted when one is pinned
        self.assertFalse(ok)

    def test_a_valid_signature_on_a_malformed_log_fails_the_form_check(self):
        bad = self.dir / "bad_log.jsonl"
        build_log(bad, lock_ns=9_000, run_ns=5_000)
        sig = runlog.sign_log(bad, self.priv)
        ok, msg = runlog.verify_log(bad, sig)
        self.assertFalse(ok)
        self.assertIn("malformed", msg)

    def test_make_key_refuses_a_folder_inside_the_repository_and_never_overwrites(self):
        with self.assertRaises(runlog.SigningKeyError):
            runlog.make_key(REPO / "runs" / "keys_must_not_exist", repo=REPO)
        self.assertFalse((REPO / "runs" / "keys_must_not_exist").exists())
        with self.assertRaises(runlog.SigningKeyError):
            runlog.make_key(self.dir / "keys", repo=REPO)

    def test_key_problems(self):
        self.assertEqual(runlog.key_problems(self.priv, repo=REPO), [])
        self.assertTrue(runlog.key_problems(self.dir / "missing.pem", repo=REPO))
        junk = self.dir / "junk.pem"
        junk.write_text("not a key", encoding="utf-8")
        self.assertTrue(runlog.key_problems(junk, repo=REPO))
        self.assertTrue(runlog.key_problems(self.priv, repo=self.dir))       # inside the "repository"

    def test_the_command_line(self):
        def call(argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = runlog.main(argv)
            return rc, buf.getvalue()

        rc, out = call(["verify", "--log", str(self.log), "--sig", str(self.sig), "--pubkey", str(self.pub)])
        self.assertEqual(rc, 0)
        self.assertIn("PASS", out)
        rc, out = call(["make-key", "--out", str(REPO / "runs" / "keys_must_not_exist")])
        self.assertEqual(rc, 2)
        self.assertIn("inside the repository", out)
        data = bytearray(self.log.read_bytes())
        data[10] ^= 1
        self.log.write_bytes(bytes(data))
        rc, out = call(["verify", "--log", str(self.log), "--sig", str(self.sig)])
        self.assertEqual(rc, 1)
        self.assertIn("FAIL", out)


class ExploreM4Tests(unittest.TestCase):
    """A TP-SEC-001 log in explore mode may leave the M4 bar unset, with no value and no reason. The settings ask for
    the bar in evidence mode only. In each mode they refuse only one of the two (settings.M4_HALF_SET). A log in
    evidence mode still fails the form check. So do a log with no mode and a log with only one of the two."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.priv, self.pub = runlog.make_key(self.dir / "keys", repo=REPO)

    def tearDown(self):
        self.tmp.cleanup()

    def log(self, name, mode="explore", value=None, reason=None):
        """A signed log in the folder `name`, with the mode and the M4 bar given. A mode of None leaves out the field.
        The summary holds an M4 measure with no bar, as a run with no bar writes it. The settings are not validated.
        The settings of this version refuse only one of the two, so such a log comes from an earlier version."""
        s = from_raw(raw_settings(mode="explore", reviewer_values={"M4": {"value": value, "reason": reason}}))

        def set_mode(h):
            if mode is None:
                del h["mode"]
            else:
                h["mode"] = mode
        path = self.dir / name / "run_log.jsonl"
        path.parent.mkdir()
        build_log(path, settings=s, mutate=set_mode,
                  summary={"type": "summary", "status": "complete",
                           "measures": {"M4": {"value": 1.0, "bar": None, "within_bar": None}}})
        runlog.sign_log(path, self.priv)
        return path

    def verify(self, path, use_repo=True):
        return runlog.verify_log(path, path.with_name(path.name + ".sig"), self.pub, use_repo=use_repo)

    def test_an_explore_log_with_no_m4_bar_passes_and_says_so(self):
        path = self.log("explore")
        self.assertEqual(runlog.check_log(path), [])
        self.assertEqual(runlog.check_log(path, use_repo=False), [])
        for use_repo in (True, False):
            ok, msg = self.verify(path, use_repo)
            self.assertTrue(ok, msg)
            self.assertEqual(msg.splitlines()[1], "M4 bar: not set (explore mode)", msg)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = runlog.main(["verify", "--log", str(path), "--sig", str(path) + ".sig", "--pubkey", str(self.pub)])
        self.assertEqual(rc, 0)
        self.assertEqual(buf.getvalue().splitlines()[1:], ["label: engine spike, not a finding",
                                                           "M4 bar: not set (explore mode)"])
        lines, refused = runlog.results([path.parent], self.pub)
        self.assertEqual(refused, [])
        self.assertIn("| value 1; within_bar null; M4 bar: not set (explore mode) |", lines[-1])

    def test_an_evidence_log_or_a_log_with_no_mode_still_needs_the_m4_value_and_its_reason(self):
        both = ["header: threshold M4 has no value", "header: the M4 value has no reason"]
        for name, mode in (("evidence", "evidence"), ("no_mode", None)):
            with self.subTest(mode=mode):
                path = self.log(name, mode=mode)
                self.assertEqual(runlog.check_log(path), both)
                ok, msg = self.verify(path)
                self.assertFalse(ok)
                self.assertIn("malformed", msg)
                self.assertNotIn("M4 bar: not set", msg)
                self.assertEqual(len(runlog.results([path.parent], self.pub)[1]), 1)

    def test_an_explore_log_with_only_one_of_the_value_and_the_reason_fails(self):
        self.assertEqual(runlog.check_log(self.log("value_only", value=0.5)), ["header: the M4 value has no reason"])
        self.assertEqual(runlog.check_log(self.log("reason_only", reason="a reason")),
                         ["header: threshold M4 has no value"])

    def test_a_log_with_an_m4_bar_prints_no_explore_line(self):
        for name, mode in (("explore_set", "explore"), ("evidence_set", "evidence")):
            with self.subTest(mode=mode):
                path = self.log(name, mode=mode, value=0.5, reason="a reason")
                ok, msg = self.verify(path)
                self.assertTrue(ok, msg)
                self.assertNotIn("M4 bar", msg)
                lines, refused = runlog.results([path.parent], self.pub)
                self.assertEqual(refused, [])
                self.assertIn("| value 1; within_bar null |", lines[-1])

    def test_the_rule_reads_tp_sec_001_logs_only(self):
        h = {"schema": runlog.SCHEMA, "mode": "explore", "thresholds": {"M4": {"value": None, "value_reason": None}}}
        self.assertTrue(runlog.m4_bar_not_set(h))
        for schema in (runlog.SCHEMA_SEC006, runlog.SCHEMA_SEC002, runlog.SCHEMA_SELFTEST):
            self.assertFalse(runlog.m4_bar_not_set({**h, "schema": schema}), schema)


if __name__ == "__main__":
    unittest.main()
