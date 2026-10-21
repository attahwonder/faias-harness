"""C-04, minimal: the model gateway.

It calls the hosted model through OpenRouter, by a pinned model ID. It does this on every call:
- It sends the routing locks of the settings file with the request (`provider`: only, allow_fallbacks, zdr,
  data_collection, quantizations).
- It reads the key from the environment variable that the settings name. The key goes only in the
  Authorization header. It is never logged and never put in an error message.
- It records the model that the response names, the host that served the call (the `provider` field of the
  response), the token use and the time.
- It sends the request through the shared egress client, so a request to a host off the allow-list is refused.
- It counts the cost from the token use and the settings prices, and stops before a call that could pass the cap.

In the live runs of 20 and 21 October 2026 the response carried the field `provider` (the host's display name) on
every call. When it does not, the served host is recorded as null and the call counts as a host mismatch.
"""
import json
import os
import re
import time
from dataclasses import dataclass, field

import httpx

from faias_harness.errors import CapReached, EgressRefused, GatewayError

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class CapMeter:
    """Counts cost from token use. `check` raises CapReached when the next call could pass the cap."""

    def __init__(self, cap_usd, price_in_per_m, price_out_per_m, max_tokens):
        self.cap_usd = float(cap_usd)
        self.price_in = float(price_in_per_m)
        self.price_out = float(price_out_per_m)
        self.max_tokens = int(max_tokens)
        self.spent_usd = 0.0
        self.calls = 0

    def cost(self, prompt_tokens, completion_tokens):
        return (prompt_tokens * self.price_in + completion_tokens * self.price_out) / 1_000_000

    def worst_case(self, est_prompt_tokens):
        """The cost of the next call if it uses all its output tokens."""
        return self.cost(est_prompt_tokens, self.max_tokens)

    def check(self, est_prompt_tokens):
        worst = self.worst_case(est_prompt_tokens)
        if self.spent_usd + worst > self.cap_usd:
            raise CapReached(f"the next call could cost up to {worst:.6f} USD; spent {self.spent_usd:.6f} of "
                             f"{self.cap_usd:.6f} USD; the call was not sent")

    def charge_unknown(self, est_prompt_tokens):
        """Count a failed attempt that the host may have charged for, at its worst case. Not a call."""
        c = self.worst_case(est_prompt_tokens)
        self.spent_usd += c
        return c

    def add(self, prompt_tokens, completion_tokens):
        c = self.cost(prompt_tokens, completion_tokens)
        self.spent_usd += c
        self.calls += 1
        return c


def estimate_tokens(messages, tools=None):
    """A cautious estimate: about 3 characters to the token, plus a small allowance per message."""
    blob = json.dumps({"m": messages, "t": tools or []}, ensure_ascii=False)
    return len(blob) // 3 + 8 * len(messages) + 16


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def host_matches(served, only):
    """True if the served host equals one locked host. A slug such as 'name/precision' is compared by its name."""
    if not served:
        return False
    return any(_norm(served) == _norm(str(o).split("/")[0]) for o in only)


@dataclass
class GatewayResult:
    reply: str
    model_requested: str
    model_served: object
    served_host: object
    host_ok: bool
    prompt_tokens: int
    completion_tokens: int
    usage_missing: bool
    cost_usd: float
    latency_ms: float
    message: dict = field(default_factory=dict)
    seed: object = None
    retries: int = 0
    retry_reasons: list = field(default_factory=list)
    finish_reason: object = None
    blocked: object = None      # the block type when a filter or refusal declined the call, else None


