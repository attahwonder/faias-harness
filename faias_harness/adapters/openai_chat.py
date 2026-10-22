"""Adapter for an OpenAI-compatible chat-completions channel (`generic-http`).

UNTESTED against a platform. Tested only against a local stand-in (tests/standins.py). Before version 0.5 this
module called the endpoint with the standard library, outside the egress client; it now goes through the shared
egress client like every other adapter.

It offers `conversation` only. A session keeps its own message history and sends it with each turn.

Profile:
  adapter: generic-http
  endpoint: the base URL; the adapter posts to <endpoint>/chat/completions
  settings.model_id: the model or deployment name sent in each request (or adapter_options.model)
  settings.api_key_env: the variable that holds the key (optional; omit it for no auth header)
  adapter_options:
    model: the model name, if it differs from settings.model_id
    params: fixed fields merged into every request, for example {temperature: 0}; no messages, model or key fields
    auth_header: Authorization (default) or api-key
    auth_scheme: Bearer (default) or none

What the channel shows: the reply text, the finish reason, the model that the response names and the token use.
A finish reason `content_filter` is recorded as blocked; `length` stays as the finish reason.
"""
from .base import ChannelError, ChannelTarget, host_entry

OPTION_KEYS = ("model", "params", "auth_header", "auth_scheme")
PARAMS_FORBIDDEN = {"messages", "model", "stream", "api_key", "key", "secret", "token", "password"}


def validate_options(options, profile_settings):
    problems = []
    o = options or {}
    for k in o:
        if k not in OPTION_KEYS:
            problems.append(f"adapter_options: unknown field '{k}' for generic-http (known: {', '.join(OPTION_KEYS)})")
    if not (o.get("model") or (profile_settings or {}).get("model_id")):
        problems.append("generic-http needs a model name: settings.model_id or adapter_options.model")
    p = o.get("params")
    if p is not None:
        if not isinstance(p, dict):
            problems.append("adapter_options.params must be a mapping")
        elif set(p) & PARAMS_FORBIDDEN:
            problems.append(f"adapter_options.params may not hold: {', '.join(sorted(set(p) & PARAMS_FORBIDDEN))}")
    if o.get("auth_header", "Authorization") not in ("Authorization", "api-key"):
        problems.append("adapter_options.auth_header must be Authorization or api-key")
    if o.get("auth_scheme", "Bearer") not in ("Bearer", "none"):
        problems.append("adapter_options.auth_scheme must be Bearer or none")
    return problems


def hosts(endpoint, options):
    return [host_entry(endpoint)]


class OpenAIChatTarget(ChannelTarget):
    adapter_id = "generic-http"

    def __init__(self, endpoint, egress, *, model_id=None, **kw):
        super().__init__(endpoint, egress, **kw)
        o = self.options
        self.model_id = o.get("model") or model_id
        self.params = dict(o.get("params") or {})
        self.auth_header = o.get("auth_header", "Authorization")
        self.auth_scheme = o.get("auth_scheme", "Bearer" if self.auth_header == "Authorization" else "none")
        self._sessions = {}
        self._n = 0

    def open_session(self):
        self._n += 1
        sid = f"s{self._n}"
        self._sessions[sid] = []
        return sid

    def send(self, session_id, message):
        ex = self._new_exchange()
        history = self._sessions[session_id]
        body = {**self.params, "model": self.model_id, "messages": history + [{"role": "user", "content": message}]}
        t0 = self.clock()
        r = self._send_with_retry("POST", self.endpoint + "/chat/completions", json=body,
                                  headers=self._auth_headers(self.auth_header, self.auth_scheme))
        ex["latency_ms"] = round((self.clock() - t0) * 1000)
        data = self._json(r)
        if r.status_code == 400 and ((data.get("error") or {}).get("code") if isinstance(data, dict) else None) == "content_filter":
            ex["blocked"], ex["block_type"] = True, "content_filter"
            return ""
        if r.status_code in (401, 403):
            raise ChannelError("auth", f"the endpoint returned HTTP {r.status_code}", r.status_code)
        if r.status_code == 404:
            raise ChannelError("config", "the endpoint returned HTTP 404", 404)
        if r.status_code >= 400:
            raise ChannelError("agent_error" if r.status_code >= 500 else "channel_error",
                               f"the endpoint returned HTTP {r.status_code}", r.status_code)
        try:
            choice = data["choices"][0]
            reply = choice["message"].get("content") or ""
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ChannelError("channel_error", "the endpoint returned no choice") from None
        fr = choice.get("finish_reason")
        if fr == "content_filter":
            ex["blocked"], ex["block_type"] = True, "content_filter"
        else:
            ex["finish_reason"] = fr
        ex["served_model"] = data.get("model")
        u = data.get("usage") or {}
        ex["usage"] = {"input_tokens": u.get("prompt_tokens"), "output_tokens": u.get("completion_tokens")} if u else None
        ex["ids"] = {"response": data.get("id")}
        history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
        return reply

    def close_session(self, session_id):
        self._sessions.pop(session_id, None)
