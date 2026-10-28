"""TP-DAT-009 parts on SRD-1. The tests cover the synthetic document store (H1) and C-05 minimal retrieval (H2).
They also cover the document rule of C-07 and the harness's own matrix (H3).

No model call. The one test that reads the configuration over HTTP runs SRD-1's chat channel on 127.0.0.1 under
`no_outside_network`.
"""
import copy
import hashlib
import importlib.util
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from faias_harness.adapters.srd1_http import Srd1ChatTarget
from faias_harness.egress import EgressClient
from faias_harness.engine import dat009, sec001, sec006
from targets.srd1.app import retrieval as rt
from targets.srd1.app.entitlements import DATA_FILE, DOCUMENT_RULE, Entitlements
from targets.srd1.app.orchestrator import Orchestrator
from targets.srd1.app.server import Srd1Server
from targets.srd1.app.trace import TraceStore
from tests.helpers import no_outside_network

REPO = Path(__file__).resolve().parents[1]
DOCS_FILE = REPO / "targets" / "srd1" / "data" / "synthetic_documents.yaml"
STORE = yaml.safe_load(DOCS_FILE.read_text(encoding="utf-8"))
DOCS = STORE["documents"]
BANK = yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))
ITEMS = dat009.load_set()["items"]


def sweep_module():
    spec = importlib.util.spec_from_file_location("sweep_barred_for_test", REPO / "tools" / "sweep_barred.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def query(item):
    return " ".join(item["turns"])


class StoreTests(unittest.TestCase):
    """H1."""

    def test_counts_domains_kinds_and_customers(self):
        self.assertEqual(len(DOCS), 16)
        self.assertEqual(sorted(d["doc_id"] for d in DOCS), [f"SYN-D-{n:04d}" for n in range(1, 17)])
        self.assertEqual(sum(d["domain"] == "retail" for d in DOCS), 8)
        self.assertEqual(sum(d["domain"] == "wealth" for d in DOCS), 8)
        self.assertEqual(sum(d["kind"] == "content" for d in DOCS), 8)
        self.assertEqual(sum(d["kind"] == "customer_record" for d in DOCS), 8)
        domains = {c["customer_ref"]: c["domain"] for c in BANK["customers"]}
        for d in DOCS:
            self.assertEqual(d["kind"] == "customer_record", d["customer_ref"] is not None, d["doc_id"])
            if d["customer_ref"] is not None:
                self.assertIn(d["customer_ref"], domains)
                self.assertEqual(d["domain"], domains[d["customer_ref"]], d["doc_id"])
        self.assertEqual(STORE["customers"], BANK["customers"])      # the store's own table agrees with the bank
        for ref, domain in domains.items():
            self.assertEqual(sum(d["customer_ref"] == ref for d in DOCS), 2)
            self.assertEqual(sum(d["customer_ref"] is None and d["domain"] == domain for d in DOCS), 4)

    def test_canaries(self):
        canaries = [d["canary"] for d in DOCS]
        self.assertEqual(len(set(canaries)), 16)
        for d in DOCS:
            self.assertRegex(d["canary"], r"^SYN-CANARY-D\d{4}-[A-Z]{4}$")
            self.assertEqual(d["canary"][12:16], d["doc_id"][-4:])
            self.assertIn(f"Document code {d['canary']}.", d["text"])
            for other in DOCS:
                if other is not d:
                    self.assertNotIn(d["canary"], other["text"])

    def test_fact_phrases_are_in_their_own_text_only(self):
        for d in DOCS:
            self.assertEqual(len(d["facts"]), 2, d["doc_id"])
            for f in d["facts"]:
                self.assertIn(sec001._alnum(f), sec001._alnum(d["text"]), (d["doc_id"], f))
                for other in DOCS:
                    if other is not d:
                        self.assertNotIn(sec001._alnum(f), sec001._alnum(other["title"] + " " + other["text"]),
                                         (d["doc_id"], f, other["doc_id"]))

    def test_references_and_fees_agree_with_the_bank(self):
        fees = {f["fee_id"]: (a["customer_ref"], f["amount_gbp"]) for a in BANK["accounts"] for f in a["fees"]}
        accounts = {a["account_ref"]: a["customer_ref"] for a in BANK["accounts"]}
        seen = set()
        for d in DOCS:
            for ref in re.findall(r"\b[A-Z]+(?:-[A-Z0-9]+)*-\d{4}(?:-[A-Z]+)?\b", d["text"] + " " + d["title"]):
                self.assertTrue(ref.startswith("SYN-"), (d["doc_id"], ref))
            for amount, fee in re.findall(r"(\d+\.\d\d) GBP \(fee (SYN-F-\d{4})\)", d["text"]):
                self.assertEqual(fees[fee], (d["customer_ref"], float(amount)), (d["doc_id"], fee))
                seen.add(fee)
            for acc in re.findall(r"SYN-A-\d{4}", d["text"]):
                self.assertEqual(accounts[acc], d["customer_ref"], (d["doc_id"], acc))
        self.assertEqual(seen, set(fees))           # every fee of the bank is in one fee history

    def test_no_key_placeholder_canary_or_barred_term(self):
        sweep = sweep_module()
        raw = DOCS_FILE.read_text(encoding="utf-8")
        for n, line in enumerate(raw.splitlines(), 1):
            self.assertIsNone(sweep.BARRED_RX.search(line), n)
            self.assertFalse(any(rx.search(line) for rx in sweep.KEY_PATTERNS), n)
        self.assertNotIn(".invalid", raw.lower())
        self.assertNotIn("placeholder", raw.lower())
        self.assertEqual(sec006.find_placeholders([raw]), [])
        self.assertNotIn(sec001._alnum(sec001.CANARY), sec001._alnum(raw))


class RetrievalTests(unittest.TestCase):
    """H2."""

    def setUp(self):
        self.store = rt.DocumentStore()
        self.ent = Entitlements()

    def r(self, stage):
        return rt.Retrieval(self.store, self.ent, stage)

    def test_ranking_is_deterministic(self):
        other = rt.DocumentStore()
        every = list(self.store.ids)
        for item in ITEMS:
            q = query(item)
            first = self.store.rank(q, every)
            self.assertEqual(first, other.rank(q, every))
            self.assertEqual(first, other.rank(q, list(reversed(every))))
            self.assertTrue(first)
        self.assertEqual(self.store.rank("zzz qqq", every), [])

    def test_pre_retrieval_never_scores_a_disallowed_document(self):
        real = rt.DocumentStore.score
        for item in ITEMS:
            allowed = self.r("pre_retrieval").allowed_ids(item["customer_ref"])
            calls = []
            with mock.patch.object(rt.DocumentStore, "score", lambda s, i, t: calls.append(i) or real(s, i, t)):
                passages, rec = self.r("pre_retrieval").retrieve(item["customer_ref"], query(item))
            self.assertTrue(calls)
            self.assertLessEqual(set(calls), allowed, item["id"])
            self.assertLessEqual(set(rec["scored"]), allowed)
            self.assertLessEqual(set(rec["passed_to_model"]), allowed)
            self.assertEqual(set(rec["filtered_out"]), set(self.store.ids) - allowed)
            self.assertEqual([p["doc_id"] for p in passages], rec["passed_to_model"])
            self.assertNotIn("display_filter", rec)

    def test_pre_ranking_scores_every_document_and_passes_allowed_only(self):
        for item in ITEMS:
            allowed = self.r("pre_ranking").allowed_ids(item["customer_ref"])
            calls = []
            real = rt.DocumentStore.score
            with mock.patch.object(rt.DocumentStore, "score", lambda s, i, t: calls.append(i) or real(s, i, t)):
                _, rec = self.r("pre_ranking").retrieve(item["customer_ref"], query(item))
            self.assertEqual(set(calls), set(self.store.ids))
            self.assertEqual(rec["scored"], list(self.store.ids))
            self.assertLessEqual(set(rec["passed_to_model"]), allowed)
            self.assertTrue(all(x["allowed"] for x in rec["retrieved"]))
            for t in item["target_doc_ids"]:
                self.assertNotIn(t, rec["passed_to_model"])
                self.assertIn(t, rec["filtered_out"])

    def test_post_generation_passes_disallowed_and_the_display_filter_removes_their_canaries(self):
        for item in ITEMS:
            r = self.r("post_generation")
            passages, rec = r.retrieve(item["customer_ref"], query(item))
            self.assertEqual(rec["filtered_out"], [])
            for t in item["target_doc_ids"]:           # every target is reachable, so it reaches the model
                self.assertIn(t, rec["passed_to_model"])
            bad = [x["doc_id"] for x in rec["retrieved"] if not x["allowed"]]
            good = [x["doc_id"] for x in rec["retrieved"] if x["allowed"]]
            self.assertEqual(rec["display_filter"], {"applies": True, "disallowed_passed": bad})
            sentences = [f"See document code {self.store.by_id[d]['canary']}." for d in bad]
            sentences += [f"Also syn canary {self.store.by_id[d]['canary'][11:].replace('-', ' ').lower()} applies!"
                          for d in bad]
            sentences += [f"Your own code is {self.store.by_id[d]['canary']}." for d in good]
            sentences.append("I hope this helps.")
            reply = " ".join(sentences)
            sent, frec = r.display_filter(reply, rec)
            self.assertEqual(frec["removed"], 2 * len(bad), item["id"])
            self.assertEqual(frec["doc_ids"], sorted(bad))
            for d in bad:
                self.assertIsNone(rt.canary_pattern(self.store.by_id[d]["canary"]).search(sent))
            for d in good:
                self.assertIn(self.store.by_id[d]["canary"], sent)
            self.assertTrue(sent.endswith("I hope this helps."))
        for stage in ("pre_retrieval", "pre_ranking"):
            self.assertEqual(self.r(stage).display_filter("Text.", {"retrieved": []}),
                             ("Text.", {"applies": False, "removed": 0, "doc_ids": []}))

    def test_config_names_the_stage_module_function_call_site_and_hashes(self):
        for stage in rt.STAGES:
            cfg = rt.Retrieval(self.store, self.ent, stage, mode="tool", credential="service").config()
            self.assertEqual(cfg["stage"], stage)
            self.assertEqual(cfg["module"], "targets/srd1/app/retrieval.py")
            self.assertEqual(cfg["function"], "Retrieval.retrieve")
            self.assertEqual(cfg["filter_function"], "Entitlements.allowed_document")
            self.assertEqual(cfg["call_site"], "Orchestrator.handle")
            self.assertEqual(cfg["display_filter_function"],
                             "Retrieval.display_filter" if stage == "post_generation" else None)
            self.assertEqual(cfg["module_sha256"],
                             hashlib.sha256((REPO / "targets/srd1/app/retrieval.py").read_bytes()).hexdigest())
            self.assertEqual(cfg["documents_sha256"], hashlib.sha256(DOCS_FILE.read_bytes()).hexdigest())
            self.assertEqual(cfg["documents_source"], "targets/srd1/data/synthetic_documents.yaml")
            self.assertEqual(cfg["top_k"], 3)
            self.assertEqual((cfg["inventory"]["count"], cfg["inventory"]["by_domain"], cfg["inventory"]["by_kind"]),
                             (16, {"retail": 8, "wealth": 8}, {"content": 8, "customer_record": 8}))
            self.assertNotIn("text", json.dumps(cfg["inventory"]))
            self.assertEqual(cfg["tool_path"], {"tool": "document_search", "credential": "service",
                                                "filter": "at the emulated store, by the presented principal"})
        self.assertIsNone(self.r("pre_retrieval").config()["tool_path"]["tool"])
        with self.assertRaises(ValueError):
            rt.Retrieval(self.store, self.ent, "display_layer")

    def test_the_context_template_hash_is_stable(self):
        want = hashlib.sha256(json.dumps([rt.CONTEXT_TEMPLATE, rt.PASSAGE_TEMPLATE], ensure_ascii=False,
                                         separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(rt.template_sha256(), want)
        self.assertEqual(self.r("pre_retrieval").config()["context_template_sha256"],
                         rt.Retrieval(rt.DocumentStore(), Entitlements(), "pre_ranking").config()["context_template_sha256"])
        passages, _ = self.r("pre_retrieval").retrieve("SYN-C-0003", "platform charge schedule")
        msg = rt.context_message(passages)
        self.assertTrue(msg.startswith("Reference documents for this question."))
        self.assertIn("[SYN-D-0005] Platform charge schedule\n", msg)
        self.assertIsNone(rt.context_message([]))

    def test_the_store_file_is_not_written(self):
        before = DOCS_FILE.read_bytes()
        r = self.r("post_generation")
        for item in ITEMS:
            r.retrieve(item["customer_ref"], query(item))
        self.assertEqual(DOCS_FILE.read_bytes(), before)


class MatrixTests(unittest.TestCase):
    """H3."""

    def test_the_matrix_equals_the_rule_document_by_document(self):
        ent = Entitlements()
        matrix = dat009.expected_matrix(BANK, DOCS)
        self.assertEqual(sorted(matrix["customers"]), ["SYN-C-0001", "SYN-C-0002", "SYN-C-0003", "SYN-C-0004"])
        pairs = 0
        for c in matrix["customers"]:
            allowed = dat009.allowed_ids(matrix, c)
            self.assertEqual(len(allowed), 6)
            self.assertEqual(set(matrix["customers"][c]["disallowed"]), {d["doc_id"] for d in DOCS} - allowed)
            for d in DOCS:
                self.assertEqual(ent.allowed_document(c, d), d["doc_id"] in allowed, (c, d["doc_id"]))
                pairs += 1
        self.assertEqual(pairs, 64)
        self.assertFalse(ent.allowed_document("SYN-C-9999", DOCS[0]))
        self.assertEqual(matrix["customers"]["SYN-C-0001"]["accounts"], ["SYN-A-0001", "SYN-A-0002"])

    def test_a_seeded_domain_edit_changes_both_the_same_way(self):
        ent = Entitlements()
        edited = copy.deepcopy(DOCS)
        target = next(d for d in edited if d["doc_id"] == "SYN-D-0001")
        target["domain"] = "wealth"

        def table(docs):
            m = dat009.expected_matrix(BANK, docs)
            return ({(c, d["doc_id"]) for c in m["customers"] for d in docs if d["doc_id"] in dat009.allowed_ids(m, c)},
                    {(c, d["doc_id"]) for c in m["customers"] for d in docs if ent.allowed_document(c, d)})
        m_before, e_before = table(DOCS)
        m_after, e_after = table(edited)
        self.assertEqual(m_before, e_before)
        self.assertEqual(m_after, e_after)
        changed = m_before ^ m_after
        self.assertEqual(changed, {(c, "SYN-D-0001") for c in ("SYN-C-0001", "SYN-C-0002", "SYN-C-0003", "SYN-C-0004")})
        self.assertNotEqual(dat009.matrix_sha256(dat009.expected_matrix(BANK, DOCS)),
                            dat009.matrix_sha256(dat009.expected_matrix(BANK, edited)))

    def test_the_default_config_is_unchanged_and_the_store_adds_the_rule(self):
        base = Entitlements().config()
        self.assertEqual(base["stage"], "before the tool call; before retrieval when C-05 exists")
        self.assertNotIn("document_rule", base)
        self.assertEqual(base["enforcement"], {"file": "targets/srd1/app/entitlements.py",
                                               "function": "Entitlements.check"})
        with self.assertRaises(ValueError):
            Entitlements(documents_file=DOCS_FILE)

    def test_read_config_entitlements_reports_the_rule_and_the_store_hash(self):
        with tempfile.TemporaryDirectory() as t, no_outside_network():
            trace = TraceStore(Path(t) / "trace.jsonl")
            ent = Entitlements(documents_file=DOCS_FILE, retrieval_stage="pre_retrieval")
            orch = Orchestrator("You are a test assistant.", None, trace, entitlements=ent)
            server = Srd1Server(orch, trace).start()
            egress = EgressClient([f"127.0.0.1:{server.port}"])
            try:
                cfg = Srd1ChatTarget(server.url, egress).read_config("entitlements")
            finally:
                egress.close()
                server.stop()
        self.assertEqual(cfg["document_rule"], DOCUMENT_RULE)
        self.assertEqual(cfg["documents_sha256"], hashlib.sha256(DOCS_FILE.read_bytes()).hexdigest())
        self.assertEqual(cfg["documents_source"], "targets/srd1/data/synthetic_documents.yaml")
        self.assertEqual(cfg["sha256"], hashlib.sha256(DATA_FILE.read_bytes()).hexdigest())
        self.assertEqual(cfg["stage"], ["before each tool call", "before retrieval (pipeline path): pre_retrieval",
                                        "at the emulated store by the presented principal (tool path)"])
        self.assertEqual(cfg["enforcement"]["document_function"], "Entitlements.allowed_document")


if __name__ == "__main__":
    unittest.main()
