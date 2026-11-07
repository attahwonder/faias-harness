"""Tests of the TP-MOD-011 engine (faias_harness/engine/run_mod011.py), its start problems, the preflight and the
shipped instrument file. They use the local stand-in model host. No live call, no outside network, no key in any file.

The benchmark items are those of items/TP-MOD-011.json; the tests read them from that file when they run, and no test
file holds their text. The comparators are the neutral fixture sentences of tests/test_mod011.py, written for the tests.
The comparator file and the overlap result are written into a temporary folder outside the repository, as the private
file of a real run would be.

The stand-in host knows each probe by its message. It gives:
  - the remainder, word for word, to the first 20 benchmark items (exact on each attempt);
  - "Perhaps " and the remainder to items 20 to 24 (near-exact at tau 0.8);
  - the remainder to item 25 on its first attempt only (the item takes its highest class over its k attempts);
  - HTTP 400 to item 26 on each attempt (SRD-1's gateway does not retry it: a send error, no result);
  - the remainder to the first 2 comparators, and to 9 of the 10 instrument passages (all but the last);
  - a plain sentence to each other probe.
A run needs a containment self-test log that passes; the tests make one on this checkout with a test key
(tests/helpers.shared_selftest).
"""
import atexit
import contextlib
import copy
import dataclasses
import hashlib
import inspect
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from faias_harness import preflight, selftest, settings as sm
from faias_harness.adapters.srd1_http import Srd1ChatError, Srd1ChatTarget
from faias_harness.engine import mod011, mod011_log, run_mod011, runlog, spike
from targets.srd1.app import orchestrator
from tests.helpers import (EVIDENCE_RAW, FAKE_KEY_VALUE, KEY_ENV, StandInModelHost, no_outside_network,
                           shared_selftest)
from tests.test_mod011 import CUTOFF, fixture_comparator, mod_raw, overlap_result

REPO = Path(__file__).resolve().parents[1]
ENV = {KEY_ENV: FAKE_KEY_VALUE}
CONFIRM = {"typed": "yes", "time": "2026-11-03T00:00:00Z"}
ENGINE = "inspect" if spike.inspect_available() else "direct"
PLAIN = "I am not able to complete that text."
EXACT, NEAR, ONCE, FAILED = 20, range(20, 25), 25, 26
COMP_EXACT, INSTRUMENT_MISS = 2, 9
K = 3
SHIPPED = REPO / "targets" / "srd1" / "settings_mod011.yaml"
# The sentence on SRD-1's trace, the same in each place that names the store file (tests/test_mod011_log.py).
TRACE_SENTENCE = ("SRD-1's trace (target/srd1_trace.jsonl) also holds the comparator text, so it goes to the "
                  "controlled evidence store with the store file.")
_FIX = {}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture():
    """The shared fixture, made at the first call: a temporary folder outside the repository, the self-test log with
    its test key, the items, the fixture comparators with their file and overlap result, and the instrument passages."""
    if not _FIX:
        tmp = tempfile.TemporaryDirectory()
        atexit.register(tmp.cleanup)
        d = Path(tmp.name)
        log, priv, pub = shared_selftest(REPO)
        items = mod011.load_items()
        comps = [fixture_comparator(n, items[n].id) for n in range(50)]
        _FIX.update(tmp=tmp, dir=d, log=log, priv=priv, pub=pub, items=items, comps=comps,
                    passages=mod011.load_instrument(), runs={})
        _FIX["files"] = comparator_files("shared")
    return _FIX


def comparator_files(name, comps=None, overlap=None, raw=None):
    """Write a comparator file and its overlap result into the folder `name` of the fixture. Returns the settings
    that name them (comparator_file, comparator_sha256, overlap_result)."""
    d = _FIX["dir"] / "comparators" / name
    d.mkdir(parents=True, exist_ok=True)
    comps = _FIX["comps"] if comps is None else comps
    raw = json.dumps({"comparators": comps}).encode("utf-8") if raw is None else raw
    (d / "comparators.json").write_bytes(raw)
    over = overlap_result(comps, sha(raw)) if overlap is None else overlap
    (d / "overlap.json").write_text(json.dumps(over), encoding="utf-8")
    return {"comparator_file": str(d / "comparators.json"), "comparator_sha256": sha(raw),
            "overlap_result": str(d / "overlap.json")}


def repeated_key_files():
    """Four pairs of files of the fixture comparators. In each, one file has a JSON object that holds a key twice, with
    the valid value last: the comparator file at its top and in a comparator, and the overlap result at its top and in
    a row. The first value is neutral: an empty list, an empty text or a number. Plain json.loads keeps the last value,
    so mod011.check_comparators alone would pass each pair. Returns (the case, the settings that name the files, the
    one start problem)."""
    fx = fixture()
    text = json.dumps({"comparators": fx["comps"]})
    out = []
    for key, old, new in (("comparators", '{"comparators": ', '{"comparators": [], "comparators": '),
                          ("prefix", '"prefix": ', '"prefix": "", "prefix": ')):
        assert old in text
        files = comparator_files(f"repeated_{key}", raw=text.replace(old, new, 1).encode("utf-8"))
        out.append((f"the comparator file, the key {key}", files,
                    f"the comparator file is refused: a JSON object holds the key '{key}' more than once"))
    for key, old, new in (("results", '"results": ', '"results": [], "results": '),
                          ("longest_run", '"longest_run": ', '"longest_run": 9, "longest_run": ')):
        files = comparator_files(f"repeated_{key}")
        over = json.dumps(overlap_result(fx["comps"], files["comparator_sha256"]))
        assert old in over
        Path(files["overlap_result"]).write_text(over.replace(old, new, 1), encoding="utf-8")
        out.append((f"the overlap result, the key {key}", files,
                    f"the overlap result is refused: a JSON object holds the key '{key}' more than once"))
    return out


def mod_settings(mode="evidence", **changes):
    """Settings for a TP-MOD-011 run (the evidence form of tests/test_mod011.mod_raw) with the shared comparator file."""
    fx = fixture()
    raw = mod_raw(mode, **{"runs_per_item": K, "cap_usd": 20.0, "store_replies": "full", **fx["files"], **changes})
    problems = sm.validate(raw)
    assert not problems, problems
    return sm.from_raw(raw)


class Responder:
    """The stand-in model (the module text)."""

    def __init__(self, fail=(), on_benchmark=None):
        fx = fixture()
        self.fail, self.on_benchmark = set(fail), on_benchmark
        self.by_text = {}
        for n, i in enumerate(fx["items"]):
            self.by_text[mod011.probe_text(i.benchmark, i.prefix)] = ("benchmark", n, i.remainder)
        for n, c in enumerate(fx["comps"]):
            self.by_text[mod011.probe_text(c["benchmark"], c["prefix"])] = ("comparator", n, c["remainder"])
        for n, p in enumerate(fx["passages"]):
            self.by_text[mod011.instrument_probe_text(p)] = ("instrument", n, p.remainder)
        assert len(self.by_text) == 110

    def __call__(self, user, nth):
        arm, n, remainder = self.by_text[user]
        if arm == "benchmark" and self.on_benchmark:
            self.on_benchmark()
        if (arm, n) in self.fail:
            return {"status": 400, "body": {"error": {"code": 400, "message": "a test error"}}}
        if arm == "benchmark" and (n < EXACT or (n == ONCE and nth == 0)):
            return remainder
        if arm == "benchmark" and n in NEAR:
            return "Perhaps " + remainder
        if (arm == "comparator" and n < COMP_EXACT) or (arm == "instrument" and n != INSTRUMENT_MISS):
            return remainder
        return PLAIN


def host(fail=(("benchmark", FAILED),), **kw):
    return StandInModelHost(respond=Responder(fail), **kw)


def run(name, *, stand_in=None, settings=None, **kw):
    """One engine run in the fixture folder. Returns (the result, the stand-in host)."""
    fx = fixture()
    h = stand_in or host()
    kw = {"engine": ENGINE, "selftest_log": str(fx["log"]), "selftest_pubkey": str(fx["pub"]), **kw}
    with h, no_outside_network(), contextlib.redirect_stdout(io.StringIO()):
        res = run_mod011.execute(settings or mod_settings(), run_dir=fx["dir"] / name, sign_key=fx["priv"],
                                 confirmation=CONFIRM, base_url=h.base_url, env=ENV, **kw)
    return res, h


def shared_run():
    """The run that several tests read (and tests/test_mod011_log.py). It is made once."""
    fx = fixture()
    if "shared" not in fx["runs"]:
        fx["runs"]["shared"] = run("shared")
    return fx["runs"]["shared"]


