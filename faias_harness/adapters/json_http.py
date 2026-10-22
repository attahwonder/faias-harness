"""Adapter for a firm's own chat endpoint that takes and returns JSON over HTTP (`json-http`).

UNTESTED against a platform. Tested only against a local stand-in (tests/standins.py).

Many firms put their own API in front of the model or agent that their app uses. This adapter reaches such an
endpoint from a request template and a path into the response. It needs no code for each endpoint, only settings.
It offers `conversation` only.

Profile:
  adapter: json-http
  endpoint: the full URL that the adapter sends to
  settings.api_key_env: the variable that holds the key or token (optional; omit it for no auth header)
  adapter_options:
    method: POST (default) or PUT
    request: the JSON body as a mapping. Any string in it may hold {message} and {session_id}. A string that is
             exactly "{message}" or "{session_id}" becomes the value itself (a session ID that is not yet known
             becomes null); in a longer string the text is put in place. The adapter fills the parsed mapping,
             so the message can never change the JSON structure.
    reply_path: where the reply text is in the response, as names and list indexes joined by dots, for example
             data.reply.text or messages.0.content. A list of strings at the path is joined with new lines.
    session_path: optional: where the response names the server's session ID; later turns of the session send it
             as {session_id}. Without it, the harness makes one session ID for each session and sends that.
    blocked_path: optional: where the response says that the reply was blocked (a true value means blocked)
    block_type_path, model_path, finish_path: optional: where the response names the block type, the model and
             the finish reason
    headers: optional fixed headers that hold no key, for example {X-Channel: web}. A key header goes through
             auth_header and the key variable, never here.
    auth_header: the header that carries the key (default Authorization)
    auth_scheme: Bearer (default for Authorization) or none (default for any other header)

What the channel shows: what the paths name, the host that answered, the HTTP status and the time. A path that the
response does not hold gives null, except reply_path: a response without it is a channel error.
"""
import copy
import re
import secrets

from .base import HEADER_NAME, ChannelError, ChannelTarget, host_entry

OPTION_KEYS = ("method", "request", "reply_path", "session_path", "blocked_path", "block_type_path", "model_path",
               "finish_path", "headers", "auth_header", "auth_scheme")
KEY_HEADERS = {"authorization", "proxy-authorization", "api-key", "x-api-key", "cookie", "ocp-apim-subscription-key",
               "x-functions-key", "x-auth-token"}
PATH = re.compile(r"[A-Za-z0-9_\-$@]+(\.[A-Za-z0-9_\-$@]+)*")
_MISSING = object()


def _has_placeholder(node, name):
    if isinstance(node, str):
        return "{" + name + "}" in node
    if isinstance(node, dict):
        return any(_has_placeholder(v, name) for v in node.values())
    if isinstance(node, list):
        return any(_has_placeholder(v, name) for v in node)
    return False


def fill(template, values):
    """A copy of the template with {name} filled in every string. A string that is exactly {name} becomes the value.

    One pass over each string, so text inside a filled value (for example a message that itself says
    "{session_id}") is never filled again.
    """
    rx = re.compile(r"\{(" + "|".join(re.escape(n) for n in values) + r")\}")

    def walk(node):
        if isinstance(node, str):
            m = rx.fullmatch(node)
            if m:
                return values[m.group(1)]
            return rx.sub(lambda mm: "" if values[mm.group(1)] is None else str(values[mm.group(1)]), node)
        if isinstance(node, dict):
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(copy.deepcopy(template))


def get_path(data, path):
    """The value at a dotted path (names and list indexes), or _MISSING."""
    node = data
    for part in path.split("."):
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return _MISSING
    return node


