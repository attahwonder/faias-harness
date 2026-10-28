"""C-02, minimal: the agent loop for one model call per customer turn.

The tool list is configured and kept in the trace, but this version calls no tool. There are no workflow gates,
no re-anchoring and no retry rules. Every turn writes one line to the trace store (C-15), also when the model
call fails.
"""
import hashlib
from pathlib import Path

from faias_harness.errors import CapReached

PROMPT_FILE = Path(__file__).with_name("system_prompt.txt")
# The positive control: the same assistant with no rules. A run with it must find successes.
WEAKENED_PROMPT_FILE = Path(__file__).with_name("system_prompt_weakened.txt")
PROMPT_VARIANTS = {"standard": PROMPT_FILE, "positive_control": WEAKENED_PROMPT_FILE}
MAX_TURNS = 20
MAX_MESSAGE_CHARS = 4000


def load_system_prompt(path=PROMPT_FILE):
    return Path(path).read_text(encoding="utf-8")


class TooManyTurns(Exception):
    pass


class MessageTooLong(Exception):
    pass


class UnknownSession(Exception):
    pass


class Orchestrator:
    def __init__(self, system_prompt, gateway, trace, tools=(), prompt_source=None):
        self.system_prompt = system_prompt
        self.prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        self.prompt_source = prompt_source      # the file name of the prompt, for config_read; None if not known
        self.gateway = gateway
        self.trace = trace
        self.tools = tuple(tools)
        self._sessions = {}
        self._n = 0

    def config(self):
        """What config_read returns: the prompt of the active variant, its SHA-256 and the tools. SRD-1 minimal
        sends no tool definition to the model and injects no templated content at run time."""
        return {"prompt": self.system_prompt, "prompt_sha256": self.prompt_sha256, "prompt_source": self.prompt_source,
                "tools_configured": list(self.tools), "tool_definitions": [], "runtime_templates": []}

    def open_session(self):
        self._n += 1
        sid = f"s{self._n}"
        self._sessions[sid] = {"history": [{"role": "system", "content": self.system_prompt}], "turns": 0}
        self.trace.append("session_open", sid, system_prompt_sha256=self.prompt_sha256, tools_configured=list(self.tools))
        return sid

    def close_session(self, sid):
        if self._sessions.pop(sid, None) is not None:
            self.trace.append("session_close", sid)

    def handle(self, sid, message):
        """Return (reply, trace_entry). A failed call writes a `turn_error` line and raises."""
        s = self._sessions.get(sid)
        if s is None:
            raise UnknownSession(sid)
        if len(message) > MAX_MESSAGE_CHARS:
            raise MessageTooLong(str(len(message)))
        if s["turns"] >= MAX_TURNS:
            raise TooManyTurns(sid)
        s["turns"] += 1
        history = s["history"] + [{"role": "user", "content": message}]
        try:
            r = self.gateway.complete(history)
        except Exception as e:      # noqa: BLE001 - recorded by class name only
            self.trace.append("turn_error", sid, turn=s["turns"], user_message=message, error=type(e).__name__,
                              cap_reached=isinstance(e, CapReached))
            raise
        s["history"] = history + [{"role": "assistant", "content": r.reply}]
        entry = self.trace.append(
            "turn", sid, turn=s["turns"], user_message=message, reply=r.reply,
            model_requested=r.model_requested, model_served=r.model_served, served_host=r.served_host,
            host_ok=r.host_ok, prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
            usage_missing=r.usage_missing, cost_usd=r.cost_usd, latency_ms=r.latency_ms, seed=r.seed, retries=r.retries, retry_reasons=r.retry_reasons,
            finish_reason=r.finish_reason, blocked=r.blocked,
            system_prompt_sha256=self.prompt_sha256, tools_configured=list(self.tools))
        return r.reply, entry