def records(res):
    return runlog.read_log(res.log_path)


def expected_records():
    """The class of each attempt that the stand-in gives (the module text), with no log read."""
    fx, out = fixture(), []
    for n, i in enumerate(fx["items"]):
        for a in range(1, K + 1):
            cls = ("no_result" if n == FAILED else "exact" if n < EXACT or (n == ONCE and a == 1) else
                   "near_exact" if n in NEAR else "unrelated")
            out.append({"arm": "benchmark", "benchmark": "gsm8k", "item": i.id, "attempt": a, "class": cls})
    for n, c in enumerate(fx["comps"]):
        for a in range(1, K + 1):
            out.append({"arm": "comparator", "benchmark": "gsm8k", "item": c["id"], "attempt": a,
                        "class": "exact" if n < COMP_EXACT else "unrelated"})
    return out


class RunTests(unittest.TestCase):
    """The shared run: the classes and M1 of the stand-in, the log form, the store and the header."""

    @classmethod
    def setUpClass(cls):
        cls.res, cls.host = shared_run()
        cls.recs = records(cls.res)
        cls.runs = [r for r in cls.recs if r.get("type") == "run"]

    def test_the_log_holds_the_classes_and_m1_of_the_stand_in_and_check_and_verify_pass(self):
        s = self.recs[-1]
        self.assertEqual((self.res.status, s["status"], s["measures_complete"]), ("complete", "complete", True))
        self.assertEqual(s["outcomes"], {"benchmark": {"exact": 61, "near_exact": 15, "no_result": 3,
                                                       "unrelated": 71},
                                         "comparator": {"exact": 6, "unrelated": 144}})
        m1 = s["measures"]["M1"]["gsm8k"]
        self.assertEqual(m1, json.loads(json.dumps(mod011.compute_m1(expected_records(), runs_per_item=K, bar=0.2,
                                                                     tau=0.8))))
        self.assertEqual((m1["benchmark"]["items"], m1["benchmark"]["exact_items"],
                          m1["benchmark"]["exact_or_near_exact_items"], m1["benchmark"]["items_without_result"]),
                         (49, 21, 26, 1))
        self.assertAlmostEqual(m1["primary"]["differential"], 21 / 49 - 2 / 50)
        self.assertAlmostEqual(m1["primary"]["fisher_p_one_sided"], mod011.fisher_one_sided(21, 49, 2, 50))
        self.assertAlmostEqual(m1["secondary"]["differential"], 26 / 49 - 2 / 50)
        self.assertEqual((m1["primary"]["materially_higher"], m1["comparator_reading"]["exact_rate"]), (True, 0.04))
        ins = self.recs[0]["instrument"]["summary"]
        self.assertEqual((ins["counts"]["exact"], ins["counts"]["unrelated"], ins["exact_rate"]), (9, 1, 0.9))
        self.assertEqual(len(self.runs), 300)
        for use_repo in (True, False):
            with self.subTest(use_repo=use_repo):
                self.assertEqual(runlog.check_log(self.res.log_path, repo=REPO, use_repo=use_repo), [])
                ok, msg = runlog.verify_log(self.res.log_path, self.res.sig_path, fixture()["pub"], repo=REPO,
                                            use_repo=use_repo)
                self.assertTrue(ok, msg)

    def test_each_attempt_is_a_new_session_with_one_message_and_the_model_probe_prompt(self):
        prompt = orchestrator.load_system_prompt(orchestrator.PROMPT_VARIANTS["model_probe"])
        self.assertEqual(len(self.host.requests), 310)
        for r in self.host.requests:
            msgs = r["body"]["messages"]
            self.assertEqual([m["role"] for m in msgs], ["system", "user"])
            self.assertEqual(msgs[0]["content"], prompt)
            self.assertNotIn("tools", r["body"])
        trace = [json.loads(x) for x in (self.res.run_dir / "target" / "srd1_trace.jsonl").read_text(
            encoding="utf-8").splitlines() if x.strip()]
        turns = [e for e in trace if e.get("kind") in ("turn", "turn_error")]
        self.assertEqual(len(turns), 310)
        self.assertEqual(len({e["session"] for e in turns}), 310)       # one session for each attempt
        self.assertEqual({e["turn"] for e in turns}, {1})
        closed = {e["session"] for e in trace if e.get("kind") == "session_close"}
        self.assertEqual(closed, {e["session"] for e in turns})          # and each session is closed
        self.assertEqual(self.recs[0]["probe"]["prompt_sha256"], runlog.sha256_hex(prompt))
        self.assertEqual(self.recs[0]["probe"]["prompt_source"], "system_prompt_model_probe.txt")

    def test_a_send_error_gives_no_result_and_the_run_goes_on(self):
        failed = [r for r in self.runs if r["item"] == fixture()["items"][FAILED].id]
        self.assertEqual(len(failed), K)
        for r in failed:
            self.assertEqual((r["class"], r["failed"], r["error"], r["rouge_l"], r["served_host"]),
                             ("no_result", True, "Srd1ChatError", None, None))
        self.assertEqual(self.runs[-1]["arm"], "comparator")

    def test_a_comparator_record_holds_the_reply_hash_only_and_the_store_holds_the_text(self):
        fx = fixture()
        comp_runs = [r for r in self.runs if r["arm"] == "comparator"]
        self.assertEqual(len(comp_runs), 150)
        for r in comp_runs:
            self.assertNotIn("reply", r)
            self.assertRegex(r["reply_sha256"], "^[0-9a-f]{64}$")
        bench = [r for r in self.runs if r["arm"] == "benchmark"]
        self.assertTrue(all("reply" in r and runlog.sha256_hex(r["reply"] or "") == r["reply_sha256"] for r in bench))
        store = (self.res.run_dir / mod011_log.STORE_FILE).read_text(encoding="utf-8")
        lines = [json.loads(x) for x in store.splitlines() if x.strip()]
        self.assertEqual(len(lines), 50 + 300)
        self.assertEqual(self.recs[-1]["store"], {"file": mod011_log.STORE_FILE, "lines": 350,
                                                  "sha256": runlog.sha256_hex(store.encode("utf-8"))})
        self.assertEqual([x["id"] for x in lines[:50]], [c["id"] for c in fx["comps"]])
        texts = [c[k] for c in fx["comps"] for k in ("prefix", "remainder", "question")]
        for c in fx["comps"]:
            self.assertIn(json.dumps(c["prefix"]), store)
            self.assertIn(json.dumps(c["remainder"]), store)
        # No comparator text in any file of the run outside the store file and SRD-1's own trace (its README says so).
        for p in sorted(self.res.run_dir.rglob("*")):
            rel = p.relative_to(self.res.run_dir).as_posix()
            if p.is_file() and rel != mod011_log.STORE_FILE and rel != "target/srd1_trace.jsonl":
                with self.subTest(file=rel):
                    data = p.read_text(encoding="utf-8", errors="ignore")
                    self.assertEqual([t for t in texts if t in data], [])
        self.assertIn("comparator", (self.res.run_dir / "target" / "README.txt").read_text(encoding="utf-8"))
        self.assertIn("controlled evidence store",
                      (self.res.run_dir / "store" / "README.txt").read_text(encoding="utf-8"))

    def test_the_header_holds_no_item_text_and_each_block_of_the_design(self):
        h = self.recs[0]
        text = json.dumps(h)
        fx = fixture()
        for i in fx["items"]:
            self.assertNotIn(i.prefix, text)
            self.assertNotIn(i.remainder, text)
        for block in ("benchmarks", "comparators", "decode", "probe", "match_rule", "cited", "readings", "not_in_run",
                      "instrument", "isolation", "store", "code", "thresholds", "corpus"):
            self.assertIn(block, h)
        self.assertEqual(h["schema"], mod011_log.SCHEMA)
        self.assertEqual((h["code"]["scorer_file"], h["code"]["record_file"], h["code"]["log_file"]),
                         (mod011_log.SCORER_FILE, mod011_log.RECORD_FILE, mod011_log.LOG_FILE))
        for f, k in ((mod011_log.SCORER_FILE, "scorer_sha256"), (mod011_log.RECORD_FILE, "record_sha256"),
                     (mod011_log.LOG_FILE, "log_sha256")):
            self.assertEqual(h["code"][k], runlog.sha256_hex((REPO / f).read_bytes()))
        self.assertEqual(h["decode"], {**h["decode"], "temperature": 0.7, "seed": 1000, "max_tokens": 200, "k": K,
                                       "greedy": False})
        self.assertEqual(h["comparators"]["writer_families"], ["family-w"])
        self.assertEqual(h["comparators"]["count"], {"gsm8k": 50})
        self.assertEqual(h["comparators"]["created"], {"first": "2026-11-03", "last": "2026-11-03"})
        self.assertEqual(h["comparators"]["training_cutoff"], CUTOFF)
        self.assertEqual(h["comparators"]["file_sha256"], fx["files"]["comparator_sha256"])
        self.assertEqual(h["benchmarks"]["gsm8k"]["selected"], [i.id for i in fx["items"]])
        self.assertEqual(h["probe"]["template_sha256"], mod011.TEMPLATE_SHA256)
        self.assertEqual(h["match_rule"]["tau"], {"value": 0.8, "reason": "r"})
        self.assertEqual(h["thresholds"]["M1"]["value"], 0.2)
        self.assertEqual(h["cited"]["probed"], ["gsm8k"])
        self.assertEqual([p["id"] for p in h["instrument"]["passages"]], [p.id for p in fx["passages"]])
        self.assertEqual(h["instrument"]["sha256"], runlog.sha256_hex((REPO / mod011.INSTRUMENT_FILE).read_bytes()))
        self.assertEqual(h["variant"]["name"], "model_probe")
        self.assertEqual(h["lock"]["covers"][:len(mod011_log.LOCK_COVERS)], mod011_log.LOCK_COVERS)

    def test_the_readings_not_in_run_and_label_and_no_outcome_anywhere(self):
        h = self.recs[0]
        self.assertEqual(h["readings"], mod011_log.READINGS)
        self.assertTrue(all(x.endswith("; the harness's reading, not a ruling") for x in h["readings"]))
        joined = " ".join(h["readings"])
        for words in ("reading (a): the comparator arm is the subtraction basis",
                      "reading (b): the comparator arm should itself sit at or near chance",
                      "comparator_reading", f"holds {mod011.MIN_REMAINDER_WORDS} words or more",
                      f"the overlap limit is {mod011.OVERLAP_LIMIT} words, on GSM8K question text only"):
            self.assertIn(words, joined)
        self.assertEqual(h["not_in_run"], mod011_log.NOT_IN_RUN)
        for words in ("step 1, the inventory of the evaluation evidence (PLAYBOOK_MOD_011.md line 26)",
                      "step 2, a prior contamination check (line 27)", "step 11, the model-selection records (line 36)",
                      "M2, held-out performance against the cited score (line 43)", "M3, ", "(line 44)", "M4, ",
                      "(line 45)", "M5, ", "(line 46)", "Component 2 (line 19", "BLEURT", "general-instruction"):
            self.assertIn(words, " ".join(h["not_in_run"]))
        label = h["label"]
        for part in (mod011_log.LABEL_BASE, mod011_log.LIMIT_74, mod011_log.MODEL_PROBE_NOTE,
                     mod011_log.DECLARED_NOTE, "one person directs the build and the review of SRD-1, so an SME check "
                     "by that person is not independent", mod011_log.ONE_FAMILY, "decode not greedy (temperature 0.7)"):
            self.assertIn(part, label)
        self.assertEqual(self.recs[-1]["label"], label)
        # The harness gives no outcome: the words are absent from the log, except in the threshold texts that the
        # header copies from the record.
        recs = copy.deepcopy(self.recs)
        del recs[0]["thresholds"]
        text = "\n".join(json.dumps(r) for r in recs)
        self.assertEqual(re.findall(r"\b(?:pass|fail|partial)\b", text, re.IGNORECASE), [])

    def test_egress_jsonl_of_the_run_folder_holds_the_requests_of_the_instrument_check_before_the_lock(self):
        """The instrument check sends through the egress client of the run, so egress.jsonl of the run
        folder holds its requests before the lock: one message to SRD-1 for each instrument passage, and the model
        call of each. Before, no test read the file: a change that sent the check through a second client with a log
        in memory only passed every test."""
        lock = self.recs[0]["lock"]["time_ns"]
        events = [json.loads(x) for x in (self.res.run_dir / "egress.jsonl").read_text(encoding="utf-8").splitlines()]
        before = [e for e in events if e["t_ns"] < lock]
        messages = [e for e in before if e["method"] == "POST" and re.fullmatch(r"/sessions/[^/]+/messages", e["path"])]
        n = len(fixture()["passages"])
        self.assertEqual(len(messages), n)
        self.assertEqual({(e["host"], e["status"], e["allowed"]) for e in messages}, {("127.0.0.1", 200, True)})
        srd1_ports = {e["port"] for e in messages}
        self.assertEqual(len(srd1_ports), 1)
        self.assertNotIn(self.host.port, srd1_ports)
        self.assertEqual(len([e for e in before if e["port"] == self.host.port]), n)

    def test_the_summary_holds_the_trace_egress_cost_and_store(self):
        s = self.recs[-1]
        self.assertEqual((s["trace"]["chain_ok"], s["trace"]["problems"]), (True, []))
        self.assertEqual((s["trace"]["file"], s["trace"]["note"]), ("target/srd1_trace.jsonl", TRACE_SENTENCE))
        self.assertEqual(s["model_calls"], 307)     # the 3 attempts that the host answered with HTTP 400 cost nothing
        self.assertEqual((s["host_mismatches"], s["host_not_reported"]), (0, 0))
        self.assertEqual(s["egress"]["sha256"], runlog.sha256_hex((self.res.run_dir / "egress.jsonl").read_bytes()))
        self.assertEqual(set(s["run_engine"]), set(mod011_log.ARMS))
        self.assertGreater(s["cost_usd"], 0)

    def test_the_results_row_gives_m1_for_each_benchmark(self):
        m1 = self.recs[-1]["measures"]["M1"]["gsm8k"]
        want = (f"gsm8k: exact differential {runlog._fmt(m1['primary']['differential'])}, fisher_p "
                f"{runlog._fmt(m1['primary']['fisher_p_one_sided'])}, materially_higher true, comparator exact_rate "
                "0.04")
        self.assertEqual(runlog.result_row(self.recs, "x")["M1"], want)
        lines, refused = runlog.results([self.res.log_path], fixture()["pub"], repo=REPO)
        self.assertEqual(refused, [])
        self.assertIn(f"| {want} |", lines[4])

    def test_the_command_line_verifies_and_rescores_with_the_store(self):
        log, sig = str(self.res.log_path), str(self.res.sig_path)
        store = str(self.res.run_dir / mod011_log.STORE_FILE)

        def main(*argv):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = runlog.main(list(argv))
            return code, out.getvalue()
        code, text = main("verify", "--log", log, "--sig", sig, "--pubkey", str(fixture()["pub"]))
        self.assertEqual(code, 0, text)
        code, text = main("verify", "--log", log, "--sig", sig, "--no-repo")
        self.assertEqual(code, 0, text)
        self.assertIn("items hash: matches (the manifest and the scorer keys in the log); comparator text is checked "
                      "by hash only", text)
        code, text = main("rescore", "--log", log)
        self.assertEqual(code, 2, text)
        self.assertIn(mod011_log.NO_STORE, text)
        code, text = main("rescore", "--log", log, "--replies", store)
        self.assertEqual(code, 0, text)
        self.assertIn("runs rescored: 300 of 300; mismatches: 0", text)
        self.assertIn("differences: 0", text)


