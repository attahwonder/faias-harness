"""A tool emulator that speaks the Model Context Protocol (MCP) over Streamable HTTP, with a ledger of every call.

UNTESTED against a platform. Tested only with a local client (tests/test_mcp_emulator.py).

Purpose. The capability `tool_routing` asks the harness to point a system's tools at harness emulators and to read
the tool ledgers (docs/ARCHITECTURE.md, section 3). Many agent platforms let a firm add an MCP server to an agent
as a tool. A firm that wires this emulator into its staging agent, in place of the real tool, lets the harness see
every call that the agent makes to that tool (design rule 5: every tool call is seen). Each tool returns a fixed,
synthetic result from the tools file. No tool acts on any system.

What it does:
- It serves one MCP endpoint (POST) on the local machine only (127.0.0.1). A platform in the cloud reaches it only
  through a tunnel that the operator runs and closes; this module does not open one.
- It answers both eras of the protocol. A request with the modern per-request `_meta` (protocol version
  2026-07-28) is served without a session; the headers MCP-Protocol-Version, Mcp-Method and Mcp-Name must match
  the body. An `initialize` request (protocol versions 2025-03-26, 2025-06-18 and 2025-11-25) opens a session with
  an Mcp-Session-Id. Every response is a single JSON object (no event stream).
- It offers tools only: tools/list and tools/call, with server/discover (modern) or initialize (legacy), and ping.
- It refuses a request whose Origin header is present and not on the allowed list (HTTP 403), and, with an API key
  variable set, a request without the matching key header (HTTP 401). The key comes from an environment variable
  and never goes in the ledger or a message.
- It appends one line to the ledger for each tools/call: the time, the era, the protocol version, the tool, the
  arguments, whether the result is an error, and a hash chained to the line before. `verify_ledger` finds an
  edited, removed or reordered line. It does not stop a person who rewrites the whole file.

What it does not do: resources, prompts, sampling, elicitation, subscriptions or event streams; the deprecated
HTTP+SSE transport; OAuth. It does not configure any platform: the firm adds the emulator's URL to its staging agent.

  py -3.14 -X utf8 -m faias_harness.emulators.mcp_tools serve --tools <tools.yaml> --ledger <ledger.jsonl> [--port 8765]
       [--api-key-env NAME] [--api-key-header X-Api-Key] [--allow-origin <origin> ...]
  py -3.14 -X utf8 -m faias_harness.emulators.mcp_tools verify --ledger <ledger.jsonl>
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

from .. import __version__

MODERN = ("2026-07-28",)
LEGACY = ("2025-11-25", "2025-06-18", "2025-03-26")
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER = "io.modelcontextprotocol/serverInfo"
TOOL_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
LOOPBACK = ("127.0.0.1", "::1", "localhost")
MAX_BODY = 1_000_000
HEADER_MISMATCH, UNSUPPORTED_VERSION = -32020, -32022
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS = -32700, -32600, -32601, -32602


# ---------- tools file ----------

class ToolsError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def validate_tools(raw):
    if not isinstance(raw, dict) or not isinstance(raw.get("tools"), list) or not raw["tools"]:
        return ["the tools file must be a mapping with a non-empty list 'tools'"]
    problems, names = [], set()
    for n, t in enumerate(raw["tools"], 1):
        where = f"tool {n}"
        if not isinstance(t, dict):
            problems.append(f"{where}: must be a mapping")
            continue
        unknown = set(t) - {"name", "description", "input_schema", "result"}
        if unknown:
            problems.append(f"{where}: unknown field(s) {', '.join(sorted(unknown))}")
        name = t.get("name")
        if not (isinstance(name, str) and TOOL_NAME.fullmatch(name)):
            problems.append(f"{where}: name must be 1 to 64 letters, digits, '_', '.' or '-'")
        elif name in names:
            problems.append(f"{where}: duplicate name {name}")
        else:
            names.add(name)
        if not (isinstance(t.get("description"), str) and t["description"].strip()):
            problems.append(f"{where}: description must be a non-empty string")
        s = t.get("input_schema")
        if not (isinstance(s, dict) and s.get("type") == "object"):
            problems.append(f"{where}: input_schema must be a JSON Schema object with type: object")
        r = t.get("result")
        if not (isinstance(r, dict) and len(r) == 1 and isinstance(next(iter(r.values()), None), str)
                and next(iter(r)) in ("text", "error")):
            problems.append(f"{where}: result must be {{text: <string>}} or {{error: <string>}}")
    return problems


def load_tools(path):
    try:
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ToolsError([f"cannot read the tools file ({type(e).__name__})"])
    problems = validate_tools(raw)
    if problems:
        raise ToolsError(problems)
    return {"name": str(raw.get("name") or "faias-tool-emulator"), "instructions": raw.get("instructions"),
            "tools": raw["tools"]}


# ---------- ledger ----------

def _canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _iso(ns):
    return datetime.fromtimestamp(ns / 1e9, timezone.utc).isoformat(timespec="microseconds")


GENESIS = "0" * 64


class Ledger:
    """An append-only JSON-lines file with a hash chain. One line for each tools/call."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size:
            raise FileExistsError("the ledger file exists; give a new file for each run")
        self.path.write_bytes(b"")
        self._lock = threading.Lock()
        self.head, self.seq = GENESIS, 0

    def append(self, entry):
        with self._lock:
            self.seq += 1
            ns = time.time_ns()
            line = {"seq": self.seq, "t_ns": ns, "time": _iso(ns), **entry, "prev": self.head}
            line["hash"] = hashlib.sha256((self.head + _canonical(line)).encode("utf-8")).hexdigest()
            with open(self.path, "ab") as f:
                f.write(_canonical(line).encode("utf-8") + b"\n")
            self.head = line["hash"]
            return line


