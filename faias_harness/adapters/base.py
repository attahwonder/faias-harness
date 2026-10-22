"""Shared parts of the channel adapters: one base class, one error, a call budget and a retry rule.

A channel adapter reaches a deployed staging agent through the channel that the agent's own front end uses (a
Direct Line conversation, a Responses API call, an A2A message, a chat-completions call). The harness is then a
client of that channel. It sees what the channel returns and nothing behind it.

Rules every channel adapter follows:
- Every request goes through the shared egress client, so the allow-list applies.
- A key comes from the environment variable that the profile names. The key never goes in a file, a log, an error
  message or `last_exchange`.
- Before each attempt that may reach the agent, the adapter calls `before_send`. The engine's call budget uses
  it to stop a run before a call that would pass the cap.
- After each `send`, `last_exchange` holds what the channel showed for that call: the host that answered, the HTTP
  statuses, the time taken, the retries, and any block or finish signal. It never holds a header or a key.
"""
import os
import re
import time
from urllib.parse import urlsplit

import httpx

from ..errors import CapReached
from ..target import Target

HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]{1,64}")   # an HTTP header name (RFC 9110 token)
RETRY_STATUSES = (429, 503)      # the request was not processed: safe to send again
MAX_RETRY_AFTER_S = 60.0
BACKOFF_S = (2.0, 4.0, 8.0)


class ChannelError(Exception):
    """A channel call failed. `kind` names the cause; `status` is the HTTP status or None. Never holds a key."""

    def __init__(self, kind, message, status=None):
        self.kind, self.status = kind, status
        super().__init__(message)


class CallBudget:
    """The budget of a run against an external target.

    The harness cannot see what the target's platform bills. The operator states an estimate of the cost of one
    call (`cost_per_call_usd`) and a cap (`cap_usd`). The budget allows floor(cap / cost) calls, or fewer if
    `max_calls` is set. `charge` raises CapReached before a call that would pass the budget; it sends nothing.
    """

    def __init__(self, cap_usd, cost_per_call_usd, max_calls=None, calls=0):
        self.cap_usd, self.cost_per_call_usd, self.max_calls = cap_usd, cost_per_call_usd, max_calls
        self.calls = calls

    @property
    def call_budget(self):
        by_cap = int(self.cap_usd / self.cost_per_call_usd + 1e-9)
        return by_cap if self.max_calls is None else min(by_cap, self.max_calls)

    @property
    def spent_usd(self):
        return round(self.calls * self.cost_per_call_usd, 8)

    def charge(self):
        if self.calls + 1 > self.call_budget:
            raise CapReached(f"the next call would pass the call budget of {self.call_budget}")
        self.calls += 1


def host_entry(url):
    """'host' or 'host:port' for the allow-list, from a URL."""
    p = urlsplit(url)
    return (p.hostname or "") + (f":{p.port}" if p.port else "")


def key_from_env(env_name, env=None):
    """The value of the named variable. The error names the variable, never a value."""
    env = os.environ if env is None else env
    value = env.get(env_name or "", "")
    if not value:
        raise ChannelError("key_missing", f"the environment variable {env_name} is not set")
    return value


class ChannelTarget(Target):
    """Base class of the channel adapters. It offers `conversation` only."""

    capabilities = frozenset({"conversation"})
    adapter_id = "channel"

    def __init__(self, endpoint, egress, *, options=None, api_key_env=None, reply_timeout_s=90.0,
                 before_send=None, env=None, sleep=time.sleep, clock=time.monotonic, retries=2):
        if not isinstance(endpoint, str) or not endpoint.startswith(("http://", "https://")):
            raise ValueError("endpoint must start with http:// or https://")
        self.endpoint = endpoint.rstrip("/")
        self.egress = egress
        self.options = dict(options or {})
        self.api_key_env = api_key_env
        self.reply_timeout_s = float(reply_timeout_s)
        self.before_send = before_send or (lambda: None)
        self.env = os.environ if env is None else env
        self.sleep, self.clock = sleep, clock
        self.retries = retries
        self.last_exchange = {}

    # ---- helpers for subclasses ----

    def _new_exchange(self):
        self.last_exchange = {"channel_host": None, "statuses": [], "retries": 0, "retry_reasons": [],
                              "latency_ms": None, "blocked": None, "block_type": None, "finish_reason": None,
                              "served_model": None, "usage": None, "ids": {}}
        return self.last_exchange

    def _request(self, method, url, *, json=None, headers=None, timeout=None):
        """One request through the egress client. Records the host and status. Raises ChannelError on transport
        failure. Never puts a header in a message."""
        ex = self.last_exchange
        ex["channel_host"] = host_entry(url)
        try:
            r = self.egress.request(method, url, json=json, headers=headers,
                                    timeout=self.reply_timeout_s if timeout is None else timeout)
        except httpx.TimeoutException:
            raise ChannelError("timeout", f"no response from {host_entry(url)} within the timeout") from None
        except httpx.HTTPError as e:
            raise ChannelError("transport", f"the request to {host_entry(url)} failed ({type(e).__name__})") from None
        ex["statuses"].append(r.status_code)
        return r

    def _send_with_retry(self, method, url, *, json=None, headers=None):
        """A request that may reach the agent. Calls `before_send` before every attempt (each is charged).

        It retries only HTTP 429 and 503, where the channel did not process the request, at most `self.retries`
        times, waiting the Retry-After seconds (at most 60) or 2, 4 and 8 seconds.
        """
        ex = self.last_exchange
        attempt = 0
        while True:
            self.before_send()
            r = self._request(method, url, json=json, headers=headers)
            if r.status_code not in RETRY_STATUSES or attempt >= self.retries:
                return r
            ex["retries"] += 1
            ex["retry_reasons"].append(f"http_{r.status_code}")
            self.sleep(self._wait_s(r, attempt))
            attempt += 1

    @staticmethod
    def _wait_s(response, attempt):
        ra = response.headers.get("Retry-After")
        try:
            return max(0.0, min(float(ra), MAX_RETRY_AFTER_S)) if ra is not None else BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]
        except ValueError:
            return BACKOFF_S[min(attempt, len(BACKOFF_S) - 1)]

    @staticmethod
    def _json(response):
        """The JSON body if it is an object, else {} (a list, a number or text is not a channel response)."""
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _auth_headers(self, header="Authorization", scheme="Bearer"):
        """The auth header from the profile's key variable, or {} if the profile names no key variable."""
        if not self.api_key_env:
            return {}
        key = key_from_env(self.api_key_env, self.env)
        return {header: f"{scheme} {key}" if scheme and scheme != "none" else key}