class StopTests(unittest.TestCase):
    """A run that stops, or that is refused before the lock by the instrument check or the prompt check."""

    def no_log(self, res_dir, h, calls):
        self.assertFalse((res_dir / "run_log.jsonl").exists())
        self.assertEqual(len(h.requests), calls)

    def test_a_cap_reached_in_the_instrument_check_refuses_the_run_before_the_lock(self):
        h = host(usage=(150_000, 0))
        with self.assertRaises(RuntimeError) as cm:
            run("cap_instrument", stand_in=h, settings=mod_settings(cap_usd=0.15))
        self.assertIn("the cap was reached in the instrument check", str(cm.exception))
        self.no_log(fixture()["dir"] / "cap_instrument", h, 1)

    def test_a_cap_reached_in_the_benchmark_arm_stops_the_run(self):
        """Each call costs USD 0.15 (150,000 input tokens at USD 1 a million). The ten instrument calls and one
        benchmark call reach the cap, so the second benchmark attempt is refused."""
        res, h = run("cap_arm", stand_in=host(usage=(150_000, 0)), settings=mod_settings(cap_usd=1.65))
        recs = records(res)
        self.assertEqual([r["type"] for r in recs], ["header", "run", "run_stopped", "summary"])
        self.assertEqual((recs[2]["reason"], recs[2]["arm"], recs[2]["attempt"]), ("stopped_at_cap", "benchmark", 2))
        self.assertEqual((res.status, recs[-1]["measures_complete"], len(h.requests)), ("stopped_at_cap", False, 11))
        self.assertEqual(runlog.check_log(res.log_path, repo=REPO), [])
        lines, diff = runlog.rescore_log(res.log_path, repo=REPO, replies=str(res.run_dir / mod011_log.STORE_FILE))
        self.assertEqual(diff, [], lines)

    def test_in_evidence_mode_a_host_mismatch_in_the_instrument_check_refuses_the_run(self):
        h = host(provider="OtherHost")
        with self.assertRaises(RuntimeError) as cm:
            run("host_instrument", stand_in=h)
        self.assertIn("a host other than the locked one served a call (OtherHost)", str(cm.exception))
        self.no_log(fixture()["dir"] / "host_instrument", h, 1)

    def test_in_evidence_mode_a_host_mismatch_in_an_arm_stops_the_run_after_its_record(self):
        h = None

        def switch():
            h.provider = "OtherHost"
        h = StandInModelHost(respond=Responder(on_benchmark=switch))
        res, _ = run("host_arm", stand_in=h)
        recs = records(res)
        self.assertEqual([r["type"] for r in recs], ["header", "run", "run_stopped", "summary"])
        self.assertEqual((recs[1]["served_host"], recs[1]["host_ok"]), ("OtherHost", False))
        self.assertEqual((recs[2]["reason"], recs[2]["served_host"]), ("stopped_host_mismatch", "OtherHost"))
        self.assertEqual((res.status, recs[-1]["host_mismatches"]), ("stopped_host_mismatch", 1))
        self.assertEqual(runlog.check_log(res.log_path, repo=REPO), [])
        # In explore mode the mismatch is counted and the run goes on.
        h = StandInModelHost(respond=Responder(on_benchmark=switch))
        res, _ = run("host_arm_explore", stand_in=h, settings=mod_settings("explore"))
        self.assertEqual((res.status, records(res)[-1]["host_mismatches"]), ("complete", 300))

    def test_an_engine_error_in_an_arm_stops_the_run_with_the_error_class_only(self):
        real, opened = Srd1ChatTarget.open_session, []

        def open_session(target, customer_ref=None):
            opened.append(customer_ref)
            if len(opened) == 12:          # ten instrument sessions, then the first benchmark attempt
                raise Srd1ChatError("SRD-1 returned HTTP 500 (a test error)")
            return real(target, customer_ref)
        with mock.patch.object(Srd1ChatTarget, "open_session", open_session):
            res, h = run("engine_error")
        recs = records(res)
        self.assertEqual([r["type"] for r in recs], ["header", "run", "run_stopped", "summary"])
        self.assertEqual((recs[2]["reason"], recs[2]["error"], res.status), ("stopped_error", "Srd1ChatError",
                                                                             "stopped_error"))
        self.assertNotIn(b"a test error", res.log_path.read_bytes())
        self.assertEqual(len(h.requests), 11)

    def test_an_engine_error_in_the_instrument_check_refuses_the_run(self):
        def open_session(target, customer_ref=None):
            raise Srd1ChatError("SRD-1 returned HTTP 500 (a test error)")
        h = host()
        with mock.patch.object(Srd1ChatTarget, "open_session", open_session), \
                self.assertRaises(RuntimeError) as cm:
            run("engine_error_instrument", stand_in=h)
        self.assertIn("the instrument check stopped on an error (Srd1ChatError)", str(cm.exception))
        self.assertNotIn("a test error", str(cm.exception))
        self.no_log(fixture()["dir"] / "engine_error_instrument", h, 0)

    def test_a_prompt_that_is_not_the_model_probe_prompt_refuses_the_run(self):
        real = Srd1ChatTarget.read_config
        cases = {"another source": lambda c: {**c, "prompt_source": "system_prompt.txt"},
                 "a hash that differs": lambda c: {**c, "prompt_sha256": sha(b"another prompt")}}
        for name, change in cases.items():
            with self.subTest(name):
                h = host()
                with mock.patch.object(Srd1ChatTarget, "read_config", lambda t, kind: change(real(t, kind))), \
                        self.assertRaises(RuntimeError) as cm:
                    run(f"prompt_{len(name)}", stand_in=h)
                self.assertIn("is not the model-probe prompt", str(cm.exception))
                self.no_log(fixture()["dir"] / f"prompt_{len(name)}", h, 0)


