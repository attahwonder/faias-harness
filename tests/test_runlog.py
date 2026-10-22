"""S5: the run log header, the lock, the Ed25519 signature and its verification."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from faias_harness.engine import runlog, sec001
from tests.helpers import make_settings

REPO = Path(__file__).resolve().parents[1]


def build_log(path, settings=None, lock_ns=1_000, run_ns=2_000, with_summary=True, mutate=None, request=None):
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
        log.write({"type": "summary", "status": "complete"})
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


if __name__ == "__main__":
    unittest.main()