# Retries: only failures that OpenRouter documents as temporary are retried
# (openrouter.ai/docs/api-reference/errors, read 20 Oct 2026):
#   HTTP 408 request timeout, 429 rate limit, 502 provider unavailable, 503 provider overloaded or no provider
#   available, and the generic server errors 500 and 504;
#   a 200 response whose body carries an error of a temporary type (rate_limit_exceeded, provider_overloaded,
#   provider_unavailable, timeout, server);
#   a timeout or a dropped connection on the way.
# Never retried: 400, 401, 402, 403 (a block is recorded as a result; see block_type), 404, 413, 422, and an empty
# reply (a retry does not help; the documentation says to raise max_tokens). Each call is retried at most
# RETRY_LIMIT times, waiting the Retry-After seconds when given (at most RETRY_MAX_WAIT_S), or else 2, 4, 8, 16, 32
# seconds. Every attempt goes through the egress client and is logged. A retried attempt that the host may have
# processed (all but 429 and 503) is counted against the cap at its worst case, and the cap is checked before each
# retry, so a retry cannot pass the cap.
RETRY_HTTP = {408: "http_408", 429: "http_429", 500: "http_500", 502: "http_502", 503: "http_503", 504: "http_504"}
NOT_PROCESSED = {429, 503}
RETRY_ERROR_TYPES = {"rate_limit_exceeded", "provider_overloaded", "provider_unavailable", "timeout", "server"}
RETRY_LIMIT = 5
RETRY_MAX_WAIT_S = 60.0


def max_call_seconds(attempt_timeout_s):
    """The longest time one `complete` call can take: every attempt at its timeout, and every wait at its maximum.
    A caller that waits for the gateway (the harness's call to SRD-1) must wait longer than this."""
    return (RETRY_LIMIT + 1) * attempt_timeout_s + RETRY_LIMIT * RETRY_MAX_WAIT_S


def retry_wait(attempt, retry_after):
    """Seconds to wait before retry number `attempt` (1 to RETRY_LIMIT)."""
    try:
        s = float(retry_after)
        if s >= 0:
            return min(s, RETRY_MAX_WAIT_S)
    except (TypeError, ValueError):
        pass
    return min(2.0 ** attempt, RETRY_MAX_WAIT_S)


# Blocks (see docs/ARCHITECTURE.md): OpenRouter answers a request that a content filter, a
# guardrail or a provider refusal declined with HTTP 403, and a refusal that the model gives as its own output with
# finish reason content_filter. These are the system declining the request, so the call returns a result with
# `blocked` set. A 403 for a spent budget or key limit (metadata.limit_source) or for a missing permission is not a
# block, and stops the run.
BLOCK_ERROR_TYPES = {"content_policy_violation", "refusal"}


def block_type(resp):
    """The block type of a 403 response, or None if the 403 is not a block."""
    try:
        err = resp.json().get("error")
    except (ValueError, AttributeError):
        return None
    if not isinstance(err, dict):
        return None
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    if meta.get("limit_source"):
        return None
    et = meta.get("error_type") or err.get("error_type")
    if et in BLOCK_ERROR_TYPES:
        return str(et)
    if str(err.get("message") or "").startswith("Request blocked"):
        return "guardrail"
    return None


def body_error_type(data):
    """The typed error in a 200 body, or None."""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return None
    meta = err.get("metadata") if isinstance(err.get("metadata"), dict) else {}
    return str(meta.get("error_type") or err.get("error_type") or err.get("type") or "unknown")