def read_ledger(path, tool=None):
    """The ledger entries, optionally for one tool only. This is what `read_tool_ledger` of the target interface
    returns for a target whose tool was wired to this emulator."""
    entries = [json.loads(x) for x in Path(path).read_text(encoding="utf-8").splitlines() if x.strip()]
    return [e for e in entries if tool is None or e.get("tool") == tool]


read_tool_ledger = read_ledger


def verify_ledger(path, expected_head=None):
    """(ok, problems). Checks each line's hash, the chain, and the order of seq. With `expected_head`, also finds a
    removed last line."""
    problems, prev = [], GENESIS
    try:
        entries = read_ledger(path)
    except (OSError, ValueError) as e:
        return False, [f"cannot read the ledger ({type(e).__name__})"]
    for n, e in enumerate(entries, 1):
        h = e.pop("hash", None)
        if e.get("seq") != n:
            problems.append(f"line {n}: seq is {e.get('seq')}, expected {n}")
        if e.get("prev") != prev:
            problems.append(f"line {n}: the chain is broken (prev does not match the line before)")
        calc = hashlib.sha256((e.get("prev", "") + _canonical(e)).encode("utf-8")).hexdigest()
        if h != calc:
            problems.append(f"line {n}: the hash does not match the content")
        prev = h or ""
    if expected_head is not None and prev != expected_head:
        problems.append("the last line is not the expected head (a line may be missing at the end)")
    return not problems, problems


# ---------- server ----------

def _decode_header(value):
    """Decode the Base64 sentinel form =?base64?...?= of Mcp-Name; plain values pass through."""
    if value and value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[9:-2]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None
    return value


