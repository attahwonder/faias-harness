"""Local stand-ins for the channels of external targets: Direct Line 3.0, the Responses API and an A2A agent.

Each is an HTTP server on 127.0.0.1 that behaves like the channel in the few ways that the tests need. No stand-in
contacts any other host. A stand-in keeps whether a request carried an auth header and whether the value matched;
it never keeps the value.

`respond(text, nth)` gives the agent's behaviour: `nth` counts earlier messages with the same text. It returns
the reply text, a list of reply texts (Direct Line: one activity each), None (no reply), or a dict for a special
case that each stand-in documents.
"""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


def default_respond(text, nth):
    return "I can only help with Example Bank accounts and fees."


class _Server:
    """A local HTTP server. `route(method, path, query, headers, body)` returns (status, body or None, headers)."""

    def __init__(self):
        outer = self
        self.requests = []

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _do(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    body = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    body = None
                parts = urlsplit(self.path)
                outer.requests.append({"method": method, "path": parts.path, "query": parts.query, "body": body,
                                       "has_auth": bool(self.headers.get("Authorization") or self.headers.get("api-key"))})
                status, out, headers = outer.route(method, parts.path, parse_qs(parts.query), self.headers, body)
                data = b"" if out is None else json.dumps(out).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def do_GET(self):
                self._do("GET")

            def do_POST(self):
                self._do("POST")

            def do_DELETE(self):
                self._do("DELETE")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._lock = threading.Lock()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


class StandInDirectLine(_Server):
    """Direct Line 3.0 in the parts the adapter uses.

    POST /v3/directline/tokens/generate (Bearer secret), GET /token (a token endpoint, no auth),
    POST /v3/directline/conversations (Bearer token), POST and GET .../conversations/{id}/activities.
    The user's own activities are echoed in the activity set, as Direct Line does. Each bot reply follows a
    typing activity. `respond` may return {"status": n} for an error on the send, or {"card": text} for a reply
    that is an Adaptive Card. `secret` is the value the stand-in expects; a wrong one gets HTTP 403.
    `reply_after_polls` delays the reply until that many reads of the activity set.
    """

    def __init__(self, respond=default_respond, secret=None, greeting="Hello, I am the Example Bank assistant.",
                 reply_after_polls=0):
        super().__init__()
        self.respond, self.secret, self.greeting, self.reply_after_polls = respond, secret, greeting, reply_after_polls
        self.convs, self.tokens, self._seen = {}, {}, {}
        self.auth_ok = []

    def _bearer(self, headers):
        h = headers.get("Authorization") or ""
        return h[7:] if h.startswith("Bearer ") else None

    def route(self, method, path, query, headers, body):
        if method == "GET" and path == "/token":
            tok = secrets.token_hex(12)
            self.tokens[tok] = None
            return 200, {"token": tok, "expires_in": 3600}, None
        if path == "/v3/directline/tokens/generate" and method == "POST":
            ok = self.secret is not None and self._bearer(headers) == self.secret
            self.auth_ok.append(ok)
            if not ok:
                return 403, {"error": {"code": "BadArgument", "message": "secret rejected"}}, None
            tok, cid = secrets.token_hex(12), "c" + secrets.token_hex(4)
            self.tokens[tok] = cid
            return 200, {"token": tok, "conversationId": cid, "expires_in": 3600}, None
        tok = self._bearer(headers)
        if tok not in self.tokens:
            return 403, {"error": {"code": "TokenExpired", "message": "token not valid"}}, None
        if path == "/v3/directline/conversations" and method == "POST":
            cid = self.tokens[tok] or ("c" + secrets.token_hex(4))
            self.tokens[tok] = cid
            self.convs[cid] = {"acts": [], "pending": [], "polls": 0}
            return 201, {"conversationId": cid, "token": tok, "expires_in": 3600, "streamUrl": "wss://unused.test"}, None
        segs = path.split("/")
        if len(segs) == 6 and segs[1:4] == ["v3", "directline", "conversations"] and segs[5] == "activities":
            cid = segs[4]
            conv = self.convs.get(cid)
            if conv is None or self.tokens.get(tok) != cid:
                return 404, {"error": {"code": "BadArgument", "message": "unknown conversation"}}, None
            with self._lock:
                if method == "POST":
                    return self._post(cid, conv, body)
                return self._get(conv, query)
        return 404, {"error": {"code": "NotFound", "message": path}}, None

    def _bot(self, reply_to, text=None, card=None):
        a = {"type": "message", "from": {"id": "examplebot", "name": "Example Bank assistant"}, "replyToId": reply_to}
        if card is not None:
            a["attachments"] = [{"contentType": "application/vnd.microsoft.card.adaptive",
                                 "content": {"type": "AdaptiveCard", "body": [{"type": "TextBlock", "text": card}]}}]
        else:
            a["text"] = text
        return a

    def _post(self, cid, conv, body):
        aid = f"{cid}|{len(conv['acts']):07d}"
        conv["acts"].append({**body, "id": aid})
        out = []
        if body.get("type") == "event" and body.get("name") == "startConversation":
            if self.greeting:
                out = [self._bot(aid, self.greeting)]
        elif body.get("type") == "message":
            text = body.get("text", "")
            nth = self._seen.get(text, 0)
            self._seen[text] = nth + 1
            r = self.respond(text, nth)
            if isinstance(r, dict) and "status" in r:
                conv["acts"].pop()
                return r["status"], {"error": {"code": "BotError", "message": "the bot returned an error"}}, None
            if isinstance(r, dict) and "card" in r:
                out = [self._bot(aid, card=r["card"])]
            elif isinstance(r, list):
                out = [self._bot(aid, t) for t in r]
            elif isinstance(r, str):
                out = [self._bot(aid, r)]
        if out:
            conv["pending"].append({"after": conv["polls"] + self.reply_after_polls,
                                    "acts": [{"type": "typing", "from": {"id": "examplebot"}}] + out})
        return 200, {"id": aid}, None

    def _get(self, conv, query):
        conv["polls"] += 1
        for p in [p for p in conv["pending"] if p["after"] < conv["polls"]]:
            for a in p["acts"]:
                conv["acts"].append({**a, "id": f"x|{len(conv['acts']):07d}"})
            conv["pending"].remove(p)
        wm = int((query.get("watermark") or ["0"])[0] or 0)
        acts = conv["acts"][wm:]
        return 200, {"activities": acts, "watermark": str(len(conv["acts"]))}, None


class StandInResponses(_Server):
    """POST /responses in the fields the adapter reads.

    `respond` may return a dict: {"refusal": text}; {"incomplete": reason, "text": text}; or
    {"status": n, "body": {...}, "headers": {...}} for an error response. `key` is the bearer value expected.
    """

    def __init__(self, respond=default_respond, key=None, model="example-model-2026-01-01"):
        super().__init__()
        self.respond, self.key, self.model = respond, key, model
        self._seen, self._n = {}, 0
        self.auth_ok = []

    def route(self, method, path, query, headers, body):
        if method != "POST" or not path.endswith("/responses"):
            return 404, {"error": {"code": "not_found"}}, None
        if self.key is not None:
            ok = (headers.get("Authorization") == f"Bearer {self.key}") or (headers.get("api-key") == self.key)
            self.auth_ok.append(ok)
            if not ok:
                return 401, {"error": {"code": "invalid_api_key"}}, None
        text = body.get("input", "")
        nth = self._seen.get(text, 0)
        self._seen[text] = nth + 1
        r = self.respond(text, nth)
        if isinstance(r, dict) and "status" in r:
            return r["status"], r.get("body", {}), r.get("headers")
        self._n += 1
        out = {"id": f"resp_{self._n}", "object": "response", "model": self.model, "status": "completed",
               "usage": {"input_tokens": 40, "output_tokens": 12}, "output": []}
        if isinstance(r, dict) and "refusal" in r:
            out["output"] = [{"type": "message", "role": "assistant", "content": [{"type": "refusal", "refusal": r["refusal"]}]}]
        else:
            t = r.get("text", "") if isinstance(r, dict) else r
            out["output"] = [{"type": "reasoning", "summary": []},
                             {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": t, "annotations": []}]}]
            if isinstance(r, dict) and r.get("incomplete"):
                out["status"] = "incomplete"
                out["incomplete_details"] = {"reason": r["incomplete"]}
        return 200, out, None


class StandInA2A(_Server):
    """An A2A agent over JSON-RPC, version 1.0 (SendMessage, GetTask) and 0.3 (message/send, tasks/get).

    `mode`: "task" (a completed task with an artifact), "message" (a bare message), "working" (the task is working
    until it is read once with GetTask or tasks/get), or "rejected". `respond` gives the reply text.
    A 1.0 method without the header A2A-Version: 1.0 gets JSON-RPC error -32601, as an agent that defaults to 0.3 does.
    """

    def __init__(self, respond=default_respond, mode="task", key=None):
        super().__init__()
        self.respond, self.mode, self.key = respond, mode, key
        self.tasks, self._seen = {}, {}
        self.versions = []

    def _state(self, v1, s):
        return "TASK_STATE_" + s.upper().replace("-", "_") if v1 else s

    def _parts(self, v1, text):
        return [{"text": text}] if v1 else [{"kind": "text", "text": text}]

    def _task(self, v1, tid, ctx, state, text):
        t = {"id": tid, "contextId": ctx, "status": {"state": self._state(v1, state)}}
        if state in ("completed",):
            t["artifacts"] = [{"artifactId": "a1", "parts": self._parts(v1, text)}]
        elif state == "rejected":
            t["status"]["message"] = {"role": "ROLE_AGENT" if v1 else "agent", "parts": self._parts(v1, text)}
        if not v1:
            t["kind"] = "task"
        return t

    def route(self, method, path, query, headers, body):
        if self.key is not None and headers.get("Authorization") != f"Bearer {self.key}":
            return 401, {"error": "unauthorised"}, None
        mid, m, params = body.get("id"), body.get("method"), body.get("params") or {}
        v1 = headers.get("A2A-Version") == "1.0"
        if m in ("SendMessage", "GetTask") and not v1:
            return 200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}, None
        if m in ("SendMessage", "message/send"):
            self.versions.append("1.0" if v1 else "0.3")
            msg = params.get("message") or {}
            text = "".join(p.get("text", "") for p in msg.get("parts") or [])
            nth = self._seen.get(text, 0)
            self._seen[text] = nth + 1
            reply = self.respond(text, nth) or ""
            ctx = msg.get("contextId") or ("ctx-" + secrets.token_hex(3))
            if self.mode == "message":
                res = {"role": "ROLE_AGENT" if v1 else "agent", "messageId": secrets.token_hex(4), "contextId": ctx,
                       "parts": self._parts(v1, reply)}
                if not v1:
                    res["kind"] = "message"
                return 200, {"jsonrpc": "2.0", "id": mid, "result": {"message": res} if v1 else res}, None
            tid = "t-" + secrets.token_hex(3)
            state = {"task": "completed", "working": "working", "rejected": "rejected"}[self.mode]
            self.tasks[tid] = (ctx, reply)
            t = self._task(v1, tid, ctx, state, reply)
            return 200, {"jsonrpc": "2.0", "id": mid, "result": {"task": t} if v1 else t}, None
        if m in ("GetTask", "tasks/get"):
            tid = params.get("id")
            if tid not in self.tasks:
                return 200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32001, "message": "task not found"}}, None
            ctx, reply = self.tasks[tid]
            # GetTask returns the task itself (no wrapper), in both versions
            return 200, {"jsonrpc": "2.0", "id": mid, "result": self._task(v1, tid, ctx, "completed", reply)}, None
        return 200, {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}, None