class StoreModeTests(unittest.TestCase):
    def test_with_hash_only_a_benchmark_record_holds_no_reply_and_the_store_rescores_it(self):
        res, _ = run("hash_only", settings=mod_settings(store_replies="hash_only"))
        recs = records(res)
        runs = [r for r in recs if r.get("type") == "run"]
        self.assertTrue(runs and all("reply" not in r for r in runs))
        self.assertEqual(recs[0]["store"], mod011_log.store_block("hash_only"))
        self.assertEqual(runlog.check_log(res.log_path, repo=REPO), [])
        lines, diff = runlog.rescore_log(res.log_path, repo=REPO, replies=str(res.run_dir / mod011_log.STORE_FILE))
        self.assertEqual(diff, [], lines)
        self.assertEqual(recs[-1]["measures"], records(shared_run()[0])[-1]["measures"])


BLOCKED, FILTERED = 18, 19      # two benchmark items that the stand-in would otherwise return word for word


class OddResponder(Responder):
    """The stand-in of the shared run, with a provider block (HTTP 403) for one benchmark item and a reply cut by the
    content filter (finish reason content_filter, with the remainder as its text) for another."""

    def __call__(self, user, nth):
        arm, n, remainder = self.by_text[user]
        if arm == "benchmark" and n == BLOCKED:
            return {"status": 403, "body": {"error": {"code": 403, "message": "flagged",
                                                   "metadata": {"error_type": "content_policy_violation"}}}}
        if arm == "benchmark" and n == FILTERED:
            return {"content": remainder, "finish_reason": "content_filter"}
        return super().__call__(user, nth)


class ReplyFormTests(unittest.TestCase):
    def test_a_provider_block_and_a_filtered_reply_give_no_result(self):
        res, _ = run("odd_replies", stand_in=StandInModelHost(respond=OddResponder((("benchmark", FAILED),))))
        recs = records(res)
        runs = [r for r in recs if r.get("type") == "run" and r["arm"] == "benchmark"]
        for n, blocked in ((BLOCKED, "content_policy_violation"), (FILTERED, "content_filter")):
            mine = [r for r in runs if r["item"] == fixture()["items"][n].id]
            self.assertEqual(len(mine), K)
            for r in mine:
                with self.subTest(item=n, attempt=r["attempt"]):
                    self.assertEqual((r["class"], r["failed"], r["blocked"], r["error"], r["rouge_l"]),
                                     ("no_result", True, blocked, None, None))
        self.assertEqual(recs[-1]["outcomes"]["benchmark"],
                         {"exact": 55, "near_exact": 15, "no_result": 9, "unrelated": 71})
        self.assertEqual(runlog.check_log(res.log_path, repo=REPO), [])

    def test_a_turn_line_with_a_block_or_the_finish_reason_error_has_no_result(self):
        # SRD-1 retries a reply with the finish reason error and then fails the send, so only a direct call reaches
        # this branch.
        item = fixture()["items"][0]
        for turn in ({"reply": item.remainder, "finish_reason": "error"},
                     {"reply": item.remainder, "finish_reason": "stop", "blocked": "content_filter"}):
            with self.subTest(turn=turn):
                reply, failed, score = run_mod011._scored(turn, None, item.prefix, item.remainder, 0.8)
                self.assertEqual((reply, failed, score["class"]), (item.remainder, True, "no_result"))
        _, failed, score = run_mod011._scored({"reply": item.remainder, "finish_reason": "stop"}, None, item.prefix,
                                              item.remainder, 0.8)
        self.assertEqual((failed, score["class"]), (False, "exact"))


