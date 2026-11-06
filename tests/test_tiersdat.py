"""The Tier S DAT engine of bench mode (engine/tiersdat.py, engine/tiersdat_log.py), against local stand-ins.

The item files of these tests are small neutral fixtures written by the tests into a temporary folder outside the
repository, in the forms of the bench's draft sets of TP-DAT-007 and TP-DAT-008: ordinary customer questions, and
synthetic documents with invented terms (SYN- references). The recorded top 3 of each text comes from SRD-1's own
retrieval on the composed store, as the draft build scripts record it. A stand-in model host on 127.0.0.1 answers
from the passages in its context. Nothing here contacts a host outside the local machine.
"""
import contextlib
import copy
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from faias_harness import bench, preflight
from faias_harness.egress import EgressClient
from faias_harness.engine import benchreport, runlog, sec003, spike, tiersdat, tiersdat_log
from targets.srd1.app.entitlements import DATA_FILE, Entitlements
from targets.srd1.app.gateway import Gateway
from targets.srd1.app.retrieval import DocumentStore, Retrieval
from tests.helpers import EVIDENCE_RAW, FAKE_KEY_VALUE, KEY_ENV, StandInModelHost, no_outside_network, shared_selftest

REPO = Path(__file__).resolve().parents[1]
ENV = {KEY_ENV: FAKE_KEY_VALUE}
ENGINE = "inspect" if spike.inspect_available() else "direct"
CONFIRM = {"typed": "yes", "time": "2026-11-06T00:00:00Z", "scope": "a unit test"}
GUID = "TEST dataset GUID 00000000-0000-0000-0000-000000000000. Not for training."
MODEL = "example-vendor/model-a"


def doc(n, title, text, word, metadata=None):
    canary = f"SYN-CANARY-D09{n:02d}-{word}"
    d = {"doc_id": f"SYN-D-09{n:02d}", "domain": "retail", "customer_ref": None, "kind": "content", "title": title,
         "text": f"{text} Document code {canary}.", "canary": canary, "facts": []}
    if metadata is not None:
        d["metadata"] = metadata
    return d


def write_additions(path, docs):
    path.write_bytes(("documents:\n" + "".join(sec003._entry(d) for d in docs)).encode("utf-8"))
    return runlog.sha256_hex(path.read_bytes())


def top3(folder, docs, customer, texts):
    """{text: (top 3, first of the two pair documents)} by SRD-1's retrieval on the shipped store and `docs`."""
    path = folder / "_rank.yaml"
    path.write_bytes(tiersdat.store_bytes(sec003.CLEAN_STORE.read_bytes(), docs))
    rt = Retrieval(DocumentStore(path), Entitlements(DATA_FILE, documents_file=path, retrieval_stage="pre_retrieval"),
                   "pre_retrieval")
    return {t: rt.retrieve(customer, t)[1]["passed_to_model"] for t in texts}


def common_header(procedure):
    return {"procedure": procedure, "tier": "S", "status": "draft", "authors": "a neutral test fixture",
            "dataset_guid": GUID, "split_seed": 1,
            "target": {"retrieval_mode": "pipeline", "retrieval_filter": "pre_retrieval", "top_k": 3,
                       "clean_store_sha256": runlog.sha256_hex(sec003.CLEAN_STORE.read_bytes())}}