class StandInJsonApp(_Server):
    """A firm's own chat endpoint: POST /api/chat with {"conversationId", "message", "channel"}, answering
    {"data": {"conversationId", "reply": {"text"}, "safety": {"blocked", "category"}, "meta": {"model"}}}.

    It keeps the exact bodies it received. `respond` may return {"blocked": category} for a blocked reply, or
    {"status": n} for an error. `key_header` and `key` are the header and value it expects.
    """

    def __init__(self, respond=default_respond, key_header=None, key=None):
        super().__init__()
        self.respond, self.key_header, self.key = respond, key_header, key
        self._seen = {}
        self.auth_ok = []

    def route(self, method, path, query, headers, body):
        if path != "/api/chat" or method != "POST":
            return 404, {"error": "not found"}, None
        if self.key_header is not None:
            ok = headers.get(self.key_header) == self.key
            self.auth_ok.append(ok)
            if not ok:
                return 401, {"error": "unauthorised"}, None
        text = (body or {}).get("message", "")
        nth = self._seen.get(text, 0)
        self._seen[text] = nth + 1
        r = self.respond(text, nth)
        if isinstance(r, dict) and "status" in r:
            return r["status"], {"error": "failed"}, None
        cid = (body or {}).get("conversationId") or ("conv-" + secrets.token_hex(3))
        blocked = isinstance(r, dict) and "blocked" in r
        return 200, {"data": {"conversationId": cid,
                              "reply": {"text": "This request cannot be processed." if blocked else r},
                              "safety": {"blocked": blocked, "category": r["blocked"] if blocked else None},
                              "meta": {"model": "example-app-model"}}}, None
