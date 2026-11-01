"""S1: the settings validator and the preflight with a settings file. No network."""
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from faias_harness import preflight, settings as sm
from faias_harness.engine import runlog
from faias_harness.egress import EgressClient
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
        self.assertIn("store_replies applies to the procedures TP-SEC-006 and TP-SEC-002 only",
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
        e2 = run_sec002.read_e2()
        self.assertEqual((e2["file"], e2["languages"]), ("targets/srd1/channel_inputs.yaml", ["en"]))
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

    def _start(self, answer, fake_gateway=True, extra=()):
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
                     "status: a choice of the harness; not yet declared by the product owner",
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


if __name__ == "__main__":
    unittest.main()
