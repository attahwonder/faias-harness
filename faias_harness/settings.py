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

A settings file with `kind: external` is the settings file of an external run (version 0.5): a run against a
deployed staging agent through a channel adapter (docs/ARCHITECTURE.md, section 13). It has no provider and no
routing locks, because the harness does not make the agent's model calls. Its budget is a cap in USD and the
operator's estimate of the cost of one call; the run stops before a call that would pass floor(cap / cost), or
`max_calls` if that is lower. In evidence mode it must declare the model and the version of the target, and the
M4 value with its reason. The declared values are recorded as declared, not observed.

The setting `procedure` (version 0.6) chooses the procedure of an SRD-1 run: TP-SEC-001 (the default) or
TP-SEC-006 (docs/ARCHITECTURE.md, section 15). Only TP-SEC-006 takes `store_replies` (full or hash_only),
`m4_baseline_log` and `reviewer_values.M3`. In evidence mode it needs the baseline log and the M3 and M4 values
with their reasons. A TP-SEC-001 file keeps its settings hash.
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
TOP_OPTIONAL = ["max_tokens", "reviewer_values", "temperature", "seed", "runs_per_item", "item_set", "kind",
                "procedure", "store_replies", "m4_baseline_log"]
RUNS_MIN, RUNS_MAX, RUNS_DEFAULT = 3, 20, 3     # three is the FAIAS minimum (PLAYBOOK_SEC_001.md line 27)
# The item set of a run: the full set in items/TP-SEC-001.json (the default), or the 20 items of the engine spike.
ITEM_SETS, ITEM_SET_DEFAULT = ("TP-SEC-001", "spike"), "TP-SEC-001"
# The procedure of an SRD-1 run (version 0.6). TP-SEC-006 runs its own item set, and only it takes store_replies,
# m4_baseline_log and a reviewer value for M3.
PROCEDURES, PROCEDURE_DEFAULT, SEC006 = ("TP-SEC-001", "TP-SEC-006"), "TP-SEC-001", "TP-SEC-006"
STORE_REPLIES = ("full", "hash_only")
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
    item_set: str = ITEM_SET_DEFAULT
    procedure: str = PROCEDURE_DEFAULT
    store_replies: str = "full"
    m4_baseline_log: object = None
    m3_value: object = None
    m3_reason: object = None
    kind = "srd1"           # a class attribute: not in the canonical content, so the settings hash is unchanged

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
        """The validated content as one dict. Its hash is the settings hash. The TP-SEC-006 fields are in it only
        for a TP-SEC-006 run, so the settings hash of a TP-SEC-001 run is unchanged."""
        out = {
            "provider": self.provider, "model_id": self.model_id, "mode": self.mode,
            "routing": self.routing(), "price_in_per_m": self.price_in_per_m,
            "price_out_per_m": self.price_out_per_m, "cap_usd": self.cap_usd,
            "api_key_env": self.api_key_env, "max_tokens": self.max_tokens,
            "temperature": self.temperature, "seed": self.seed, "runs_per_item": self.runs_per_item,
            "item_set": self.item_set,
            "reviewer_values": {"M4": {"value": self.m4_value, "reason": self.m4_reason}},
        }
        if self.procedure == SEC006:
            out.update(procedure=self.procedure, store_replies=self.store_replies, m4_baseline_log=self.m4_baseline_log)
            out["reviewer_values"]["M3"] = {"value": self.m3_value, "reason": self.m3_reason}
        return out

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
    if "kind" in raw and raw["kind"] != "srd1":
        problems.append("kind must be 'srd1' in a settings file for SRD-1 minimal (an external run uses kind: external)")

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
    proc = raw.get("procedure", PROCEDURE_DEFAULT)
    if proc not in PROCEDURES:
        problems.append(f"procedure must be one of {', '.join(PROCEDURES)}")
    sec006 = proc == SEC006
    if sec006:
        if "item_set" in raw and raw["item_set"] != SEC006:
            problems.append("item_set must be TP-SEC-006 for the procedure TP-SEC-006")
        if "store_replies" in raw and raw["store_replies"] not in STORE_REPLIES:
            problems.append(f"store_replies must be one of {', '.join(STORE_REPLIES)}")
        bl = raw.get("m4_baseline_log")
        if bl is not None and not (isinstance(bl, str) and bl.strip()):
            problems.append("m4_baseline_log must be the path of a TP-SEC-001 run log, or null")
    else:
        if "item_set" in raw and raw["item_set"] not in ITEM_SETS:
            problems.append(f"item_set must be one of {', '.join(ITEM_SETS)}")
        for k in ("store_replies", "m4_baseline_log"):
            if k in raw:
                problems.append(f"{k} applies to the procedure TP-SEC-006 only")
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
    m4 = m3 = None
    allowed = {"M3", "M4"} if sec006 else {"M4"}
    if "reviewer_values" in raw and rv is not None:
        if not isinstance(rv, dict) or set(rv) - allowed:
            problems.append("reviewer_values may hold M3 and M4 only" if sec006 else "reviewer_values may hold M4 only")
        else:
            m4, m3 = rv.get("M4"), rv.get("M3")
            for name, m in (("M3", m3), ("M4", m4)):
                if m is None:
                    continue
                if not isinstance(m, dict) or set(m) - {"value", "reason"}:
                    problems.append(f"reviewer_values.{name} must hold 'value' and 'reason'")
                else:
                    if m.get("value") is not None and not (_num(m["value"]) and 0 <= m["value"] <= 1):
                        problems.append(f"reviewer_values.{name}.value must be a number from 0 to 1")
                    if m.get("reason") is not None and not (isinstance(m["reason"], str) and m["reason"].strip()):
                        problems.append(f"reviewer_values.{name}.reason must be a non-empty string")

    if evidence and sec006:
        if not isinstance(m3, dict) or m3.get("value") is None or m3.get("reason") is None:
            problems.append("evidence mode: reviewer_values.M3 needs a value and a reason")
        if not raw.get("m4_baseline_log"):
            problems.append("evidence mode: m4_baseline_log is not set (the TP-SEC-001 run log that M4 compares with)")
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
    m3 = (raw.get("reviewer_values") or {}).get("M3") or {}
    proc = raw.get("procedure", PROCEDURE_DEFAULT)
    return Settings(
        provider=raw["provider"], model_id=raw["model_id"], mode=raw["mode"],
        only=tuple(r.get("only") or ()), allow_fallbacks=r.get("allow_fallbacks"), zdr=r.get("zdr"),
        data_collection=r.get("data_collection"), quantizations=tuple(r.get("quantizations") or ()),
        price_in_per_m=raw["price_in_per_m"], price_out_per_m=raw["price_out_per_m"], cap_usd=raw["cap_usd"],
        api_key_env=raw["api_key_env"], max_tokens=raw.get("max_tokens", DEFAULT_MAX_TOKENS),
        m4_value=m4.get("value"), m4_reason=m4.get("reason"),
        temperature=raw.get("temperature"), seed=raw.get("seed"),
        runs_per_item=raw.get("runs_per_item", RUNS_DEFAULT),
        item_set=raw.get("item_set", SEC006 if proc == SEC006 else ITEM_SET_DEFAULT),
        procedure=proc, store_replies=raw.get("store_replies", "full"), m4_baseline_log=raw.get("m4_baseline_log"),
        m3_value=m3.get("value"), m3_reason=m3.get("reason"),
    )


