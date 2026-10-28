"""Load and validate a target profile (one YAML file per target). See docs/ARCHITECTURE.md, section 5.

A profile never holds a key. A key is an environment variable. The profile may name that variable
in settings.api_key_env.
Paths in a profile (firm_values, attestation_file) are relative to the repository root, unless absolute.
The optional `adapter_options` (version 0.5) holds the settings of a channel adapter; the adapter checks them
(adapters/registry.py). A field name from a fixed list of key-like names is refused anywhere in it.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .capabilities import CAPABILITIES

REPO = Path(__file__).resolve().parents[1]

TOP_KEYS = ["name", "adapter", "endpoint", "trace_source", "capabilities", "precondition_map",
            "firm_values", "attestation_file", "settings"]
OPTIONAL_TOP_KEYS = ["status", "adapter_options"]
SETTINGS_KEYS = ["provider", "model_id", "price_in_per_m", "price_out_per_m", "cap_usd"]
OPTIONAL_SETTINGS_KEYS = ["api_key_env"]
FORBIDDEN_SETTINGS = {"api_key", "key", "secret", "token", "password"}
FORBIDDEN_OPTION_KEYS = FORBIDDEN_SETTINGS | {"client_secret", "access_token", "bearer", "authorization", "api-key"}
CATEGORIES = ("environment", "access", "artefacts", "personnel")
STATUSES = ("met", "partly", "not met")


class ProfileError(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


@dataclass
class Profile:
    path: Path
    name: str
    adapter: str
    endpoint: Optional[str]
    trace_source: Optional[str]
    capabilities: tuple
    precondition_map: Optional[dict]
    firm_values: Optional[Path]
    attestation_file: Optional[Path]
    settings: dict
    status: Optional[str] = None
    adapter_options: Optional[dict] = None
    extra: dict = field(default_factory=dict)


def _path(value):
    if value in (None, ""):
        return None
    p = Path(str(value))
    return p if p.is_absolute() else REPO / p


def _is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _key_like_paths(node, trail=""):
    """The dotted paths of key-like field names anywhere in a mapping. Values are never read into a message."""
    out = []
    if isinstance(node, dict):
        for k, v in node.items():
            path = f"{trail}.{k}" if trail else str(k)
            if str(k).lower() in FORBIDDEN_OPTION_KEYS:
                out.append(path)
            out += _key_like_paths(v, path)
    elif isinstance(node, list):
        for v in node:
            out += _key_like_paths(v, trail)
    return out


def validate(raw):
    """Return the list of problems in a loaded profile (empty if none)."""
    problems = []
    if not isinstance(raw, dict):
        return ["the profile is not a mapping"]
    for k in TOP_KEYS:
        if k not in raw:
            problems.append(f"missing field '{k}'")
    for k in raw:
        if k not in TOP_KEYS and k not in OPTIONAL_TOP_KEYS:
            problems.append(f"unknown field '{k}'")
    for k in ("name", "adapter"):
        if k in raw and not (isinstance(raw[k], str) and raw[k].strip()):
            problems.append(f"'{k}' must be a non-empty string")
    for k in ("endpoint", "trace_source", "firm_values", "attestation_file", "status"):
        if raw.get(k) is not None and not isinstance(raw[k], str):
            problems.append(f"'{k}' must be a string or null")
    ep = raw.get("endpoint")
    if isinstance(ep, str) and not ep.startswith(("http://", "https://")):
        problems.append("'endpoint' must start with http:// or https://")
    if isinstance(ep, str) and "?" in ep:
        problems.append("'endpoint' must not have a query string, because a query can hold a key "
                        "(openai-responses takes its query from adapter_options.query)")
    caps = raw.get("capabilities")
    if "capabilities" in raw:
        if not isinstance(caps, list):
            problems.append("'capabilities' must be a list")
        else:
            for c in caps:
                if c not in CAPABILITIES:
                    problems.append(f"unknown capability '{c}' (known: {', '.join(CAPABILITIES)})")
            if len(set(map(str, caps))) != len(caps):
                problems.append("'capabilities' has a duplicate")
    pm = raw.get("precondition_map")
    if pm is not None:
        if not isinstance(pm, dict):
            problems.append("'precondition_map' must be a mapping or null")
        else:
            for tp, cats in pm.items():
                if not isinstance(cats, dict):
                    problems.append(f"precondition_map {tp}: must map categories to items")
                    continue
                for cat, item in cats.items():
                    where = f"precondition_map {tp} {cat}"
                    if cat not in CATEGORIES:
                        problems.append(f"{where}: unknown category (known: {', '.join(CATEGORIES)})")
                    if not isinstance(item, dict) or item.get("status") not in STATUSES:
                        problems.append(f"{where}: status must be one of {', '.join(STATUSES)}")
                    elif not isinstance(item.get("ids", []), list):
                        problems.append(f"{where}: ids must be a list")
    ao = raw.get("adapter_options")
    if ao is not None:
        if not isinstance(ao, dict):
            problems.append("'adapter_options' must be a mapping or null")
        else:
            for path in _key_like_paths(ao):
                problems.append(f"adapter_options.{path}: a key never goes in a profile; name an environment variable in settings.api_key_env")
    st = raw.get("settings")
    if "settings" in raw:
        if not isinstance(st, dict):
            problems.append("'settings' must be a mapping")
        else:
            for k in SETTINGS_KEYS:
                if k not in st:
                    problems.append(f"settings: missing '{k}' (use null if not set)")
            for k in st:
                if k in FORBIDDEN_SETTINGS:
                    problems.append(f"settings.{k}: a key never goes in a profile; name an environment variable in api_key_env")
                elif k not in SETTINGS_KEYS and k not in OPTIONAL_SETTINGS_KEYS:
                    problems.append(f"settings: unknown field '{k}'")
            for k in ("provider", "model_id", "api_key_env"):
                if st.get(k) is not None and not isinstance(st[k], str):
                    problems.append(f"settings.{k} must be a string or null")
            for k in ("price_in_per_m", "price_out_per_m", "cap_usd"):
                if st.get(k) is not None and not _is_number(st[k]):
                    problems.append(f"settings.{k} must be a number or null")
    return problems


def load_profile(path):
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ProfileError([f"cannot read {path}: {e}"])
    problems = validate(raw)
    if problems:
        raise ProfileError(problems)
    return Profile(
        path=path, name=raw["name"], adapter=raw["adapter"], endpoint=raw["endpoint"],
        trace_source=raw["trace_source"], capabilities=tuple(raw["capabilities"]),
        precondition_map=raw["precondition_map"], firm_values=_path(raw["firm_values"]),
        attestation_file=_path(raw["attestation_file"]), settings=dict(raw["settings"]),
        status=raw.get("status"), adapter_options=raw.get("adapter_options"),
    )