class EngineTests(unittest.TestCase):
    def test_an_inspect_sample_names_the_probe_by_its_hash_only(self):
        comps = fixture()["comps"]
        for p in run_mod011._probes(fixture()["items"], comps)["comparator"]:
            s = run_mod011._sample(p)
            self.assertEqual((s.id, s.cls), (p.id, "comparator"))
            self.assertIn(runlog.sha256_hex(p.text), s.text)
            for part in (p.text, p.prefix, p.remainder):
                self.assertNotIn(part, s.text)

    def test_the_inspect_engine_gives_the_records_of_the_direct_engine(self):
        if not spike.inspect_available():       # run_all needs a plain OK, so this test says so and does not skip
            print("[test note] engines: inspect_ai is not installed here, so the TP-MOD-011 Inspect test checks "
                  "nothing. The harness installs nothing; a checkout with inspect_ai compares the two engines.",
                  flush=True)
            return
        keys = ("arm", "item", "attempt", "class", "rouge_l", "longest_run", "reply_sha256")

        def facts(res):
            return sorted(runlog.canonical({k: r[k] for k in keys}) for r in records(res) if r.get("type") == "run")
        a, _ = run("engine_direct", engine="direct")
        b = shared_run()[0]
        self.assertEqual(facts(a), facts(b))
        self.assertEqual(records(a)[-1]["measures"], records(b)[-1]["measures"])
        for arm in mod011_log.ARMS:
            engine = records(b)[-1]["run_engine"][arm]
            self.assertEqual(engine["name"], "inspect_ai")
            data = (b.run_dir / engine["log_file"]).read_text(encoding="utf-8")
            self.assertEqual([c["prefix"] for c in fixture()["comps"] if c["prefix"] in data], [])


class RefusalTests(unittest.TestCase):
    """Each start problem of design section 8 (and of the brief): run_mod011.start_problems names it, and the engine
    refuses the run before SRD-1 is built, with no model call and no run log. One subTest for each."""

    def setUp(self):
        fx = fixture()
        self.dir = Path(tempfile.mkdtemp(dir=fx["dir"]))
        comps = fx["comps"]
        self.comps = comps

        def change(n, **kv):
            c = copy.deepcopy(comps)
            c[n].update(kv)
            return c
        bad_licence = self.dir / "LICENSE"
        bad_licence.write_bytes((REPO / mod011.ALLOW_LIST["gsm8k"]["licence_file"]).read_bytes() + b"\n")
        digest_text = (REPO / mod011.ALLOW_LIST["gsm8k"]["digest_file"]).read_text(encoding="utf-8")
        item_sha = runlog.sha256_hex((REPO / mod011.ITEM_FILE).read_bytes())
        bad_digest = self.dir / "gsm8k.sha256"
        bad_digest.write_text(digest_text.replace(item_sha, sha(b"another item file")), encoding="utf-8")
        bad_item = self.dir / Path(mod011.ITEM_FILE).name
        bad_item.write_bytes((REPO / mod011.ITEM_FILE).read_bytes() + b"\n")
        bad_instrument = self.dir / Path(mod011.INSTRUMENT_FILE).name
        bad_instrument.write_bytes((REPO / mod011.INSTRUMENT_FILE).read_bytes() + b"\n")
        bad_instrument_digest = self.dir / Path(mod011.INSTRUMENT_DIGEST_FILE).name
        bad_instrument_digest.write_bytes((REPO / mod011.INSTRUMENT_DIGEST_FILE).read_bytes() + b"\n")
        rows = [{"id": c["id"], "longest_run": 8 if c["id"] == "C-07" else 3} for c in comps]
        entry = dict(mod011.ALLOW_LIST["gsm8k"])
        not_json = comparator_files("not_json", raw=b"not JSON")
        other_file = comparator_files("other_file")
        other_file["overlap_result"] = comparator_files("other_overlap", overlap=overlap_result(comps, sha(b"x")))[
            "overlap_result"]
        # (name, settings changes, patches, variant, the words of the one problem)
        self.cases = [
            ("a benchmark not on the map", {"benchmarks": ("bench-x",)}, {}, "model_probe",
             "the benchmark 'bench-x' is not on the allow-list of TP-MOD-011"),
            ("a licence file that fails its digest", {}, {"licence_file": str(bad_licence)}, "model_probe",
             "gsm8k: the licence file"),
            ("an item file that fails its digest", {}, {"item_file": str(bad_item)}, "model_probe",
             "does not have the SHA-256 of the digest file third_party/gsm8k/gsm8k.sha256"),
            ("a digest file that the code does not pin", {}, {"digest_file": str(bad_digest)}, "model_probe",
             f"gsm8k: the digest file {bad_digest} does not have the SHA-256 that mod011.py pins"),
            ("the digest file absent", {}, {"digest_file": str(self.dir / "absent.sha256")}, "model_probe",
             "is absent, so no file of the entry can be checked"),
            ("an instrument file that fails its digest", {}, {"instrument": str(bad_instrument)}, "model_probe",
             "the instrument file"),
            ("an instrument digest file that the code does not pin", {},
             {"instrument_digest": str(bad_instrument_digest)}, "model_probe",
             f"the instrument digest file {bad_instrument_digest} does not have the SHA-256 that mod011.py pins"),
            ("no comparator file named", {"comparator_file": None}, {}, "model_probe",
             "no comparator file is named (comparator_file)"),
            ("a comparator file inside the repository", {"comparator_file": str(REPO / "README.md")}, {},
             "model_probe", "the comparator file is inside the repository"),
            ("a comparator file absent", {"comparator_file": str(self.dir / "absent.json")}, {}, "model_probe",
             "the comparator file is absent"),
            ("a comparator file that fails its hash", {"comparator_sha256": sha(b"another file")}, {}, "model_probe",
             "does not have the SHA-256 that the settings name (comparator_sha256)"),
            ("a comparator file that is not JSON", not_json, {}, "model_probe", "the comparator file is not JSON"),
            ("fewer than 50 comparators", comparator_files("fewer", comps[:49]), {}, "model_probe",
             "has 49 comparators"),
            ("a comparator date on the cut-off", comparator_files("date", change(3, created=CUTOFF)), {},
             "model_probe", "on or before the declared training"),
            ("an overlap result at the limit", comparator_files(
                "limit", overlap=overlap_result(comps, fx["files"]["comparator_sha256"], rows)), {}, "model_probe",
             "C-07: a run of 8 words in common"),
            ("an overlap result absent", {"overlap_result": str(self.dir / "absent_overlap.json")}, {},
             "model_probe", "no overlap result"),
            ("an overlap result of another comparator file", other_file, {}, "model_probe", "for another comparator"),
            ("an overlap result of other GSM8K files", comparator_files("files", overlap={
                **overlap_result(comps, fx["files"]["comparator_sha256"]),
                "gsm8k_files": [{"name": "test.jsonl", "sha256": sha(b"x")}]}), {}, "model_probe",
             "does not read the files"),
            ("no SME record hash in evidence mode", {"sme_record_sha256": None}, {}, "model_probe",
             "the SHA-256 of the SME record"),
            ("a variant other than model_probe", {}, {}, "standard",
             "the variant standard does not apply to TP-MOD-011"),
            ("tools on", {"tools_enabled": True}, {}, "model_probe", "tools are on"),
            ("another procedure", {"procedure": "TP-SEC-001"}, {}, "model_probe",
             "the settings do not name the procedure TP-MOD-011"),
        ]
        self.entry = entry

    @contextlib.contextmanager
    def patched(self, patches):
        with contextlib.ExitStack() as stack:
            files = {"instrument": "INSTRUMENT_FILE", "instrument_digest": "INSTRUMENT_DIGEST_FILE"}
            entry = {**self.entry, **{k: v for k, v in patches.items() if k not in files}}
            stack.enter_context(mock.patch.dict(mod011.ALLOW_LIST, {"gsm8k": entry}))
            for key, name in files.items():
                if key in patches:
                    stack.enter_context(mock.patch.object(mod011, name, patches[key]))
            yield

    def test_each_start_problem_is_named_and_refuses_the_run_before_srd1_is_built(self):
        self.assertEqual(run_mod011.start_problems(mod_settings()), [])
        names = set()
        for n, (name, changes, patches, variant, want) in enumerate(self.cases):
            with self.subTest(name):
                names.add(name)
                s = dataclasses.replace(mod_settings(), **changes)
                with self.patched(patches):
                    problems = run_mod011.start_problems(s, variant)
                    self.assertEqual(len(problems), 1, problems)
                    self.assertIn(want, problems[0])
                    h = host()
                    with self.assertRaises(ValueError) as cm:
                        run(f"refused_{n}", stand_in=h, settings=s, variant=variant)
                    self.assertIn(want, str(cm.exception))
                self.assertFalse((fixture()["dir"] / f"refused_{n}" / "run_log.jsonl").exists())
                self.assertEqual(h.requests, [])
        self.assertEqual(len(names), 22)

    def test_a_repeated_key_in_the_comparator_file_or_the_overlap_result_is_refused_before_srd1_is_built(self):
        """Each case of repeated_key_files: run_mod011.start_problems names the file and the key in one problem, in the
        words of the helper of TP-SEC-002 (sec002.unique_keys). A direct call of the engine refuses it in the same words
        before SRD-1 is built: no run folder and no model call."""
        for n, (name, files, want) in enumerate(repeated_key_files()):
            with self.subTest(name):
                s = mod_settings(**files)
                self.assertEqual(run_mod011.start_problems(s), [want])
                h, run_dir = host(), fixture()["dir"] / f"repeated_key_{n}"
                with self.assertRaises(ValueError) as cm:
                    run(f"repeated_key_{n}", stand_in=h, settings=s)
                self.assertEqual(str(cm.exception), f"TP-MOD-011: the run is refused: {want}")
                self.assertEqual((run_dir.exists(), len(h.requests)), (False, 0))

    def test_an_overlap_field_of_the_wrong_type_is_refused_before_srd1_is_built(self):
        """An overlap result whose results, comparator_file or gsm8k_files has the wrong type: start_problems names it
        as an absent field (mod011.check_comparators), and a direct call of the engine refuses it in the same words
        before SRD-1 is built: no run folder and no model call. Before, each stopped start_problems and the engine with
        a TypeError or an AttributeError."""
        fx = fixture()
        cases = {"results": (5, "C-00: the overlap result has no row for it"),
                 "comparator_file": (5, "the overlap result is for another comparator file (its SHA-256 differs)"),
                 "gsm8k_files": (5, "the overlap result does not read the files train.jsonl, test.jsonl of gsm8k")}
        for n, (key, (value, want)) in enumerate(cases.items()):
            with self.subTest(key):
                files = comparator_files(f"overlap_type_{n}")
                over = {**overlap_result(fx["comps"], files["comparator_sha256"]), key: value}
                Path(files["overlap_result"]).write_text(json.dumps(over), encoding="utf-8")
                s = mod_settings(**files)
                problems = run_mod011.start_problems(s)
                self.assertTrue(problems[0].startswith(want), problems)
                h, run_dir = host(), fixture()["dir"] / f"overlap_type_{n}"
                with self.assertRaises(ValueError) as cm:
                    run(f"overlap_type_{n}", stand_in=h, settings=s)
                self.assertEqual(str(cm.exception), "TP-MOD-011: the run is refused: " + "; ".join(problems))
                self.assertEqual((run_dir.exists(), len(h.requests)), (False, 0))

    def test_an_extra_overlap_row_whose_longest_run_is_not_a_whole_number_is_read_as_absent_in_the_header(self):
        """An overlap result that passes the check and holds one more row, whose longest run is a text: the header
        reads that row as absent, as the check does, so the longest run is that of the rows of the comparators. Before,
        the header stopped the engine with a TypeError after the instrument check had sent its calls."""
        fx = fixture()
        over = overlap_result(fx["comps"], fx["files"]["comparator_sha256"])
        extra = {**over, "results": over["results"] + [{"id": "Z", "longest_run": "x"}]}
        self.assertEqual(mod011.check_comparators({"comparators": fx["comps"]}, fx["items"],
                                                  file_sha256=fx["files"]["comparator_sha256"], overlap=extra,
                                                  training_cutoff=CUTOFF, target_family="family-t"), [])
        features = mod011.match_features(fx["items"], fx["comps"])
        block = mod011_log.comparators_block(mod_settings(), fx["comps"], fx["files"]["comparator_sha256"], extra,
                                             sha(b"overlap"), features)
        self.assertEqual(block["overlap_result"]["longest_run"], 3)

    def test_without_a_passing_self_test_of_this_code_the_run_is_gated(self):
        fx = fixture()
        empty = Path(tempfile.mkdtemp(dir=fx["dir"]))
        status = selftest.current(repo=REPO, selftest_log=str(fx["log"]), pubkey=fx["pub"])
        self.assertIn("TP-MOD-011", selftest.GATED)
        cases = {
            "no self-test log": (None, "TP-MOD-011 needs a passing isolation self-test log; none found"),
            "a failed self-test": ({**status, "result": "FAIL"}, "the newest one is FAIL"),
            "a self-test of other code": ({**status, "manifest_matches": False}, "the code manifest does not match"),
            "a key that is not pinned in evidence mode": ({**status, "pinned": False}, "needs the self-test log "
                                                                                       "verified with a pinned"),
        }
        self.assertEqual(selftest.gate_problems("TP-MOD-011", status, "evidence"), [])
        for name, (st, want) in cases.items():
            with self.subTest(name):
                problems = selftest.gate_problems("TP-MOD-011", st, "evidence")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(want, problems[0])
        h = host()
        with self.assertRaises(RuntimeError) as cm:
            run("gated", stand_in=h, selftest_log=str(empty))
        self.assertIn("TP-MOD-011 is gated: TP-MOD-011 needs a passing isolation self-test log", str(cm.exception))
        self.assertEqual(h.requests, [])
        with self.assertRaises(RuntimeError) as cm:
            run("gated_unpinned", stand_in=host(), selftest_pubkey=None)
        self.assertIn("pinned public key", str(cm.exception))

    def test_an_unknown_engine_is_refused(self):
        with self.assertRaises(ValueError):
            run("engine_x", stand_in=host(), engine="other")

    def test_the_spike_engine_refuses_the_model_probe_variant(self):
        with self.assertRaises(ValueError) as cm:
            spike.execute(mod_settings(), run_dir=fixture()["dir"] / "spike_probe", sign_key=None, confirmation=CONFIRM,
                          variant="model_probe")
        self.assertIn("the variant model_probe applies to the procedure TP-MOD-011 only", str(cm.exception))


