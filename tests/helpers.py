"""Helpers for the unit tests: valid settings, a guard against outside network use, and a local stand-in model host.

No test in this folder contacts a host outside the local machine. The stand-in is an HTTP server on 127.0.0.1
that behaves like the model host in the few ways that the tests need.
"""
import atexit
import contextlib
import copy
import errno
import ipaddress
import itertools
import json
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from faias_harness import settings as settings_mod

# A made-up value for tests. It is not a key. Tests put it in a variable and check that it never appears in output.
FAKE_KEY_VALUE = "TESTVALUE_not_a_real_key_0001"
KEY_ENV = "OPENROUTER_API_KEY"

EVIDENCE_RAW = {
    "provider": "openrouter", "model_id": "example-vendor/example-model", "mode": "evidence",
    "routing": {"only": ["examplehost"], "allow_fallbacks": False, "zdr": True, "data_collection": "deny",
                "quantizations": ["fp8"]},
    "price_in_per_m": 1.0, "price_out_per_m": 2.0, "cap_usd": 1.0, "api_key_env": KEY_ENV, "max_tokens": 200,
    "temperature": 0.7, "seed": 1000, "item_set": "spike",
    "reviewer_values": {"M4": {"value": 0.5, "reason": "test value for the unit tests"}},
}


def raw_settings(**changes):
    raw = copy.deepcopy(EVIDENCE_RAW)
    for k, v in changes.items():
        raw[k] = v
    return raw


def make_settings(**changes):
    raw = raw_settings(**changes)
    problems = settings_mod.validate(raw)
    assert not problems, problems
    return settings_mod.from_raw(raw)


def own_agt012_items(test, items, folder):
    """Point the TP-AGT-012 item file at a copy of `items` (agt012.Item) in `folder`, until `test` ends. Then `check`
    compares the log of a run on these items with these items. It compares a log of the shipped set with
    items/TP-AGT-012.json in the same way. Returns the path of the copy."""
    from pathlib import Path

    from faias_harness.engine import agt012
    rows = [{"id": i.id, "class": i.cls, "route": i.route, "activity": i.activity, "objective": i.objective,
             "customer_ref": i.customer_ref, "fees": [dict(f) for f in i.fees], "expected_hold": i.expected_hold,
             "turns": list(i.turns), "emulator": dict(i.emulator), "technique": i.technique} for i in items]
    Path(folder).mkdir(parents=True, exist_ok=True)
    path = Path(folder) / "test_items_TP-AGT-012.json"
    path.write_text(json.dumps({"procedure": agt012.TP, "items": rows}), encoding="utf-8")
    patcher = mock.patch.object(agt012, "ITEM_FILE", str(path.resolve()))
    patcher.start()
    test.addCleanup(patcher.stop)
    return path


def lock_time_twice_clock():
    """A clock that gives its first value twice, then 1 ns more at each call. The engine takes the lock time from
    the first value. A clock that moves in steps (Python before 3.13 on Windows) can give that time again."""
    start, calls = time.time_ns(), itertools.count()
    return lambda: start + max(0, next(calls) - 1)


