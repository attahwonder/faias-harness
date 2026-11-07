"""S1: the settings validator and the preflight with a settings file. No network."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from faias_harness import preflight, settings as sm, yamlio
from faias_harness.engine import runlog
from faias_harness.egress import EgressClient
from faias_harness.profile import ProfileError, load_profile, read_firm_values
from tests.helpers import (EVIDENCE_RAW, FAKE_KEY_VALUE, KEY_ENV, make_settings, no_outside_network, raw_settings)

REPO = Path(__file__).resolve().parents[1]
LOCKS = ("only", "allow_fallbacks", "zdr", "data_collection", "quantizations")
# The settings hash of each shipped settings file, read before TP-SEC-002 entered the settings (the first four), before
# TP-SEC-003 entered them (settings_sec002.yaml, read at b19c2ad), and after the hash of a TP-SEC-003 file took its
# arms in the order of the engine (settings_sec003.yaml). Each digest is written in two halves, so that no line holds
# a key-like run of hex characters (tools/sweep_barred.py).
SHIPPED_DIGESTS = {
    "settings.yaml": ("eb167771cb383ca170f4150d16adaae1", "56d0e998388598b886c0be948984d236"),
    "settings_sec006.yaml": ("b7be607874067da04bc7e92b4800a1be", "8137fcdc1897288fc702bf3529f0462c"),
    "settings_agt012.yaml": ("1b9e5fde7d487145132cbb1515f5d774", "1a76e2b7f53f4d7c0d33ead0beefedf7"),
    "settings_dat009.yaml": ("ca0bdd5ddb6ca1e68014af7b89c8df3d", "af478a3b0d2a08d9294677a864178d25"),
    "settings_sec002.yaml": ("cac6f392878a678f40e2e0ce61bcd4b4", "4d3470706d401efbd51acb288f5b8346"),
    "settings_sec003.yaml": ("0117670441e1034b12868a4aed22a67f", "39e5864525e5fc3ee06dcaef6a6cbaf2"),
}
SEC002_FILE = REPO / "targets" / "srd1" / "settings_sec002.yaml"
M1_BAR = {"M1": {"value": 0.1, "reason": "test value for the unit tests"}}


def sec002_raw(**changes):
    """A TP-SEC-002 settings mapping in evidence mode, with every key that the procedure takes."""
    return raw_settings(**{"procedure": "TP-SEC-002", "item_set": "TP-SEC-002", "store_replies": "hash_only",
                           "baseline_log": "baselines/sec001/run_log.jsonl", "baseline_pubkey": "keys/run.pub.pem",
                           "reviewer_values": M1_BAR, **changes})


def run_main(argv, **hooks):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = preflight.main(argv, **hooks)
    return rc, buf.getvalue()


class ValidatorTests(unittest.TestCase):
    def test_valid_evidence_file(self):
        self.assertEqual(sm.validate(raw_settings()), [])

    def test_shipped_settings_file_loads_in_explore_mode(self):
        s = sm.load_settings(REPO / "targets" / "srd1" / "settings.yaml")
        self.assertEqual(s.mode, "explore")
        self.assertEqual(s.api_key_env, "OPENROUTER_API_KEY")

    def test_each_missing_lock_is_rejected_in_evidence_mode(self):
        for lock in LOCKS:
            raw = raw_settings()
            del raw["routing"][lock]
            problems = sm.validate(raw)
            self.assertTrue(any(f"routing.{lock}" in p for p in problems), (lock, problems))

    def test_each_wrong_lock_value_is_rejected_in_evidence_mode(self):
        wrong = {"only": [], "allow_fallbacks": True, "zdr": False, "data_collection": "allow", "quantizations": []}
        for lock, value in wrong.items():
            raw = raw_settings()
            raw["routing"][lock] = value
            problems = sm.validate(raw)
            self.assertTrue(any(f"routing.{lock}" in p for p in problems), (lock, problems))

    def test_null_lock_is_rejected_in_evidence_mode(self):
        for lock in LOCKS:
            raw = raw_settings()
            raw["routing"][lock] = None
            self.assertTrue(any(f"routing.{lock}" in p for p in sm.validate(raw)), lock)

    def test_evidence_mode_needs_temperature_and_seed(self):
        for k in ("temperature", "seed"):
            self.assertTrue(any(f"evidence mode: {k}" in p for p in sm.validate(raw_settings(**{k: None}))), k)
            raw = raw_settings()
            del raw[k]
            self.assertTrue(any(f"evidence mode: {k}" in p for p in sm.validate(raw)), k)

    def test_bad_temperature_and_seed_values_are_rejected(self):
        for k, v in (("temperature", 2.5), ("temperature", -1), ("temperature", "hot"), ("seed", -1),
                     ("seed", 1.5), ("seed", True), ("seed", 2**31)):
            self.assertTrue(any(k in p for p in sm.validate(raw_settings(**{k: v}))), (k, v))

    def test_runs_per_item_is_three_by_default_and_never_below_three(self):
        self.assertEqual(sm.from_raw(raw_settings()).runs_per_item, 3)
        self.assertEqual(sm.validate(raw_settings(runs_per_item=9)), [])
        self.assertEqual(sm.from_raw(raw_settings(runs_per_item=9)).runs_per_item, 9)
        for bad in (2, 0, 21, 4.5, True, "9"):
            self.assertTrue(any("runs_per_item" in p for p in sm.validate(raw_settings(runs_per_item=bad))), bad)
        self.assertNotEqual(sm.from_raw(raw_settings()).digest(), sm.from_raw(raw_settings(runs_per_item=9)).digest())

    def test_evidence_mode_needs_exactly_one_endpoint(self):
        raw = raw_settings()
        raw["routing"]["only"] = ["examplehost", "otherhost"]
        self.assertTrue(any("exactly one endpoint" in p for p in sm.validate(raw)))
        raw["mode"] = "explore"
        self.assertEqual(sm.validate(raw), [])

    def test_temperature_and_seed_change_the_hash(self):
        a = sm.from_raw(raw_settings()).digest()
        self.assertNotEqual(a, sm.from_raw(raw_settings(temperature=0.2)).digest())
        self.assertNotEqual(a, sm.from_raw(raw_settings(seed=1001)).digest())

    def test_explore_mode_allows_unset_locks(self):
        raw = raw_settings(mode="explore", routing={}, model_id=None, price_in_per_m=None, price_out_per_m=None,
                           cap_usd=None, reviewer_values=None)
        self.assertEqual(sm.validate(raw), [])

    def test_evidence_mode_needs_model_prices_cap_and_m4(self):
        for k in ("model_id", "price_in_per_m", "price_out_per_m", "cap_usd"):
            self.assertTrue(sm.validate(raw_settings(**{k: None})), k)
        raw = raw_settings()
        raw["reviewer_values"]["M4"]["reason"] = None
        self.assertTrue(any("M4" in p for p in sm.validate(raw)))

    def test_a_key_in_the_file_is_rejected_and_not_repeated(self):
        raw = raw_settings(api_key=FAKE_KEY_VALUE)
        problems = " ".join(sm.validate(raw))
        self.assertIn("never goes in a settings file", problems)
        self.assertNotIn(FAKE_KEY_VALUE, problems)
        raw = raw_settings(api_key_env=FAKE_KEY_VALUE + "-x y")
        problems = " ".join(sm.validate(raw))
        self.assertIn("NAME of an environment variable", problems)
        self.assertNotIn(FAKE_KEY_VALUE, problems)

    def test_unknown_field_and_bad_mode(self):
        self.assertTrue(any("unknown field" in p for p in sm.validate(raw_settings(extra=1))))
        self.assertTrue(any("mode" in p for p in sm.validate(raw_settings(mode="fast"))))

    def test_hash_is_stable_and_changes_with_the_content(self):
        a, b = make_settings(), make_settings()
        self.assertEqual(a.digest(), b.digest())
        self.assertNotEqual(a.digest(), make_settings(model_id="other/model").digest())

    def test_load_error_does_not_echo_file_content(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "s.yaml"
            p.write_text("provider: [unclosed " + FAKE_KEY_VALUE, encoding="utf-8")
            with self.assertRaises(sm.SettingsError) as cm:
                sm.load_settings(p)
            self.assertNotIn(FAKE_KEY_VALUE, str(cm.exception))


class M4BarTests(unittest.TestCase):
    """A TP-SEC-001 file may hold the M4 value with its reason. In explore mode it may hold neither. It may never hold
    only one of the two (sm.M4_HALF_SET), because the log check refuses such a log (runlog.m4_bar_not_set). The rule
    is the same for SRD-1 minimal and for an external run."""
    EVIDENCE = "evidence mode: reviewer_values.M4 needs a value and a reason"
    FULL = {"value": 0.5, "reason": "test value for the unit tests"}
    HALF = ({"value": 0.5, "reason": None}, {"value": None, "reason": "test value for the unit tests"})
    EXT = {"kind": "external", "mode": "explore", "item_set": "spike", "cap_usd": 10.0, "cost_per_call_usd": 0.01,
           "declared": {"model_id": "example-model", "target_version": "1.0.3"}}

    def srd1(self, mode, rv):
        return sm.validate(raw_settings(mode=mode, reviewer_values=rv))

    def ext(self, mode, rv):
        return sm.validate_external({**self.EXT, "mode": mode, "reviewer_values": rv})

    def test_a_half_set_bar_is_refused_in_explore_mode(self):
        for name, check in (("srd1", self.srd1), ("external", self.ext)):
            for m4 in self.HALF:
                with self.subTest(name, m4=m4):
                    self.assertEqual(check("explore", {"M4": m4}), [sm.M4_HALF_SET])

    def test_evidence_mode_keeps_its_one_message_for_a_half_set_bar(self):
        for name, check in (("srd1", self.srd1), ("external", self.ext)):
            for m4 in self.HALF:
                with self.subTest(name, m4=m4):
                    self.assertEqual(check("evidence", {"M4": m4}), [self.EVIDENCE])

    def test_a_full_bar_loads_in_each_mode_and_no_bar_in_explore_mode(self):
        for name, check in (("srd1", self.srd1), ("external", self.ext)):
            for rv in ({"M4": self.FULL}, {"M4": {"value": None, "reason": None}}, {"M4": None}, {}, None):
                with self.subTest(name, rv=rv):
                    self.assertEqual(check("explore", rv), [])
            with self.subTest(name, mode="evidence"):
                self.assertEqual(check("evidence", {"M4": self.FULL}), [])
                self.assertEqual(check("evidence", None), [self.EVIDENCE])

    def test_the_rule_reads_the_m4_bar_of_tp_sec_001_only(self):
        """A TP-SEC-006 file and an external TP-SEC-002 file keep their own rules. Their starts ask for the bars in each
        mode (preflight.sec006_start_problems and preflight.sec002_start_problems)."""
        sec006 = raw_settings(mode="explore", procedure="TP-SEC-006", item_set="TP-SEC-006",
                              reviewer_values={"M4": self.HALF[0]})
        self.assertNotIn(sm.M4_HALF_SET, sm.validate(sec006))
        sec002 = {**self.EXT, "procedure": "TP-SEC-002", "item_set": "TP-SEC-002",
                  "channel_inputs": "targets/srd1/channel_inputs.yaml", "reviewer_values": {"M1": self.HALF[0]}}
        self.assertNotIn(sm.M4_HALF_SET, sm.validate_external(sec002))


class Sec002SettingsTests(unittest.TestCase):
    """The settings of a TP-SEC-002 run: baseline_log and baseline_pubkey, the M1 bar, and the settings hash."""

    def test_a_tp_sec_002_file_with_its_keys_loads(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "settings_sec002.yaml"
            p.write_text(yaml.safe_dump(sec002_raw()), encoding="utf-8")
            s = sm.load_settings(p)
        self.assertEqual((s.procedure, s.item_set, s.store_replies), ("TP-SEC-002", "TP-SEC-002", "hash_only"))
        self.assertEqual((s.m4_baseline_log, s.m4_baseline_pubkey),
                         ("baselines/sec001/run_log.jsonl", "keys/run.pub.pem"))
        self.assertEqual((s.m1_value, s.m1_reason, s.m4_value), (0.1, "test value for the unit tests", None))
        raw = sec002_raw()
        del raw["item_set"]
        self.assertEqual(sm.from_raw(raw).item_set, "TP-SEC-002")        # the default item set of the procedure
        for k, v in (("item_set", "TP-SEC-001"), ("store_replies", "text")):
            self.assertTrue(any(f"{k} must be" in p for p in sm.validate(sec002_raw(**{k: v}))), k)

    def test_the_tp_sec_006_baseline_names_are_refused_in_a_tp_sec_002_file(self):
        for old, new in (("m4_baseline_log", "baseline_log"), ("m4_baseline_pubkey", "baseline_pubkey")):
            problems = sm.validate(sec002_raw(**{old: "x"}))
            self.assertIn(f"{old} applies to the procedure TP-SEC-006 only; a TP-SEC-002 file names it {new}", problems)

    def test_the_baseline_names_apply_to_tp_sec_002_only(self):
        others = {"TP-SEC-001": {}, "TP-SEC-006": {"item_set": "TP-SEC-006"},
                  "TP-AGT-012": {"item_set": "TP-AGT-012"}, "TP-DAT-009": {"item_set": "TP-DAT-009"}}
        for proc, extra in others.items():
            for k in ("baseline_log", "baseline_pubkey"):
                problems = sm.validate(raw_settings(procedure=proc, **extra, **{k: "x"}))
                self.assertIn(f"{k} applies to the procedure TP-SEC-002 only", problems, (proc, k))
        self.assertIn("store_replies applies to the procedures TP-SEC-006, TP-SEC-002 and TP-MOD-011 only",
                      sm.validate(raw_settings(store_replies="full")))

    def test_the_reviewer_value_m1_is_for_tp_sec_002_only(self):
        self.assertEqual(sm.validate(sec002_raw()), [])
        self.assertIn("reviewer_values may hold M4 only", sm.validate(raw_settings(reviewer_values=M1_BAR)))
        sec006 = raw_settings(procedure="TP-SEC-006", item_set="TP-SEC-006", reviewer_values=M1_BAR)
        self.assertIn("reviewer_values may hold M3 and M4 only", sm.validate(sec006))
        m4 = {"M4": {"value": 0.5, "reason": "r"}}
        self.assertIn("reviewer_values may hold M1 only", sm.validate(sec002_raw(reviewer_values={**M1_BAR, **m4})))
        bad = sm.validate(sec002_raw(reviewer_values={"M1": {"value": 1.5, "reason": " "}}))
        self.assertIn("reviewer_values.M1.value must be a number from 0 to 1", bad)
        self.assertIn("reviewer_values.M1.reason must be a non-empty string", bad)

    def test_evidence_mode_names_each_missing_part_and_asks_for_no_m4(self):
        cases = {"reviewer_values": "evidence mode: reviewer_values.M1 needs a value and a reason",
                 "baseline_log": "evidence mode: baseline_log is not set",
                 "baseline_pubkey": "evidence mode: baseline_pubkey is not set"}
        for k, want in cases.items():
            raw = sec002_raw()
            del raw[k]
            problems = sm.validate(raw)
            self.assertEqual(len(problems), 1, (k, problems))
            self.assertTrue(problems[0].startswith(want), (k, problems))
        problems = sm.validate(sec002_raw(reviewer_values={"M1": {"value": 0.1, "reason": None}}))
        self.assertEqual(len(problems), 1, problems)
        self.assertFalse(any("M4" in p for p in sm.validate(sec002_raw(reviewer_values=None))))
        # Explore mode loads a file with no baseline: the preflight then refuses --probes and --start
        raw = sec002_raw(mode="explore", baseline_log=None, baseline_pubkey=None, reviewer_values=None)
        self.assertEqual(sm.validate(raw), [])
        self.assertIsNone(sm.from_raw(raw).m4_baseline_log)

    def test_the_tp_sec_002_fields_enter_the_settings_hash_of_that_procedure_only(self):
        a = sm.from_raw(sec002_raw())
        for change in ({"baseline_log": "other/run_log.jsonl"}, {"baseline_pubkey": "other.pem"},
                       {"store_replies": "full"}, {"reviewer_values": {"M1": {"value": 0.2, "reason": "r"}}},
                       {"reviewer_values": {"M1": {"value": 0.1, "reason": "another reason"}}}):
            self.assertNotEqual(sm.from_raw(sec002_raw(**change)).digest(), a.digest(), change)
        self.assertEqual(a.canonical()["baseline_log"], "baselines/sec001/run_log.jsonl")
        self.assertEqual(a.canonical()["reviewer_values"]["M1"], M1_BAR["M1"])
        for f in ("baseline_log", "baseline_pubkey"):
            self.assertNotIn(f, make_settings().canonical())
            self.assertNotIn(f, make_settings(procedure="TP-SEC-006", item_set="TP-SEC-006",
                                              reviewer_values=None, mode="explore").canonical())
        self.assertNotIn("M1", make_settings().canonical()["reviewer_values"])

    def test_the_settings_hash_of_each_shipped_file_is_unchanged(self):
        for name, halves in SHIPPED_DIGESTS.items():
            self.assertEqual(sm.load_settings(REPO / "targets" / "srd1" / name).digest(), "".join(halves), name)

    def test_the_shipped_tp_sec_002_file_loads_in_explore_mode_with_no_baseline(self):
        s = sm.load_settings(SEC002_FILE)
        self.assertEqual((s.procedure, s.item_set, s.mode, s.store_replies), ("TP-SEC-002", "TP-SEC-002", "explore",
                                                                               "full"))
        self.assertEqual((s.m4_baseline_log, s.m4_baseline_pubkey, s.m1_value, s.m1_reason), (None, None, None, None))
        evidence = yaml.safe_load(SEC002_FILE.read_text(encoding="utf-8"))
        evidence["mode"] = "evidence"
        problems = " ".join(sm.validate(evidence))
        for k in ("reviewer_values.M1", "baseline_log", "baseline_pubkey"):
            self.assertIn(k, problems)

    def test_the_declaration_of_the_channel_inputs_holds_its_fields(self):
        from faias_harness.engine import run_sec002, sec002_families
        data = yaml.safe_load((REPO / run_sec002.E2_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["languages"], ["en"])
        self.assertEqual(data["transformation_families"], list(sec002_families.FAMILIES))
        for k in ("declared_by", "declared_on", "basis"):
            self.assertTrue(str(data[k]).strip(), k)
        self.assertEqual(data["status"], "a choice of the harness; not yet declared by the product owner")
        self.assertIs(data["product_owner_declared"], False)      # the code reads this field, not the status text
        e2 = run_sec002.read_e2()
        self.assertEqual((e2["file"], e2["languages"], e2["product_owner_declared"]),
                         ("targets/srd1/channel_inputs.yaml", ["en"], False))
        self.assertEqual(e2["sha256"], runlog.sha256_hex((REPO / run_sec002.E2_FILE).read_text(encoding="utf-8")))

    def test_the_default_executor_runs_tp_sec_002_with_its_engine(self):
        from faias_harness.engine import run_sec002
        calls = []
        s = make_settings(**{**sec002_raw(), "mode": "explore"})
        with mock.patch.object(run_sec002, "execute", lambda **kw: calls.append(kw) or "the run"):
            self.assertEqual(preflight._default_executor(settings=s, run_dir="r"), "the run")
        self.assertEqual(calls, [{"settings": s, "run_dir": "r"}])


class PreflightSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.att = self.dir / "attestation.txt"
        self.att.write_text("Test system, synthetic data only.\n", encoding="utf-8")
        self.profile = self.dir / "profile.yaml"
        doc = yaml.safe_load((REPO / "targets" / "srd1" / "profile.yaml").read_text(encoding="utf-8"))
        doc["attestation_file"] = str(self.att)
        self.profile.write_text(yaml.safe_dump(doc), encoding="utf-8")
        self.settings_path = self.dir / "settings.yaml"
        self.settings_path.write_text(yaml.safe_dump(raw_settings()), encoding="utf-8")
        priv, _ = runlog.make_key(self.dir / "keys", repo=REPO)
        self.key = priv

    def tearDown(self):
        self.tmp.cleanup()

    def argv(self, *extra):
        return ["--profile", str(self.profile), "--settings", str(self.settings_path), *extra]

    def env_with_key(self):
        return mock.patch.dict(os.environ, {KEY_ENV: FAKE_KEY_VALUE})

    def env_without_key(self):
        env = {k: v for k, v in os.environ.items() if k != KEY_ENV}
        return mock.patch.dict(os.environ, env, clear=True)

    def test_prints_provider_model_host_precision_estimate_and_cap(self):
        with no_outside_network(), self.env_with_key():
            rc, out = run_main(self.argv())
        self.assertEqual(rc, 0)
        for text in ("Decoding: temperature 0.7, seed 1000 (+ call number), max_tokens 200", "Runs per item: 3 (FAIAS minimum 3)",
                     "M4 bar: 0.5; reason: test value for the unit tests",
                     "Provider: openrouter", "Model: example-vendor/example-model", "Host (routing.only): examplehost",
                     "Precision (routing.quantizations): fp8", "Worst-case estimate for the whole run: USD",
                     "cap: USD 1.0", f"Key variable: {KEY_ENV} is present", "Mode: evidence",
                     "Judge-family check: not applicable"):
            self.assertIn(text, out)
        self.assertNotIn(FAKE_KEY_VALUE, out)

    def test_each_missing_lock_makes_the_preflight_refuse(self):
        for lock in LOCKS:
            raw = raw_settings()
            del raw["routing"][lock]
            self.settings_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            with no_outside_network(), self.env_with_key():
                rc, out = run_main(self.argv("--start", "--sign-key", str(self.key)))
            self.assertEqual(rc, 2, lock)
            self.assertIn("SETTINGS INVALID", out)
            self.assertIn(f"routing.{lock}", out)

    def test_start_refuses_without_the_key_variable_and_prints_no_value(self):
        called = []
        with no_outside_network(), self.env_without_key():
            rc, out = run_main(self.argv("--start", "--sign-key", str(self.key)),
                               gateway_factory=lambda s: called.append("gateway"), executor=lambda **k: called.append("run"))
        self.assertEqual(rc, 2)
        self.assertIn(f"the environment variable {KEY_ENV} is not set", out)
        self.assertEqual(called, [])

    def test_start_refuses_a_half_set_m4_bar_in_explore_mode_before_any_run(self):
        called = []
        raw = raw_settings(mode="explore", reviewer_values={"M4": {"value": 0.5, "reason": None}})
        self.settings_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        with no_outside_network(), self.env_with_key():
            rc, out = run_main(self.argv("--start", "--sign-key", str(self.key)),
                               gateway_factory=lambda s: called.append("gateway"),
                               executor=lambda **k: called.append("run"))
        self.assertEqual(rc, 2)
        self.assertIn(f"SETTINGS INVALID. No run starts:\n  - {sm.M4_HALF_SET}\n", out)
        self.assertEqual(called, [])

    def test_start_refuses_without_a_signing_key_or_with_a_key_inside_the_repository(self):
        with no_outside_network(), self.env_with_key():
            rc, out = run_main(self.argv("--start"))
            self.assertEqual(rc, 2)
            self.assertIn("no signing key given", out)
            inside = REPO / "tests" / "_inside_key_test.pem"
            inside.write_bytes(self.key.read_bytes())
            try:
                rc, out = run_main(self.argv("--start", "--sign-key", str(inside)))
            finally:
                inside.unlink()
            self.assertEqual(rc, 2)
            self.assertIn("inside the repository", out)

    def _start(self, answer, fake_gateway=True, extra=(), run=None):
        from faias_harness.engine import spike
        calls = {"gateway": 0, "run": 0, "prompt": None}

        class G:      # stands in for the gateway: two probe calls, no network
            class cap:
                spent_usd = 0.001

        def gateway_factory(s):
            calls["gateway"] += 1
            return G()

        def executor(**kw):
            calls["run"] += 1
            calls["variant"] = kw.get("variant")
            if run:             # the executor of the test
                return run(**kw)
            from types import SimpleNamespace
            return SimpleNamespace(status="complete", log_path="log", sig_path="sig", summary={"label": "x", "model_calls": 0, "cost_usd": 0})

        def input_fn(prompt):
            calls["prompt"] = prompt
            return answer

        probes = {"judge_family": "not applicable: the spike has no model judge",
                  "tool_call": {"ok": True}, "reasoning_text": {"present": False}, "cost_usd": 0.001}
        with no_outside_network(), self.env_with_key(), mock.patch.object(spike, "run_probes", lambda g: probes):
            rc, out = run_main(self.argv("--start", "--sign-key", str(self.key), *extra), input_fn=input_fn,
                               gateway_factory=gateway_factory, executor=executor, inspect_check=lambda: None)
        return rc, out, calls

    def test_the_run_starts_only_after_a_typed_yes(self):
        rc, out, calls = self._start("yes")
        self.assertEqual((rc, calls["run"]), (0, 1))
        self.assertNotIn(FAKE_KEY_VALUE, out)
        self.assertIn("Type yes to start the run", calls["prompt"])
        self.assertIn("tool-call probe: ok", out)
        self.assertIn("reasoning-text probe", out)
        self.assertIn("judge-family check: not applicable", out)

    def test_start_refuses_without_the_inspect_engine(self):
        with no_outside_network(), self.env_with_key():
            rc, out = run_main(self.argv("--start", "--sign-key", str(self.key)),
                               inspect_check=lambda: "the run engine inspect_ai is not installed")
        self.assertEqual(rc, 2)
        self.assertIn("inspect_ai is not installed", out)

    def test_the_positive_control_flag_reaches_the_run(self):
        rc, out, calls = self._start("yes")
        self.assertEqual(calls["variant"], "standard")
        rc, out, calls = self._start("yes", extra=("--positive-control",))
        self.assertEqual((rc, calls["variant"]), (0, "positive_control"))
        self.assertIn("Variant: positive control", out)

    def test_anything_else_than_yes_starts_nothing(self):
        for answer in ("", "y", "YES", "no", "yes please", "Yes"):
            rc, out, calls = self._start(answer)
            self.assertEqual((rc, calls["run"]), (4, 0), answer)
            self.assertIn("Run not started", out)

    # ---------- an error from the engine after the typed yes ----------

    def test_a_refusal_of_the_engine_before_the_run_log_is_one_line_and_exit_2(self):
        """A RuntimeError or a ValueError from the executor, while the run folder holds no run_log.jsonl: one `Run
        refused:` line, then the run folder when it exists, and exit 2. The exception does not go on (no traceback)."""
        for exc in (RuntimeError, ValueError):
            for made in (False, True):
                with self.subTest(exc=exc.__name__, folder_made=made):
                    run_dir = self.dir / f"refused_{exc.__name__}_{made}"

                    def run(**kw):
                        if made:
                            kw["run_dir"].mkdir()
                        raise exc("the run is refused before the lock: a reason")

                    rc, out, calls = self._start("yes", extra=("--run-dir", str(run_dir)), run=run)
                    self.assertEqual((rc, calls["run"]), (2, 1), out[-400:])
                    tail = "Run refused: the run is refused before the lock: a reason\n"
                    tail += f"Run folder: {run_dir}\n" if made else ""
                    self.assertTrue(out.endswith("is labelled 'engine spike, not a finding'.\n" + tail), out[-400:])
                    self.assertEqual(out.count("Run refused:"), 1)
                    self.assertNotIn("Traceback", out)

    def test_an_error_of_the_engine_after_the_run_log_is_opened_goes_on_unchanged(self):
        """A run_log.jsonl in the run folder shows that the engine passed the lock. A RuntimeError or a ValueError
        then goes on unchanged, with its traceback."""
        for exc in (RuntimeError, ValueError):
            with self.subTest(exc=exc.__name__):
                raised = exc("an error after the lock")

                def run(**kw):
                    kw["run_dir"].mkdir()
                    (kw["run_dir"] / "run_log.jsonl").write_bytes(b"{}\n")
                    raise raised

                with self.assertRaises(exc) as cm:
                    self._start("yes", extra=("--run-dir", str(self.dir / f"locked_{exc.__name__}")), run=run)
                self.assertIs(cm.exception, raised)

    def test_another_error_type_of_the_engine_is_not_caught(self):
        for exc in (KeyError, OSError):
            with self.subTest(exc=exc.__name__):
                raised = exc("another error")

                def run(**kw):
                    raise raised

                with self.assertRaises(exc) as cm:
                    self._start("yes", extra=("--run-dir", str(self.dir / f"other_{exc.__name__}")), run=run)
                self.assertIs(cm.exception, raised)

    def test_profile_alone_still_starts_nothing(self):
        doc = yaml.safe_load(self.profile.read_text(encoding="utf-8"))
        doc["settings"].update(provider="example-provider", model_id="example-model", cap_usd=5.0)
        self.profile.write_text(yaml.safe_dump(doc), encoding="utf-8")
        with no_outside_network():
            rc, out = run_main(["--profile", str(self.profile), "--start"])
        self.assertEqual(rc, 3)
        self.assertIn("a profile alone names no run", out)

    def test_the_shipped_tp_sec_002_file_prints_its_lines_and_baseline_required(self):
        from faias_harness.engine import sec002_families
        called = []
        hooks = dict(gateway_factory=lambda s: called.append("gateway"), executor=lambda **k: called.append("run"),
                     inspect_check=lambda: None)
        with no_outside_network(), self.env_with_key():
            rc, out = run_main(["--profile", str(self.profile), "--settings", str(SEC002_FILE)], **hooks)
        self.assertEqual(rc, 0, out[-800:])
        for text in ("Engine run: TP-SEC-002 on SRD-1 minimal, item set TP-SEC-002",
                     "M1 bar (material uplift): not set; reason: not set",
                     "Baseline (a TP-SEC-001 run log on the full set): not named", "Baseline public key: not named",
                     "Replies in the log: full", "Channel inputs (E2, targets/srd1/channel_inputs.yaml): languages en; "
                     "transformation families " + ", ".join(sec002_families.FAMILIES),
                     "status: a choice of the harness; not yet declared by the product owner; "
                     "product_owner_declared: false",
                     preflight.BASELINE_REQUIRED, "Probes: not run"):
            self.assertIn(text, out)
        self.assertTrue(preflight.BASELINE_REQUIRED.startswith("BASELINE REQUIRED: "))
        # With every other condition of the probes met, BASELINE REQUIRED alone refuses them. --start refuses too.
        self.settings_path.write_text(yaml.safe_dump(sec002_raw(mode="explore", baseline_log=None,
                                                                baseline_pubkey=None)), encoding="utf-8")
        for flag, refused in (("--probes", "PROBES REFUSED"), ("--start", "START REFUSED")):
            for f in (SEC002_FILE, self.settings_path):
                with no_outside_network(), self.env_with_key():
                    rc, out = run_main(["--profile", str(self.profile), "--settings", str(f), flag, "--sign-key",
                                        str(self.key)], **hooks)
                self.assertEqual(rc, 2, (f.name, flag, out[-800:]))
                self.assertIn(refused, out)
                self.assertIn("  - " + preflight.BASELINE_REQUIRED, out)
        with no_outside_network(), self.env_with_key():
            rc, out = run_main(self.argv("--probes"), **hooks)
        self.assertEqual(out.split("PROBES REFUSED. Fix these points first:\n")[1].splitlines(),
                         ["  - " + preflight.BASELINE_REQUIRED])
        self.assertEqual(called, [])

    def test_tp_sec_002_without_its_item_file_has_no_estimate_and_start_refuses(self):
        """The pool of the item file names the baseline items, and the estimate counts its turns. So without the item
        file the preflight reads no baseline and gives no estimate, and --start refuses once, by name."""
        from faias_harness.engine import sec002
        absent = self.dir / "absent_TP-SEC-002.json"
        self.settings_path.write_text(yaml.safe_dump(sec002_raw(mode="explore")), encoding="utf-8")
        with no_outside_network(), self.env_with_key(), mock.patch.object(sec002, "ITEM_FILE", str(absent)):
            rc, out = run_main(self.argv())
            self.assertEqual(rc, 0, out[-800:])
            for text in ("Worst-case estimate: not available (the item file is not found); cap: USD 1.0",
                         f"Item file: {absent} is not found",
                         "Baseline check: not made (the pool of the item file names the baseline items, and the item "
                         "file is not found)"):
                self.assertIn(text, out)
            rc, out = run_main(self.argv("--start", "--sign-key", str(self.key)), inspect_check=lambda: None,
                               executor=lambda **k: self.fail("no run may start"))
        self.assertEqual(rc, 2, out[-800:])
        self.assertEqual(out.split("START REFUSED. No run starts. Fix these points first:\n")[1].splitlines(),
                         [f"  - the item file {absent} is not found"])


@contextlib.contextmanager
def absent_file(rel):
    """Each read of the file `rel` acts as for an absent file. Path.is_file gives False, and read_text, read_bytes and
    open raise FileNotFoundError. The file does not change, and every other file reads as it is."""
    target = (REPO / rel).resolve()
    real_is_file, real_read_text, real_read_bytes, real_open = Path.is_file, Path.read_text, Path.read_bytes, open

    def gone(p):
        return Path(p).resolve() == target

    def is_file(self):
        return False if gone(self) else real_is_file(self)

    def read_text(self, *a, **k):
        if gone(self):
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read_text(self, *a, **k)

    def read_bytes(self):
        if gone(self):
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_read_bytes(self)

    def opener(file, *a, **k):
        if isinstance(file, (str, os.PathLike)) and gone(file):
            raise FileNotFoundError(2, "No such file or directory", str(file))
        return real_open(file, *a, **k)
    with mock.patch.object(Path, "is_file", is_file), mock.patch.object(Path, "read_text", read_text), \
            mock.patch.object(Path, "read_bytes", read_bytes), mock.patch("builtins.open", opener):
        yield


def changed_copy(rel, text):
    """Each read of the item file `rel` gives `text`, as a changed copy of the file on disk would. The file does not
    change, and every other file reads as it is."""
    target, real = (REPO / rel).resolve(), Path.read_text

    def read_text(self, *a, **k):
        return text if self.resolve() == target else real(self, *a, **k)
    return mock.patch.object(Path, "read_text", read_text)


@contextlib.contextmanager
def changed_bytes(rel, data):
    """Each read of the file `rel` gives the bytes `data`. `read_bytes` gives them as they are. `read_text` decodes
    them strictly, as a read of a changed copy does, so bytes that are not UTF-8 raise UnicodeDecodeError. The file
    does not change, and every other file reads as it is."""
    target, real_text, real_bytes = (REPO / rel).resolve(), Path.read_text, Path.read_bytes

    def read_bytes(self):
        return data if self.resolve() == target else real_bytes(self)

    def read_text(self, encoding=None, errors=None, *a, **k):
        if self.resolve() == target:
            return data.decode(encoding or "utf-8", errors or "strict")
        return real_text(self, encoding, errors, *a, **k)
    with mock.patch.object(Path, "read_text", read_text), mock.patch.object(Path, "read_bytes", read_bytes):
        yield


class ItemFileRefusalTests(unittest.TestCase):
    """A changed copy of the item file of TP-SEC-001 (its full set), TP-SEC-006, TP-AGT-012 or TP-DAT-009
    that its loader refuses: a JSON object that holds the key `items` twice. With the prices set, the preflight prints
    the refusal and no estimate, and --start refuses in one problem line, before any model call. Before, the estimate
    raised the loader's RepeatedKey, and the preflight stopped with a traceback."""
    TOOLS = {"tools_enabled": True, "tool_key_env": "FAIAS_TEST_TOOL_KEY"}
    CASES = {"TP-SEC-001": ("items/TP-SEC-001.json", {"item_set": "TP-SEC-001"}),
             "TP-SEC-006": ("items/TP-SEC-006.json", {"procedure": "TP-SEC-006", "item_set": "TP-SEC-006",
                                                      "reviewer_values": {
                                                          "M3": {"value": 0.5, "reason": "test value for the unit tests"},
                                                          "M4": {"value": 0.5, "reason": "test value for the unit tests"}}}),
             "TP-AGT-012": ("items/TP-AGT-012.json", {"procedure": "TP-AGT-012", "item_set": "TP-AGT-012",
                                                      "reviewer_values": {}, **TOOLS}),
             "TP-DAT-009": ("items/TP-DAT-009.json", {"procedure": "TP-DAT-009", "item_set": "TP-DAT-009",
                                                      "reviewer_values": {}, **TOOLS})}
    REASON = "a JSON object holds the key 'items' more than once"

    @classmethod
    def setUpClass(cls):
        from tests.helpers import shared_selftest
        log, _, pub = shared_selftest(REPO)
        cls.selftest = ["--selftest-log", str(log), "--selftest-pubkey", str(pub)]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        att = self.dir / "attestation.txt"
        att.write_text("Test system, synthetic data only.\n", encoding="utf-8")
        doc = yaml.safe_load((REPO / "targets" / "srd1" / "profile.yaml").read_text(encoding="utf-8"))
        doc["attestation_file"] = str(att)
        self.profile = self.dir / "profile.yaml"
        self.profile.write_text(yaml.safe_dump(doc), encoding="utf-8")
        self.key = runlog.make_key(self.dir / "keys", repo=REPO)[0]

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_repeated_key_in_each_item_file_gives_the_refusal_no_estimate_and_one_problem_line(self):
        for tp, (rel, changes) in self.CASES.items():
            with self.subTest(procedure=tp):
                settings = self.dir / f"settings_{tp}.yaml"
                settings.write_text(yaml.safe_dump(raw_settings(mode="explore", **changes)), encoding="utf-8")
                text = (REPO / rel).read_text(encoding="utf-8").lstrip()
                self.assertTrue(text.startswith("{"))
                called = []
                argv = ["--profile", str(self.profile), "--settings", str(settings), *self.selftest]
                with no_outside_network(), mock.patch.dict(os.environ, {KEY_ENV: FAKE_KEY_VALUE}), \
                        changed_copy(rel, '{"items": [], ' + text[1:]):
                    rc, out = run_main(argv)
                    self.assertEqual(rc, 0, out[-800:])
                    self.assertIn(f"Item file: {rel} is refused: {self.REASON}\n", out)
                    self.assertIn("Worst-case estimate: not available (the item file is refused); cap: USD 1.0\n", out)
                    rc, out = run_main(argv + ["--start", "--sign-key", str(self.key)], inspect_check=lambda: None,
                                       gateway_factory=lambda s: called.append("model call"),
                                       executor=lambda **k: called.append("run"))
                self.assertEqual((rc, called), (2, []), out[-800:])
                self.assertNotIn("Traceback", out)
                self.assertEqual(out.split("START REFUSED. No run starts. Fix these points first:\n")[1].splitlines(),
                                 [f"  - {tp}: the item file {rel} is refused: {self.REASON}"])

    def test_with_no_item_file_the_estimate_line_says_not_found_when_a_tp_sec_003_item_file_is_present(self):
        """TP-AGT-012 with its item file absent, and a TP-SEC-003 item file present (sec003.ITEM_FILE names a file that
        exists), with the prices set. The estimate line says "the item file is not found". The TP-SEC-003 file is not
        the item file of TP-AGT-012, so it does not count."""
        from faias_harness.engine import agt012, sec003
        present = self.dir / "TP-SEC-003_present.json"
        present.write_text("{}\n", encoding="utf-8")
        settings = self.dir / "settings_absent_TP-AGT-012.yaml"
        settings.write_text(yaml.safe_dump(raw_settings(mode="explore", **self.CASES["TP-AGT-012"][1])),
                            encoding="utf-8")
        with no_outside_network(), mock.patch.dict(os.environ, {KEY_ENV: FAKE_KEY_VALUE}), \
                mock.patch.object(agt012, "ITEM_FILE", str(self.dir / "absent_TP-AGT-012.json")), \
                mock.patch.object(sec003, "ITEM_FILE", str(present)):
            rc, out = run_main(["--profile", str(self.profile), "--settings", str(settings), *self.selftest])
        self.assertEqual(rc, 0, out[-800:])
        self.assertIn("Worst-case estimate: not available (the item file is not found); cap: USD 1.0\n", out)
        self.assertNotIn("Traceback", out)

    def test_with_no_item_file_each_procedure_gives_no_estimate_and_start_refuses_in_one_problem_line(self):
        """The item file of each procedure is not found, and the prices are set. The estimate line says "the item file
        is not found", and --start refuses in one problem line that names the file, before any model call. Before, the
        preflight of TP-SEC-001 (full set), TP-SEC-006 and TP-DAT-009 stopped with FileNotFoundError at the
        estimate."""
        for tp, (rel, changes) in self.CASES.items():
            with self.subTest(procedure=tp):
                settings = self.dir / f"settings_absent_{tp}.yaml"
                settings.write_text(yaml.safe_dump(raw_settings(mode="explore", **changes)), encoding="utf-8")
                called = []
                argv = ["--profile", str(self.profile), "--settings", str(settings), *self.selftest]
                with no_outside_network(), mock.patch.dict(os.environ, {KEY_ENV: FAKE_KEY_VALUE}), absent_file(rel):
                    rc, out = run_main(argv)
                    self.assertEqual(rc, 0, out[-800:])
                    self.assertIn("Worst-case estimate: not available (the item file is not found); cap: USD 1.0\n",
                                  out)
                    rc, out = run_main(argv + ["--start", "--sign-key", str(self.key)], inspect_check=lambda: None,
                                       gateway_factory=lambda s: called.append("model call"),
                                       executor=lambda **k: called.append("run"))
                self.assertEqual((rc, called), (2, []), out[-800:])
                self.assertNotIn("Traceback", out)
                self.assertEqual(out.split("START REFUSED. No run starts. Fix these points first:\n")[1].splitlines(),
                                 [f"  - the item file {rel} is not found"])

    def test_a_lone_surrogate_in_each_item_file_gives_the_refusal_no_estimate_and_one_problem_line(self):
        """A changed copy of each item file with a key whose value holds a lone surrogate (the JSON escape \\ud83d
        alone). The loader refuses it and names the key (jsonkeys.unique_keys). So the preflight prints the refusal and
        no estimate, and --start refuses in one problem line, before any model call. Before, the loader read the
        copy."""
        reason = "a JSON object holds a lone surrogate in the key or the value of 'note'"
        for tp, (rel, changes) in self.CASES.items():
            with self.subTest(procedure=tp):
                settings = self.dir / f"settings_lone_{tp}.yaml"
                settings.write_text(yaml.safe_dump(raw_settings(mode="explore", **changes)), encoding="utf-8")
                text = (REPO / rel).read_text(encoding="utf-8").lstrip()
                self.assertTrue(text.startswith("{"))
                called = []
                argv = ["--profile", str(self.profile), "--settings", str(settings), *self.selftest]
                with no_outside_network(), mock.patch.dict(os.environ, {KEY_ENV: FAKE_KEY_VALUE}), \
                        changed_copy(rel, '{"note": "a placeholder \\ud83d", ' + text[1:]):
                    rc, out = run_main(argv)
                    self.assertEqual(rc, 0, out[-800:])
                    self.assertIn(f"Item file: {rel} is refused: {reason}\n", out)
                    self.assertIn("Worst-case estimate: not available (the item file is refused); cap: USD 1.0\n", out)
                    rc, out = run_main(argv + ["--start", "--sign-key", str(self.key)], inspect_check=lambda: None,
                                       gateway_factory=lambda s: called.append("model call"),
                                       executor=lambda **k: called.append("run"))
                self.assertEqual((rc, called), (2, []), out[-800:])
                self.assertNotIn("Traceback", out)
                self.assertEqual(out.split("START REFUSED. No run starts. Fix these points first:\n")[1].splitlines(),
                                 [f"  - {tp}: the item file {rel} is refused: {reason}"])


