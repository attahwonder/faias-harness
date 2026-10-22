"""Adapter for an Agent2Agent (A2A) channel over JSON-RPC, protocol version 1.0 or 0.3.

UNTESTED against a platform. Tested only against a local stand-in A2A agent (tests/standins.py).

It offers `conversation` only. Each `send` is one A2A message. Within one session, the second and later turns send
the `contextId` that the agent returned. A task that is not yet in a final state is read again with GetTask (1.0)
or tasks/get (0.3) until it is final, needs input, or the timeout passes. The reads are not charged as calls.

Use this adapter only where A2A is the path that the firm's own front end uses to reach the agent. If customers
reach the agent through another channel, a result through A2A is a result for a different path.

Profile:
  adapter: a2a
  endpoint: the agent's JSON-RPC endpoint (not the agent card URL)
  settings.api_key_env: the variable that holds the key or bearer token (optional; omit it for no auth header)
  adapter_options:
    version: "1.0" (default) or "0.3"
    auth_header: the header that carries the key (default Authorization)
    auth_scheme: Bearer (default) or none
    poll_ms: the wait between task reads (default 1000)

Wire forms (1.0, with the header A2A-Version: 1.0): method SendMessage; role ROLE_USER; parts {"text": ...};
result {"task": {...}} or {"message": {...}}; states such as TASK_STATE_COMPLETED.
Wire forms (0.3): method message/send; role user; parts {"kind": "text", "text": ...}; result is the task or message
itself, with `kind`; states such as completed.

What the channel shows: the reply text (artifact text parts, or the text parts of a message, or of the status
message), the task state, the task and context IDs, the host that answered and the time. A task in the state
rejected is recorded as blocked (block type a2a_rejected). It does not show the model, the tokens or the cost.
"""
import secrets

from .base import ChannelError, ChannelTarget, host_entry

OPTION_KEYS = ("version", "auth_header", "auth_scheme", "poll_ms")
VERSIONS = ("1.0", "0.3")
FINAL = {"completed", "failed", "canceled", "rejected"}
INTERRUPTED = {"input-required", "auth-required"}
METHODS = {"1.0": {"send": "SendMessage", "get": "GetTask"}, "0.3": {"send": "message/send", "get": "tasks/get"}}


def validate_options(options, profile_settings):
    problems = []
    o = options or {}
    for k in o:
        if k not in OPTION_KEYS:
            problems.append(f"adapter_options: unknown field '{k}' for a2a (known: {', '.join(OPTION_KEYS)})")
    if str(o.get("version", "1.0")) not in VERSIONS:
        problems.append("adapter_options.version must be \"1.0\" or \"0.3\"")
    ah = o.get("auth_header", "Authorization")
    if not (isinstance(ah, str) and ah.strip() and all(c.isalnum() or c in "-_" for c in ah)):
        problems.append("adapter_options.auth_header must be a header name")
    if o.get("auth_scheme", "Bearer") not in ("Bearer", "none"):
        problems.append("adapter_options.auth_scheme must be Bearer or none")
    v = o.get("poll_ms")
    if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and 100 <= v <= 30000):
        problems.append("adapter_options.poll_ms must be a whole number from 100 to 30000")
    return problems


def hosts(endpoint, options):
    return [host_entry(endpoint)]


def norm_state(state):
    """'TASK_STATE_INPUT_REQUIRED' or 'input-required' -> 'input-required'."""
    s = str(state or "").lower()
    if s.startswith("task_state_"):
        s = s[len("task_state_"):]
    return s.replace("_", "-")


def parts_text(parts):
    out = []
    for p in parts or []:
        if isinstance(p, dict) and isinstance(p.get("text"), str) and p.get("kind", "text") == "text":
            out.append(p["text"])
    return out


def split_result(result):
    """(kind, object) from a send or get result of either version: kind is 'task' or 'message'."""
    if not isinstance(result, dict):
        return None, None
    if isinstance(result.get("task"), dict):
        return "task", result["task"]
    if isinstance(result.get("message"), dict):
        return "message", result["message"]
    if result.get("kind") in ("task", "message"):
        return result["kind"], result
    if "status" in result:
        return "task", result
    if "parts" in result:
        return "message", result
    return None, None


