"""The MCP tool emulator: both protocol eras, header checks, Origin and key checks, and the ledger chain.

A small client in this file speaks to the emulator on 127.0.0.1. No test contacts a host outside the local machine.
A pass shows that the emulator follows the rules tested here. It does not show that any platform's MCP client works
with it.
"""
import base64
import json
import tempfile
import unittest
from pathlib import Path

import httpx

from faias_harness.emulators import mcp_tools as mt
from tests.helpers import FAKE_KEY_VALUE, no_outside_network

REPO = Path(__file__).resolve().parents[1]
TOOLS = mt.load_tools(REPO / "targets" / "examples" / "mcp_tools.yaml")
CLIENT_INFO = {"name": "faias-test-client", "version": "0"}


def modern_meta(version="2026-07-28"):
    return {mt.META_VERSION: version, "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": {}}


class EmulatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.http = httpx.Client(trust_env=False, timeout=10)
        self.guard = no_outside_network()
        self.guard.__enter__()

    def tearDown(self):
        self.guard.__exit__(None, None, None)
        self.http.close()
        self.tmp.cleanup()

    def emu(self, name="ledger.jsonl", **kw):
        e = mt.ToolEmulator(TOOLS, self.dir / name, **kw).start()
        self.addCleanup(e.stop)
        return e

    def post(self, e, body, headers=None):
        return self.http.post(e.url, json=body, headers={"Accept": "application/json, text/event-stream", **(headers or {})})

    def modern(self, e, method, params=None, mid=1, version="2026-07-28", **hdr):
        params = {**(params or {}), "_meta": modern_meta(version)}
        h = {"MCP-Protocol-Version": version, "Mcp-Method": method}
        if method == "tools/call":
            h["Mcp-Name"] = params.get("name", "")
        h.update(hdr)
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if mid is not None:
            body["id"] = mid
        return self.post(e, body, h)

    def test_legacy_session_flow(self):
        e = self.emu()
        r = self.post(e, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": CLIENT_INFO}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["result"]["protocolVersion"], "2025-06-18")
        sid = r.headers["Mcp-Session-Id"]
        h = {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-06-18"}
        self.assertEqual(self.post(e, {"jsonrpc": "2.0", "method": "notifications/initialized"}, h).status_code, 202)
        tools = self.post(e, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, h).json()["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["get_account_summary", "request_fee_refund", "update_contact_address"])
        self.assertEqual(tools[0]["inputSchema"]["type"], "object")
        res = self.post(e, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "request_fee_refund", "arguments": {"account_ref": "TEST-0001", "fee_id": "F1"}}}, h).json()["result"]
        self.assertFalse(res["isError"])
        self.assertIn("REF-TEST-0001", res["content"][0]["text"])
        err = self.post(e, {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "update_contact_address", "arguments": {"account_ref": "TEST-0001", "address": "1 Test Street"}}}, h).json()
        self.assertTrue(err["result"]["isError"])
        unknown = self.post(e, {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}, h)
        self.assertEqual(unknown.json()["error"]["code"], mt.INVALID_PARAMS)
        self.assertEqual(self.post(e, {"jsonrpc": "2.0", "id": 6, "method": "tools/list"}).status_code, 400)
        self.assertEqual(self.post(e, {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}, {"Mcp-Session-Id": "x"}).status_code, 404)
        self.assertEqual(self.http.delete(e.url, headers={"Mcp-Session-Id": sid}).status_code, 200)
        self.assertEqual(self.post(e, {"jsonrpc": "2.0", "id": 8, "method": "tools/list"}, h).status_code, 404)
        entries = mt.read_ledger(e.ledger.path)
        self.assertEqual([x["tool"] for x in entries], ["request_fee_refund", "update_contact_address"])
        self.assertEqual(entries[0]["arguments"], {"account_ref": "TEST-0001", "fee_id": "F1"})
        self.assertEqual((entries[0]["era"], entries[0]["protocol_version"]), ("legacy", "2025-06-18"))
        self.assertEqual(mt.read_tool_ledger(e.ledger.path, tool="update_contact_address")[0]["is_error"], True)

    def test_initialize_with_an_unknown_version_gets_the_newest_legacy(self):
        e = self.emu()
        r = self.post(e, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}})
        self.assertEqual(r.json()["result"]["protocolVersion"], "2025-11-25")

    def test_modern_requests_and_header_rules(self):
        e = self.emu()
        d = self.modern(e, "server/discover").json()["result"]
        self.assertEqual(d["supportedVersions"], ["2026-07-28"])
        self.assertIn(mt.META_SERVER, d["_meta"])
        self.assertEqual(len(self.modern(e, "tools/list").json()["result"]["tools"]), 3)
        ok = self.modern(e, "tools/call", {"name": "get_account_summary", "arguments": {"account_ref": "TEST-0001"}})
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json()["result"]["resultType"], "complete")
        b64 = "=?base64?" + base64.b64encode(b"get_account_summary").decode() + "?="
        self.assertEqual(self.modern(e, "tools/call", {"name": "get_account_summary", "arguments": {}},
                                     **{"Mcp-Name": b64}).status_code, 200)
        cases = [
            (self.modern(e, "tools/list", **{"MCP-Protocol-Version": "2025-06-18"}), 400, mt.HEADER_MISMATCH),
            (self.modern(e, "tools/list", **{"Mcp-Method": "tools/call"}), 400, mt.HEADER_MISMATCH),
            (self.modern(e, "tools/call", {"name": "get_account_summary", "arguments": {}}, **{"Mcp-Name": "other"}), 400, mt.HEADER_MISMATCH),
            (self.modern(e, "tools/list", version="2027-01-01"), 400, mt.UNSUPPORTED_VERSION),
            (self.modern(e, "resources/list"), 404, mt.METHOD_NOT_FOUND),
        ]
        for r, status, code in cases:
            with self.subTest(status=status, code=code):
                self.assertEqual(r.status_code, status)
                self.assertEqual(r.json()["error"]["code"], code)
        unsupported = self.modern(e, "tools/list", version="2027-01-01").json()["error"]["data"]
        self.assertIn("2025-11-25", unsupported["supported"])
        self.assertEqual(self.modern(e, "tools/list", mid=None).status_code, 202)
        self.assertEqual(self.http.get(e.url).status_code, 405)
        self.assertEqual([x["era"] for x in mt.read_ledger(e.ledger.path)], ["modern", "modern"])

    def test_origin_and_key(self):
        env = {"EMU_KEY": FAKE_KEY_VALUE}
        e = self.emu(api_key_env="EMU_KEY", env=env, allowed_origins=["https://staging.example.test"])
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}}
        self.assertEqual(self.post(e, init).status_code, 401)
        self.assertEqual(self.post(e, init, {"X-Api-Key": "wrong"}).status_code, 401)
        self.assertEqual(self.post(e, init, {"X-Api-Key": FAKE_KEY_VALUE, "Origin": "https://other.test"}).status_code, 403)
        r = self.post(e, init, {"X-Api-Key": FAKE_KEY_VALUE, "Origin": "https://staging.example.test"})
        self.assertEqual(r.status_code, 200)
        sid = r.headers["Mcp-Session-Id"]
        self.post(e, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "get_account_summary",
                      "arguments": {"account_ref": "TEST-0001"}}}, {"X-Api-Key": FAKE_KEY_VALUE, "Mcp-Session-Id": sid})
        self.assertNotIn(FAKE_KEY_VALUE, e.ledger.path.read_text(encoding="utf-8"))
        with self.assertRaises(ValueError):
            mt.ToolEmulator(TOOLS, self.dir / "x.jsonl", api_key_env="UNSET_VARIABLE", env={})

    def test_ledger_chain_finds_edits_removals_and_reorders(self):
        e = self.emu()
        sid = self.post(e, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                            "params": {"protocolVersion": "2025-11-25"}}).headers["Mcp-Session-Id"]
        for i in range(3):
            self.post(e, {"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call", "params": {
                "name": "get_account_summary", "arguments": {"account_ref": f"TEST-000{i}"}}}, {"Mcp-Session-Id": sid})
        path, head = e.ledger.path, e.ledger.head
        self.assertEqual(mt.verify_ledger(path, expected_head=head), (True, []))
        lines = path.read_text(encoding="utf-8").splitlines()
        variants = {
            "edited": [lines[0], lines[1].replace("TEST-0001", "TEST-9999"), lines[2]],
            "removed": [lines[0], lines[2]],
            "reordered": [lines[1], lines[0], lines[2]],
            "last removed": lines[:2],
        }
        for name, ls in variants.items():
            with self.subTest(name=name):
                p = self.dir / f"{name}.jsonl"
                p.write_text("\n".join(ls) + "\n", encoding="utf-8")
                ok, problems = mt.verify_ledger(p, expected_head=head)
                self.assertFalse(ok)
                self.assertTrue(problems)
        with self.assertRaises(FileExistsError):
            mt.Ledger(path)

    def test_local_only_and_tools_file_rules(self):
        with self.assertRaises(ValueError):
            mt.ToolEmulator(TOOLS, self.dir / "y.jsonl", host="0.0.0.0")
        self.assertTrue(mt.validate_tools({"tools": [{"name": "bad name", "description": "", "input_schema": {},
                                                      "result": {"text": 1}}]}))
        self.assertTrue(mt.validate_tools({"tools": []}))
        self.assertEqual(mt.validate_tools(json.loads(json.dumps({"tools": TOOLS["tools"]}))), [])


if __name__ == "__main__":
    unittest.main()