class LoneSurrogateYamlTests(unittest.TestCase):
    """A settings file and a profile that hold the YAML escape \\ud83d in a value, or the bytes ED A0 BD (a surrogate
    written as if it were UTF-8). The pure-Python YAML loader reads the escape as a lone surrogate, and the loader of
    the harness refuses it with one problem that names the key. libyaml refuses the escape (a ScannerError). Both
    loaders refuse the bytes, which are not UTF-8. The escape pair of an emoji is refused too: libyaml refuses it, and
    the pure-Python loader reads two lone surrogates. An emoji and U+FFFD pass. Before, the pure-Python loader passed
    the escape, and the bytes stopped the preflight with UnicodeDecodeError."""
    LOADERS = [yaml.SafeLoader] + ([yaml.CSafeLoader] if hasattr(yaml, "CSafeLoader") else [])

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def files(self, value):
        """A settings file with `value` as its model_id, and a profile with `value` as its name. `value` is YAML text."""
        settings = self.dir / "settings.yaml"
        settings.write_bytes(yaml.safe_dump(raw_settings(mode="explore", model_id="VALUE")).encode("ascii")
                             .replace(b"VALUE", value))
        doc = yaml.safe_load((REPO / "targets" / "srd1" / "profile.yaml").read_text(encoding="utf-8"))
        profile = self.dir / "profile.yaml"
        profile.write_bytes(yaml.safe_dump({**doc, "name": "VALUE"}).encode("ascii").replace(b"VALUE", value))
        return (("model_id", settings, sm.load_settings, sm.SettingsError),
                ("name", profile, load_profile, ProfileError))

    def problems(self, load, error, path, loader):
        with mock.patch.object(yamlio, "loader", lambda: loader), self.assertRaises(error) as cm:
            load(path)
        return cm.exception.problems

    def test_the_settings_and_the_profile_loaders_refuse_a_lone_surrogate_with_each_yaml_loader(self):
        for value in (b'"example \\ud83d"', b'"example \\ud83d\\ude00"'):
            for key, path, load, error in self.files(value):
                for loader in self.LOADERS:
                    with self.subTest(value=value, file=path.name, loader=loader.__name__):
                        problems = self.problems(load, error, path, loader)
                        if loader is yaml.SafeLoader:
                            self.assertEqual(problems, [f"a YAML mapping holds a lone surrogate in the key or the "
                                                        f"value of '{key}'"])
                        elif error is sm.SettingsError:
                            self.assertEqual(problems, ["cannot read settings.yaml: ScannerError"])
                        else:
                            self.assertEqual(len(problems), 1, problems)
                            self.assertIn("found invalid Unicode character escape code", problems[0])

    def test_the_bytes_of_a_surrogate_are_refused_as_not_utf8_and_an_emoji_and_the_replacement_character_pass(self):
        for key, path, load, error in self.files(b"'example \xed\xa0\xbd'"):
            for loader in self.LOADERS:
                with self.subTest(file=path.name, loader=loader.__name__):
                    problems = self.problems(load, error, path, loader)
                    want = ("cannot read settings.yaml: UnicodeDecodeError" if error is sm.SettingsError else
                            f"cannot read {path}: 'utf-8' codec can't decode byte 0xed")
                    self.assertEqual(len(problems), 1, problems)
                    self.assertTrue(problems[0].startswith(want), problems)
        valid = f"example {chr(0x1F600)} {chr(0xFFFD)}"
        for key, path, load, error in self.files(f'"{valid}"'.encode("utf-8")):
            for loader in self.LOADERS:
                with self.subTest(file=path.name, loader=loader.__name__), \
                        mock.patch.object(yamlio, "loader", lambda: loader):
                    loaded = load(path)
                    self.assertEqual(loaded.model_id if key == "model_id" else loaded.name, valid)


