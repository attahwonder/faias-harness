"""Load and validate a settings file for an engine run. See docs/ARCHITECTURE.md, section 12.

A settings file holds no key. It names the environment variable that holds the key (api_key_env).
Two modes:
  explore   a trial. The routing locks may be partly set.
  evidence  a run that is kept as evidence. All five routing locks must be set, with these values:
            routing.only (a non-empty list of hosts), routing.allow_fallbacks false, routing.zdr true,
            routing.data_collection "deny", routing.quantizations (a non-empty list).
            routing.only holds one endpoint only, so two runs cannot reach different endpoints.
            temperature and seed must be set. The gateway sends seed + call number with each call.

The validator never copies the value of a rejected field into a message, so a key typed in the wrong field
is not repeated.
"""
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

TOP_REQUIRED = ["provider", "model_id", "mode", "routing", "price_in_per_m", "price_out_per_m", "cap_usd",
                "api_key_env"]
TOP_OPTIONAL = ["max_tokens", "reviewer_values", "temperature", "seed", "runs_per_item"]
RUNS_MIN, RUNS_MAX, RUNS_DEFAULT = 3, 20, 3     # three is the FAIAS minimum (PLAYBOOK_SEC_001.md line 27)
ROUTING_KEYS = ["only", "allow_fallbacks", "zdr", "data_collection", "quantizations"]
FORBIDDEN = {"api_key", "key", "secret", "token", "password"}
MODES = ("explore", "evidence")
DEFAULT_MAX_TOKENS = 400
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


