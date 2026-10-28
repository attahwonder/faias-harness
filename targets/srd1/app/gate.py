"""C-09, minimal: the refund gate, in the orchestration layer and not in the prompt (since 0.6).

A refund above the per-refund cap, or one that takes the session's total of refunds above the per-session limit, is
held: the orchestrator makes no MCP call, writes a `gate_hold` line to the trace and tells the model that a colleague
must approve it. SRD-1 minimal has no approval queue (C-11), so the hold is recorded and nothing more. An amount that
is not a finite number (for example NaN or an infinity) is held too, because the gate cannot compare it. An amount of
zero or less is held. An integer too large for a float is above the cap.

The cap and the limit come from the risk-appetite file (A-03). Its values stay null until the owner declares them.
With a null value the gate cannot be built, so the orchestrator does not enable tools ("refund cap not declared").

config_read("gate") also names the irreversible actions (IRREVERSIBLE_ACTIONS: one class, the fee refund, SPEC.md
section 3, C-09), the function that checks, and the SHA-256 of this file (since 0.7, for TP-AGT-012 steps 2 and 3).
"""
import hashlib
import math
from pathlib import Path

import yaml

RISK_FILE = Path(__file__).resolve().parents[1] / "risk_appetite.yaml"
MODULE = "targets/srd1/app/gate.py"
FUNCTION = "RefundGate.check"
CALL_SITE = "targets/srd1/app/orchestrator.py: Orchestrator._tool_call"
# The action classes that SRD-1 minimal gates. It has one tool that acts: fee_refund.
IRREVERSIBLE_ACTIONS = ({"class": "fee refund", "tool": "fee_refund", "gate": "C-09",
                         "why": "destructive and not idempotent: a second call records a second refund"},)


class GateNotDeclared(ValueError):
    pass


def _num(v):
    """A finite int or float, not a bool. NaN and the infinities are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return isinstance(v, int) or math.isfinite(v)


def load_risk_appetite(path=RISK_FILE):
    """(the fee-refund values, the SHA-256 of the file)."""
    raw = Path(path).read_bytes()
    data = yaml.safe_load(raw.decode("utf-8")) or {}
    return (data.get("fee_refund") or {}), hashlib.sha256(raw).hexdigest()


def declared_problems(path=RISK_FILE):
    """The values that the gate needs and the file does not declare, as messages (empty if all are declared)."""
    values, _ = load_risk_appetite(path)
    out = []
    if not (_num(values.get("cap_gbp")) and values["cap_gbp"] > 0):
        out.append("refund cap not declared")
    if not (_num(values.get("session_limit_gbp")) and values["session_limit_gbp"] > 0):
        out.append("session refund limit not declared")
    return out


class RefundGate:
    def __init__(self, cap_gbp, session_limit_gbp, *, source=None, sha256=None, time_to_halt_s=None):
        if not (_num(cap_gbp) and cap_gbp > 0):
            raise GateNotDeclared("refund cap not declared")
        if not (_num(session_limit_gbp) and session_limit_gbp > 0):
            raise GateNotDeclared("session refund limit not declared")
        self.cap_gbp, self.session_limit_gbp = float(cap_gbp), float(session_limit_gbp)
        self.source, self.sha256, self.time_to_halt_s = source, sha256, time_to_halt_s

    @classmethod
    def from_file(cls, path=RISK_FILE):
        values, sha = load_risk_appetite(path)
        return cls(values.get("cap_gbp"), values.get("session_limit_gbp"), source=Path(path).name, sha256=sha,
                   time_to_halt_s=values.get("time_to_halt_s"))

    def check(self, session_total_gbp, amount):
        """None if the refund may go to the tool, else the reason for the hold."""
        if not _num(amount):
            return "the amount is not a number"
        if amount <= 0:
            return "the amount is not above zero"
        try:
            value = float(amount)
        except OverflowError:       # a positive integer too large for a float is above any cap
            return "above the refund cap"
        if value > self.cap_gbp:
            return "above the refund cap"
        if session_total_gbp + value > self.session_limit_gbp:
            return "above the session limit"
        return None

    def config(self):
        """What config_read("gate") returns."""
        return {"refund_cap_gbp": self.cap_gbp, "session_limit_gbp": self.session_limit_gbp,
                "time_to_halt_s": self.time_to_halt_s, "source": self.source, "sha256": self.sha256,
                "module": MODULE, "layer": "orchestrator", "function": FUNCTION, "call_site": CALL_SITE,
                "module_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "irreversible_actions": [dict(a) for a in IRREVERSIBLE_ACTIONS]}
