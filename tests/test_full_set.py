"""The item set of items/TP-SEC-001.json in the engine: settings, loader, baseline arm, header, log checks and a run
end to end against the local stand-in model host (no outside network)."""
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from faias_harness import settings as settings_mod
from faias_harness.engine import runlog, sec001, spike
from tests.helpers import FAKE_KEY_VALUE, KEY_ENV, StandInModelHost, make_settings, no_outside_network, raw_settings

REPO = Path(__file__).resolve().parents[1]
ITEMS, BASELINE = sec001.load_items("TP-SEC-001")
ENVELOPE = {"product and fee questions", "complaints intake", "contact-detail change", "fee refund", "human hand-off"}
ENGINE = "inspect" if spike.inspect_available() else "direct"


class SettingsTests(unittest.TestCase):
    def test_item_set_default_choice_and_refusal(self):
        raw = raw_settings()
        del raw["item_set"]
        self.assertEqual(settings_mod.from_raw(raw).item_set, "TP-SEC-001")
        self.assertEqual(make_settings(item_set="spike").item_set, "spike")
        self.assertIn("item_set must be one of TP-SEC-001, spike", settings_mod.validate(raw_settings(item_set="other")))
        self.assertNotEqual(make_settings(item_set="spike").digest(), make_settings(item_set="TP-SEC-001").digest())


class LoaderTests(unittest.TestCase):
    def test_counts_activities_and_what_the_scorer_needs(self):
        self.assertEqual(Counter(i.cls for i in ITEMS), {"C1": 40, "C2": 25, "C3": 26, "C4": 40})
        self.assertEqual(len(BASELINE), 25)
        self.assertTrue(all(b.cls == sec001.BASELINE for b in BASELINE))
        self.assertEqual(len({i.id for i in ITEMS + BASELINE}), 156)
        for i in ITEMS + BASELINE:
            self.assertIn(i.activity, ENVELOPE, i.id)
            self.assertTrue(i.objective and i.text, i.id)
            if i.cls == "C1":
                self.assertTrue(i.marker, i.id)
            if i.cls in ("C2", "C3"):
                self.assertTrue(i.subject and i.subject == i.subject.lower(), i.id)

    def test_the_spike_set_is_unchanged(self):
        self.assertEqual(sec001.load_items("spike"), (sec001.ITEMS, ()))
        with self.assertRaises(ValueError):
            sec001.load_items("other")


class BaselineTests(unittest.TestCase):
    def test_baseline_runs_are_counted_and_kept_out_of_the_measures(self):
        runs = [{"item": "C1-01", "cls": "C1", "run": n, "success": n == 1, "outcome": "attacked" if n == 1 else "defended"}
                for n in (1, 2, 3)]
        base = [{"item": "B-01", "cls": sec001.BASELINE, "run": n, "success": False, "outcome": o}
                for n, o in ((1, "answered"), (2, "blocked"), (3, "no_result"))]
        self.assertEqual(sec001.compute_measures(runs + base), sec001.compute_measures(runs))
        t = sec001.results_table(runs + base)
        self.assertEqual(t["baseline"], {"runs": 3, "answered": 1, "blocked": 1, "no_result": 1})
        self.assertEqual(t["C1"]["attacked"], 1)
        self.assertNotIn("baseline", sec001.results_table(runs))

    def test_baseline_outcome(self):
        self.assertEqual(sec001.baseline_outcome("Here is the fee list."), "answered")
        self.assertEqual(sec001.baseline_outcome("", None, None), "no_result")
        self.assertEqual(sec001.baseline_outcome("cut", "length"), "no_result")
        self.assertEqual(sec001.baseline_outcome("x", None, True), "blocked")


def _header(item_set):
    h = runlog.build_header(make_settings(item_set=item_set), allow_hosts=["openrouter.ai"], system_prompt_sha256="p" * 64,
                            confirmation={"typed": "yes", "time": "2026-01-01T00:00:00Z"}, engine="direct")
    return runlog.lock_header(h, 1_000)


class HeaderAndCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "run_log.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, h, request):
        log = runlog.RunLog(self.path)
        log.write(h)
        log.write({"type": "run", "item": ITEMS[0].id, "class": "C1", "run": 1, "t_ns": 2_000, "request": request,
                   "reply": "p", "served_host": "ExampleHost", "served_model": "m/x"})
        log.write({"type": "summary", "status": "complete"})
        log.close()

    def test_full_set_header(self):
        h = _header("TP-SEC-001")
        self.assertEqual(h["schema"], runlog.SCHEMA)
        self.assertEqual(h["label"], "uncalibrated scorer, not a finding")
        c = h["corpus"]
        self.assertEqual((c["item_set"], c["items"], c["baseline"]), ("TP-SEC-001", 131, 25))
        self.assertEqual(c["per_class"], {"C1": 40, "C2": 25, "C3": 26, "C4": 40})
        self.assertEqual(len(c["manifest"]), 156)
        self.assertTrue(all(set(r) == {"id", "class", "activity", "objective"} for r in c["manifest"]))
        self.assertFalse(any("clean-baseline arm" in x or "100 or more" in x for x in h["not_in_run"]))
        self.assertNotEqual(h["lock"]["hash"], _header("spike")["lock"]["hash"])

    def test_the_form_check_passes_and_catches_seeded_faults(self):
        self.write(_header("TP-SEC-001"), ITEMS[0].text)
        self.assertEqual(runlog.check_log(self.path), [])
        # a request that is not the item's text
        self.write(_header("TP-SEC-001"), ITEMS[0].text + " changed")
        self.assertTrue(any("request is not the text" in p for p in runlog.check_log(self.path)))
        # an items hash that is not the hash of the set in the repository
        h = _header("TP-SEC-001")
        h["corpus"]["items_sha256"] = "0" * 64
        self.write(h, ITEMS[0].text)
        self.assertTrue(any("items hash differs" in p for p in runlog.check_log(self.path)))
        # the label of the other set
        h = _header("TP-SEC-001")
        h["label"] = sec001.LABEL
        self.write(h, ITEMS[0].text)
        self.assertTrue(any("label" in p for p in runlog.check_log(self.path)))
        # an unknown item set
        h = _header("TP-SEC-001")
        h["corpus"]["item_set"] = "other"
        self.write(h, ITEMS[0].text)
        self.assertIn("header: the item set is unknown", runlog.check_log(self.path))

    def test_a_version_1_spike_log_still_passes(self):
        h = _header("spike")
        h["schema"] = runlog.SCHEMA_V1
        del h["corpus"]["item_set"]
        self.write(h, sec001.ITEMS[0].text)
        self.assertEqual(runlog.check_log(self.path), [])


def respond(text, nth):
    """A stand-in model: it gives the marker of the first C1 item on its first run, and answers every other
    request with a refusal; a baseline request gets a plain answer."""
    if text == ITEMS[0].text:
        return ITEMS[0].marker if nth == 0 else "I cannot do that."
    if any(text == b.text for b in BASELINE):
        return "Here is the information you asked for."
    return "I can only help with Example Bank accounts. A human adviser can look at your request."


class FullSetRunTests(unittest.TestCase):
    def test_full_set_run_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            priv, pub = runlog.make_key(Path(d) / "keys", repo=REPO)
            settings = make_settings(item_set="TP-SEC-001", cap_usd=10.0)
            with StandInModelHost(respond) as host, no_outside_network():
                res = spike.execute(settings, run_dir=Path(d) / "run", sign_key=priv,
                                    confirmation={"typed": "yes", "time": "2026-10-22T00:00:00Z"},
                                    base_url=host.base_url, env={KEY_ENV: FAKE_KEY_VALUE}, engine=ENGINE)
            self.assertEqual(res.status, "complete")
            recs = runlog.read_log(res.log_path)
            runs, summary = [r for r in recs if r["type"] == "run"], recs[-1]
            self.assertEqual(len(runs), 156 * 3)
            self.assertEqual(len(host.requests), 156 * 3)
            base = [r for r in runs if r["class"] == sec001.BASELINE]
            self.assertEqual(len(base), 75)
            self.assertTrue(all(r["score"]["kind"] == "baseline" and r["outcome"] == "answered" for r in base))
            self.assertEqual(summary["label"], "uncalibrated scorer, not a finding")
            self.assertEqual(summary["results_table"]["baseline"], {"runs": 75, "answered": 75, "blocked": 0, "no_result": 0})
            m = summary["measures"]
            self.assertEqual(m["items_complete"], 131)
            self.assertEqual(m["M1"]["overall"]["successful_items"], 1)
            self.assertEqual(m["M1"]["C1"]["successful_runs"], 1)
            ok, msg = runlog.verify_log(res.log_path, res.sig_path, pub)
            self.assertTrue(ok, msg)


if __name__ == "__main__":
    unittest.main()
