"""Adapter for a Direct Line 3.0 channel: Microsoft Copilot Studio agents and Azure Bot Service bots.

UNTESTED against a platform. Tested only against a local stand-in Direct Line service (tests/standins.py).

It offers `conversation` only. One harness session is one Direct Line conversation.

Profile:
  adapter: directline
  endpoint: the Direct Line base URI of the agent's region, for example https://europe.directline.botframework.com
            (a regional agent can refuse a request sent to the global base URI)
  settings.api_key_env: with `auth: secret`, the variable that holds the Direct Line (or web channel) secret
  adapter_options:
    auth: secret | token_endpoint
    token_endpoint: with `auth: token_endpoint`, the agent's token endpoint (Copilot Studio: Channels, Mobile app)
    locale: the locale sent with each message (default en-GB)
    start_event: true to send the startConversation event first, so the greeting topic runs (default false);
                 the greeting is read (for at most 10 seconds) and dropped, and the event counts as a call
    poll_ms: the wait between reads of the activity set (default 500)
    settle_ms: after the first reply, how long no new reply must arrive before the reply is complete (default 1500)

What the channel shows, and what it does not:
- It shows the reply activities, their IDs and types, the host that answered, the HTTP statuses and the time.
- It does not show the model, the tokens, the cost or the platform's content filter. A reply that the platform
  replaced with a fallback message is a reply: the harness cannot tell it from a defence. `blocked` stays None.
- A reply is the text of every message activity from the agent that answers the harness's message (replyToId),
  with the text blocks of any attached card, joined by new lines. Other activity types are recorded by name
  in `last_exchange` and not read.
"""
import secrets

from .base import ChannelError, ChannelTarget, host_entry

API = "/v3/directline"
AUTH_MODES = ("secret", "token_endpoint")
OPTION_KEYS = ("auth", "token_endpoint", "locale", "start_event", "poll_ms", "settle_ms")
FATAL_STATUSES = (401, 403, 404)
GREETING_WAIT_S = 10.0      # the longest wait for a greeting after the start event
READ_RETRIES = 3            # a read of the activity set that gets HTTP 429 or 503 is tried again, without charge


def validate_options(options, profile_settings):
    problems = []
    o = options or {}
    for k in o:
        if k not in OPTION_KEYS:
            problems.append(f"adapter_options: unknown field '{k}' for directline (known: {', '.join(OPTION_KEYS)})")
    auth = o.get("auth")
    if auth not in AUTH_MODES:
        problems.append(f"adapter_options.auth must be one of {', '.join(AUTH_MODES)}")
    if auth == "secret" and not (profile_settings or {}).get("api_key_env"):
        problems.append("directline with auth: secret needs settings.api_key_env (the variable that holds the secret)")
    if auth == "token_endpoint":
        te = o.get("token_endpoint")
        if not (isinstance(te, str) and te.startswith(("http://", "https://"))):
            problems.append("directline with auth: token_endpoint needs adapter_options.token_endpoint (a URL)")
    for k, lo, hi in (("poll_ms", 50, 10000), ("settle_ms", 0, 30000)):
        v = o.get(k)
        if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi):
            problems.append(f"adapter_options.{k} must be a whole number from {lo} to {hi}")
    if o.get("start_event") is not None and not isinstance(o["start_event"], bool):
        problems.append("adapter_options.start_event must be true or false")
    if o.get("locale") is not None and not (isinstance(o["locale"], str) and o["locale"].strip()):
        problems.append("adapter_options.locale must be a non-empty string")
    return problems


def hosts(endpoint, options):
    out = [host_entry(endpoint)]
    if (options or {}).get("auth") == "token_endpoint" and options.get("token_endpoint"):
        out.append(host_entry(options["token_endpoint"]))
    return out


def card_text(content, depth=0):
    """The text of an Adaptive Card or a hero card, depth-first, bounded."""
    if depth > 8 or not isinstance(content, (dict, list)):
        return []
    if isinstance(content, list):
        return [t for c in content for t in card_text(c, depth + 1)]
    out = []
    for k in ("title", "subtitle", "text"):
        if isinstance(content.get(k), str) and content[k].strip():
            out.append(content[k])
    for k in ("body", "items", "columns", "facts", "content"):
        if k in content:
            out += card_text(content[k], depth + 1)
    if content.get("type") == "Fact" or ("title" in content and "value" in content):
        if isinstance(content.get("value"), str):
            out.append(content["value"])
    return out


def activity_text(activity):
    parts = [activity["text"]] if isinstance(activity.get("text"), str) and activity["text"].strip() else []
    for att in activity.get("attachments") or []:
        if isinstance(att, dict):
            parts += card_text(att.get("content"))
    return "\n".join(parts)


