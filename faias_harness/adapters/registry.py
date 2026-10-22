"""The channel adapters that an external run can use, by the adapter ID that a profile names.

`srd1-http` is not listed: SRD-1 minimal runs through the SRD-1 engine (engine/spike.py), which builds it in the
same process. Every adapter here reaches a deployed staging agent from outside, through its channel.
"""
from dataclasses import dataclass

from . import a2a, directline, json_http, openai_chat, openai_responses


@dataclass(frozen=True)
class Entry:
    module: object
    cls: type
    channel: str
    observes: str


ADAPTERS = {
    "directline": Entry(directline, directline.DirectLineTarget,
                        "Direct Line 3.0 (Microsoft Copilot Studio agents, Azure Bot Service bots)",
                        "reply text and cards, activity IDs and types, channel host, HTTP status, time"),
    "openai-responses": Entry(openai_responses, openai_responses.ResponsesTarget,
                              "Responses API (OpenAI; Azure AI Foundry agents through a project's Responses endpoint)",
                              "reply text, refusal and content-filter signals, model named in the response, token use, "
                              "channel host, HTTP status, time"),
    "a2a": Entry(a2a, a2a.A2ATarget, "Agent2Agent (A2A) JSON-RPC, protocol version 1.0 or 0.3",
                 "reply text, task state, task and context IDs, channel host, HTTP status, time"),
    "json-http": Entry(json_http, json_http.JsonHttpTarget,
                       "templated JSON over HTTP (a firm's own chat endpoint in front of its app's model or agent)",
                       "reply text at the configured path; block, model and finish signals where configured paths "
                       "name them; channel host, HTTP status, time"),
    "generic-http": Entry(openai_chat, openai_chat.OpenAIChatTarget, "OpenAI-compatible chat completions",
                          "reply text, finish reason, model named in the response, token use, channel host, HTTP status, time"),
}


def is_external(adapter_id):
    return adapter_id in ADAPTERS


def problems(profile):
    """Why this profile cannot be used for an external run (empty list if it can)."""
    out = []
    if profile.adapter not in ADAPTERS:
        return [f"adapter '{profile.adapter}' cannot drive an external run (known: {', '.join(sorted(ADAPTERS))})"]
    if not profile.endpoint:
        out.append("an external run needs the profile's endpoint")
    if "conversation" not in profile.capabilities:
        out.append("an external run needs the capability conversation in the profile")
    out += ADAPTERS[profile.adapter].module.validate_options(profile.adapter_options, profile.settings)
    return out


def allow_hosts(profile):
    """The hosts that the run may contact: the endpoint and any host the adapter options name."""
    return ADAPTERS[profile.adapter].module.hosts(profile.endpoint, profile.adapter_options or {})


def key_env(profile):
    """The environment variable that must be set for the run, or None if the adapter needs no key."""
    env = (profile.settings or {}).get("api_key_env")
    if profile.adapter == "directline":
        return env if (profile.adapter_options or {}).get("auth") == "secret" else None
    return env


def build(profile, egress, **kw):
    """A target for the profile. `kw` passes reply_timeout_s, before_send and, for tests, env, sleep and clock."""
    entry = ADAPTERS[profile.adapter]
    extra = {"model_id": (profile.settings or {}).get("model_id")} if profile.adapter == "generic-http" else {}
    return entry.cls(profile.endpoint, egress, options=profile.adapter_options or {},
                     api_key_env=key_env(profile), **extra, **kw)