def load_settings(path):
    """Load a settings file: SRD-1 minimal (no `kind`, or `kind: srd1`) or an external run (`kind: external`)."""
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise SettingsError([f"cannot read {path.name}: {type(e).__name__}"])
    if isinstance(raw, dict) and raw.get("kind") == "external":
        problems = validate_external(raw)
        if problems:
            raise SettingsError(problems)
        return external_from_raw(raw)
    problems = validate(raw)
    if problems:
        raise SettingsError(problems)
    return from_raw(raw)


# ---------- settings of an external run (version 0.5) ----------

EXT_REQUIRED = ["kind", "mode", "cap_usd", "cost_per_call_usd"]
EXT_OPTIONAL = ["item_set", "runs_per_item", "max_calls", "reply_timeout_s", "min_interval_ms",
                "max_consecutive_errors", "declared", "reviewer_values"]
EXT_DEFAULTS = {"reply_timeout_s": 90, "min_interval_ms": 500, "max_consecutive_errors": 5}
DECLARED_KEYS = ("model_id", "target_version")


@dataclass(frozen=True)
class ExternalSettings:
    mode: str
    cap_usd: float
    cost_per_call_usd: float
    item_set: str = ITEM_SET_DEFAULT
    runs_per_item: int = RUNS_DEFAULT
    max_calls: object = None
    reply_timeout_s: float = EXT_DEFAULTS["reply_timeout_s"]
    min_interval_ms: int = EXT_DEFAULTS["min_interval_ms"]
    max_consecutive_errors: int = EXT_DEFAULTS["max_consecutive_errors"]
    declared_model_id: object = None
    declared_target_version: object = None
    m4_value: object = None
    m4_reason: object = None
    kind = "external"

    @property
    def call_budget(self):
        by_cap = int(self.cap_usd / self.cost_per_call_usd + 1e-9)
        return by_cap if self.max_calls is None else min(by_cap, self.max_calls)

    def canonical(self):
        return {
            "kind": "external", "mode": self.mode, "item_set": self.item_set, "runs_per_item": self.runs_per_item,
            "cap_usd": self.cap_usd, "cost_per_call_usd": self.cost_per_call_usd, "max_calls": self.max_calls,
            "reply_timeout_s": self.reply_timeout_s, "min_interval_ms": self.min_interval_ms,
            "max_consecutive_errors": self.max_consecutive_errors,
            "declared": {"model_id": self.declared_model_id, "target_version": self.declared_target_version},
            "reviewer_values": {"M4": {"value": self.m4_value, "reason": self.m4_reason}},
        }

    def digest(self):
        blob = json.dumps(self.canonical(), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _key_like(node):
    if isinstance(node, dict):
        return any(str(k).lower() in FORBIDDEN or _key_like(v) for k, v in node.items())
    if isinstance(node, list):
        return any(_key_like(v) for v in node)
    return False


def _whole(v, lo, hi):
    return isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def validate_external(raw):
    """Return the list of problems in the settings of an external run (empty if none)."""
    if not isinstance(raw, dict):
        return ["the settings are not a mapping"]
    problems = []
    for k in EXT_REQUIRED:
        if k not in raw:
            problems.append(f"missing field '{k}'")
    for k in raw:
        if k in FORBIDDEN:
            problems.append(f"'{k}': a key never goes in a settings file; the profile names the key variable")
        elif k not in EXT_REQUIRED and k not in EXT_OPTIONAL:
            problems.append(f"unknown field '{k}' (an external run has no provider, routing or price fields)")
    if _key_like({k: v for k, v in raw.items() if k not in FORBIDDEN}):
        problems.append("a nested field has a key-like name; a key never goes in a settings file")
    mode = raw.get("mode")
    if "mode" in raw and mode not in MODES:
        problems.append(f"mode must be one of {', '.join(MODES)}")
    for k in ("cap_usd", "cost_per_call_usd"):
        if k in raw and not (_num(raw[k]) and raw[k] > 0):
            problems.append(f"{k} must be a number above 0")
    if _num(raw.get("cap_usd")) and _num(raw.get("cost_per_call_usd")) and raw["cost_per_call_usd"] > 0 \
            and raw["cap_usd"] > 0 and raw["cap_usd"] < raw["cost_per_call_usd"]:
        problems.append("cap_usd is below cost_per_call_usd: the budget allows no call")
    if "max_calls" in raw and raw["max_calls"] is not None and not _whole(raw["max_calls"], 1, 1_000_000):
        problems.append("max_calls must be a whole number from 1 to 1000000 or null")
    if "runs_per_item" in raw and not _whole(raw["runs_per_item"], RUNS_MIN, RUNS_MAX):
        problems.append(f"runs_per_item must be a whole number from {RUNS_MIN} (the FAIAS minimum) to {RUNS_MAX}")
    if "item_set" in raw and raw["item_set"] not in ITEM_SETS:
        problems.append(f"item_set must be one of {', '.join(ITEM_SETS)}")
    if "reply_timeout_s" in raw and not (_num(raw["reply_timeout_s"]) and 5 <= raw["reply_timeout_s"] <= 600):
        problems.append("reply_timeout_s must be a number from 5 to 600")
    if "min_interval_ms" in raw and not _whole(raw["min_interval_ms"], 0, 60000):
        problems.append("min_interval_ms must be a whole number from 0 to 60000")
    if "max_consecutive_errors" in raw and not _whole(raw["max_consecutive_errors"], 1, 100):
        problems.append("max_consecutive_errors must be a whole number from 1 to 100")
    dec = raw.get("declared")
    if dec is not None:
        if not isinstance(dec, dict) or set(dec) - set(DECLARED_KEYS):
            problems.append(f"declared may hold {', '.join(DECLARED_KEYS)} only")
            dec = {}
        for k in DECLARED_KEYS:
            if dec.get(k) is not None and not (isinstance(dec[k], str) and dec[k].strip()):
                problems.append(f"declared.{k} must be a non-empty string or null")
    dec = dec if isinstance(dec, dict) else {}
    rv = raw.get("reviewer_values")
    m4 = None
    if rv is not None:
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
    if mode == "evidence":
        for k in DECLARED_KEYS:
            if not (isinstance(dec.get(k), str) and dec[k].strip()):
                problems.append(f"evidence mode: declared.{k} is not set (the operator states it; it is recorded as declared)")
        if not isinstance(m4, dict) or m4.get("value") is None or m4.get("reason") is None:
            problems.append("evidence mode: reviewer_values.M4 needs a value and a reason")
    return problems


def external_from_raw(raw):
    """Build an ExternalSettings from a mapping that has passed validate_external()."""
    dec = raw.get("declared") or {}
    m4 = (raw.get("reviewer_values") or {}).get("M4") or {}
    return ExternalSettings(
        mode=raw["mode"], cap_usd=raw["cap_usd"], cost_per_call_usd=raw["cost_per_call_usd"],
        item_set=raw.get("item_set", ITEM_SET_DEFAULT), runs_per_item=raw.get("runs_per_item", RUNS_DEFAULT),
        max_calls=raw.get("max_calls"),
        reply_timeout_s=raw.get("reply_timeout_s", EXT_DEFAULTS["reply_timeout_s"]),
        min_interval_ms=raw.get("min_interval_ms", EXT_DEFAULTS["min_interval_ms"]),
        max_consecutive_errors=raw.get("max_consecutive_errors", EXT_DEFAULTS["max_consecutive_errors"]),
        declared_model_id=dec.get("model_id"), declared_target_version=dec.get("target_version"),
        m4_value=m4.get("value"), m4_reason=m4.get("reason"),
    )
