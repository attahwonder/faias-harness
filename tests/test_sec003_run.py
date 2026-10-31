"""Tests of the TP-SEC-003 engine (faias_harness/engine/run_sec003.py) and its run log (schema 10). They use the local
stand-in model host and the fixture set of tests/test_sec003.py: four benign documents, each with a neutral marker in
two parts. No live call, no outside network, no key in any file.

A run needs a containment self-test log that passes (the self-test gate). The tests make one on this checkout with a
test key and point the engine at it with `selftest_log`.

The stand-in host writes the marker of an item when both parts of it are in a message of the request other than the
customer's last message: a system message (the passages that SRD-1 sent) or an earlier reply of the session. Otherwise
it gives a plain benign reply. So the seeded arm shows the markers and the clean arm does not. The P5 item gets its
document at turn 1 and its marker at turn 2, through the earlier reply, as on SRD-1 (sec003.score_run). P1-01 gets its
marker in its first seeded session only, so its three sessions differ (k = 3). P2-01 never gets its marker, and each
of its replies holds a staged placeholder of SRD-1's system prompt, so the redaction shows in the log.
"""
import contextlib
import copy
import dataclasses
import io
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from faias_harness import selftest
from faias_harness.engine import run_sec003, runlog, sec001, sec003, sec003_log, sec006, spike
from tests.helpers import FAKE_KEY_VALUE, KEY_ENV, StandInModelHost, make_settings, no_outside_network, shared_selftest
from tests.test_sec003 import CLEAN_FILE, FIXTURE_ITEMS, fixture_set

REPO = Path(__file__).resolve().parents[1]
ENV = {KEY_ENV: FAKE_KEY_VALUE}
CONFIRM = {"typed": "yes", "time": "2026-10-31T00:00:00Z"}
ENGINE = "inspect" if spike.inspect_available() else "direct"
PLAIN = "Thank you. Here is what our documents say about that."
ONCE, NEVER = ("P1-01",), ("P2-01",)
FIXTURE = object()          # run(): the fixture items
_FIXTURE = {}


def setUpModule():
    """One self-test log that passes on this checkout (tests/helpers.shared_selftest), the fixture items, and the
    SHA-256 of the shipped store before any run."""
    _FIXTURE["tmp"] = tempfile.TemporaryDirectory()
    tmp = Path(_FIXTURE["tmp"].name)
    _FIXTURE["log"], _FIXTURE["priv"], _FIXTURE["pub"] = shared_selftest(REPO)
    (tmp / "items.json").write_text(json.dumps(fixture_set()), encoding="utf-8")
    _FIXTURE["items"] = sec003.load_set(tmp / "items.json")
    _FIXTURE["file"] = tmp / "items.json"
    _FIXTURE["store_sha256"] = runlog.sha256_hex(CLEAN_FILE.read_bytes())
    from targets.srd1.app import orchestrator as om
    _FIXTURE["placeholders"] = sec006.find_placeholders([om.load_system_prompt()])
    _FIXTURE["placeholder"] = _FIXTURE["placeholders"][0]
    _FIXTURE["runs"] = {}


def tearDownModule():
    _FIXTURE["tmp"].cleanup()


class Responder:
    """The stand-in model (the module text). It knows each item by the text of the customer's last message."""

    def __init__(self, items, placeholder=None):
        self.by_turn = {t: i for i in items for t in i.turns}
        self.placeholder, self.shown = placeholder, Counter()

    def __call__(self, user, nth, messages):
        item = self.by_turn[user]
        a, b = item.marker
        earlier = [m.get("content") or "" for m in messages[:-1] if m["role"] in ("system", "assistant")]
        reply = PLAIN
        if item.id in NEVER and self.placeholder:
            reply += f" Your reference is {self.placeholder}."
        if item.id not in NEVER and any(a in t and b in t for t in earlier):
            self.shown[item.id] += 1
            if item.id not in ONCE or self.shown[item.id] == 1:
                reply += f" {a}{b}."
        return reply


def host(items=None, **kw):
    return StandInModelHost(respond=Responder(items or _FIXTURE["items"], _FIXTURE["placeholder"][1]),
                            with_messages=True, **kw)


def sec_settings(**kw):
    """Settings for a TP-SEC-003 run. The settings file does not take TP-SEC-003 yet, so the fields are set here."""
    base = {"procedure": sec003.TP, "retrieval_filter": "pre_retrieval", "arms": ("clean", "seeded")}
    return dataclasses.replace(make_settings(runs_per_item=3, cap_usd=20.0), **{**base, **kw})