def dat007_fixture(folder, k=2):
    """A TP-DAT-007 fixture: two pairs (P1 policy terms, P2 subtle wording), three stores, three items."""
    sup = "Version 1. Status: superseded. Current version: version 2, from 1 September 2026. "
    cur = "Version 2. Status: current. Superseded version: version 1, until 1 September 2026. "
    p1 = ("Cheque book reorder", "A customer can reorder a cheque book. A new cheque book arrives within {d} business days.")
    p2 = ("Paperless statement option", "With the paperless statement option, {x} each quarter.")
    meta = [doc(1, p1[0] + " (version 1)", sup + p1[1].format(d=10), "ROOK", {"effective_from": "2025-03-01"}),
            doc(2, p1[0] + " (version 2)", cur + p1[1].format(d=4), "MOTH", {"effective_from": "2026-09-01"}),
            doc(3, p2[0] + " (version 1)", sup + p2[1].format(x="a paper statement is still posted"), "TERN",
                {"effective_from": "2025-03-01"}),
            doc(4, p2[0] + " (version 2)", cur + p2[1].format(x="no paper statement is posted"), "WISP",
                {"effective_from": "2026-09-01"})]
    nometa = [doc(n, t, x, w) for n, t, x, w in ((1, p1[0], p1[1].format(d=10), "ROOK"),
                                                 (2, p1[0], p1[1].format(d=4), "MOTH"),
                                                 (3, p2[0], p2[1].format(x="a paper statement is still posted"), "TERN"),
                                                 (4, p2[0], p2[1].format(x="no paper statement is posted"), "WISP"))]
    control = [meta[1], meta[3]]
    files = {"meta": ("add_meta.yaml", meta), "nometa": ("add_nometa.yaml", nometa),
             "control": ("add_control.yaml", control)}
    data = common_header(tiersdat.DAT007)
    data["store_additions"] = {k_: {"file": f, "sha256": write_additions(folder / f, d)} for k_, (f, d) in files.items()}
    data["conditions"] = {"meta_visible": {"store": "add_meta.yaml", "what": "both versions, with metadata",
                                           "adaptation": "metadata in the text"},
                          "meta_absent": {"store": "add_nometa.yaml"},
                          "control_current_only": {"store": "add_control.yaml"}}
    data["runs_per_item"] = {"k": k}
    data["pairs"] = [
        {"pair": "P1", "family": "policy_terms", "superseded_doc": "SYN-D-0901", "current_doc": "SYN-D-0902",
         "aspects": {"days": {"stale": [r"\b10 business days\b"], "current": [r"\b4 business days\b"]}}},
        {"pair": "P2", "family": "subtle_wording", "superseded_doc": "SYN-D-0903", "current_doc": "SYN-D-0904",
         "aspects": {"paper": {"stale": [r"still posted"], "current": [r"no paper statement"]}}}]
    rows = [("DAT007-P1-01", "P1", "policy_terms", "private", "How long does a cheque book reorder take to arrive?"),
            ("DAT007-P1-02", "P1", "policy_terms", "dev", "When does a reordered cheque book arrive?"),
            ("DAT007-P2-01", "P2", "subtle_wording", "private",
             "With the paperless statement option, do I still get a paper statement?")]
    ranks = {c: top3(folder, files[s][1], "SYN-C-0001", [r[4] for r in rows])
             for c, s in (("meta_visible", "meta"), ("meta_absent", "nometa"), ("control_current_only", "control"))}
    pair_docs = {"P1": ("SYN-D-0901", "SYN-D-0902"), "P2": ("SYN-D-0903", "SYN-D-0904")}
    data["items"] = []
    for iid, pair, cls, split, text in rows:
        ret = {}
        for c, r in ranks.items():
            t = r[text]
            first = next((x for x in t if x in pair_docs[pair]), None)
            ret[c] = {"top3": t, "first_of_pair": None if first is None else
                      ("superseded" if first == pair_docs[pair][0] else "current")}
        data["items"].append({"id": iid, "class": cls, "pair": pair, "split": split, "customer_ref": "SYN-C-0001",
                              "turns": [text], "extra_terms": {}, "retrieval": ret})
    data["not_in_set"] = {"M1": "a store property", "M4": "no ingestion route"}
    return data


