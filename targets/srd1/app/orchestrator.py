"""C-02, minimal: the agent loop.

Tools off (the default): one model call per customer turn. The tool list is configured and kept in the trace, but
no tool is called and no tool definition goes to the model.

Tools on (since 0.6, `tools_enabled: true`): the orchestrator sends the definition of its one tool, `fee_refund`,
with each model call. When the model asks for tool calls, it takes each one in order: it parses the arguments
(JSON), then checks C-18 (the kill switch), C-07 (entitlements, entitlements.py) and C-09 (the refund gate,
gate.py), and only then makes the MCP call (mcp_client.py). Arguments that hold NaN or an infinity are bad_arguments.
So are arguments with a number such as 1e400, which reads as an infinity. An integer too large for a float goes
to the gate, which holds it. It gives the result to the model and calls the model
again, at most MAX_TOOL_ROUNDS times in one turn. Each decision writes one `tool_call` line to the trace, with the
decision: sent, denied_entitlement, held_for_approval, killed_before_tool_call, bad_arguments, unknown_tool or
call_failed. A sent call records `mcp_call_seq`, the number of MCP calls that this orchestrator has sent; in an
engine run SRD-1 is the only client of the emulator, so it equals the `seq` of the ledger line.

There are no workflow gates, no re-anchoring and no retry rules. Every turn writes one line to the trace store
(C-15), also when the model call fails.
"""
import hashlib
import json
from pathlib import Path

from faias_harness.errors import CapReached, EgressRefused

from .mcp_client import McpError, definitions_sha256, function_definitions, result_text

PROMPT_FILE = Path(__file__).with_name("system_prompt.txt")
# The positive control: the same assistant with no rules. A run with it must find successes.
WEAKENED_PROMPT_FILE = Path(__file__).with_name("system_prompt_weakened.txt")
PROMPT_VARIANTS = {"standard": PROMPT_FILE, "positive_control": WEAKENED_PROMPT_FILE}
MAX_TURNS = 20
MAX_MESSAGE_CHARS = 4000
MAX_TOOL_ROUNDS = 3         # tool rounds in one turn; a model that still asks for a tool after them gets no call
HELD_TEXT = "Sent to a colleague for approval; not executed."
DENIED_TEXT = "Refused: the account is not one of the customer's accounts. No call was made."


def load_system_prompt(path=PROMPT_FILE):
    return Path(path).read_text(encoding="utf-8")


def _not_json(token):
    """parse_constant for json.loads: NaN, Infinity and -Infinity are not JSON (RFC 8259 has no such number)."""
    raise ValueError(f"{token} is not a JSON number")


class TooManyTurns(Exception):
    pass


class MessageTooLong(Exception):
    pass


class UnknownSession(Exception):
    pass


class UnknownCustomer(Exception):
    pass


class Killed(Exception):
    """The kill switch (C-18) is on."""