class Gateway:
    def __init__(self, settings, egress, *, env=None, base_url=OPENROUTER_BASE, cap=None, sleep=None):
        if not settings.model_id:
            raise ValueError("the gateway needs settings.model_id")
        if cap is None:
            if None in (settings.cap_usd, settings.price_in_per_m, settings.price_out_per_m):
                raise ValueError("the gateway needs cap_usd and both prices; it makes no call without a cap")
            cap = CapMeter(settings.cap_usd, settings.price_in_per_m, settings.price_out_per_m, settings.max_tokens)
        self.settings = settings
        self.egress = egress
        self.base_url = base_url.rstrip("/")
        self.cap = cap
        self._env = env if env is not None else os.environ
        self._sleep = sleep or time.sleep

    def next_seed(self):
        """The seed for the next call: the base seed + the number of calls made, or None if no seed is set."""
        return None if self.settings.seed is None else self.settings.seed + self.cap.calls

    def request_body(self, messages, tools=None, seed=None):
        body = {"model": self.settings.model_id, "messages": messages, "max_tokens": self.settings.max_tokens,
                "provider": self.settings.routing()}
        if self.settings.temperature is not None:
            body["temperature"] = self.settings.temperature
        if seed is not None:
            body["seed"] = seed
        if tools:
            body["tools"] = tools
        return body

    def complete(self, messages, *, tools=None):
        key = self._env.get(self.settings.api_key_env, "")
        if not key:
            raise GatewayError(f"the environment variable {self.settings.api_key_env} is not set")
        self.cap.check(estimate_tokens(messages, tools))
        seed = self.next_seed()
        body = self.request_body(messages, tools, seed)
        est = estimate_tokens(messages, tools)
        retries, reasons = 0, []
        while True:
            reason, wait_hint, processed = None, None, True
            t0 = time.perf_counter()
            try:
                resp = self.egress.request("POST", self.base_url + "/chat/completions", json=body,
                                           headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
            except EgressRefused:
                raise
            except (httpx.TimeoutException, httpx.TransportError) as e:
                resp, reason = None, "network_" + type(e).__name__
            except Exception as e:      # noqa: BLE001 - only the class name is kept, so no detail can hold a key
                raise GatewayError(f"the call failed: {type(e).__name__}") from None
            latency_ms = (time.perf_counter() - t0) * 1000
            data = None
            if resp is not None:
                if resp.status_code in RETRY_HTTP:
                    reason, wait_hint = RETRY_HTTP[resp.status_code], resp.headers.get("Retry-After")
                    processed = resp.status_code not in NOT_PROCESSED
                elif resp.status_code == 200:
                    try:
                        data = resp.json()
                    except ValueError:
                        data = None
                    et = body_error_type(data)
                    if et is not None and et in RETRY_ERROR_TYPES:
                        reason = "body_" + et
            if reason is None:
                break
            if retries >= RETRY_LIMIT:
                raise GatewayError(f"the call failed after {retries} retries: {reason}")
            if processed:       # the host may have charged for it: count the worst case
                self.cap.charge_unknown(est)
            retries += 1
            reasons.append(reason)
            self.cap.check(est)
            self._sleep(retry_wait(retries, wait_hint))
        if resp.status_code == 403 and (block := block_type(resp)) is not None:
            return self._blocked(resp, block, messages, tools, est, seed, retries, reasons, latency_ms)
        if resp.status_code != 200:
            raise GatewayError(f"the model host returned HTTP {resp.status_code}")
        try:
            if data is None:
                data = resp.json()
            message = data["choices"][0]["message"]
            finish = data["choices"][0].get("finish_reason")
        except (ValueError, KeyError, IndexError, TypeError):
            raise GatewayError("the model host returned a response in an unexpected form") from None
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        pt, ct = usage.get("prompt_tokens"), usage.get("completion_tokens")
        usage_missing = not (isinstance(pt, int) and isinstance(ct, int))
        if usage_missing:       # count the worst case, so a missing field cannot hide spend
            pt, ct = estimate_tokens(messages, tools), self.settings.max_tokens
        cost = self.cap.add(pt, ct)
        served = data.get("provider")
        if isinstance(served, dict):
            served = served.get("name")
        return GatewayResult(
            reply=message.get("content") or "", model_requested=self.settings.model_id,
            model_served=data.get("model"), served_host=served, host_ok=host_matches(served, self.settings.only),
            prompt_tokens=pt, completion_tokens=ct, usage_missing=usage_missing, cost_usd=cost,
            latency_ms=round(latency_ms, 3), message=message, seed=seed, retries=retries,
            retry_reasons=reasons, finish_reason=finish,
            blocked="content_filter" if finish == "content_filter" else None)

    def _blocked(self, resp, block, messages, tools, est, seed, retries, reasons, latency_ms):
        """A 403 block: no reply. The host may have charged for it, so it counts at worst case. The served host is
        the provider that the error names, or None (host not reported); None is not a host mismatch."""
        cost = self.cap.charge_unknown(est)
        self.cap.calls += 1
        try:
            meta = resp.json()["error"].get("metadata") or {}
        except (ValueError, KeyError, AttributeError, TypeError):
            meta = {}
        served = meta.get("provider_name") if isinstance(meta, dict) else None
        return GatewayResult(
            reply="", model_requested=self.settings.model_id, model_served=None, served_host=served,
            host_ok=host_matches(served, self.settings.only) if served else None,
            prompt_tokens=est, completion_tokens=self.settings.max_tokens, usage_missing=True, cost_usd=cost,
            latency_ms=round(latency_ms, 3), message={}, seed=seed, retries=retries, retry_reasons=reasons,
            finish_reason=None, blocked=block)