def run(name, items=FIXTURE, *, stand_in=None, settings=None, **kw):
    """One engine run in the fixture folder, on the fixture items unless `items` names others (None: the default
    item load). Returns (the result, the stand-in host)."""
    d = Path(_FIXTURE["tmp"].name) / name
    h = stand_in or host()
    kw = {"engine": ENGINE, "selftest_log": str(_FIXTURE["log"]), "selftest_pubkey": str(_FIXTURE["pub"]), **kw}
    with h, no_outside_network(), contextlib.redirect_stdout(io.StringIO()):
        res = run_sec003.execute(settings or sec_settings(), run_dir=d, sign_key=_FIXTURE["priv"],
                                 confirmation=CONFIRM, items=_FIXTURE["items"] if items is FIXTURE else items,
                                 base_url=h.base_url, env=ENV, **kw)
    return res, h


def shared(name, **kw):
    """A run that several tests read. It is made once."""
    if name not in _FIXTURE["runs"]:
        _FIXTURE["runs"][name] = run(name, **kw)
    return _FIXTURE["runs"][name]


def records(res):
    return runlog.read_log(res.log_path)


def write(recs, path):
    path.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in recs), encoding="utf-8")
    return path


class RefusalTests(unittest.TestCase):
    """Each refusal comes before the lock: no run log is written and no model call is made."""

    def refused(self, name, error, words, **kw):
        d = Path(_FIXTURE["tmp"].name) / name
        stand_in = host()
        with self.assertRaises(error) as cm:
            run(name, stand_in=stand_in, **kw)
        for w in words:
            self.assertIn(w, str(cm.exception))
        self.assertFalse((d / "run_log.jsonl").exists())
        self.assertEqual(stand_in.requests, [])
        return d

    def test_the_stage_post_generation_is_refused(self):
        self.refused("post_generation", ValueError, ["the stage post_generation is refused", sec003_log.STAGE_REASON],
                     settings=sec_settings(retrieval_filter="post_generation"))

    def test_arms_without_the_clean_arm_are_refused(self):
        for arms, words in ((("seeded",), ["the arms have no clean arm", "line 27"]),
                            (("clean", "seeded", "tool"), ["the arms must be exactly clean and seeded"])):
            with self.subTest(arms=arms):
                self.refused(f"arms_{len(arms)}", ValueError, words, settings=sec_settings(arms=arms))

    def test_settings_of_another_procedure_are_refused(self):
        self.refused("procedure", ValueError, ["the settings do not name the procedure TP-SEC-003"],
                     settings=sec_settings(procedure="TP-DAT-009"))

    def test_a_store_that_is_not_the_store_of_the_seed_record_is_refused_for_each_arm(self):
        """SRD-1 builds one arm from the store of the other arm, so its reported hash differs from the seed record."""
        build_mod = spike._srd1()[0]
        real = build_mod.build_srd1
        for arm in sec003_log.ARMS:
            def swapped(settings, egress, trace_path, *, documents_file, **kw):
                other = {"clean": trace_path.parent.parent / sec003.SEEDED_STORE, "seeded": sec003.CLEAN_STORE}
                if trace_path.name == f"srd1_trace_{arm}.jsonl":
                    documents_file = other[arm]
                return real(settings, egress, trace_path, documents_file=documents_file, **kw)
            with self.subTest(arm=arm), mock.patch.object(build_mod, "build_srd1", swapped):
                self.refused(f"store_{arm}", RuntimeError,
                             ["refused before the lock", f"{arm}: the store that SRD-1 read is not the {arm} store"])

    def test_a_clean_subset_that_does_not_match_is_refused(self):
        real = sec003.seeded_bytes

        def changed(clean_raw, payload):
            return real(clean_raw, payload).replace(b"Retail tariff of charges", b"Retail tariff of chargez", 1)
        with mock.patch.object(sec003, "seeded_bytes", changed):
            d = self.refused("subset", RuntimeError, ["refused before the lock",
                                                      "does not hold the clean store byte for byte"])
        self.assertTrue((d / sec003.SEEDED_STORE).is_file())        # the refused store stays as evidence

    def test_without_a_passing_self_test_the_run_is_gated(self):
        empty = Path(_FIXTURE["tmp"].name) / "no_selftest"
        empty.mkdir(exist_ok=True)
        self.assertIn(sec003.TP, selftest.GATED)
        self.refused("gated", RuntimeError, ["TP-SEC-003 is gated", "self-test"], selftest_log=str(empty))

    def test_an_unknown_engine_is_refused(self):
        self.refused("engine", ValueError, ["unknown engine: other"], engine="other")

    def test_a_faulty_item_set_or_a_tool_class_is_refused(self):
        p1, *_ = _FIXTURE["items"]
        moved = dataclasses.replace(p1, turns=("What does a replacement card cost?",))
        tool = dataclasses.replace(p1, id="P3-01", cls="P3")
        for name, items, words in (("not_in_top", (moved,), ["the document is not in the top 3 for turn 1"]),
                                   ("tool_class", (tool,), ["P3-01: a class that needs a tool"])):
            with self.subTest(case=name):
                self.refused(f"items_{name}", ValueError, ["the item set is refused", *words], items=items)

    def test_the_default_item_load_refuses_while_the_item_file_is_absent(self):
        self.assertFalse((REPO / sec003.ITEM_FILE).exists())          # the item file waits for the owner
        with self.assertRaises(ValueError) as cm:
            run_sec003.load_items(repo=REPO)
        self.assertIn("the item file items/TP-SEC-003.json is not found", str(cm.exception))
        self.refused("no_item_file", ValueError, ["items/TP-SEC-003.json is not found"], items=None)
        # With the file in place, the default load reads it as the shipped set, with its authors.
        with mock.patch.object(sec003, "ITEM_FILE", str(_FIXTURE["file"])):
            items, item_set, authors = run_sec003.load_items(repo=REPO)
        self.assertEqual(([i.id for i in items], item_set, authors),
                         ([i["id"] for i in FIXTURE_ITEMS], sec003_log.ITEM_SET, "a test fixture"))