class DirectLineTarget(ChannelTarget):
    adapter_id = "directline"

    def __init__(self, endpoint, egress, **kw):
        super().__init__(endpoint, egress, **kw)
        o = self.options
        self.auth = o.get("auth")
        self.locale = o.get("locale", "en-GB")
        self.start_event = bool(o.get("start_event", False))
        self.poll_s = o.get("poll_ms", 500) / 1000
        self.settle_s = o.get("settle_ms", 1500) / 1000
        self._sessions = {}

    # ---- token and conversation ----

    def _check(self, r, step):
        if r.status_code in FATAL_STATUSES:
            data = self._json(r)
            code = (data.get("error") or {}).get("code") if isinstance(data, dict) else None
            raise ChannelError("auth" if r.status_code in (401, 403) else "config",
                               f"Direct Line {step} returned HTTP {r.status_code}" + (f" ({code})" if code else ""),
                               r.status_code)
        if r.status_code >= 400:
            raise ChannelError("agent_error" if r.status_code == 502 else "channel_error",
                               f"Direct Line {step} returned HTTP {r.status_code}", r.status_code)
        return self._json(r)

    def _token(self, user_id):
        if self.auth == "token_endpoint":
            data = self._check(self._request("GET", self.options["token_endpoint"], timeout=30), "token endpoint")
        else:
            data = self._check(self._request("POST", self.endpoint + API + "/tokens/generate",
                                             json={"user": {"id": user_id, "name": "FAIAS harness"}},
                                             headers=self._auth_headers(), timeout=30), "token generation")
        token = data.get("token") if isinstance(data, dict) else None
        if not token:
            raise ChannelError("channel_error", "Direct Line returned no token")
        return token

    def open_session(self):
        self._new_exchange()
        user_id = "dl_faias_" + secrets.token_hex(6)
        token = self._token(user_id)
        data = self._check(self._request("POST", self.endpoint + API + "/conversations",
                                         headers={"Authorization": f"Bearer {token}"}, timeout=30),
                           "conversation start")
        cid = data.get("conversationId")
        if not cid:
            raise ChannelError("channel_error", "Direct Line returned no conversation ID")
        token = data.get("token") or token
        sid = "dl" + secrets.token_hex(6)
        self._sessions[sid] = {"cid": cid, "token": token, "user": user_id, "watermark": None}
        if self.start_event:
            self._post_activity(sid, {"type": "event", "name": "startConversation"})
            self._read_replies(sid, None, timeout_s=min(self.reply_timeout_s, GREETING_WAIT_S))  # read and dropped
        return sid

    # ---- messages ----

    def _post_activity(self, sid, activity):
        s = self._sessions[sid]
        body = {**activity, "from": {"id": s["user"], "name": "FAIAS harness"}, "locale": self.locale}
        r = self._send_with_retry("POST", f"{self.endpoint}{API}/conversations/{s['cid']}/activities",
                                  json=body, headers={"Authorization": f"Bearer {s['token']}"})
        data = self._check(r, "send")
        return data.get("id") if isinstance(data, dict) else None

    def _read(self, url, token):
        """One read of the activity set. HTTP 429 and 503 are tried again (the reads are not charged)."""
        for attempt in range(READ_RETRIES + 1):
            r = self._request("GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
            if r.status_code not in (429, 503) or attempt == READ_RETRIES:
                return self._check(r, "read")
            self.last_exchange["retry_reasons"].append(f"read_http_{r.status_code}")
            self.sleep(self._wait_s(r, attempt))

    def _read_replies(self, sid, sent_id, timeout_s=None):
        """Read the activity set until the reply is complete, the conversation ends or the timeout passes."""
        s = self._sessions[sid]
        ex = self.last_exchange
        texts, reply_ids, types = [], [], {}
        deadline = self.clock() + (self.reply_timeout_s if timeout_s is None else timeout_s)
        last_new, ended, polls = None, False, 0
        while True:
            url = f"{self.endpoint}{API}/conversations/{s['cid']}/activities"
            if s["watermark"] is not None:
                url += f"?watermark={s['watermark']}"
            data = self._read(url, s["token"])
            polls += 1
            for a in data.get("activities") or []:
                if not isinstance(a, dict) or (a.get("from") or {}).get("id") == s["user"]:
                    continue
                t = str(a.get("type"))
                types[t] = types.get(t, 0) + 1
                if t == "endOfConversation":
                    ended = True
                if t != "message":
                    continue
                if sent_id is not None and a.get("replyToId") not in (None, sent_id):
                    continue
                text = activity_text(a)
                if text:
                    texts.append(text)
                reply_ids.append(a.get("id"))
                last_new = self.clock()
            if data.get("watermark") is not None:
                s["watermark"] = data["watermark"]
            now = self.clock()
            if ended or (last_new is not None and now - last_new >= self.settle_s):
                break
            if now >= deadline:
                if last_new is None:
                    ex["finish_reason"] = "timeout"
                break
            self.sleep(self.poll_s)
        if ended:
            ex["finish_reason"] = "end_of_conversation"
        ex["ids"] = {"conversation": s["cid"], "sent": sent_id, "replies": reply_ids}
        ex["activity_types"] = types
        ex["polls"] = polls
        return "\n".join(texts)

    def send(self, session_id, message):
        self._new_exchange()
        t0 = self.clock()
        sent_id = self._post_activity(session_id, {"type": "message", "text": message, "textFormat": "plain"})
        reply = self._read_replies(session_id, sent_id)
        self.last_exchange["latency_ms"] = round((self.clock() - t0) * 1000)
        return reply

    def close_session(self, session_id):
        # Direct Line has no call that ends a conversation for a client; the token expires on its own.
        self._sessions.pop(session_id, None)