class PlanTests(unittest.TestCase):
    def test_the_plan_and_the_worst_case_estimate(self):
        s = mod_settings()
        items = run_mod011.load_items(s)[0]
        self.assertEqual(run_mod011.plan(s, items), 10 + 2 * 50 * K)
        with_file = run_mod011.estimate(s)
        self.assertGreater(with_file, 0)
        # Without the comparator file each comparator probe counts at SRD-1's longest message, so the bound rises.
        self.assertGreater(run_mod011.estimate(dataclasses.replace(s, comparator_file=None)), with_file)
        # With the file, each comparator probe counts by its own text: longer prefixes give a higher bound.
        longer = [dict(c, prefix=c["prefix"] + " " + c["prefix"]) for c in fixture()["comps"]]
        self.assertGreater(run_mod011.estimate(mod_settings(**comparator_files("longer", comps=longer))), with_file)
        self.assertIsNone(run_mod011.estimate(dataclasses.replace(s, price_in_per_m=None)))
        # The bound holds over the requests of a run: each call at the full output limit.
        res = records(shared_run()[0])
        self.assertLessEqual(res[-1]["model_calls"], run_mod011.plan(s, items))

    def test_a_comparator_file_with_a_repeated_key_counts_at_the_bound(self):
        """The estimate does not read the comparators of a file with a repeated key (repeated_key_files): each
        comparator probe counts at SRD-1's longest message, as with no comparator file."""
        bound = run_mod011.estimate(dataclasses.replace(mod_settings(), comparator_file=None))
        for name, files, _ in repeated_key_files()[:2]:
            with self.subTest(name):
                self.assertEqual(run_mod011.estimate(mod_settings(**files)), bound)

    def test_a_comparator_that_gives_no_probe_text_counts_at_the_bound(self):
        """The estimate does not read the comparators of a file when one of them is not an object with a benchmark of
        the allow-list and a text prefix, or when the field comparators is not a list: each comparator probe counts at
        SRD-1's longest message, as with no comparator file. Before, such a file stopped the estimate with a
        TypeError or a KeyError."""
        bound = run_mod011.estimate(dataclasses.replace(mod_settings(), comparator_file=None))
        one = fixture()["comps"][0]
        cases = {"a number": {"comparators": [5]}, "a text": {"comparators": "ab"},
                 "an object": {"comparators": {"a": 1}},
                 "a benchmark not on the allow-list": {"comparators": [dict(one, benchmark="bench-x")]},
                 "a benchmark that is a list": {"comparators": [dict(one, benchmark=[1])]},
                 "no prefix": {"comparators": [{k: v for k, v in one.items() if k != "prefix"}]},
                 "a prefix that is a number": {"comparators": [dict(one, prefix=5)]}}
        for n, (name, data) in enumerate(cases.items()):
            with self.subTest(name):
                files = comparator_files(f"no_probe_{n}", raw=json.dumps(data).encode("utf-8"))
                self.assertEqual(run_mod011.estimate(mod_settings(**files)), bound)

    def test_each_probe_fits_in_one_message_of_srd1(self):
        fx = fixture()
        texts = ([mod011.probe_text(i.benchmark, i.prefix) for i in fx["items"]]
                 + [mod011.instrument_probe_text(p) for p in fx["passages"]])
        self.assertLessEqual(max(len(t) for t in texts), orchestrator.MAX_MESSAGE_CHARS)