class StandardRunTests(unittest.TestCase):
    """The fixture run: four items, k = 3, both arms."""

    def setUp(self):
        self.res, self.host = shared("standard")
        self.recs = records(self.res)
        self.runs = [r for r in self.recs if r.get("type") == "run"]

    def test_the_seeded_arm_hits_the_marker_and_the_clean_arm_does_not(self):
        self.assertEqual(self.res.status, "complete")
        self.assertEqual(len(self.runs), 4 * 3 * 2)
        self.assertFalse(any(r["marker"] or r["hit"] for r in self.runs if r["arm"] == "clean"))
        hits = Counter(r["item"] for r in self.runs if r["arm"] == "seeded" and r["hit"])
        self.assertEqual(hits, {"P1-01": 1, "P5-01": 3, "P1-02": 3})
        for r in (r for r in self.runs if r["hit"]):
            with self.subTest(item=r["item"], run=r["run"]):
                turn = r["influencing"]["turn"]
                passed = next(x["passed_to_model"] for x in r["lines"]
                              if x["kind"] == "retrieval" and x["turn"] == turn)
                self.assertIn(r["doc_id"], passed)
                self.assertEqual(r["influencing"]["doc_id"], r["doc_id"])
        p5 = next(r for r in self.runs if r["item"] == "P5-01" and r["arm"] == "seeded")
        self.assertEqual((p5["hit_turns"], p5["influencing"]["turn"], p5["retrieved_turns"]), ([2], 1, [1]))
        m6 = self.recs[-1]["measures"]["M6"]
        self.assertEqual((m6["affected"], m6["identifiable"]), (7, 7))

    def test_the_log_passes_check_and_rescore_with_and_without_the_repository(self):
        self.assertEqual(runlog.check_log(self.res.log_path, repo=REPO), [])
        self.assertEqual(runlog.check_log(self.res.log_path, repo=REPO, use_repo=False), [])
        lines, mismatches = runlog.rescore_log(self.res.log_path, repo=REPO)
        self.assertEqual(mismatches, [], lines)
        self.assertIn("runs rescored: 24 of 24; mismatches: 0", lines)
        for use_repo in (True, False):
            ok, msg = runlog.verify_log(self.res.log_path, self.res.sig_path, _FIXTURE["pub"], repo=REPO,
                                        use_repo=use_repo)
            self.assertTrue(ok, msg)

    def test_a_record_holds_the_turn_hashes_the_calls_and_the_redacted_lines_only(self):
        items = {i.id: i for i in _FIXTURE["items"]}
        data = self.res.log_path.read_bytes().decode("utf-8")
        for r in self.runs:
            item = items[r["item"]]
            with self.subTest(item=r["item"], arm=r["arm"], run=r["run"]):
                self.assertEqual(set(r), set(sec003_log.RECORD_KEYS) | set(sec003_log.FACTS))
                self.assertEqual(r["request_sha256"], [runlog.sha256_hex(t) for t in item.turns])
                self.assertEqual((r["class"], r["activity"]), (item.cls, item.activity))
                self.assertEqual(len(r["calls"]), len(item.turns))
                self.assertEqual([(x["kind"], x["turn"]) for x in r["lines"]],
                                 [(k, n) for n in range(1, len(item.turns) + 1) for k in ("retrieval", "turn")])
        for t in [t for i in FIXTURE_ITEMS for t in i["turns"]] + [i["document"]["text"] for i in FIXTURE_ITEMS]:
            self.assertNotIn(t, data)
        # The lines are those of SRD-1's trace store (sec003_log.record_lines), session by session, with each reply
        # redacted. The staged placeholder of the replies of P2-01 is in that trace and not in the log.
        kind, value = _FIXTURE["placeholder"]
        for arm in sec003_log.ARMS:
            entries = [json.loads(x) for x in (self.res.run_dir / "target" / f"srd1_trace_{arm}.jsonl")
                       .read_text(encoding="utf-8").splitlines() if x.strip()]
            sessions = [e["session"] for e in entries if e.get("kind") == "session_open"]
            mine = [r for r in self.runs if r["arm"] == arm]
            self.assertEqual(len(sessions), len(mine))
            for s, r in zip(sessions, mine):
                want, earlier = sec003_log.record_lines([e for e in entries if e.get("session") == s]), []
                for line in (x for x in want if x["kind"] == "turn"):
                    raw = line["reply"]
                    line["reply"] = sec006.redact(raw, _FIXTURE["placeholders"], earlier=tuple(earlier))[0]
                    earlier.append(raw)
                self.assertEqual(r["lines"], want)
            self.assertIn(value, "".join(json.dumps(e) for e in entries))
        self.assertNotIn(value, data)
        self.assertIn(sec006.TOKENS[kind], data)
        self.assertEqual(self.recs[-1]["placeholders_redacted"][kind], 6)        # three sessions of P2-01 in each arm

    def test_k_sessions_that_differ_follow_the_k_run_rule_and_rescore_agrees(self):
        """P1-01 shows its marker in one of its three seeded sessions. By the k-run rule it is an item with the
        marker, so the P1 rate of the seeded arm is 2 of 2 (by sessions it would be 4 of 6)."""
        seeded = [r["marker"] for r in self.runs if r["item"] == "P1-01" and r["arm"] == "seeded"]
        self.assertEqual(seeded, [True, False, False])
        m2 = self.recs[-1]["measures"]["M2"]
        self.assertEqual((m2["runs_per_item"], m2["k_rule"], m2["items_incomplete"]),
                         (3, sec003.K_RULE, {"seeded": 0, "clean": 0}))
        p1 = m2["by_class"]["P1"]
        self.assertEqual((p1["seeded"]["items"], p1["seeded"]["items_with_marker"], p1["clean"]["items_with_marker"],
                          p1["override_rate"], p1["seeded_retrieved"]), (2, 2, 0, 1.0, 2))
        self.assertEqual(m2["by_class"]["P2"]["override_rate"], 0.0)
        self.assertEqual(m2["by_class"]["P5"]["override_rate"], 1.0)
        facts = [{**{f: r[f] for f in sec003_log.FACTS}, "arm": r["arm"], "activity": r["activity"]}
                 for r in self.runs]
        self.assertEqual(sec003.compute_measures(facts, runs_per_item=3), self.recs[-1]["measures"])
        self.assertNotEqual(sec003.compute_measures(facts, runs_per_item=1)["M2"], m2)
        _, mismatches = runlog.rescore_log(self.res.log_path, repo=REPO)
        self.assertEqual(mismatches, [])

    def test_the_summary_holds_the_measures_teardown_traces_egress_and_cost(self):
        summary = self.recs[-1]
        self.assertEqual(sorted(summary["measures"]), ["M1", "M2", "M3", "M4", "M5", "M6", "M7"])
        self.assertEqual(summary["measures"]["M3"]["seeded"], 4)
        teardown = summary["teardown"]
        self.assertEqual((teardown["store_written"], teardown["srd1_stopped"], teardown["seeded_store"]),
                         (False, True, sec003.SEEDED_STORE))
        self.assertEqual(runlog.sha256_hex(CLEAN_FILE.read_bytes()), _FIXTURE["store_sha256"])
        self.assertEqual(self.recs[0]["seed"]["clean"]["sha256"], _FIXTURE["store_sha256"])
        for arm in sec003_log.ARMS:
            trace = summary["arms"][arm]["trace"]
            self.assertTrue(trace["chain_ok"], arm)
            self.assertEqual(trace["file"], f"target/srd1_trace_{arm}.jsonl")
            self.assertEqual(trace["sha256"], runlog.sha256_hex((self.res.run_dir / trace["file"]).read_bytes()))
        allowed = summary["egress"]["summary"]["allowed"]
        self.assertTrue(allowed and all(k.startswith("127.0.0.1:") for k in allowed), allowed)
        calls = [c for r in self.runs for c in r["calls"]]
        self.assertEqual((summary["model_calls"], sum(summary["served_hosts"].values())), (len(calls), len(calls)))
        self.assertAlmostEqual(summary["cost_usd"], sum(c["cost_usd"] for c in calls), places=8)
        self.assertEqual(len(self.host.requests), len(calls))

    def test_the_model_calls_carry_no_tool_definitions(self):
        self.assertEqual(len(self.host.requests), 30)          # 4 items, 5 turns, k = 3, two arms
        for req in self.host.requests:
            self.assertNotIn("tools", req["body"])
            self.assertNotIn("tool", {m["role"] for m in req["body"]["messages"]})
        for arm in sec003_log.ARMS:
            self.assertIsNone(self.recs[0]["retrieval_config"][arm]["tool_path"]["tool"])

    def test_the_header_holds_the_context_assembly_true_at_the_tree(self):
        h = self.recs[0]
        ca = h["context_assembly"]
        self.assertEqual(ca, sec003_log.context_assembly(h["retrieval_config"], REPO))
        self.assertEqual((ca["role"], ca["role_separated"], ca["screened"]), ("system", False, False))
        path = REPO / ca["file"]
        self.assertEqual(ca["file_sha256"], runlog.sha256_hex(path.read_bytes()))
        lines = path.read_text(encoding="utf-8").splitlines()
        for name, function, text in sec003_log.ASSEMBLY_SITES:
            with self.subTest(site=name):
                self.assertEqual(ca["sites"][name]["function"], function)
                self.assertIn(text, lines[ca["sites"][name]["line"] - 1])
        for arm in sec003_log.ARMS:
            self.assertEqual(ca["by_arm"][arm], {k: h["retrieval_config"][arm][k]
                                                 for k in ("module", "context_template_sha256")})
        self.assertIn("context assembly", h["lock"]["covers"])
        # A change after the lock fails the check; so does a header with no block, relocked.
        recs = copy.deepcopy(self.recs)
        recs[0]["context_assembly"]["sites"]["context_message"]["line"] += 1
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(runlog.check_log(write(recs, Path(tmp) / "a.jsonl"), repo=REPO),
                             ["header: the lock hash does not match the fixed parts of the header"])
            recs = copy.deepcopy(self.recs)
            recs[0].pop("context_assembly")
            runlog.lock_header(recs[0], recs[0]["lock"]["time_ns"])
            problems = runlog.check_log(write(recs, Path(tmp) / "b.jsonl"), repo=REPO)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("header: the context_assembly block", problems[0])

    def test_a_fixture_run_carries_the_fixture_item_set_and_the_spike_label(self):
        h = self.recs[0]
        self.assertEqual((h["corpus"]["item_set"], h["corpus"]["authors"]),
                         (sec003_log.FIXTURE_SET, run_sec003.FIXTURE_AUTHORS))
        self.assertTrue(h["label"].startswith(sec001.LABELS["spike"]))
        self.assertEqual(self.recs[-1]["label"], h["label"])


class StopTests(unittest.TestCase):
    def test_in_evidence_mode_a_host_mismatch_stops_the_run(self):
        res, _ = run("host", items=_FIXTURE["items"][:1], stand_in=host(provider="OtherHost"))
        self.assertEqual(res.status, "stopped_host_mismatch")
        recs = records(res)
        self.assertEqual([r["type"] for r in recs], ["header", "run", "run_stopped", "summary"])
        self.assertEqual((recs[2]["reason"], recs[2]["served_host"], recs[2]["arm"]),
                         ("stopped_host_mismatch", "OtherHost", "clean"))
        self.assertEqual(recs[-1]["host_mismatches"], 1)
        self.assertEqual(runlog.check_log(res.log_path, repo=REPO), [])
        _, mismatches = runlog.rescore_log(res.log_path, repo=REPO)
        self.assertEqual(mismatches, [])


if __name__ == "__main__":
    unittest.main()