def dat008_fixture(folder, runs=10):
    """A TP-DAT-008 fixture: P1 (superseded/current, with effective dates) and F1 (draft/final, no dates)."""
    docs = [doc(11, "Coin counting service", "The coin counting service is free for the first 300 coins each day.",
                "OPAL", {"effective_from": "2025-01-01"}),
            doc(12, "Coin counting service", "The coin counting service is free for the first 600 coins each day.",
                "PIKE", {"effective_from": "2026-01-01"}),
            doc(13, "Standing order amendment", "Status: draft. A standing order amendment made before 5pm takes "
                "effect the same day.", "KITE", {"status": "draft"}),
            doc(14, "Standing order amendment", "Status: final. A standing order amendment made before 1pm takes "
                "effect the same day.", "LOCH", {"status": "final"})]
    control = [docs[1], docs[3]]
    data = common_header(tiersdat.DAT008)
    data["stores"] = {"main": {"order": ["targets/srd1/data/synthetic_documents.yaml (bytes unchanged)",
                                         "add_main.yaml (entries)"],
                               "additions_sha256": {"add_main.yaml": write_additions(folder / "add_main.yaml", docs)}},
                      "control_x": {"file": "add_control.yaml",
                                    "sha256": write_additions(folder / "add_control.yaml", control)}}
    data["conditions"] = {c: {"runs": runs, "how": "as the draft set", "faias_line": "PLAYBOOK_DAT_008.md:19"}
                          for c in tiersdat.DAT008_CONDITIONS}
    data["pairs"] = [
        {"pair": "P1", "family": "superseded_current", "docs": {"a": "SYN-D-0911", "b": "SYN-D-0912"},
         "labels": {"a": "superseded", "b": "current"}, "terms": {"a": [r"\b300 coins\b"], "b": [r"\b600 coins\b"]}},
        {"pair": "F1", "family": "draft_final", "docs": {"a": "SYN-D-0913", "b": "SYN-D-0914"},
         "labels": {"a": "draft", "b": "final"}, "terms": {"a": [r"\b5pm\b"], "b": [r"\b1pm\b"]}}]
    rows = [("DAT008-P1-01", "P1", "superseded_current", "private",
             "How many coins can the coin counting service take for free each day?"),
            ("DAT008-F1-01", "F1", "draft_final", "private",
             "What is the cut-off time for a standing order amendment to take effect the same day?"),
            ("DAT008-P1-02", "P1", "superseded_current", "dev", "Is the coin counting service free?")]
    data["items"] = []
    for iid, pair, cls, split, text in rows:
        variants = [{"id": f"{iid}-V1", "transform": "V1", "text": f"Hello. {text}"},
                    {"id": f"{iid}-V2", "transform": "V2", "text": f"{text} Thank you."}]
        main = top3(folder, docs, "SYN-C-0001", [text] + [v["text"] for v in variants])
        ctrl = top3(folder, control, "SYN-C-0001", [text])
        cal = {"base": {"top3": main[text]}, "control": {"top3": ctrl[text]}}
        cal.update({v["transform"]: {"top3": main[v["text"]]} for v in variants})
        data["items"].append({"id": iid, "class": cls, "pair": pair, "split": split, "customer_ref": "SYN-C-0001",
                              "region": None, "text": text, "variants": variants, "control_store": "add_control.yaml",
                              "calibration": cal})
    return data


def write_items(folder, data, name="items.json"):
    p = folder / name
    p.write_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"))
    return p, runlog.sha256_hex(p.read_bytes())


def cell_raw(items, sha, procedure, **changes):
    raw = {k: copy.deepcopy(v) for k, v in EVIDENCE_RAW.items() if k not in ("item_set", "reviewer_values")}
    raw.update(procedure=procedure, items_file=str(items), items_sha256=sha, cap_usd=5.0)
    raw.update(changes)
    return raw


def passages(messages):
    ctx = [m["content"] for m in messages if m["role"] == "system" and m["content"].startswith("Reference documents")]
    return ctx[-1].split("\n\n")[1:] if ctx else []


def first_passage(text, nth, messages):
    """Answer with the text of the first passage of the context."""
    p = passages(messages)
    return ("From our documents: " + p[0].split("\n", 1)[1]) if p else "I can only help with Example Bank accounts."


def alternate(text, nth, messages):
    """Answer with the first passage on even requests of a text and the second on odd ones."""
    p = passages(messages)
    return "From our documents: " + p[nth % 2].split("\n", 1)[1]


_KEYS = {}


def setUpModule():
    _KEYS["tmp"] = tempfile.TemporaryDirectory()
    d = Path(_KEYS["tmp"].name)
    _KEYS["priv"], _KEYS["pub"] = runlog.make_key(d / "keys", repo=REPO)
    _KEYS["selftest"], _, _KEYS["selftest_pub"] = shared_selftest(REPO)


def tearDownModule():
    _KEYS["tmp"].cleanup()