def validate_options(options, profile_settings):
    problems = []
    o = options or {}
    for k in o:
        if k not in OPTION_KEYS:
            problems.append(f"adapter_options: unknown field '{k}' for json-http (known: {', '.join(OPTION_KEYS)})")
    if o.get("method", "POST") not in ("POST", "PUT"):
        problems.append("adapter_options.method must be POST or PUT")
    req = o.get("request")
    if not isinstance(req, dict) or not req:
        problems.append("adapter_options.request must be a mapping (the JSON body)")
    elif not _has_placeholder(req, "message"):
        problems.append("adapter_options.request must hold {message} in one of its strings")
    if not (isinstance(o.get("reply_path"), str) and PATH.fullmatch(o["reply_path"])):
        problems.append("adapter_options.reply_path must be names and list indexes joined by dots, for example data.reply.text")
    for k in ("session_path", "blocked_path", "block_type_path", "model_path", "finish_path"):
        if o.get(k) is not None and not (isinstance(o[k], str) and PATH.fullmatch(o[k])):
            problems.append(f"adapter_options.{k} must be names and list indexes joined by dots")
    h = o.get("headers")
    if h is not None:
        if not isinstance(h, dict) or not all(isinstance(k, str) and HEADER_NAME.fullmatch(k) and isinstance(v, str)
                                              for k, v in h.items()):
            problems.append("adapter_options.headers must map header names to text values")
        else:
            bad = sorted(k for k in h if k.lower() in KEY_HEADERS)
            if bad:
                problems.append(f"adapter_options.headers may not hold a key header ({', '.join(bad)}); use "
                                "auth_header and settings.api_key_env")
    ah = o.get("auth_header", "Authorization")
    if not (isinstance(ah, str) and HEADER_NAME.fullmatch(ah)):
        problems.append("adapter_options.auth_header must be a header name")
    if o.get("auth_scheme", "Bearer") not in ("Bearer", "none"):
        problems.append("adapter_options.auth_scheme must be Bearer or none")
    return problems


def hosts(endpoint, options):
    return [host_entry(endpoint)]


class JsonHttpTarget(ChannelTarget):
    adapter_id = "json-http"

    def __init__(self, endpoint, egress, **kw):
        super().__init__(endpoint, egress, **kw)
        o = self.options
        self.method = o.get("method", "POST")
        self.template = o.get("request") or {}
        self.reply_path = o.get("reply_path")
        self.session_path = o.get("session_path")
        self.paths = {k: o.get(k) for k in ("blocked_path", "block_type_path", "model_path", "finish_path")}
        self.headers = dict(o.get("headers") or {})
        self.auth_header = o.get("auth_header", "Authorization")
        self.auth_scheme = o.get("auth_scheme", "Bearer" if self.auth_header.lower() == "authorization" else "none")
        self._sessions = {}

    def open_session(self):
        sid = "j" + secrets.token_hex(6)
        # without a session path, the harness's own ID is the session ID; with one, the server names it later
        self._sessions[sid] = {"server_id": None if self.session_path else sid}
        return sid

    def _opt(self, data, key):
        path = self.paths.get(key)
        if not path:
            return None
        v = get_path(data, path)
        return None if v is _MISSING else v

    def send(self, session_id, message):
        ex = self._new_exchange()
        s = self._sessions[session_id]
        body = fill(self.template, {"message": message, "session_id": s["server_id"]})
        headers = {**self.headers, **self._auth_headers(self.auth_header, self.auth_scheme)}
        t0 = self.clock()
        r = self._send_with_retry(self.method, self.endpoint, json=body, headers=headers)
        ex["latency_ms"] = round((self.clock() - t0) * 1000)
        if r.status_code in (401, 403):
            raise ChannelError("auth", f"the endpoint returned HTTP {r.status_code}", r.status_code)
        if r.status_code == 404:
            raise ChannelError("config", "the endpoint returned HTTP 404", 404)
        if r.status_code >= 400:
            raise ChannelError("agent_error" if r.status_code >= 500 else "channel_error",
                               f"the endpoint returned HTTP {r.status_code}", r.status_code)
        data = self._json(r)
        reply = get_path(data, self.reply_path)
        if reply is _MISSING:
            raise ChannelError("channel_error", f"the response holds nothing at reply_path {self.reply_path}")
        if isinstance(reply, list):
            reply = "\n".join(str(x) for x in reply if x is not None)
        elif reply is None:
            reply = ""
        elif not isinstance(reply, str):
            reply = str(reply)
        if self.session_path:
            sid = get_path(data, self.session_path)
            if sid is not _MISSING and sid is not None:
                s["server_id"] = sid
        blocked = self._opt(data, "blocked_path")
        if blocked:
            ex["blocked"] = True
            ex["block_type"] = self._opt(data, "block_type_path") or "json_http_blocked"
        ex["served_model"] = self._opt(data, "model_path")
        ex["finish_reason"] = self._opt(data, "finish_path")
        ex["ids"] = {"session": s["server_id"]}
        return reply

    def close_session(self, session_id):
        self._sessions.pop(session_id, None)