class ToolEmulator:
    """The MCP tool emulator. Use as a context manager, or call start() and stop()."""

    def __init__(self, tools, ledger_path, *, host="127.0.0.1", port=0, path="/mcp", api_key_env=None,
                 api_key_header="X-Api-Key", allowed_origins=(), env=None):
        if host not in LOOPBACK:
            raise ValueError("the emulator listens on the local machine only; reach it from a platform through a tunnel")
        env = os.environ if env is None else env
        self._key = None
        if api_key_env:
            self._key = env.get(api_key_env, "")
            if not self._key:
                raise ValueError(f"the environment variable {api_key_env} is not set")
        self.api_key_header = api_key_header
        self.tools = tools
        self.by_name = {t["name"]: t for t in tools["tools"]}
        self.ledger = Ledger(ledger_path)
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins}
        self.path = path
        self.sessions = {}
        self.methods = {}
        self._mlock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "faias-tool-emulator"

            def log_message(self, *a):
                pass

            def _send(self, status, body=None, headers=None):
                data = b"" if body is None else _canonical(body).encode("utf-8")
                self.send_response(status)
                if body is not None:
                    self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _gate(self):
                """Path, Origin and key checks. Returns True if the request may go on."""
                if self.path.split("?")[0] != outer.path:
                    self._send(404, {"jsonrpc": "2.0", "error": {"code": INVALID_REQUEST, "message": "not the MCP endpoint"}})
                    return False
                origin = self.headers.get("Origin")
                if origin is not None and origin.rstrip("/").lower() not in outer.allowed_origins:
                    self._send(403, {"jsonrpc": "2.0", "error": {"code": INVALID_REQUEST, "message": "origin not allowed"}})
                    return False
                if outer._key is not None:
                    given = self.headers.get(outer.api_key_header) or ""
                    if not hmac.compare_digest(given.encode("utf-8"), outer._key.encode("utf-8")):
                        self._send(401, {"jsonrpc": "2.0", "error": {"code": INVALID_REQUEST, "message": "missing or wrong API key"}})
                        return False
                return True

            def do_GET(self):
                if self._gate():
                    self._send(405, None, {"Allow": "POST"})

            def do_DELETE(self):
                if not self._gate():
                    return
                sid = self.headers.get("Mcp-Session-Id")
                if sid and outer.sessions.pop(sid, None) is not None:
                    self._send(200)
                else:
                    self._send(405 if not sid else 404, None, {"Allow": "POST"})

            def do_POST(self):
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = -1
                if n < 0 or n > MAX_BODY:
                    self.close_connection = True      # the body is not read, so the connection cannot be reused
                    self._send(413, {"jsonrpc": "2.0", "error": {"code": INVALID_REQUEST, "message": "body too large"}},
                               {"Connection": "close"})
                    return
                raw = self.rfile.read(n)                 # read before any refusal, so a kept-alive connection stays in step
                if not self._gate():
                    return
                try:
                    msg = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": PARSE_ERROR, "message": "parse error"}})
                    return
                if not (isinstance(msg, dict) and msg.get("jsonrpc") == "2.0" and isinstance(msg.get("method"), str)):
                    self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "not a JSON-RPC request"}})
                    return
                status, body, headers = outer.handle(msg, self.headers)
                self._send(status, body, headers)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.host, self.port = host, self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://{self.host}:{self.port}{self.path}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    # ---- protocol ----

    def _count(self, era, method):
        with self._mlock:
            k = f"{era}:{method}"
            self.methods[k] = self.methods.get(k, 0) + 1

    def _server_info(self):
        return {"name": self.tools["name"], "version": __version__}

    def _tool_list(self):
        return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]}
                for t in self.tools["tools"]]

    def _call(self, params, era, version, session):
        name = params.get("name") if isinstance(params, dict) else None
        args = params.get("arguments", {}) if isinstance(params, dict) else None
        if name not in self.by_name:
            return None, {"code": INVALID_PARAMS, "message": "unknown tool"}
        if not isinstance(args, dict):
            return None, {"code": INVALID_PARAMS, "message": "arguments must be an object"}
        r = self.by_name[name]["result"]
        is_error = "error" in r
        self.ledger.append({"era": era, "protocol_version": version, "session": session, "tool": name,
                            "arguments": args, "is_error": is_error})
        return {"content": [{"type": "text", "text": r.get("error") if is_error else r["text"]}], "isError": is_error}, None

    @staticmethod
    def _err(mid, code, message, data=None):
        e = {"code": code, "message": message}
        if data is not None:
            e["data"] = data
        return {"jsonrpc": "2.0", "id": mid, "error": e}

    def handle(self, msg, headers):
        """(status, body or None, extra headers) for one JSON-RPC message."""
        method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        meta = params.get("_meta") if isinstance(params, dict) else None
        modern_version = meta.get(META_VERSION) if isinstance(meta, dict) else None
        if method == "initialize" or modern_version is None:
            return self._handle_legacy(method, mid, params, headers)
        return self._handle_modern(method, mid, params, headers, modern_version)

    def _handle_modern(self, method, mid, params, headers, version):
        self._count("modern", method)
        if headers.get("MCP-Protocol-Version") != version:
            return 400, self._err(mid, HEADER_MISMATCH, "Header mismatch: MCP-Protocol-Version"), None
        if version not in MODERN:
            return 400, self._err(mid, UNSUPPORTED_VERSION, "Unsupported protocol version",
                                  {"supported": list(MODERN + LEGACY), "requested": version}), None
        if headers.get("Mcp-Method") != method:
            return 400, self._err(mid, HEADER_MISMATCH, "Header mismatch: Mcp-Method"), None
        if method == "tools/call" and _decode_header(headers.get("Mcp-Name")) != params.get("name"):
            return 400, self._err(mid, HEADER_MISMATCH, "Header mismatch: Mcp-Name"), None
        if mid is None:
            return 202, None, None
        if method == "server/discover":
            result = {"supportedVersions": list(MODERN), "capabilities": {"tools": {}},
                      "_meta": {META_SERVER: self._server_info()}}
            if self.tools.get("instructions"):
                result["instructions"] = self.tools["instructions"]
        elif method == "tools/list":
            result = {"tools": self._tool_list()}
        elif method == "tools/call":
            result, err = self._call(params, "modern", version, None)
            if err:
                return 200, self._err(mid, **err), None
        elif method == "ping":
            result = {}
        else:
            return 404, self._err(mid, METHOD_NOT_FOUND, "Method not found"), None
        return 200, {"jsonrpc": "2.0", "id": mid, "result": {"resultType": "complete", **result}}, None

    def _handle_legacy(self, method, mid, params, headers):
        self._count("legacy", method)
        if method == "initialize":
            asked = params.get("protocolVersion") if isinstance(params, dict) else None
            version = asked if asked in LEGACY else LEGACY[0]
            sid = secrets.token_hex(16)
            self.sessions[sid] = {"version": version}
            result = {"protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": self._server_info()}
            if self.tools.get("instructions"):
                result["instructions"] = self.tools["instructions"]
            return 200, {"jsonrpc": "2.0", "id": mid, "result": result}, {"Mcp-Session-Id": sid}
        sid = headers.get("Mcp-Session-Id")
        if not sid:
            return 400, self._err(mid, INVALID_REQUEST, "missing Mcp-Session-Id; send initialize first"), None
        session = self.sessions.get(sid)
        if session is None:
            return 404, self._err(mid, INVALID_REQUEST, "unknown or ended session"), None
        if mid is None:                           # a notification, for example notifications/initialized
            return 202, None, None
        if method == "tools/list":
            result = {"tools": self._tool_list()}
        elif method == "tools/call":
            result, err = self._call(params, "legacy", session["version"], sid[:8])
            if err:
                return 200, self._err(mid, **err), None
        elif method == "ping":
            result = {}
        else:
            return 200, self._err(mid, METHOD_NOT_FOUND, "Method not found"), None
        return 200, {"jsonrpc": "2.0", "id": mid, "result": result}, None