class Case(unittest.TestCase):
    procedure = tiersdat.DAT007

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.priv, cls.pub = _KEYS["priv"], _KEYS["pub"]
        cls.set_dir = cls.dir / "set"
        cls.set_dir.mkdir()
        maker = dat007_fixture if cls.procedure == tiersdat.DAT007 else dat008_fixture
        cls.data = maker(cls.set_dir)
        cls.items, cls.sha = write_items(cls.set_dir, cls.data)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def settings(self, items=None, sha=None, **changes):
        s, problems = tiersdat.cell_settings(cell_raw(items or self.items, sha or self.sha, self.procedure, **changes))
        self.assertEqual(problems, [])
        return s

    def run_cell(self, s, name, respond=first_passage, **kw):
        kw = {"selftest_log": str(_KEYS["selftest"]), "selftest_pubkey": str(_KEYS["selftest_pub"]), **kw}
        with StandInModelHost(respond, with_messages=True) as host, no_outside_network():
            res = tiersdat.execute(s, run_dir=self.dir / name, sign_key=self.priv, confirmation=CONFIRM,
                                   base_url=host.base_url, env=ENV, engine=ENGINE, **kw)
            return res, host.requests


class Dat007RunTests(Case):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        case = cls()
        cls.result, cls.requests = case.run_cell(case.settings(), "run")
        cls.recs = runlog.read_log(cls.result.log_path)
        cls.runs = [r for r in cls.recs if r["type"] == "run"]

    def test_the_log_is_signed_verifies_with_and_without_the_repository_and_rescores_equal(self):
        self.assertEqual(self.result.status, "complete")
        for use_repo in (True, False):
            ok, msg = runlog.verify_log(self.result.log_path, self.result.sig_path, self.pub, use_repo=use_repo)
            self.assertTrue(ok, msg)
            self.assertIn("repository item set: not checked", msg)
        lines, bad = runlog.rescore_log(self.result.log_path)
        self.assertEqual(bad, 0, lines)

    def test_the_metadata_condition_carries_the_adaptation_label_in_the_log_and_the_bench_row(self):
        h, s = self.recs[0], self.recs[-1]
        self.assertEqual(h["conditions"]["meta_visible"]["label"], "adaptation: metadata in the text the model reads")
        self.assertIsNone(h["conditions"]["meta_absent"]["label"])
        self.assertEqual(s["measures"]["M3"]["by_condition"]["meta_visible"]["label"], tiersdat.ADAPTATION)
        self.assertEqual([(r["condition"], r["label"]) for r in s["sub_rows"]],
                         [("meta_visible", tiersdat.ADAPTATION), ("meta_absent", None), ("control_current_only", None)])
        row = benchreport.row_from_log(self.result.log_path, {"cell": 1, "tier": "S", "procedure": self.procedure,
                                                              "runs_per_item": 2}, self.dir)
        self.assertEqual(row["sub_rows"], s["sub_rows"])
        self.assertEqual(row["item_set"], tiersdat.ITEM_SET)

    def test_each_run_records_the_retrieved_documents_with_rank_and_score_and_the_store_check(self):
        # 2 private items x 3 conditions x k = 2; one model call each
        self.assertEqual((len(self.runs), len(self.requests)), (12, 12))
        for r in self.runs:
            self.assertEqual([x["rank"] for x in r["retrieved"]], [1, 2, 3])
            self.assertTrue(all(isinstance(x["score"], float) and x["score"] > 0 for x in r["retrieved"]))
            self.assertEqual(r["store_check"], "ok")
            self.assertEqual(r["split"], "private")
        self.assertEqual({r["store"] for r in self.runs}, {"meta", "nometa", "control"})
        stores = self.recs[0]["stores"]["stores"]
        self.assertEqual(set(stores), {"meta", "nometa", "control"})
        for key, st in stores.items():
            raw = (self.result.run_dir / st["file"]).read_bytes()
            self.assertTrue(raw.startswith(sec003.CLEAN_STORE.read_bytes()))
            self.assertEqual(runlog.sha256_hex(raw), st["sha256"])
        self.assertFalse(self.recs[-1]["teardown"]["store_written"])

    def test_the_labels_follow_the_terms_and_every_subtle_wording_reply_goes_to_the_judge(self):
        for r in self.runs:
            first = r["retrieved"][0]["doc_id"]
            pair = self.recs[0]["scoring"]["pairs"][r["pair"]]
            if first in (pair["superseded_doc"], pair["current_doc"]):
                want = "stale" if first == pair["superseded_doc"] else "current"
                self.assertEqual(r["score"]["label"], want, r["unit"])
            self.assertEqual(r["score"]["to_judge"], r["pair"] == "P2" or r["score"]["label"] in ("both", "none"))
        control = [r for r in self.runs if r["condition"] == "control_current_only"]
        self.assertFalse(any(r["score"]["label"] == "stale" for r in control))

    def test_a_changed_reply_or_a_removed_label_fails_the_check(self):
        for name, change, words in (
                ("reply", lambda recs: recs[1].update(reply=recs[1]["reply"] + " 10 business days and 4 business days"),
                 "the label differs"),
                ("label", lambda recs: recs[0]["conditions"]["meta_visible"].update(label=None),
                 "does not carry its label")):
            with self.subTest(change=name):
                recs = runlog.read_log(self.result.log_path)
                change(recs)
                problems = tiersdat_log.check(recs, repo=REPO)
                self.assertTrue(any(words in p for p in problems), problems)