class OperatorYamlFileTests(unittest.TestCase):
    """Four more YAML files of the operator. Three are the firm-values file, the tools file and the store of the tool
    emulator. The fourth is the TP-SEC-002 declaration of the channel inputs. A copy of each file starts with the line
    `probe: <value>`. The value is the YAML escape \\ud83d, or the bytes ED A0 BD. The bytes are a surrogate written as
    if it were UTF-8. The pure-Python YAML loader reads the escape as a lone surrogate. The loader of the file refuses
    it with one problem that names the key. The libyaml loader refuses the escape (a ScannerError). Both loaders
    refuse the bytes, which are not UTF-8. Each refusal has the form of the other refusals of that file. The
    preflight prints it, and --start refuses in one problem line, before any model call. An emoji and U+FFFD pass.
    Before, the pure-Python loader passed the escape. The bytes or the ScannerError stopped the preflight with a
    traceback, and the bytes stopped the tool emulator with UnicodeDecodeError."""
    LOADERS = [yaml.SafeLoader] + ([yaml.CSafeLoader] if hasattr(yaml, "CSafeLoader") else [])
    FILES = {"firm": "targets/srd1/firm_values.yaml", "tools": "targets/srd1/tools.yaml",
             "store": "targets/srd1/data/synthetic_bank.yaml", "e2": "targets/srd1/channel_inputs.yaml"}
    ESCAPE, BYTES = b'probe: "example \\ud83d"\n', b"probe: 'example \xed\xa0\xbd'\n"
    LONE = {"firm": "firm_values.yaml holds", "tools": "the tools file holds", "store": "the store holds",
            "e2": "the declaration of the channel inputs (channel_inputs.yaml) holds"}
    READ = {"firm": "cannot read firm_values.yaml: {}", "tools": "cannot read the tools file ({})",
            "store": "cannot read the store ({})",
            "e2": "the declaration of the channel inputs cannot be read ({}): channel_inputs.yaml"}
    TOOLS = {"tools_enabled": True, "tool_key_env": "FAIAS_TEST_TOOL_KEY"}

    @classmethod
    def setUpClass(cls):
        from tests.helpers import shared_selftest
        log, _, pub = shared_selftest(REPO)
        cls.selftest = ["--selftest-log", str(log), "--selftest-pubkey", str(pub)]

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        att = self.dir / "attestation.txt"
        att.write_text("Test system, synthetic data only.\n", encoding="utf-8")
        doc = yaml.safe_load((REPO / "targets" / "srd1" / "profile.yaml").read_text(encoding="utf-8"))
        doc["attestation_file"] = str(att)
        self.profile = self.dir / "profile.yaml"
        self.profile.write_text(yaml.safe_dump(doc), encoding="utf-8")
        self.key = runlog.make_key(self.dir / "keys", repo=REPO)[0]

    def want(self, kind, prefix, loader):
        """The one problem of the file `kind` with the line `prefix` first, read with `loader`."""
        if prefix == self.ESCAPE and loader is yaml.SafeLoader:
            return f"{self.LONE[kind]} a lone surrogate in the key or the value of 'probe'"
        return self.READ[kind].format("ScannerError" if prefix == self.ESCAPE else "UnicodeDecodeError")

    def problem(self, kind, path):
        """The one problem of the loader of `kind` for the file at `path`, or None when it reads the file."""
        from faias_harness.emulators import handlers, mcp_tools
        from faias_harness.engine import run_agt012, run_sec002
        if kind == "firm":
            problems = run_agt012.halt_home_problems(5, path)
            self.assertLessEqual(len(problems), 1)
            self.assertEqual(read_firm_values(path)[1], problems[0] if problems else None)
            return problems[0] if problems else None
        try:
            {"tools": mcp_tools.load_tools, "store": handlers.load_store, "e2": run_sec002.read_e2}[kind](path)
        except mcp_tools.ToolsError as e:
            self.assertEqual(len(e.problems), 1, e.problems)
            return e.problems[0]
        except ValueError as e:         # handlers.StoreError is a ValueError
            return str(e)
        return None

    def test_each_loader_refuses_a_lone_surrogate_and_bytes_that_are_not_utf8_and_reads_a_valid_text(self):
        from faias_harness.emulators import mcp_tools
        valid = f'probe: "example {chr(0x1F600)} {chr(0xFFFD)}"\n'.encode("utf-8")
        for kind, rel in self.FILES.items():
            raw, path = (REPO / rel).read_bytes(), self.dir / Path(rel).name
            for loader in self.LOADERS:
                with mock.patch.object(yamlio, "loader", lambda: loader):
                    for prefix in (self.ESCAPE, self.BYTES):
                        path.write_bytes(prefix + raw)
                        with self.subTest(file=kind, loader=loader.__name__, value=prefix):
                            self.assertEqual(self.problem(kind, path), self.want(kind, prefix, loader))
                    path.write_bytes(valid + raw)
                    with self.subTest(file=kind, loader=loader.__name__, value="valid"):
                        self.assertIsNone(self.problem(kind, path))
        tools = self.dir / "tools_bytes.yaml"
        tools.write_bytes(self.BYTES + (REPO / self.FILES["tools"]).read_bytes())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mcp_tools.main(["serve", "--tools", str(tools), "--ledger", str(self.dir / "ledger.jsonl")])
        self.assertEqual((rc, out.getvalue()), (2, "refused: cannot read the tools file (UnicodeDecodeError)\n"))
        self.assertFalse((self.dir / "ledger.jsonl").exists())

    def test_the_preflight_names_each_refusal_and_start_refuses_in_one_problem_line_before_any_model_call(self):
        agt = self.dir / "settings_agt012.yaml"
        agt.write_text(yaml.safe_dump(raw_settings(mode="explore", procedure="TP-AGT-012", item_set="TP-AGT-012",
                                                   reviewer_values={}, **self.TOOLS)), encoding="utf-8")
        sec002 = self.dir / "settings_sec002.yaml"
        sec002.write_text(yaml.safe_dump(sec002_raw(mode="explore")), encoding="utf-8")
        variants = [(self.ESCAPE, loader) for loader in self.LOADERS] + [(self.BYTES, self.LOADERS[-1])]
        for kind, rel in self.FILES.items():
            raw = (REPO / rel).read_bytes()
            for prefix, loader in variants:
                problem = self.want(kind, prefix, loader)
                line = {"firm": f"Firm values: the file is refused: {problem}; ",
                        "tools": f"Tool emulator file: {rel} is refused: {problem}\n",
                        "store": f"Tool emulator file: {rel} is refused: {problem}\n",
                        "e2": f"Channel inputs (E2): {problem}\n"}[kind]
                start = (f"TP-AGT-012: the tool emulator file {rel} is refused: {problem}"
                         if kind in ("tools", "store") else problem)
                called = []
                argv = ["--profile", str(self.profile), "--settings", str(sec002 if kind == "e2" else agt),
                        *self.selftest]
                with self.subTest(file=kind, loader=loader.__name__, value=prefix), no_outside_network(), \
                        mock.patch.dict(os.environ, {KEY_ENV: FAKE_KEY_VALUE}), \
                        mock.patch.object(yamlio, "loader", lambda: loader), changed_bytes(rel, prefix + raw):
                    rc, out = run_main(argv)
                    self.assertEqual(rc, 0, out[-800:])
                    self.assertNotIn("Traceback", out)
                    self.assertIn(line, out)
                    if kind in ("tools", "store"):
                        self.assertIn("Worst-case estimate: not available (a tool emulator file is refused); cap: "
                                      "USD 1.0\n", out)
                    rc, out = run_main(argv + ["--start", "--sign-key", str(self.key)], inspect_check=lambda: None,
                                       gateway_factory=lambda s: called.append("model call"),
                                       executor=lambda **k: called.append("run"))
                    self.assertEqual((rc, called), (2, []), out[-800:])
                    self.assertNotIn("Traceback", out)
                    self.assertEqual(out.split("START REFUSED. No run starts. Fix these points first:\n")[1]
                                     .splitlines(), [f"  - {start}"])


if __name__ == "__main__":
    unittest.main()
