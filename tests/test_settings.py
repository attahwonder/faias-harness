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


if __name__ == "__main__":
    unittest.main()
