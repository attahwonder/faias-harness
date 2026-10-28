"""Helpers for the unit tests: valid settings, a guard against outside network use, and a local stand-in model host.

No test in this folder contacts a host outside the local machine. The stand-in is an HTTP server on 127.0.0.1
that behaves like the model host in the few ways that the tests need.
"""
import contextlib
import copy
import ipaddress
import itertools
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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


class StandInModelHost:
    """A local HTTP server that answers POST /api/v1/chat/completions like the model host in the fields we use.

    `respond(user_text, nth)` returns the reply text; `nth` counts earlier requests with the same user text.
    It may instead return a dict: {"status": n, "body": {...}} for an error response, or
    {"content": text, "finish_reason": reason} for a reply with a finish reason.
    The request bodies are kept in `requests`. Only the presence of the Authorization header is kept.
    """

    def __init__(self, respond=None, provider="ExampleHost", usage=(30, 20)):
        self.respond = respond or (lambda text, nth: "I can only help with Example Bank accounts and fees.")
        self.provider = provider
        self.usage = usage
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
                if body.get("tools"):
                    msg = {"role": "assistant", "content": None,
                           "tool_calls": [{"id": "c1", "type": "function",
                                           "function": {"name": body["tools"][0]["function"]["name"], "arguments": "{\"word\": \"ready\"}"}}]}
                else:
                    r = outer.respond(user, nth)
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
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

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