# ---------- command line ----------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="faias_harness.emulators.mcp_tools", description="MCP tool emulator with a ledger.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="serve the tools on the local machine and write the ledger")
    s.add_argument("--tools", required=True)
    s.add_argument("--ledger", required=True, help="a new file for this run")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--path", default="/mcp")
    s.add_argument("--api-key-env", help="the environment variable that holds the key the client must send")
    s.add_argument("--api-key-header", default="X-Api-Key")
    s.add_argument("--allow-origin", action="append", default=[])
    v = sub.add_parser("verify", help="verify the hash chain of a ledger")
    v.add_argument("--ledger", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "verify":
        ok, problems = verify_ledger(a.ledger)
        print("PASS: the ledger chain is intact" if ok else "FAIL: " + "; ".join(problems))
        return 0 if ok else 1
    try:
        tools = load_tools(a.tools)
        emu = ToolEmulator(tools, a.ledger, port=a.port, path=a.path, api_key_env=a.api_key_env,
                           api_key_header=a.api_key_header, allowed_origins=a.allow_origin)
    except (ToolsError, ValueError, FileExistsError) as e:
        print(f"refused: {e}")
        return 2
    print(f"MCP tool emulator: {len(tools['tools'])} tools at {emu.url}; ledger {a.ledger}")
    if a.api_key_env is None:
        print("WARNING: no API key is set. Anyone who can reach this port can call the tools. "
              "Set --api-key-env before you expose the port through a tunnel.")
    print("Synthetic results only. Press Ctrl+C to stop.")
    emu.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        emu.stop()
        print(f"stopped. Ledger head: {emu.ledger.head}; tool calls: {emu.ledger.seq}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