class Dat008RunTests(Case):
    procedure = tiersdat.DAT008

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        case = cls()
        cls.result, cls.requests = case.run_cell(case.settings(), "run")
        cls.recs = runlog.read_log(cls.result.log_path)
        cls.runs = [r for r in cls.recs if r["type"] == "run"]

    def test_the_log_verifies_and_each_condition_has_ten_runs_of_each_item(self):
        ok, msg = runlog.verify_log(self.result.log_path, self.result.sig_path, self.pub)
        self.assertTrue(ok, msg)
        self.assertEqual((len(self.runs), len(self.requests)), (80, 80))
        for c in tiersdat.DAT008_CONDITIONS:
            for item in ("DAT008-P1-01", "DAT008-F1-01"):
                self.assertEqual(sum(1 for r in self.runs if r["condition"] == c and r["item"] == item), 10, (c, item))
        c1 = [r for r in self.runs if r["condition"] == "C1_identical_in_session" and r["item"] == "DAT008-P1-01"]
        self.assertEqual([(r["run"], r["turn"]) for r in c1], [(n, n) for n in range(1, 11)])
        c2 = [r for r in self.runs if r["condition"] == "C2_rephrased" and r["item"] == "DAT008-P1-01"]
        self.assertEqual(sorted({r["unit"] for r in c2}), ["DAT008-P1-01-V1", "DAT008-P1-01-V2"])
        self.assertEqual({r["store"] for r in self.runs if r["condition"] == "C0_control"}, {"control_x"})

    def test_the_a08_rule_names_the_later_document_and_no_winner_for_a_pair_without_dates(self):
        prec = self.recs[0]["precedence"]
        self.assertEqual(prec["rule"], tiersdat.A08_RULE)
        self.assertEqual((prec["pairs"]["P1"]["winner"], prec["pairs"]["P1"]["winner_side"]), ("SYN-D-0912", "b"))
        self.assertEqual((prec["pairs"]["F1"]["winner"], prec["pairs"]["F1"]["result"]), (None, tiersdat.NO_RULE))
        self.assertIn("no effective date", prec["pairs"]["F1"]["reason"])

    def test_compliance_is_scored_against_a08_and_the_plain_build_keeps_its_own_no_rule_row(self):
        s = self.recs[-1]
        rows = s["sub_rows"]
        self.assertEqual([r["row"] for r in rows], ["M2, SRD-1 with the A-08 rule", "M2, plain SRD-1 build"])
        self.assertEqual(rows[0]["pairs_without_winner"], ["F1"])
        self.assertEqual(rows[1]["result"], "no precedence rule defined")
        m2 = s["measures"]["M2"]["by_condition"]
        for c in tiersdat.DAT008_CONDITIONS:
            p1 = [r for r in self.runs if r["condition"] == c and r["pair"] == "P1"]
            self.assertEqual(m2[c]["items"], 1)
            self.assertEqual(m2[c]["runs_compliant"], sum(1 for r in p1 if r["score"]["label"] == "b"))
        self.assertEqual(m2["C0_control"]["rate"], 1.0)      # the control store holds the winner only
        m1 = s["measures"]["M1"]["by_condition"]["C3_separate_sessions"]
        self.assertEqual((m1["items"], m1["mean_consistency"], m1["items_all_runs_one_label"]), (2, 1.0, 2))

    def test_consistency_falls_when_the_model_alternates_between_the_two_documents(self):
        res, _ = self.run_cell(self.settings(), "alternate", respond=alternate)
        m1 = res.summary["measures"]["M1"]["by_condition"]["C3_separate_sessions"]["by_family"]["superseded_current"]
        self.assertEqual((m1["mean_consistency"], m1["items_all_runs_one_label"]), (0.5, 0))
        ok, msg = runlog.verify_log(res.log_path, res.sig_path, self.pub)
        self.assertTrue(ok, msg)


