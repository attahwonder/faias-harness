"""C-02 and C-08, minimal: SRD-1's MCP client for its tools (since 0.6).

It speaks the session flow of MCP over HTTP: `initialize` (protocol version 2025-06-18), the notification
`notifications/initialized`, `tools/list`, `tools/call`, and a DELETE at close. It is tested against the harness's
MCP tool emulator on 127.0.0.1 only (faias_harness/emulators/mcp_tools.py), to which an engine run routes SRD-1's
tools (the capability `tool_routing`).

Every request goes through the shared egress client, so the allow-list applies to it. The key for the tool server
comes from the environment variable that the settings name (`tool_key_env`). It goes only in one request header. It
is never put in the trace, the run log or an error message: an error names a class or an HTTP status only.
"""
import hashlib
import json
import os

from faias_harness.errors import EgressRefused

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "srd1-minimal", "version": "0.6"}
KEY_HEADER = "X-Api-Key"


class McpError(Exception):
    """A failed MCP request. The text names a class, a status or a JSON-RPC code, never a key."""


def function_definitions(tools):
    """The tool definitions in the function form that the model gateway sends, from MCP tool entries
    (`inputSchema`) or tools-file entries (`input_schema`)."""
    return [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                              "parameters": t.get("inputSchema", t.get("input_schema"))}}
            for t in tools]


def definitions_sha256(definitions):
    blob = json.dumps(definitions, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class McpClient:
    def __init__(self, url, egress, *, key_env, env=None, key_header=KEY_HEADER, timeout=30.0):
        env = os.environ if env is None else env
        self._key = env.get(key_env, "") if key_env else ""
        if not self._key:
            raise ValueError(f"the environment variable {key_env} is not set")
        self.url = url
        self.egress = egress
        self.key_header = key_header
        self.timeout = timeout
        self.session_id = None
        self._id = 0

    def _headers(self):
        h = {self.key_header: self._key, "Accept": "application/json, text/event-stream",
             "Content-Type": "application/json"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
            h["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return h

    def _send(self, method, body=None):
        try:
            return self.egress.request(method, self.url, json=body, headers=self._headers(), timeout=self.timeout)
        except EgressRefused:
            raise
        except Exception as e:      # noqa: BLE001 - only the class name is kept, so no detail can hold a key
            raise McpError(f"the tool server could not be reached ({type(e).__name__})") from None

    def _rpc(self, method, params=None, notify=False):
        body = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notify:
            self._id += 1
            body["id"] = self._id
        r = self._send("POST", body)
        if notify:
            if r.status_code not in (200, 202):
                raise McpError(f"the tool server returned HTTP {r.status_code} for {method}")
            return None
        if r.status_code != 200:
            raise McpError(f"the tool server returned HTTP {r.status_code} for {method}")
        try:
            data = r.json()
        except ValueError:
            raise McpError(f"the tool server returned a response in an unexpected form for {method}") from None
        if not isinstance(data, dict) or "error" in data:
            code = data.get("error", {}).get("code") if isinstance(data, dict) else None
            raise McpError(f"the tool server refused {method} (JSON-RPC code {code})")
        return data.get("result"), r

    def open(self):
        result, r = self._rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                             "clientInfo": CLIENT_INFO})
        self.session_id = r.headers.get("Mcp-Session-Id")
        if not self.session_id or not isinstance(result, dict) or result.get("protocolVersion") != PROTOCOL_VERSION:
            raise McpError("the tool server did not open a session at protocol version " + PROTOCOL_VERSION)
        self._rpc("notifications/initialized", notify=True)
        return self

    def list_tools(self):
        result, _ = self._rpc("tools/list")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            raise McpError("the tool server returned a tool list in an unexpected form")
        return result["tools"]

    def call(self, name, arguments):
        """The MCP result: `content`, `isError` and, for a computed result, `structuredContent`."""
        result, _ = self._rpc("tools/call", {"name": name, "arguments": arguments})
        if not isinstance(result, dict):
            raise McpError("the tool server returned a tool result in an unexpected form")
        return result

    def close(self):
        if self.session_id:
            try:
                self._send("DELETE")
            finally:
                self.session_id = None


def result_text(result):
    """The text that goes back to the model: the text parts of the MCP result, joined."""
    parts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(parts)
