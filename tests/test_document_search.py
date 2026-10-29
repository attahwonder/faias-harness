"""Tests of the document_search handler of the MCP tool emulator for TP-DAT-009, and of its tools file.

A small client in this file speaks to the emulator on 127.0.0.1 under `no_outside_network`. No model call.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx
import yaml

from faias_harness.emulators import handlers
from faias_harness.emulators import mcp_tools as mt
from faias_harness.engine import dat009, sec006
from targets.srd1.app import retrieval as rt
from targets.srd1.app.entitlements import Entitlements
from tests.helpers import no_outside_network

REPO = Path(__file__).resolve().parents[1]
TOOLS_FILE = REPO / "targets" / "srd1" / "tools_retrieval.yaml"
DOCS_FILE = REPO / "targets" / "srd1" / "data" / "synthetic_documents.yaml"
BANK, DOCS, _, _ = dat009.load_data()
MATRIX = dat009.expected_matrix(BANK, DOCS)
ITEMS = dat009.load_set()["items"]
CUSTOMERS = sorted(MATRIX["customers"])
META = {mt.META_VERSION: "2026-07-28", "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "0"},
        "io.modelcontextprotocol/clientCapabilities": {}}


def query(item):
    return " ".join(item["turns"])


class DocumentSearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.guard = no_outside_network()
        self.guard.__enter__()
        self.http = httpx.Client(trust_env=False, timeout=10)
        self.emu = mt.ToolEmulator(mt.load_tools(TOOLS_FILE), self.dir / "ledger.jsonl").start()
        self.mid = 0

    def tearDown(self):
        self.emu.stop()
        self.http.close()
        self.guard.__exit__(None, None, None)
        self.tmp.cleanup()

    def rpc(self, method, params=None, name=None):
        self.mid += 1
        h = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": method, "Accept": "application/json"}
        if name:
            h["Mcp-Name"] = name
        body = {"jsonrpc": "2.0", "id": self.mid, "method": method, "params": {**(params or {}), "_meta": META}}
        r = self.http.post(self.emu.url, json=body, headers=h)
        self.assertEqual(r.status_code, 200)
        return r.json()["result"]

    def search(self, args):
        return self.rpc("tools/call", {"name": "document_search", "arguments": args}, name="document_search")

    def test_the_tools_file_and_its_annotations(self):
        text = TOOLS_FILE.read_text(encoding="utf-8")
        raw = yaml.safe_load(text)
        self.assertEqual(mt.validate_tools(raw), [])
        self.assertEqual(sec006.find_placeholders([text]), [])
        self.assertNotIn(".invalid", text.lower())
        tools = self.rpc("tools/list")["tools"]
        self.assertEqual([t["name"] for t in tools], ["document_search"])
        t = tools[0]
        self.assertTrue(t["annotations"]["readOnlyHint"])
        self.assertFalse(t["annotations"]["destructiveHint"])
        self.assertTrue(t["annotations"]["idempotentHint"])
        self.assertFalse(t["annotations"]["openWorldHint"])
        self.assertEqual(t["inputSchema"]["required"], ["query"])
        self.assertEqual(set(t["inputSchema"]["properties"]), {"query", "on_behalf_of"})
        self.assertEqual(t["outputSchema"], handlers.DocumentSearch.OUTPUT_SCHEMA)
        self.assertEqual(self.emu.store_path.resolve(), DOCS_FILE.resolve())

    def test_on_behalf_of_a_customer_returns_that_customer_s_documents_only(self):
        for item in ITEMS:
            for c in CUSTOMERS:
                res = self.search({"query": query(item), "on_behalf_of": c})
                sc = res["structuredContent"]
                self.assertFalse(res["isError"])
                self.assertEqual((sc["status"], sc["principal"]), ("ok", c))
                ids = [d["doc_id"] for d in sc["documents"]]
                self.assertTrue(ids)
                self.assertLessEqual(set(ids), dat009.allowed_ids(MATRIX, c), (item["id"], c))
        self.assertTrue(mt.verify_ledger(self.emu.ledger.path)[0])

    def test_without_on_behalf_of_every_match_returns_under_the_service_credential(self):
        searcher = handlers.DocumentSearch(yaml.safe_load(DOCS_FILE.read_text(encoding="utf-8")))
        crossings = 0
        for item in ITEMS:
            sc = self.search({"query": query(item)})["structuredContent"]
            self.assertEqual(sc["principal"], "service")
            ids = [d["doc_id"] for d in sc["documents"]]
            self.assertEqual(ids, [i for i, _ in searcher.rank(query(item), list(searcher.documents))[:3]])
            for t in item["target_doc_ids"]:
                self.assertIn(t, ids)                   # the service credential reaches every target
            crossings += bool(set(ids) - dat009.allowed_ids(MATRIX, item["customer_ref"]))
        self.assertEqual(crossings, len(ITEMS))

    def test_the_ledger_line_holds_the_principal_and_the_returned_doc_ids(self):
        """The ledger line of a document_search call names the principal that the call presented and the documents
        of the result. A rejected call names the principal too, with no documents."""
        a = self.search({"query": "platform charge schedule"})["structuredContent"]
        b = self.search({"query": "platform charge schedule", "on_behalf_of": "SYN-C-0001"})["structuredContent"]
        self.search({"query": "  ", "on_behalf_of": "SYN-C-0003"})
        lines = mt.read_ledger(self.emu.ledger.path, tool="document_search")
        self.assertEqual([(x["status"], x["is_error"], x["refused"]) for x in lines],
                         [("ok", False, None)] * 2 + [("rejected", True, None)])
        self.assertEqual([x["principal"] for x in lines], ["service", "SYN-C-0001", "SYN-C-0003"])
        self.assertEqual([x["returned_doc_ids"] for x in lines],
                         [[d["doc_id"] for d in a["documents"]], [d["doc_id"] for d in b["documents"]], []])
        self.assertTrue(set(lines[0]["returned_doc_ids"]) - dat009.allowed_ids(MATRIX, "SYN-C-0001"))
        self.assertLessEqual(set(lines[1]["returned_doc_ids"]), dat009.allowed_ids(MATRIX, "SYN-C-0001"))
        self.assertTrue(mt.verify_ledger(self.emu.ledger.path)[0])

    def test_a_fee_refund_ledger_line_has_no_document_fields(self):
        """The two fields come from the handler of document_search only, so a fee_refund line is as before."""
        emu = mt.ToolEmulator(mt.load_tools(REPO / "targets" / "srd1" / "tools.yaml"), self.dir / "refund.jsonl").start()
        self.addCleanup(emu.stop)
        h = {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "fee_refund"}
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "fee_refund", "_meta": META, "arguments": {"account_ref": "SYN-A-0001",
                                                                           "fee_id": "SYN-F-0001", "amount_gbp": 12.0}}}
        self.assertEqual(self.http.post(emu.url, json=body, headers=h).status_code, 200)
        line = mt.read_ledger(emu.ledger.path)[0]
        self.assertEqual(set(line) - {"seq", "t_ns", "time", "prev", "hash"},
                         {"era", "protocol_version", "session", "tool", "arguments", "is_error", "refused", "status",
                          "refund_ref"})

    def test_the_handler_ranks_as_retrieval_py_on_every_item_query(self):
        store, ent = rt.DocumentStore(), Entitlements()
        searcher = handlers.DocumentSearch(yaml.safe_load(DOCS_FILE.read_text(encoding="utf-8")))
        for item in ITEMS:
            q = query(item)
            self.assertEqual(searcher.rank(q, list(searcher.documents)), store.rank(q, list(store.ids)), item["id"])
            got = [d["doc_id"] for d in searcher.call({"query": q, "on_behalf_of": item["customer_ref"]})[0]["documents"]]
            _, rec = rt.Retrieval(store, ent, "pre_retrieval").retrieve(item["customer_ref"], q)
            self.assertEqual(got, rec["passed_to_model"], item["id"])

    def test_rejected_calls(self):
        for args, reason in (({"query": "  "}, "query must be a non-empty string"), ({}, "query must be a non-empty string"),
                             ({"query": "fees", "on_behalf_of": "SYN-C-9999"}, "unknown principal"),
                             ({"query": "fees", "on_behalf_of": "service"}, "unknown principal"),
                             ({"query": "fees", "on_behalf_of": 7}, "unknown principal")):
            res = self.search(args)
            self.assertTrue(res["isError"])
            self.assertEqual(res["structuredContent"], {"status": "rejected", "reason": reason})
        with self.assertRaises(handlers.StoreError):
            handlers.DocumentSearch({"accounts": []})
        with self.assertRaises(handlers.StoreError):
            handlers.DocumentSearch({"documents": DOCS})
        _, digest = handlers.load_store(DOCS_FILE)
        self.assertEqual(digest, hashlib.sha256(DOCS_FILE.read_bytes()).hexdigest())

    def test_the_store_file_is_unchanged_after_ten_calls(self):
        before = hashlib.sha256(DOCS_FILE.read_bytes()).hexdigest()
        self.assertEqual(self.emu.store_sha256, before)
        for n, item in enumerate(ITEMS[:10]):
            self.search({"query": query(item), **({"on_behalf_of": item["customer_ref"]} if n % 2 else {})})
        self.assertEqual(self.emu.ledger.seq, 10)
        self.assertEqual(hashlib.sha256(DOCS_FILE.read_bytes()).hexdigest(), before)

    def test_tools_yaml_still_serves_fee_refund_only(self):
        """The build report checks the byte identity of tools.yaml with git. This test checks its content."""
        tools = mt.load_tools(REPO / "targets" / "srd1" / "tools.yaml")
        self.assertEqual([t["name"] for t in tools["tools"]], ["fee_refund"])
        self.assertEqual(tools["store"].name, "synthetic_bank.yaml")


if __name__ == "__main__":
    unittest.main()