class SettingsFileTests(unittest.TestCase):
    def test_the_shipped_file_loads_in_explore_mode_and_holds_no_digest(self):
        s = sm.load_settings(SHIPPED)
        self.assertEqual((s.procedure, s.item_set, s.mode, s.benchmarks, s.store_replies, s.tools_enabled),
                         ("TP-MOD-011", "TP-MOD-011", "explore", ("gsm8k",), "full", False))
        self.assertEqual((s.comparator_file, s.comparator_sha256, s.overlap_result, s.sme_record_sha256),
                         (None, None, None, None))
        self.assertEqual(run_mod011.settings_problems(s), [])
        self.assertIsNone(re.search(r"[0-9a-f]{40,}", SHIPPED.read_text(encoding="utf-8")))
        self.assertEqual(run_mod011.start_problems(s), ["no comparator file is named (comparator_file)"])

    def test_an_evidence_copy_gets_the_routing_locks_with_no_new_rule(self):
        """settings.validate applies the five routing locks to every procedure in evidence mode."""
        for key, value, want in (("zdr", False, "routing.zdr must be true"),
                                 ("data_collection", "allow", "routing.data_collection must be 'deny'")):
            with self.subTest(key):
                raw = mod_raw()
                raw["routing"][key] = value
                self.assertTrue(any(want in p for p in sm.validate(raw)), sm.validate(raw))


class PreflightTests(unittest.TestCase):
    """The preflight with a TP-MOD-011 settings file. No model call: the probes and the executor are stand-ins."""

    def setUp(self):
        fx = fixture()
        self.dir = Path(tempfile.mkdtemp(dir=fx["dir"]))
        att = self.dir / "attestation.txt"
        att.write_text("Test system, synthetic data only.\n", encoding="utf-8")
        doc = yaml.safe_load((REPO / "targets" / "srd1" / "profile.yaml").read_text(encoding="utf-8"))
        doc["attestation_file"] = str(att)
        self.profile = self.dir / "profile.yaml"
        self.profile.write_text(yaml.safe_dump(doc), encoding="utf-8")
        self.raw = mod_raw("evidence", runs_per_item=K, cap_usd=20.0, store_replies="full", **fx["files"])
        self.settings = self.dir / "settings.yaml"
        self.settings.write_text(yaml.safe_dump(self.raw), encoding="utf-8")
        self.empty = self.dir / "no_selftest"
        self.empty.mkdir()
        self.key = runlog.make_key(self.dir / "keys", repo=REPO)[0]
        self.selftest = ["--selftest-log", str(fx["log"]), "--selftest-pubkey", str(fx["pub"])]

    def main(self, *argv, calls=None, settings=None):
        class G:
            class cap:
                spent_usd = 0.0

        def executor(**kw):
            calls.append(kw)
            return SimpleNamespace(status="complete", log_path="log", sig_path="sig",
                                   summary={"label": "x", "model_calls": 0, "cost_usd": 0})

        probes = {"judge_family": "not applicable", "tool_call": {"ok": True}, "reasoning_text": {"present": False},
                  "cost_usd": 0.0}
        out = io.StringIO()
        with no_outside_network(), mock.patch.dict("os.environ", {KEY_ENV: FAKE_KEY_VALUE}), \
                mock.patch.object(spike, "run_probes", lambda g: probes), contextlib.redirect_stdout(out):
            rc = preflight.main(["--profile", str(self.profile), "--settings", str(settings or self.settings), *argv],
                                input_fn=lambda p: "yes", gateway_factory=lambda s: G(), executor=executor,
                                inspect_check=lambda: None)
        return rc, out.getvalue()

    def test_the_report_prints_the_mod011_lines_and_no_comparator_text(self):
        rc, out = self.main(*self.selftest)
        self.assertEqual(rc, 0, out)
        fx = fixture()
        for text in ("Engine run: TP-MOD-011 on SRD-1 minimal, item set TP-MOD-011",
                     "Benchmarks: gsm8k (the allow-list: gsm8k)",
                     "M1 bar (material differential): 0.2; reason: test value",
                     "tau (near-exact match): 0.8; reason: r",
                     "match_tolerance (the match features): {'answer_digits': 1, 'steps': 1}; reason: r",
                     f"Comparator file: {fx['files']['comparator_file']} (private; its text is never printed)",
                     "training cut-off 2026-01-31 (source: the model card, section 2); target family family-t; "
                     "SME record hash set",
                     "Cited benchmarks: 1 declared", "Variant: model_probe",
                     "Instrument check: 10 passages of public-domain works, one attempt each",
                     "Replies in the log: full (benchmark records)",
                     "Worst-case estimate for the whole run: USD", "Isolation self-test: PASS at", "Probes: not run"):
            self.assertIn(text, out)
        self.assertEqual([c["prefix"] for c in fx["comps"] if c["prefix"] in out], [])
        self.assertNotIn(FAKE_KEY_VALUE, out)
        shipped = self.main(settings=SHIPPED)[1]
        self.assertIn("Comparator file: not named (private; its text is never printed); overlap result: not named",
                      shipped)
        self.assertIn("Worst-case estimate: not available (prices not set)", shipped)

    def test_start_refuses_each_start_problem_and_the_self_test_gate(self):
        bad = self.dir / "bad.yaml"
        bad.write_text(yaml.safe_dump({**self.raw, "comparator_file": None}), encoding="utf-8")
        calls = []
        rc, out = self.main("--start", "--sign-key", str(self.key), "--selftest-log", str(self.empty), calls=calls,
                            settings=bad)
        self.assertEqual((rc, calls), (2, []), out)
        refused = out.split("START REFUSED. No run starts. Fix these points first:\n")[1].splitlines()
        self.assertEqual(refused, ["  - no comparator file is named (comparator_file)",
                                   "  - TP-MOD-011 needs a passing isolation self-test log; none found (run "
                                   "faias_harness.selftest)"])
        self.assertEqual(preflight.mod011_start_problems(sm.load_settings(bad)),
                         ["no comparator file is named (comparator_file)"])
        for extra, want in ((("--variant", "standard"), "the variant standard does not apply to TP-MOD-011"),
                            (("--positive-control",), "the variant positive_control does not apply to TP-MOD-011")):
            with self.subTest(extra=extra):
                rc, out = self.main("--start", "--sign-key", str(self.key), *self.selftest, *extra, calls=calls)
                self.assertEqual((rc, calls), (2, []), out)
                refused = out.split("Fix these points first:\n")[1].splitlines()
                self.assertEqual(len(refused), 1, refused)
                self.assertTrue(refused[0].startswith(f"  - {want}: the engine runs SRD-1 minimal with the "
                                                      "model-probe prompt"), refused)

    def test_start_refuses_a_repeated_key_in_the_comparator_file_or_the_overlap_result_in_one_problem_line(self):
        """Each case of repeated_key_files: the preflight prints no traceback and no comparator text, and --start
        refuses in one problem line, before the probes and the executor."""
        fx = fixture()
        for n, (name, files, want) in enumerate(repeated_key_files()):
            with self.subTest(name):
                path = self.dir / f"repeated_key_{n}.yaml"
                path.write_text(yaml.safe_dump({**self.raw, **files}), encoding="utf-8")
                calls = []
                rc, out = self.main("--start", "--sign-key", str(self.key), *self.selftest, calls=calls, settings=path)
                self.assertEqual((rc, calls), (2, []), out)
                self.assertEqual(out.split("Fix these points first:\n")[1].splitlines(), [f"  - {want}"])
                self.assertNotIn("Traceback", out)
                self.assertEqual([c["prefix"] for c in fx["comps"] if c["prefix"] in out], [])

    def test_a_lone_surrogate_in_the_comparator_file_or_the_overlap_result_is_refused_in_one_problem_line(self):
        """A text of the comparator file (a writer family) or of the overlap result (its field reads) holds a lone
        surrogate (the JSON escape \\ud83d alone). The engine reads each file with jsonkeys.unique_keys. So
        start_problems names the file and the key, the report has no traceback, and --start refuses in one problem
        line, before the probes and the executor. The same texts with an emoji (a surrogate pair of escapes) and U+FFFD
        give no problem."""
        fx = fixture()
        lone, valid = "a placeholder \ud83d", f"a placeholder {chr(0x1F600)} {chr(0xFFFD)}"
        names = {"comparator": ("the comparator file", "writer_family"), "overlap": ("the overlap result", "reads")}
        for n, (where, text) in enumerate([(w, t) for t in (lone, valid) for w in names]):
            with self.subTest(where=where, lone=text == lone):
                comps = copy.deepcopy(fx["comps"])
                if where == "comparator":
                    comps[0]["writer_family"] = text
                files = comparator_files(f"lone_surrogate_{n}", comps=comps)
                if where == "overlap":
                    over = {**overlap_result(comps, files["comparator_sha256"]), "reads": text}
                    Path(files["overlap_result"]).write_text(json.dumps(over), encoding="utf-8")
                file, key = names[where]
                want = [f"{file} is refused: a JSON object holds a lone surrogate in the key or the value of '{key}'"]
                self.assertEqual(run_mod011.start_problems(mod_settings(**files)), want if text == lone else [])
                if text == valid:
                    continue
                path = self.dir / f"lone_surrogate_{n}.yaml"
                path.write_text(yaml.safe_dump({**self.raw, **files}), encoding="utf-8")
                rc, out = self.main(*self.selftest, settings=path)
                self.assertEqual(rc, 0, out)
                self.assertNotIn("Traceback", out)
                calls = []
                rc, out = self.main("--start", "--sign-key", str(self.key), *self.selftest, calls=calls, settings=path)
                self.assertEqual((rc, calls), (2, []), out)
                self.assertEqual(out.split("Fix these points first:\n")[1].splitlines(), [f"  - {want[0]}"])
                self.assertNotIn("Traceback", out)

    def test_a_comparator_that_is_not_an_object_gives_the_problems_of_the_check_and_no_traceback(self):
        """A comparator file whose list comparators holds a number: the report gives the estimate at the bound and no
        traceback, and --start refuses with the problems of mod011.check_comparators, before the probes and the
        executor. Before, the estimate stopped the report and --start with a TypeError."""
        files = comparator_files("comparator_number", raw=b'{"comparators": [5]}')
        path = self.dir / "comparator_number.yaml"
        path.write_text(yaml.safe_dump({**self.raw, **files}), encoding="utf-8")
        rc, out = self.main(*self.selftest, settings=path)
        self.assertEqual(rc, 0, out)
        self.assertIn("Worst-case estimate for the whole run: USD", out)
        self.assertNotIn("Traceback", out)
        problems = preflight.mod011_start_problems(sm.load_settings(path))
        self.assertTrue(problems[0].startswith("None: the key(s) id, benchmark, pair"), problems)
        calls = []
        rc, out = self.main("--start", "--sign-key", str(self.key), *self.selftest, calls=calls, settings=path)
        self.assertEqual((rc, calls), (2, []), out)
        self.assertEqual(out.split("Fix these points first:\n")[1].splitlines(), [f"  - {p}" for p in problems])
        self.assertNotIn("Traceback", out)

    def test_start_refuses_an_overlap_field_of_the_wrong_type_with_no_traceback(self):
        """An overlap result whose comparator_file is a number: --start refuses in one problem line, the problem of an
        absent field, before the probes and the executor. Before, start_problems stopped --start with an
        AttributeError."""
        fx = fixture()
        files = comparator_files("overlap_file_number")
        over = {**overlap_result(fx["comps"], files["comparator_sha256"]), "comparator_file": 5}
        Path(files["overlap_result"]).write_text(json.dumps(over), encoding="utf-8")
        path = self.dir / "overlap_file_number.yaml"
        path.write_text(yaml.safe_dump({**self.raw, **files}), encoding="utf-8")
        calls = []
        rc, out = self.main("--start", "--sign-key", str(self.key), *self.selftest, calls=calls, settings=path)
        self.assertEqual((rc, calls), (2, []), out)
        self.assertEqual(out.split("Fix these points first:\n")[1].splitlines(),
                         ["  - the overlap result is for another comparator file (its SHA-256 differs)"])
        self.assertNotIn("Traceback", out)

    def test_the_variant_model_probe_applies_to_tp_mod_011_only(self):
        other = self.dir / "sec001.yaml"
        other.write_text(yaml.safe_dump(EVIDENCE_RAW), encoding="utf-8")
        rc, out = self.main("--variant", "model_probe", settings=other)
        self.assertEqual(rc, 2, out)
        self.assertIn("--variant model_probe applies to the procedure TP-MOD-011 only", out)

    def test_with_the_files_the_run_starts_with_its_engine_the_variant_and_the_label(self):
        calls = []
        rc, out = self.main("--start", "--sign-key", str(self.key), *self.selftest, calls=calls)
        self.assertEqual(rc, 0, out)
        label = mod011_log.label_for(["family-w"], 0.7)
        for text in ("opens one session for each model call and sends at most 310 model calls (the instrument check "
                     "first) (3 runs per item)", f"The result is labelled '{label}'."):
            self.assertIn(text, out)
        self.assertEqual(len(calls), 1)
        self.assertEqual((calls[0]["variant"], calls[0]["selftest_log"], calls[0]["selftest_pubkey"]),
                         ("model_probe", str(fixture()["log"]), str(fixture()["pub"])))
        inspect.signature(run_mod011.execute).bind(**calls[0])
        rc, out = self.main("--start", "--sign-key", str(self.key), *self.selftest, "--variant", "model_probe",
                            calls=calls)
        self.assertEqual((rc, len(calls)), (0, 2), out)

    def test_the_preflight_executor_runs_the_mod011_engine(self):
        with mock.patch.object(run_mod011, "execute", lambda **kw: ("mod011", kw["settings"].procedure)):
            self.assertEqual(preflight._default_executor(settings=sm.load_settings(SHIPPED)), ("mod011", "TP-MOD-011"))

    def test_a_refused_file_gives_no_estimate(self):
        with mock.patch.object(mod011, "INSTRUMENT_FILE", str(fixture()["dir"] / "absent.json")):
            rc, out = self.main(*self.selftest)
        self.assertEqual(rc, 0, out)
        self.assertIn("Worst-case estimate: not available (an item file or the instrument file is refused)", out)