class SettingsError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass(frozen=True)
class Settings:
    provider: str
    model_id: object
    mode: str
    only: tuple
    allow_fallbacks: object
    zdr: object
    data_collection: object
    quantizations: tuple
    price_in_per_m: object
    price_out_per_m: object
    cap_usd: object
    api_key_env: str
    max_tokens: int
    m4_value: object
    m4_reason: object
    temperature: object = None
    seed: object = None
    runs_per_item: int = RUNS_DEFAULT

    def routing(self):
        """The routing locks that are set, in the form that the provider-selection field takes."""
        r = {}
        if self.only:
            r["only"] = list(self.only)
        if self.allow_fallbacks is not None:
            r["allow_fallbacks"] = self.allow_fallbacks
        if self.zdr is not None:
            r["zdr"] = self.zdr
        if self.data_collection is not None:
            r["data_collection"] = self.data_collection
        if self.quantizations:
            r["quantizations"] = list(self.quantizations)
        return r

    def canonical(self):
        """The validated content as one dict. Its hash is the settings hash."""
        return {
            "provider": self.provider, "model_id": self.model_id, "mode": self.mode,
            "routing": self.routing(), "price_in_per_m": self.price_in_per_m,
            "price_out_per_m": self.price_out_per_m, "cap_usd": self.cap_usd,
            "api_key_env": self.api_key_env, "max_tokens": self.max_tokens,
            "temperature": self.temperature, "seed": self.seed, "runs_per_item": self.runs_per_item,
            "reviewer_values": {"M4": {"value": self.m4_value, "reason": self.m4_reason}},
        }

    def digest(self):
        blob = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def key_present(self):
        """True if the environment variable is set and not empty. The value is never read into a message."""
        return bool(os.environ.get(self.api_key_env))


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def validate(raw):
    """Return the list of problems in a loaded settings mapping (empty if none)."""
    if not isinstance(raw, dict):
        return ["the settings are not a mapping"]
    problems = []
    for k in TOP_REQUIRED:
        if k not in raw:
            problems.append(f"missing field '{k}'")
    for k in raw:
        if k in FORBIDDEN:
            problems.append(f"'{k}': a key never goes in a settings file; name an environment variable in api_key_env")
        elif k not in TOP_REQUIRED and k not in TOP_OPTIONAL:
            problems.append(f"unknown field '{k}'")

    mode = raw.get("mode")
    if "mode" in raw and mode not in MODES:
        problems.append(f"mode must be one of {', '.join(MODES)}")
    evidence = mode == "evidence"

    if "provider" in raw and raw["provider"] != "openrouter":
        problems.append("provider must be 'openrouter'")
    if "model_id" in raw and raw["model_id"] is not None and not (isinstance(raw["model_id"], str) and raw["model_id"].strip()):
        problems.append("model_id must be a non-empty string or null")
    for k in ("price_in_per_m", "price_out_per_m", "cap_usd"):
        if raw.get(k) is not None and not (_num(raw[k]) and raw[k] > 0):
            problems.append(f"{k} must be a number above 0 or null")
    env = raw.get("api_key_env")
    if "api_key_env" in raw and not (isinstance(env, str) and ENV_NAME.fullmatch(env)):
        problems.append("api_key_env must be the NAME of an environment variable (letters, digits, underscore)")
    mt = raw.get("max_tokens")
    if "max_tokens" in raw and not (isinstance(mt, int) and not isinstance(mt, bool) and 1 <= mt <= 4096):
        problems.append("max_tokens must be a whole number from 1 to 4096")
    k = raw.get("runs_per_item")
    if "runs_per_item" in raw and not (isinstance(k, int) and not isinstance(k, bool) and RUNS_MIN <= k <= RUNS_MAX):
        problems.append(f"runs_per_item must be a whole number from {RUNS_MIN} (the FAIAS minimum) to {RUNS_MAX}")
    t = raw.get("temperature")
    if t is not None and not (_num(t) and 0 <= t <= 2):
        problems.append("temperature must be a number from 0 to 2 or null")
    sd = raw.get("seed")
    if sd is not None and not (isinstance(sd, int) and not isinstance(sd, bool) and 0 <= sd < 2**31):
        problems.append("seed must be a whole number from 0 to 2147483647 or null")

    routing = raw.get("routing")
    if "routing" in raw:
        if not isinstance(routing, dict):
            problems.append("routing must be a mapping")
            routing = {}
        for k in routing:
            if k in FORBIDDEN:
                problems.append(f"routing.{k}: a key never goes in a settings file")
            elif k not in ROUTING_KEYS:
                problems.append(f"routing: unknown field '{k}'")
        only = routing.get("only")
        if only is not None and not (isinstance(only, list) and all(isinstance(x, str) and x.strip() for x in only)):
            problems.append("routing.only must be a list of host names")
        qs = routing.get("quantizations")
        if qs is not None and not (isinstance(qs, list) and all(isinstance(x, str) and x.strip() for x in qs)):
            problems.append("routing.quantizations must be a list of precision names")
        for k in ("allow_fallbacks", "zdr"):
            if routing.get(k) is not None and not isinstance(routing[k], bool):
                problems.append(f"routing.{k} must be true or false")
        dc = routing.get("data_collection")
        if dc is not None and dc not in ("allow", "deny"):
            problems.append("routing.data_collection must be 'allow' or 'deny'")
    else:
        routing = {}

    rv = raw.get("reviewer_values")
    m4 = None
    if "reviewer_values" in raw and rv is not None:
        if not isinstance(rv, dict) or set(rv) - {"M4"}:
            problems.append("reviewer_values may hold M4 only")
        else:
            m4 = rv.get("M4")
            if m4 is not None:
                if not isinstance(m4, dict) or set(m4) - {"value", "reason"}:
                    problems.append("reviewer_values.M4 must hold 'value' and 'reason'")
                else:
                    if m4.get("value") is not None and not (_num(m4["value"]) and 0 <= m4["value"] <= 1):
                        problems.append("reviewer_values.M4.value must be a number from 0 to 1")
                    if m4.get("reason") is not None and not (isinstance(m4["reason"], str) and m4["reason"].strip()):
                        problems.append("reviewer_values.M4.reason must be a non-empty string")

    if evidence:
        # The five routing locks: each must be set, and set to the locked value.
        only = routing.get("only")
        if not only:
            problems.append("evidence mode: routing.only is not set (a non-empty list of hosts)")
        elif isinstance(only, list) and len(only) != 1:
            problems.append("evidence mode: routing.only must hold exactly one endpoint")
        if routing.get("allow_fallbacks") is None:
            problems.append("evidence mode: routing.allow_fallbacks is not set (it must be false)")
        elif routing["allow_fallbacks"] is not False:
            problems.append("evidence mode: routing.allow_fallbacks must be false")
        if routing.get("zdr") is None:
            problems.append("evidence mode: routing.zdr is not set (it must be true)")
        elif routing["zdr"] is not True:
            problems.append("evidence mode: routing.zdr must be true")
        if routing.get("data_collection") is None:
            problems.append("evidence mode: routing.data_collection is not set (it must be 'deny')")
        elif routing["data_collection"] != "deny":
            problems.append("evidence mode: routing.data_collection must be 'deny'")
        if not routing.get("quantizations"):
            problems.append("evidence mode: routing.quantizations is not set (a non-empty list)")
        if not (isinstance(raw.get("model_id"), str) and raw["model_id"].strip()):
            problems.append("evidence mode: model_id is not set")
        for k in ("price_in_per_m", "price_out_per_m", "cap_usd"):
            if raw.get(k) is None:
                problems.append(f"evidence mode: {k} is not set")
        for k in ("temperature", "seed"):
            if raw.get(k) is None:
                problems.append(f"evidence mode: {k} is not set")
        if not isinstance(m4, dict) or m4.get("value") is None or m4.get("reason") is None:
            problems.append("evidence mode: reviewer_values.M4 needs a value and a reason")
    return problems


def from_raw(raw):
    """Build a Settings from a mapping that has passed validate()."""
    r = raw.get("routing") or {}
    m4 = (raw.get("reviewer_values") or {}).get("M4") or {}
    return Settings(
        provider=raw["provider"], model_id=raw["model_id"], mode=raw["mode"],
        only=tuple(r.get("only") or ()), allow_fallbacks=r.get("allow_fallbacks"), zdr=r.get("zdr"),
        data_collection=r.get("data_collection"), quantizations=tuple(r.get("quantizations") or ()),
        price_in_per_m=raw["price_in_per_m"], price_out_per_m=raw["price_out_per_m"], cap_usd=raw["cap_usd"],
        api_key_env=raw["api_key_env"], max_tokens=raw.get("max_tokens", DEFAULT_MAX_TOKENS),
        m4_value=m4.get("value"), m4_reason=m4.get("reason"),
        temperature=raw.get("temperature"), seed=raw.get("seed"),
        runs_per_item=raw.get("runs_per_item", RUNS_DEFAULT),
    )


def load_settings(path):
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise SettingsError([f"cannot read {path.name}: {type(e).__name__}"])
    problems = validate(raw)
    if problems:
        raise SettingsError(problems)
    return from_raw(raw)
