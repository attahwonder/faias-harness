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


class _Base(unittest.TestCase):
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



class EmulatorTests(_Base):
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
        # the refused call to an unknown tool is in the ledger too (since the review of 22 October 2026)
        self.assertEqual([(x["tool"], x["refused"]) for x in entries],
                         [("request_fee_refund", None), ("update_contact_address", None), ("nope", "unknown tool")])
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


class ReviewFixTests(_Base):
    """Fixes from the review of 22 October 2026: refused calls in the ledger, the client capabilities, the legacy
    header, the cache fields, and refusal without parsing the body."""

    def test_refused_calls_are_in_the_ledger(self):
        e = self.emu(name="refused.jsonl")
        self.modern(e, "tools/call", {"name": "transfer_funds", "arguments": {"to": "X"}})
        self.modern(e, "tools/call", {"name": "get_account_summary", "arguments": ["not", "an", "object"]})
        entries = mt.read_ledger(e.ledger.path)
        self.assertEqual([(x["tool"], x["refused"]) for x in entries],
                         [("transfer_funds", "unknown tool"), ("get_account_summary", "arguments must be an object")])
        self.assertEqual(entries[0]["arguments"], {"to": "X"})
        self.assertTrue(mt.verify_ledger(e.ledger.path)[0])

    def test_client_capabilities_legacy_header_and_cache_fields(self):
        e = self.emu(name="caps.jsonl")
        meta = {mt.META_VERSION: "2026-07-28"}
        r = self.post(e, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": meta}},
                      {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"})
        self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, mt.INVALID_PARAMS))
        res = self.modern(e, "tools/list").json()["result"]
        self.assertEqual((res["ttlMs"], res["cacheScope"]), (0, "private"))
        try:
            import mcp.types as mtypes          # optional: check the shapes against the MCP SDK when it is installed
        except ImportError:
            mtypes = None
        # only an SDK that knows the 2026-07-28 types can check them; an older SDK is skipped, not failed
        if mtypes is not None and hasattr(mtypes, "DiscoverResult"):
            mtypes.ListToolsResult.model_validate(res)
            mtypes.DiscoverResult.model_validate(self.modern(e, "server/discover").json()["result"])
            call = self.modern(e, "tools/call", {"name": "get_account_summary", "arguments": {"account_ref": "TEST-0001"}})
            mtypes.CallToolResult.model_validate(call.json()["result"])
        sid = self.post(e, {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                            "params": {"protocolVersion": "2025-06-18"}}).headers["Mcp-Session-Id"]
        bad = self.post(e, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                        {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(bad.status_code, 400)
        ok = self.post(e, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}, {"Mcp-Session-Id": sid})
        self.assertEqual(ok.status_code, 200)      # a 2025-03-26 client sends no header

    def test_refusal_does_not_parse_the_body_and_closes(self):
        e = self.emu(name="key.jsonl", api_key_env="EMU_KEY", env={"EMU_KEY": FAKE_KEY_VALUE})
        r = self.post(e, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual((r.status_code, r.headers.get("Connection")), (401, "close"))
        r2 = self.post(e, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"X-Api-Key": FAKE_KEY_VALUE})
        self.assertEqual(r2.status_code, 200)

    def test_a_refused_body_is_dropped_so_the_reply_arrives(self):
        """The refusal reads and drops the declared body before the close. Without that, the close resets the
        connection, and the client can lose the reply (a rare ReadError in self-test probe S11)."""
        import socket
        import time
        e = self.emu(name="origin.jsonl")
        body = b"x" * mt.MAX_BODY
        head = (f"POST {e.path} HTTP/1.1\r\nHost: {e.host}:{e.port}\r\nOrigin: https://other.test\r\n"
                f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n").encode("ascii")
        data = b""
        with socket.create_connection((e.host, e.port), timeout=10) as s:
            s.sendall(head + body)
            time.sleep(0.3)             # a reset, if one comes, now arrives before the reply is read
            while chunk := s.recv(65536):
                data += chunk
        self.assertTrue(data.startswith(b"HTTP/1.1 403"), data[:80])
        self.assertIn(b"origin not allowed", data)

    def test_a_refused_body_sent_slowly_does_not_hold_the_connection(self):
        """A refused client sends its declared body one byte at a time. Its connection closes in about 3 seconds. The
        drop of the body stops after DROP_DEADLINE_S seconds in total. Without that limit, the handler waits for the
        whole body while each gap is shorter than the socket timeout."""
        import socket
        import time
        e = self.emu(name="slow.jsonl")
        head = (f"POST {e.path} HTTP/1.1\r\nHost: {e.host}:{e.port}\r\nOrigin: https://other.test\r\n"
                f"Content-Type: application/json\r\nContent-Length: 200000\r\n\r\n").encode("ascii")
        closed_after = None
        with socket.create_connection((e.host, e.port), timeout=10) as s:
            t0 = time.monotonic()
            s.sendall(head)
            s.settimeout(0.5)
            while time.monotonic() - t0 < 8:
                try:
                    s.sendall(b"x")             # one byte of the body each 0.5 s
                    chunk = s.recv(65536)
                except socket.timeout:
                    continue
                except OSError:                 # a reset closes the connection too
                    closed_after = time.monotonic() - t0
                    break
                if not chunk:
                    closed_after = time.monotonic() - t0
                    break
        self.assertIsNotNone(closed_after, "the connection was still open after 8 s")
        self.assertLess(closed_after, 3.5)
        self.assertEqual(mt.DROP_DEADLINE_S, 2)


STORE_TOOLS_FILE = REPO / "targets" / "examples" / "mcp_tools_store.yaml"
STORE_FILE = REPO / "targets" / "srd1" / "data" / "synthetic_bank.yaml"


class HandlerTests(_Base):
    """The handler form (0.6): a result that a built-in handler computes from a synthetic store."""

    def store_emu(self, name="store.jsonl", **kw):
        e = mt.ToolEmulator(mt.load_tools(STORE_TOOLS_FILE), self.dir / name, **kw).start()
        self.addCleanup(e.stop)
        return e

    def call(self, e, args, mid=1):
        return self.modern(e, "tools/call", {"name": "fee_refund", "arguments": args}, mid=mid).json()["result"]

    def test_the_handler_form_validates_and_an_unknown_handler_is_refused(self):
        tools = mt.load_tools(STORE_TOOLS_FILE)
        self.assertEqual(Path(tools["store"]).resolve(), STORE_FILE.resolve())
        self.assertEqual(tools["tools"][0]["result"], {"handler": "fee_refund"})
        bad = json.loads(json.dumps({"tools": tools["tools"]}))
        bad["tools"][0]["result"] = {"handler": "transfer_funds"}
        self.assertTrue(any("unknown handler" in p for p in mt.validate_tools(bad)))
        bad["tools"][0]["result"] = {"handler": "fee_refund"}
        bad["tools"][0]["annotations"] = {"destructiveHint": "yes"}
        self.assertTrue(any("annotations" in p for p in mt.validate_tools(bad)))
        with self.assertRaises(ValueError):          # a handler tool with no store does not start
            mt.ToolEmulator({**tools, "store": None}, self.dir / "none.jsonl")
        self.assertEqual(mt.validate_tools(json.loads(json.dumps({"tools": TOOLS["tools"]}))), [])   # shipped file

    def test_a_known_fee_is_recorded_and_an_unknown_fee_is_an_error(self):
        e = self.store_emu()
        ok = self.call(e, {"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0001", "amount_gbp": 12.0, "reason": "r"})
        self.assertFalse(ok["isError"])
        self.assertEqual(ok["structuredContent"]["status"], "recorded")
        self.assertEqual(ok["structuredContent"]["refund_ref"], "REF-SYN-0001")
        self.assertEqual(json.loads(ok["content"][0]["text"]), ok["structuredContent"])
        bad = self.call(e, {"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0099", "amount_gbp": 12.0}, mid=2)
        self.assertTrue(bad["isError"])
        self.assertEqual(bad["structuredContent"], {"status": "rejected", "reason": "no such fee"})
        other = self.call(e, {"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0001", "amount_gbp": 99.0}, mid=3)
        self.assertEqual(other["structuredContent"]["reason"], "the amount differs from the fee")
        odd = self.call(e, {"account_ref": ["SYN-A-0001"], "fee_id": "SYN-F-0001", "amount_gbp": 12.0}, mid=4)
        self.assertEqual(odd["structuredContent"]["reason"], "no such account")
        entries = mt.read_ledger(e.ledger.path)
        self.assertEqual([(x["status"], x["refund_ref"], x["is_error"]) for x in entries],
                         [("recorded", "REF-SYN-0001", False), ("rejected", None, True), ("rejected", None, True),
                          ("rejected", None, True)])
        self.assertTrue(mt.verify_ledger(e.ledger.path)[0])

    def test_two_calls_for_one_fee_give_two_refunds_and_the_store_is_not_written(self):
        before = STORE_FILE.read_bytes()
        e = self.store_emu()
        self.assertEqual(e.store_sha256, __import__("hashlib").sha256(before).hexdigest())
        args = {"account_ref": "SYN-A-0004", "fee_id": "SYN-F-0006", "amount_gbp": 45}
        refs = [self.call(e, args, mid=i)["structuredContent"]["refund_ref"] for i in range(1, 3)]
        self.assertEqual(refs, ["REF-SYN-0001", "REF-SYN-0002"])
        for i in range(8):
            self.call(e, args, mid=10 + i)
        self.assertEqual(len(mt.read_ledger(e.ledger.path)), 10)
        self.assertEqual(STORE_FILE.read_bytes(), before)

    def test_tools_list_carries_the_title_annotations_and_output_schema(self):
        e = self.store_emu()
        t = self.modern(e, "tools/list").json()["result"]["tools"][0]
        self.assertEqual(t["annotations"], {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False,
                                            "openWorldHint": False})
        self.assertEqual(t["title"], "Fee refund")
        self.assertEqual(t["outputSchema"]["properties"]["status"]["enum"], ["recorded", "rejected"])
        legacy = self.emu(name="fixed.jsonl")._tool_list()[0]
        self.assertEqual(set(legacy), {"name", "description", "inputSchema"})     # fixed tools: as before

    def test_the_store_option_replaces_the_store_of_the_tools_file(self):
        alt = self.dir / "alt_store.yaml"
        alt.write_text("accounts:\n  - {account_ref: SYN-A-0101, customer_ref: SYN-C-0101, fees: "
                       "[{fee_id: SYN-F-0101, amount_gbp: 5}]}\n", encoding="utf-8")
        e = self.store_emu(store=alt)
        self.assertEqual(self.call(e, {"account_ref": "SYN-A-0101", "fee_id": "SYN-F-0101", "amount_gbp": 5})
                         ["structuredContent"]["status"], "recorded")
        with self.assertRaises(ValueError):
            mt.ToolEmulator(mt.load_tools(STORE_TOOLS_FILE), self.dir / "missing.jsonl", store=self.dir / "nope.yaml")

    def raw_call(self, e, amount_text, mid=1):
        """A tools/call whose amount is the JSON text given, as a client may send it (CHECK_060_P5 F1 and F2)."""
        params = {"name": "fee_refund", "arguments": {"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0001",
                                                      "amount_gbp": "AMOUNT", "reason": "r"}, "_meta": modern_meta()}
        body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": params})
        return self.http.post(e.url, content=body.replace('"AMOUNT"', amount_text).encode("utf-8"), headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "fee_refund"})

    def test_nan_infinity_and_a_number_out_of_range_are_not_json_and_not_recorded(self):
        e = self.store_emu()
        for text in ("NaN", "Infinity", "-Infinity", "1e400"):
            with self.subTest(amount=text):
                r = self.raw_call(e, text)
                self.assertEqual((r.status_code, r.json()["error"]["code"]), (400, mt.PARSE_ERROR))
        self.assertEqual(mt.read_ledger(e.ledger.path), [])
        out = []
        for i, text in enumerate(("-12.0", "0", "1" + "0" * 400, "12.0"), 1):
            out.append(self.raw_call(e, text, mid=i).json()["result"]["structuredContent"])
        self.assertEqual([(x["status"], x.get("reason")) for x in out],
                         [("rejected", "amount_gbp must be above zero"), ("rejected", "amount_gbp must be above zero"),
                          ("rejected", "the amount differs from the fee"), ("recorded", None)])
        self.assertEqual([x["status"] for x in mt.read_ledger(e.ledger.path)], ["rejected"] * 3 + ["recorded"])
        self.assertTrue(mt.verify_ledger(e.ledger.path)[0])
        handler = mt.handlers_mod.FeeRefund(mt.handlers_mod.load_store(STORE_FILE)[0])
        for v in (float("nan"), float("inf"), float("-inf")):
            self.assertEqual(handler.call({"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0001", "amount_gbp": v}),
                             ({"status": "rejected", "reason": "amount_gbp must be a number"}, True))

    def test_the_ledger_writes_and_reads_strict_json_only(self):
        e = self.store_emu()
        self.call(e, {"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0001", "amount_gbp": 12.0})
        with self.assertRaises(ValueError):
            e.ledger.append({"tool": "fee_refund", "arguments": {"amount_gbp": float("nan")}})
        self.call(e, {"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0001", "amount_gbp": 12.0}, mid=2)
        self.assertEqual([x["seq"] for x in mt.read_ledger(e.ledger.path)], [1, 2])   # no gap after the refused line
        self.assertTrue(mt.verify_ledger(e.ledger.path)[0])
        bad = self.dir / "nan_ledger.jsonl"
        bad.write_bytes(e.ledger.path.read_bytes().replace(b'"amount_gbp":12.0', b'"amount_gbp":NaN', 1))
        ok, problems = mt.verify_ledger(bad)
        self.assertEqual((ok, problems), (False, ["cannot read the ledger (ValueError)"]))
        big = self.dir / "big_ledger.jsonl"          # re-check R1: 1e400 reads as an infinity
        big.write_bytes(e.ledger.path.read_bytes().replace(b'"amount_gbp":12.0', b'"amount_gbp":1e400', 1))
        self.assertEqual(mt.verify_ledger(big), (False, ["cannot read the ledger (ValueError)"]))

    def test_a_store_fee_that_is_not_a_finite_number_above_zero_is_refused(self):
        """Re-check R3: a NaN fee would equal every amount, so the handler refuses the store."""
        def store(fee):
            return {"accounts": [{"account_ref": "SYN-A-0001", "fees": [{"fee_id": "SYN-F-0001", "amount_gbp": fee}]}]}
        for fee in (float("nan"), float("inf"), float("-inf"), 0, -12.0, "12.0", True, None):
            with self.subTest(fee=fee):
                with self.assertRaises(mt.handlers_mod.StoreError):
                    mt.handlers_mod.FeeRefund(store(fee))
        self.assertEqual(mt.handlers_mod.FeeRefund(store(12.0)).fees, {("SYN-A-0001", "SYN-F-0001"): 12.0})


class SyntheticBankTests(unittest.TestCase):
    """T10: SRD-1's synthetic customers, accounts and fees."""

    def test_counts_domains_and_references(self):
        import yaml
        data = yaml.safe_load(STORE_FILE.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(data["customers"]), 4)
        self.assertEqual({c["domain"] for c in data["customers"]}, {"retail", "wealth"})
        self.assertGreaterEqual(len(data["accounts"]), 6)
        fees = [f for a in data["accounts"] for f in a["fees"]]
        self.assertGreaterEqual(len(fees), 10)
        refs = [c["customer_ref"] for c in data["customers"]] + [a["account_ref"] for a in data["accounts"]] + \
            [a["customer_ref"] for a in data["accounts"]] + [f["fee_id"] for f in fees] + [data["default_customer_ref"]]
        self.assertTrue(all(r.startswith("SYN-") for r in refs), refs)
        self.assertEqual(len({f["fee_id"] for f in fees}), len(fees))
        known = {c["customer_ref"] for c in data["customers"]}
        self.assertTrue(all(a["customer_ref"] in known for a in data["accounts"]))

    def test_no_reserved_host_and_no_placeholder_marking(self):
        from faias_harness.engine import sec006
        for path in (STORE_FILE, STORE_TOOLS_FILE):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=path.name):
                self.assertEqual(sec006.find_placeholders([text]), [])
                self.assertNotIn(".invalid", text.lower())