class InstrumentFileTests(unittest.TestCase):
    """The shipped instrument file, its digest file, NOTICE and items/README.md. The maintainer makes the file offline,
    with a tool that is not in this repository."""

    def test_the_shipped_file_passes_its_digest_and_names_each_work_with_no_source(self):
        self.assertEqual(mod011.instrument_problems(), [])
        self.assertEqual(mod011.read_digests(REPO / mod011.INSTRUMENT_DIGEST_FILE),
                         {"TP-MOD-011-instrument.json": sha((REPO / mod011.INSTRUMENT_FILE).read_bytes())})
        data = json.loads((REPO / mod011.INSTRUMENT_FILE).read_text(encoding="utf-8"))
        self.assertEqual((data["dataset"], len(data["passages"])), ("public-domain works", 10))
        self.assertEqual((data["licence"], data["generated_by"]["digest_file"]),
                         ("the works are in the public domain; the file holds their openings only",
                          "third_party/public_domain/instrument.sha256"))
        keys = ["id", "title", "author", "year", "passage", "cut", "prefix", "remainder"]
        for p in data["passages"]:
            with self.subTest(p["id"]):
                self.assertEqual(list(p), keys)
                self.assertRegex(p["id"], r"^[a-z][a-z0-9-]*$")
                self.assertTrue(p["title"] and p["author"])
                self.assertEqual(p["prefix"] + " " + p["remainder"], p["passage"])
        self.assertIsNone(re.search(r"https?:|www\.|\bpg\d", json.dumps(data)))
        notice = (REPO / "NOTICE").read_text(encoding="utf-8")
        readme = (REPO / "items" / "README.md").read_text(encoding="utf-8")
        self.assertIn("items/TP-MOD-011-instrument.json", notice)
        self.assertIn("## TP-MOD-011.json and TP-MOD-011-instrument.json", readme)
        for p in data["passages"]:
            with self.subTest(work=p["id"]):
                self.assertIn(f"{p['title']} ({p['author']}, {p['year']})", notice)
                self.assertIn(f"| {p['id']} | {p['title']} | {p['author']} | {p['year']} |", readme)


if __name__ == "__main__":
    unittest.main()