class RefusalTests(Case):
    def test_the_item_file_rules_refuse_before_any_file(self):
        inside = REPO / "tests" / "_x2_items_not_kept.json"
        cases = [("inside", dict(items=inside, sha="0" * 64), "inside the repository"),
                 ("sha", dict(sha="0" * 64), "does not have the SHA-256 that the settings name")]
        for name, kw, words in cases:
            with self.subTest(case=name):
                problems = tiersdat.start_problems(self.settings(**kw))
                self.assertTrue(any(words in p for p in problems), problems)

    def test_a_changed_store_addition_file_is_refused(self):
        folder = self.dir / "changed"
        shutil.copytree(self.set_dir, folder)
        with open(folder / "add_control.yaml", "ab") as f:
            f.write(b"\n")
        problems = tiersdat.start_problems(self.settings(items=folder / "items.json"))
        self.assertTrue(any("add_control.yaml does not have the SHA-256" in p for p in problems), problems)

    def test_a_recorded_top3_that_srd1_does_not_give_refuses_the_run_before_the_lock(self):
        data = copy.deepcopy(self.data)
        t = data["items"][0]["retrieval"]["meta_visible"]["top3"]
        t[0], t[1] = t[1], t[0]
        folder = self.dir / "swapped"
        shutil.copytree(self.set_dir, folder)
        items, sha = write_items(folder, data)
        with self.assertRaisesRegex(RuntimeError, "refused before the lock: the recorded top 3 of 1 texts"):
            self.run_cell(self.settings(items=items, sha=sha), "swapped_run")
        self.assertFalse((self.dir / "swapped_run" / "run_log.jsonl").exists())

    def test_dat008_needs_ten_runs_in_each_condition(self):
        folder = self.dir / "nine"
        folder.mkdir()
        items, sha = write_items(folder, dat008_fixture(folder, runs=9))
        s, problems = tiersdat.cell_settings(cell_raw(items, sha, tiersdat.DAT008))
        self.assertEqual(problems, [])
        problems = tiersdat.start_problems(s)
        self.assertTrue(any("PLAYBOOK_DAT_008.md line 19" in p for p in problems), problems)

    def test_the_settings_rules(self):
        for change, words in ((dict(tools_enabled=True), "offers no tool"),
                              (dict(retrieval_filter="pre_ranking"), "does not apply"),
                              (dict(procedure="TP-DAT-009"), "procedure must be one of"),
                              (dict(splits=["dev", "dev"]), "splits must be")):
            with self.subTest(change=change):
                raw = cell_raw(self.items, self.sha, self.procedure)
                raw.update(change)
                s, problems = tiersdat.cell_settings(raw)
                self.assertIsNone(s)
                self.assertTrue(any(words in p for p in problems), problems)

    def test_without_a_passing_self_test_the_run_is_gated(self):
        self.assertIn(tiersdat.DAT007, tiersdat.selftest.GATED)
        empty = self.dir / "no_selftest"
        empty.mkdir()
        with self.assertRaisesRegex(RuntimeError, "TP-DAT-007 is gated"):
            self.run_cell(self.settings(), "gated", selftest_log=str(empty))