def task_text(task):
    texts = [t for a in task.get("artifacts") or [] if isinstance(a, dict) for t in parts_text(a.get("parts"))]
    if not texts:
        texts = parts_text(((task.get("status") or {}).get("message") or {}).get("parts"))
    return "\n".join(texts)


class A2ATarget(ChannelTarget):
    adapter_id = "a2a"

    def __init__(self, endpoint, egress, **kw):
        super().__init__(endpoint, egress, **kw)
        o = self.options
        self.version = str(o.get("version", "1.0"))
        self.auth_header = o.get("auth_header", "Authorization")
        self.auth_scheme = o.get("auth_scheme", "Bearer")
        self.poll_s = o.get("poll_ms", 1000) / 1000
        self._sessions = {}
        self._rpc_id = 0

    def _headers(self):
        h = self._auth_headers(self.auth_header, self.auth_scheme)
        if self.version == "1.0":
            h["A2A-Version"] = "1.0"
        return h

    def _rpc(self, method, params, *, charged):
        self._rpc_id += 1
        body = {"jsonrpc": "2.0", "id": str(self._rpc_id), "method": method, "params": params}
        if charged:
            r = self._send_with_retry("POST", self.endpoint, json=body, headers=self._headers())
        else:
            r = self._request("POST", self.endpoint, json=body, headers=self._headers(), timeout=30)
        if r.status_code in (401, 403):
            raise ChannelError("auth", f"the A2A endpoint returned HTTP {r.status_code}", r.status_code)
        if r.status_code == 404:
            raise ChannelError("config", "the A2A endpoint returned HTTP 404", 404)
        if r.status_code >= 400:
            raise ChannelError("agent_error" if r.status_code >= 500 else "channel_error",
                               f"the A2A endpoint returned HTTP {r.status_code}", r.status_code)
        data = self._json(r)
        if isinstance(data, dict) and isinstance(data.get("error"), dict):
            code = data["error"].get("code")
            kind = "config" if code == -32601 else "channel_error"
            raise ChannelError(kind, f"the A2A agent returned JSON-RPC error {code}")
        return (data or {}).get("result")

    def _message(self, text, context_id):
        if self.version == "1.0":
            m = {"role": "ROLE_USER", "messageId": secrets.token_hex(8), "parts": [{"text": text}]}
        else:
            m = {"kind": "message", "role": "user", "messageId": secrets.token_hex(8),
                 "parts": [{"kind": "text", "text": text}]}
        if context_id:
            m["contextId"] = context_id
        return m

    def open_session(self):
        sid = "a" + secrets.token_hex(6)
        self._sessions[sid] = {"context": None}
        return sid

    def send(self, session_id, message):
        ex = self._new_exchange()
        s = self._sessions[session_id]
        t0 = self.clock()
        result = self._rpc(METHODS[self.version]["send"], {"message": self._message(message, s["context"])},
                           charged=True)
        kind, obj = split_result(result)
        if kind is None:
            raise ChannelError("channel_error", "the A2A agent returned no task and no message")
        reads = 0
        if kind == "task":
            deadline = self.clock() + self.reply_timeout_s
            while norm_state((obj.get("status") or {}).get("state")) not in FINAL | INTERRUPTED:
                if self.clock() >= deadline:
                    ex["finish_reason"] = "timeout"
                    break
                self.sleep(self.poll_s)
                k2, o2 = split_result(self._rpc(METHODS[self.version]["get"], {"id": obj.get("id")}, charged=False))
                reads += 1
                if k2 == "task":
                    obj = o2
            state = norm_state((obj.get("status") or {}).get("state"))
            text = task_text(obj)
            ex["task_state"] = state
            if state == "rejected":
                ex["blocked"], ex["block_type"] = True, "a2a_rejected"
            elif state in ("failed", "canceled") or state in INTERRUPTED:
                ex["finish_reason"] = state
            ex["ids"] = {"task": obj.get("id"), "context": obj.get("contextId")}
        else:
            text = "\n".join(parts_text(obj.get("parts")))
            ex["ids"] = {"message": obj.get("messageId"), "context": obj.get("contextId")}
        ex["task_reads"] = reads
        if ex["ids"].get("context"):
            s["context"] = ex["ids"]["context"]
        ex["latency_ms"] = round((self.clock() - t0) * 1000)
        return text

    def close_session(self, session_id):
        self._sessions.pop(session_id, None)