class Orchestrator:
    def __init__(self, system_prompt, gateway, trace, tools=(), prompt_source=None, *, mcp=None, entitlements=None,
                 gate=None, max_tool_rounds=MAX_TOOL_ROUNDS):
        """`mcp`: an open McpClient; it turns tools on, and then `entitlements` and `gate` are required."""
        self.system_prompt = system_prompt
        self.prompt_sha256 = hashlib.sha256(system_prompt.encode("utf-8")).hexdigest()
        self.prompt_source = prompt_source      # the file name of the prompt, for config_read; None if not known
        self.gateway = gateway
        self.trace = trace
        self.tools = tuple(tools)
        self.mcp, self.entitlements, self.gate = mcp, entitlements, gate
        self.max_tool_rounds = max_tool_rounds
        self.tools_on = mcp is not None
        self.tool_list, self.tool_definitions, self.definitions_sha256 = [], [], None
        if self.tools_on:
            if entitlements is None or gate is None:
                raise ValueError("tools on needs the entitlement check (C-07) and the refund gate (C-09)")
            self.tool_list = mcp.list_tools()
            self.tool_definitions = function_definitions(self.tool_list)
            self.definitions_sha256 = definitions_sha256(self.tool_definitions)
        self.tool_names = {d["function"]["name"] for d in self.tool_definitions}
        self.killed = False
        self.mcp_calls = 0
        self._sessions = {}
        self._n = 0

    def config(self):
        """What config_read returns: the prompt of the active variant, its SHA-256 and the tools. With tools off,
        SRD-1 minimal sends no tool definition to the model. It injects no templated content at run time."""
        return {"prompt": self.system_prompt, "prompt_sha256": self.prompt_sha256, "prompt_source": self.prompt_source,
                "tools_configured": list(self.tools), "tool_definitions": list(self.tool_definitions),
                "runtime_templates": []}

    def config_kind(self, kind):
        """config_read of one kind: prompt, tools, gate or entitlements. None for another kind."""
        if kind == "prompt":
            return self.config()
        if kind == "tools":
            return {"enabled": self.tools_on, "names": sorted(self.tool_names),
                    "definitions": list(self.tool_definitions), "definitions_sha256": self.definitions_sha256,
                    "annotations": {t["name"]: t.get("annotations") for t in self.tool_list},
                    "max_tool_rounds": self.max_tool_rounds}
        if kind == "gate":
            if self.gate is None:
                return {"enabled": False, "layer": "orchestrator", "module": "targets/srd1/app/gate.py",
                        "note": "tools are off, so the refund gate is not built"}
            return {"enabled": True, **self.gate.config()}
        if kind == "entitlements":
            if self.entitlements is None:
                return {"enabled": False, "note": "no entitlement data is loaded"}
            return {"enabled": self.tools_on, **self.entitlements.config()}
        return None

    def set_kill_switch(self, on):
        """C-18. It takes effect before the next model call and before the next MCP call."""
        self.killed = bool(on)
        return self.trace.append("kill_switch", None, on=self.killed)

    def open_session(self, customer_ref=None):
        if customer_ref is not None and (self.entitlements is None or not self.entitlements.known(customer_ref)):
            raise UnknownCustomer(str(customer_ref)[:64])
        if customer_ref is None and self.entitlements is not None:
            customer_ref = self.entitlements.default_customer
        self._n += 1
        sid = f"s{self._n}"
        self._sessions[sid] = {"history": [{"role": "system", "content": self.system_prompt}], "turns": 0,
                               "customer_ref": customer_ref, "refund_total_gbp": 0.0}
        extra = {"customer_ref": customer_ref, "tool_definitions_sha256": self.definitions_sha256} if self.tools_on else {}
        self.trace.append("session_open", sid, system_prompt_sha256=self.prompt_sha256, tools_configured=list(self.tools),
                          **extra)
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
        if self.killed:
            raise Killed(sid)
        s["turns"] += 1
        history = s["history"] + [{"role": "user", "content": message}]
        results, rounds = [], 0
        while True:
            if self.killed:         # C-18: checked before each model call
                self.trace.append("turn_halted", sid, turn=s["turns"], at="before_model_call", model_calls=len(results))
                raise Killed(sid)
            try:
                r = self.gateway.complete(history, tools=self.tool_definitions) if self.tools_on \
                    else self.gateway.complete(history)
            except Exception as e:      # noqa: BLE001 - recorded by class name only
                self.trace.append("turn_error", sid, turn=s["turns"], user_message=message, error=type(e).__name__,
                                  cap_reached=isinstance(e, CapReached))
                raise
            results.append(r)
            calls = (r.message or {}).get("tool_calls") if self.tools_on else None
            if not calls or not isinstance(calls, list) or rounds >= self.max_tool_rounds:
                break
            rounds += 1
            history = history + [{"role": "assistant", "content": r.message.get("content"), "tool_calls": calls}]
            for tc in calls:
                text = self._tool_call(sid, s, tc, rounds)
                history.append({"role": "tool", "tool_call_id": tc.get("id") if isinstance(tc, dict) else None,
                                "content": text})
        s["history"] = history + [{"role": "assistant", "content": r.reply}]
        extra = {}
        if self.tools_on:
            pending = (r.message or {}).get("tool_calls")
            extra = {"model_calls": len(results), "tool_rounds": rounds,
                     "tool_rounds_exhausted": bool(pending) and rounds >= self.max_tool_rounds,
                     "cost_usd_turn": sum(x.cost_usd for x in results),
                     "host_ok_all": all(x.host_ok for x in results)}
        entry = self.trace.append(
            "turn", sid, turn=s["turns"], user_message=message, reply=r.reply,
            model_requested=r.model_requested, model_served=r.model_served, served_host=r.served_host,
            host_ok=r.host_ok, prompt_tokens=r.prompt_tokens, completion_tokens=r.completion_tokens,
            usage_missing=r.usage_missing, cost_usd=r.cost_usd, latency_ms=r.latency_ms, seed=r.seed, retries=r.retries, retry_reasons=r.retry_reasons,
            finish_reason=r.finish_reason, blocked=r.blocked,
            system_prompt_sha256=self.prompt_sha256, tools_configured=list(self.tools), **extra)
        return r.reply, entry

    def _tool_call(self, sid, s, tc, rnd):
        """One tool call that the model asked for. Returns the text that goes back to the model."""
        fn = tc.get("function") if isinstance(tc, dict) else None
        name = fn.get("name") if isinstance(fn, dict) else None
        raw = fn.get("arguments") if isinstance(fn, dict) else None
        base = {"tool": name if isinstance(name, str) else None, "round": rnd,
                "tool_call_id": tc.get("id") if isinstance(tc, dict) else None}
        try:
            args = json.loads(raw, parse_constant=_not_json) if isinstance(raw, str) else raw
            json.dumps(args, allow_nan=False)   # 1e400 reads as an infinity, and a dict can hold NaN. Both are bad.
        except (TypeError, ValueError):
            args = None
        if not isinstance(args, dict):
            self.trace.append("tool_call", sid, **base, arguments=None, decision="bad_arguments")
            return "The tool arguments are not a valid JSON object. No call was made."
        base["arguments"] = args
        if name not in self.tool_names:
            self.trace.append("tool_call", sid, **base, decision="unknown_tool")
            return "There is no tool of that name. No call was made."
        if self.killed:             # C-18: checked before each MCP call
            self.trace.append("tool_call", sid, **base, decision="killed_before_tool_call")
            raise Killed(sid)
        denial = self.entitlements.check(s["customer_ref"], args.get("account_ref"))
        if denial:                  # C-07: the customer's entitlement, before the call
            self.trace.append("entitlement_denial", sid, customer_ref=s["customer_ref"],
                              account_ref=args.get("account_ref"), tool=name, reason=denial)
            self.trace.append("tool_call", sid, **base, decision="denied_entitlement")
            return DENIED_TEXT
        amount = args.get("amount_gbp")
        try:
            hold = self.gate.check(s["refund_total_gbp"], amount)
        except (TypeError, ValueError, OverflowError):     # the gate cannot compare the amount, so no call
            self.trace.append("tool_call", sid, **base, decision="bad_arguments")
            return "The tool arguments are not valid. No call was made."
        if hold:                    # C-09: in the orchestrator, not the prompt
            self.trace.append("gate_hold", sid, tool=name, amount_gbp=amount, cap_gbp=self.gate.cap_gbp,
                              session_limit_gbp=self.gate.session_limit_gbp,
                              session_total_gbp=s["refund_total_gbp"], reason=hold, layer="orchestrator")
            self.trace.append("tool_call", sid, **base, decision="held_for_approval")
            return HELD_TEXT
        self.mcp_calls += 1
        seq = self.mcp_calls
        try:
            result = self.mcp.call(name, args)
        except (McpError, EgressRefused) as e:
            self.trace.append("tool_call", sid, **base, decision="call_failed", mcp_call_seq=seq, error=type(e).__name__)
            return "The tool call failed. No result."
        sc = result.get("structuredContent") if isinstance(result.get("structuredContent"), dict) else {}
        if sc.get("status") != "rejected":      # a refund that the back office did not reject counts to the limit
            s["refund_total_gbp"] += float(amount)
        self.trace.append("tool_call", sid, **base, decision="sent", mcp_call_seq=seq, status=sc.get("status"),
                          refund_ref=sc.get("refund_ref"), is_error=bool(result.get("isError")))
        return result_text(result)