def _is_local(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


@contextlib.contextmanager
def no_outside_network():
    """Make any socket connection or name lookup for a non-local host raise. Local connections pass."""
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if not _is_local(host):
            raise AssertionError(f"test tried to connect to {host}")
        return real_connect(self, address)

    def getaddrinfo(host, *a, **k):
        if host is not None and not _is_local(host):
            raise AssertionError(f"test tried to look up {host}")
        return real_getaddrinfo(host, *a, **k)

    with mock.patch.object(socket.socket, "connect", connect), mock.patch("socket.getaddrinfo", getaddrinfo):
        yield


@contextlib.contextmanager
def refused_at(port, host="127.0.0.1"):
    """A connect to (host, port) fails at once with ConnectionRefusedError. Any other connect is as before.

    On Windows a connect to a closed local port takes about 2 s, because the system tries again before it gives up.
    On Linux it fails at once. A test of a refused connection proves the retries and the error class, not that delay.
    """
    real_connect = socket.socket.connect

    def connect(self, address):
        if isinstance(address, tuple) and tuple(address[:2]) == (host, port):
            raise ConnectionRefusedError(errno.ECONNREFUSED, f"connection to {host}:{port} refused")
        return real_connect(self, address)

    with mock.patch.object(socket.socket, "connect", connect):
        yield


_SELFTEST = {}


def shared_selftest(repo):
    """One containment self-test log for this test process, with its test key. It returns (log, private key, public key).

    The first call runs the self-test into a temporary folder, which is removed when the process ends. Each later call
    gives the same log, or the same failure. The self-test must pass on the first try, because a retry could hide a
    flake. The engine tests use the log as a precondition of their runs. tests/test_selftest.py makes its own runs,
    because the run is its subject.
    """
    if "error" in _SELFTEST:
        raise AssertionError(_SELFTEST["error"])
    if "log" not in _SELFTEST:
        from faias_harness import selftest
        from faias_harness.engine import runlog
        tmp = tempfile.TemporaryDirectory()
        atexit.register(tmp.cleanup)
        d = Path(tmp.name)
        priv, pub = runlog.make_key(d / "keys", repo=repo)
        out = d / "st"
        with no_outside_network():
            selftest.run(out, sign_key=priv, repo=repo)
        log = out / selftest.LOG_NAME
        status = selftest.current(repo=repo, selftest_log=str(log), pubkey=pub)
        if not (status and status["result"] == "PASS"):
            failed = [r.get("id") for r in runlog.read_log(log) if r.get("type") == "probe" and r.get("result") == "FAIL"]
            _SELFTEST["error"] = f"the self-test fixture did not pass; failed probes: {failed}"
            raise AssertionError(_SELFTEST["error"])
        _SELFTEST.update(tmp=tmp, log=log, priv=priv, pub=pub)
    return _SELFTEST["log"], _SELFTEST["priv"], _SELFTEST["pub"]


class StandInModelHost:
    """A local HTTP server that answers POST /api/v1/chat/completions like the model host in the fields we use.

    `respond(user_text, nth)` returns the reply text; `nth` counts earlier requests with the same user text.
    It may instead return a dict: {"status": n, "body": {...}} for an error response, or
    {"content": text, "finish_reason": reason} for a reply with a finish reason.
    The request bodies are kept in `requests`. Only the presence of the Authorization header is kept.
    With `with_messages`, `respond(user_text, nth, messages)` also gets the messages of the request (the system prompt
    and the earlier turns), so a stand-in can follow the system prompt.
    A request with `tools` gets one fixed tool call ({"word": "ready"}). With `tool_reply` (since 0.6),
    `tool_reply(messages)` decides instead: a list of tool calls (each {"id", "name", "arguments"}) or a reply text.
    """

    def __init__(self, respond=None, provider="ExampleHost", usage=(30, 20), with_messages=False, tool_reply=None):
        self.respond = respond or (lambda text, nth: "I can only help with Example Bank accounts and fees.")
        self.provider = provider
        self.usage = usage
        self.with_messages = with_messages
        self.tool_reply = tool_reply
        self.requests = []
        self._seen = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8"))
                outer.requests.append({"path": self.path, "body": body, "has_auth": bool(self.headers.get("Authorization"))})
                user = [m for m in body["messages"] if m["role"] == "user"][-1]["content"]
                nth = outer._seen.get(user, 0)
                outer._seen[user] = nth + 1
                finish, status = None, 200
                decided = outer.tool_reply(body["messages"]) if body.get("tools") and outer.tool_reply else None
                if isinstance(decided, list):
                    msg = {"role": "assistant", "content": None,
                           "tool_calls": [{"id": c["id"], "type": "function",
                                           "function": {"name": c["name"], "arguments": c["arguments"]}}
                                          for c in decided]}
                elif isinstance(decided, str):
                    msg = {"role": "assistant", "content": decided}
                elif body.get("tools"):
                    msg = {"role": "assistant", "content": None,
                           "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": body["tools"][0]["function"]["name"], "arguments": "{\"word\": \"ready\"}"}}]}
                else:
                    r = outer.respond(user, nth, body["messages"]) if outer.with_messages else outer.respond(user, nth)
                    if isinstance(r, dict) and "status" in r:
                        status, out = r["status"], r["body"]
                    elif isinstance(r, dict):
                        r, finish = r["content"], r.get("finish_reason")
                    msg = {"role": "assistant", "content": r}
                    if "17 times 3" in user:
                        msg["reasoning"] = "17 times 3 is 51."
                if status == 200:
                    out = {"id": "gen-1", "model": body["model"], "provider": outer.provider,
                           "choices": [{"message": msg, "finish_reason": finish}],
                           "usage": {"prompt_tokens": outer.usage[0], "completion_tokens": outer.usage[1]}}
                data = json.dumps(out).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        # A stop waits for the end of the current poll. The default poll is 0.5 s, so a short poll makes a stop fast.
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}/api/v1"

    @property
    def host_entry(self):
        return f"127.0.0.1:{self.port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)
