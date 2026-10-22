"""Adapter for a Responses API channel: the OpenAI Responses API and endpoints that follow it, for example Azure AI
Foundry agents reached through a project's Responses endpoint.

UNTESTED against a platform. Tested only against a local stand-in (tests/standins.py).

It offers `conversation` only. Each `send` is one POST to `<endpoint>/responses`. Within one session, the second
and later turns send `previous_response_id`, unless `chain: false`.

Profile:
  adapter: openai-responses
  endpoint: the base URL; the adapter posts to <endpoint>/responses
  settings.api_key_env: the variable that holds the key or bearer token (optional; omit it for no auth header)
  adapter_options:
    body: fixed fields merged into every request, for example {model: <deployment>} or
          {agent: {name: <agent name>, type: agent_reference}}. It may not hold `input`, `previous_response_id`,
          `stream` or a key-like field.
    query: fixed query parameters, for example {api-version: <version>}
    auth_header: the header that carries the key: Authorization (default), api-key, Ocp-Apim-Subscription-Key or another
    auth_scheme: Bearer (default for Authorization) or none (default for any other header)
    max_output_tokens: the output limit sent with each request (default 400)
    store: true or false (default false); with chain: true, set it to true so the service keeps the earlier turn
    chain: true (default) or false

What the channel shows:
- the reply text (`output_text` parts of `message` items), the model that the response names, the token use,
  the status and any incomplete reason;
- a refusal part, an incomplete reason `content_filter`, or an HTTP 400 whose error code is `content_filter` is
  recorded as blocked; an incomplete reason `max_output_tokens` is recorded as finish reason `length`.
It does not show the host that served the model, so `served_host` is the channel host.
"""
from urllib.parse import urlencode

from .base import HEADER_NAME, ChannelError, ChannelTarget, host_entry

OPTION_KEYS = ("body", "query", "auth_header", "auth_scheme", "max_output_tokens", "store", "chain")
BODY_FORBIDDEN = {"input", "previous_response_id", "stream", "api_key", "key", "secret", "token", "password"}
BLOCK_CODES = {"content_filter", "content_policy_violation"}


def validate_options(options, profile_settings):
    problems = []
    o = options or {}
    for k in o:
        if k not in OPTION_KEYS:
            problems.append(f"adapter_options: unknown field '{k}' for openai-responses (known: {', '.join(OPTION_KEYS)})")
    body = o.get("body")
    if not isinstance(body, dict) or not body:
        problems.append("adapter_options.body must be a mapping that names the model or the agent")
    else:
        bad = sorted(set(body) & BODY_FORBIDDEN)
        if bad:
            problems.append(f"adapter_options.body may not hold: {', '.join(bad)}")
    q = o.get("query")
    if q is not None and not (isinstance(q, dict) and all(isinstance(v, (str, int)) for v in q.values())):
        problems.append("adapter_options.query must be a mapping of names to text values")
    ah = o.get("auth_header", "Authorization")
    if not (isinstance(ah, str) and HEADER_NAME.fullmatch(ah)):
        problems.append("adapter_options.auth_header must be a header name, for example Authorization, api-key "
                        "or Ocp-Apim-Subscription-Key")
    if o.get("auth_scheme", "Bearer") not in ("Bearer", "none"):
        problems.append("adapter_options.auth_scheme must be Bearer or none")
    mot = o.get("max_output_tokens")
    if mot is not None and not (isinstance(mot, int) and not isinstance(mot, bool) and 16 <= mot <= 32768):
        problems.append("adapter_options.max_output_tokens must be a whole number from 16 to 32768")
    for k in ("store", "chain"):
        if o.get(k) is not None and not isinstance(o[k], bool):
            problems.append(f"adapter_options.{k} must be true or false")
    return problems


def hosts(endpoint, options):
    return [host_entry(endpoint)]


def output_text(data):
    """(text, refusal) from a Responses API result."""
    texts, refusals = [], []
    for item in data.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                refusals.append(part["refusal"])
    return "\n".join(texts), "\n".join(refusals)


class ResponsesTarget(ChannelTarget):
    adapter_id = "openai-responses"

    def __init__(self, endpoint, egress, **kw):
        super().__init__(endpoint, egress, **kw)
        o = self.options
        self.body = dict(o.get("body") or {})
        self.query = dict(o.get("query") or {})
        self.auth_header = o.get("auth_header", "Authorization")
        self.auth_scheme = o.get("auth_scheme", "Bearer" if self.auth_header.lower() == "authorization" else "none")
        self.max_output_tokens = o.get("max_output_tokens", 400)
        self.store = bool(o.get("store", False))
        self.chain = bool(o.get("chain", True))
        self._sessions = {}
        self._n = 0

    def open_session(self):
        self._n += 1
        sid = f"r{self._n}"
        self._sessions[sid] = {"previous": None}
        return sid

    def send(self, session_id, message):
        ex = self._new_exchange()
        s = self._sessions[session_id]
        body = {**self.body, "input": message, "max_output_tokens": self.max_output_tokens, "store": self.store}
        if self.chain and s["previous"]:
            body["previous_response_id"] = s["previous"]
        url = self.endpoint + "/responses" + (("?" + urlencode(self.query)) if self.query else "")
        headers = self._auth_headers(self.auth_header, self.auth_scheme)
        t0 = self.clock()
        r = self._send_with_retry("POST", url, json=body, headers=headers)
        ex["latency_ms"] = round((self.clock() - t0) * 1000)
        data = self._json(r)
        if r.status_code == 400:
            code = ((data.get("error") or {}).get("code") if isinstance(data, dict) else None) or ""
            if code in BLOCK_CODES:
                ex["blocked"], ex["block_type"] = True, code
                return ""
        if r.status_code in (401, 403):
            raise ChannelError("auth", f"the Responses endpoint returned HTTP {r.status_code}", r.status_code)
        if r.status_code == 404:
            raise ChannelError("config", "the Responses endpoint returned HTTP 404", 404)
        if r.status_code >= 400:
            raise ChannelError("agent_error" if r.status_code >= 500 else "channel_error",
                               f"the Responses endpoint returned HTTP {r.status_code}", r.status_code)
        text, refusal = output_text(data)
        ex["served_model"] = data.get("model")
        u = data.get("usage") or {}
        ex["usage"] = {"input_tokens": u.get("input_tokens"), "output_tokens": u.get("output_tokens")} if u else None
        ex["ids"] = {"response": data.get("id")}
        reason = (data.get("incomplete_details") or {}).get("reason")
        if refusal and not text:
            ex["blocked"], ex["block_type"] = True, "refusal"
        if reason in BLOCK_CODES:
            ex["blocked"], ex["block_type"] = True, reason
        elif reason == "max_output_tokens":
            ex["finish_reason"] = "length"
        ex["status"] = data.get("status")
        if data.get("id"):
            s["previous"] = data["id"]
        return text

    def close_session(self, session_id):
        self._sessions.pop(session_id, None)
