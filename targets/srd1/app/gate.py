"""C-09, minimal: the refund gate, in the orchestration layer and not in the prompt (since 0.6).

A refund above the per-refund cap, or one that takes the session's total of refunds above the per-session limit, is
held: the orchestrator makes no MCP call, writes a `gate_hold` line to the trace and tells the model that a colleague
must approve it. SRD-1 minimal has no approval queue (C-11), so the hold is recorded and nothing more. An amount that
is not a number is held too, because the gate cannot compare it.

The cap and the limit come from the risk-appetite file (A-03). Its values stay null until the owner declares them.
With a null value the gate cannot be built, so the orchestrator does not enable tools ("refund cap not declared").
"""
import hashlib
from pathlib import Path

import yaml

RISK_FILE = Path(__file__).resolve().parents[1] / "risk_appetite.yaml"
MODULE = "targets/srd1/app/gate.py"


class GateNotDeclared(ValueError):
    pass


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


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
        if float(amount) > self.cap_gbp:
            return "above the refund cap"
        if session_total_gbp + float(amount) > self.session_limit_gbp:
            return "above the session limit"
        return None

    def config(self):
        """What config_read("gate") returns."""
        return {"refund_cap_gbp": self.cap_gbp, "session_limit_gbp": self.session_limit_gbp,
                "time_to_halt_s": self.time_to_halt_s, "source": self.source, "sha256": self.sha256,
                "module": MODULE, "layer": "orchestrator"}