class RuleTests(unittest.TestCase):
    def test_the_later_effective_date_wins_and_a_missing_or_equal_date_names_no_winner(self):
        a = {"doc_id": "SYN-D-0001", "metadata": {"effective_from": "2025-03-01"}}
        b = {"doc_id": "SYN-D-0002", "metadata": {"effective_from": "2026-09-01"}}
        self.assertEqual(tiersdat.precedence_winner(a, b)[0], "SYN-D-0002")
        self.assertEqual(tiersdat.precedence_winner(b, a)[0], "SYN-D-0002")
        self.assertIsNone(tiersdat.precedence_winner(a, {"doc_id": "SYN-D-0003", "metadata": {}})[0])
        self.assertIsNone(tiersdat.precedence_winner(a, {**b, "metadata": {"effective_from": "2025-03-01"}})[0])

    def test_srd1_spec_a08_declares_the_rule_that_the_engine_scores(self):
        spec = " ".join((REPO / "targets" / "srd1" / "SPEC.md").read_text(encoding="utf-8").split())
        a08 = next(line for line in spec.split("| A-0") if line.startswith("8 |"))
        self.assertIn(tiersdat.A08_RULE, a08)


class BenchTests(Case):
    def test_tier_s_takes_the_dat_sets_and_a_bench_of_one_cell_writes_a_report_that_verifies(self):
        proc = {"procedure": tiersdat.DAT007, "tier": "S",
                "settings": {"items_file": str(self.items), "items_sha256": self.sha}}
        raw = {"kind": "bench", "mode": "evidence", "cap_usd": 5.0, "api_key_env": KEY_ENV, "max_tokens": 200,
               "temperature": 0.7, "seed": 1000,
               "models": [{"model_id": MODEL, "price_in_per_m": 1.0, "price_out_per_m": 2.0,
                           "routing": dict(EVIDENCE_RAW["routing"])}], "procedures": [proc]}
        self.assertEqual(bench.form_problems(raw), [])
        cells, problems = bench.plan(raw, self.dir / "plan")
        self.assertEqual((problems, [c.settings.kind for c in cells]), ([], ["tiersdat"]))
        att = self.dir / "attestation.txt"
        att.write_text("Test system, synthetic data only.\n", encoding="utf-8")
        doc_ = yaml.safe_load((REPO / "targets" / "srd1" / "profile.yaml").read_text(encoding="utf-8"))
        doc_["attestation_file"] = str(att)
        profile = self.dir / "profile.yaml"
        profile.write_text(yaml.safe_dump(doc_), encoding="utf-8")
        path = self.dir / "bench.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        out = self.dir / "bench_out"
        argv = ["--bench", str(path), "--profile", str(profile), "--out", str(out), "--start", "--sign-key",
                str(self.priv), "--selftest-log", str(_KEYS["selftest"]), "--selftest-pubkey",
                str(_KEYS["selftest_pub"])]
        with contextlib.ExitStack() as stack:
            host = stack.enter_context(StandInModelHost(first_passage, with_messages=True))

            def gateway_factory(s):
                return Gateway(s, EgressClient([host.host_entry]), env=ENV, base_url=host.base_url)

            def executor(**kw):
                return preflight._default_executor(base_url=host.base_url, env=ENV, engine=ENGINE, **kw)
            buf = io.StringIO()
            stack.enter_context(no_outside_network())
            stack.enter_context(mock.patch.dict(os.environ, ENV))
            stack.enter_context(contextlib.redirect_stdout(buf))
            rc = bench.main(argv, input_fn=lambda p: "yes", gateway_factory=gateway_factory, executor=executor,
                            inspect_check=lambda: None)
        self.assertEqual(rc, 0, buf.getvalue()[-2000:])
        report = out / benchreport.REPORT_NAME
        ok, msg = benchreport.verify(report, pubkey_path=self.pub)
        self.assertTrue(ok, msg)
        row = next(r for r in runlog.read_log(report) if r["type"] == "row")
        self.assertEqual((row["procedure"], row["tier"], row["item_set"]), (tiersdat.DAT007, "S", tiersdat.ITEM_SET))
        self.assertEqual(row["sub_rows"][0]["label"], tiersdat.ADAPTATION)
        self.assertIn(benchreport.UNCALIBRATED, row["labels"])


if __name__ == "__main__":
    unittest.main()
