"""C-09, minimal: the refund gate, in the orchestration layer and not in the prompt (since 0.6).

A refund above the per-refund cap, or one that takes the session's total of refunds above the per-session limit, is
held: the orchestrator makes no MCP call, writes a `gate_hold` line to the trace and tells the model that a colleague
must approve it. SRD-1 minimal has no approval queue (C-11), so the hold is recorded and nothing more. An amount that
is not a finite number (for example NaN or an infinity) is held too, because the gate cannot compare it. An amount of
zero or less is held. An integer too large for a float is above the cap.

The cap and the limit come from the risk-appetite file (A-03). The owner declared them on 29 October 2026. The cap is
25 GBP, and the session limit is 50 GBP. With a null value the gate cannot be built, so the orchestrator does not enable
tools ("refund cap not declared"). The reader of the file (read_risk_appetite) gives one problem for a file that
cannot be read. It does the same for a file that is not UTF-8 YAML and for a lone surrogate. It also does the same for
a top level or a fee_refund value that is not a mapping, and for a declared_by or declared_on value that is not a text.
The function load_risk_appetite raises that problem as RiskFileError.

Since 0.7, config_read("gate") also names the irreversible actions (IRREVERSIBLE_ACTIONS: one class, the fee
refund, SPEC.md section 3, C-09), the function that checks, and the SHA-256 of this file. TP-AGT-012 reads them
in steps 2 and 3.
"""
import hashlib
import math
from pathlib import Path

import yaml

from faias_harness import yamlio
from faias_harness.text import lone_surrogate_keys, lone_surrogate_problem

RISK_FILE = Path(__file__).resolve().parents[1] / "risk_appetite.yaml"
MODULE = "targets/srd1/app/gate.py"
FUNCTION = "RefundGate.check"
CALL_SITE = "targets/srd1/app/orchestrator.py: Orchestrator._tool_call"
# The action classes that SRD-1 minimal gates. It has one tool that acts: fee_refund.
IRREVERSIBLE_ACTIONS = ({"class": "fee refund", "tool": "fee_refund", "gate": "C-09",
                         "why": "destructive and not idempotent: a second call records a second refund"},)


class GateNotDeclared(ValueError):
    pass


class RiskFileError(ValueError):
    """The risk-appetite file is refused (read_risk_appetite). The message is the one problem."""


def _num(v):
    """A finite int or float, not a bool. NaN and the infinities are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return isinstance(v, int) or math.isfinite(v)


def read_risk_appetite(path=RISK_FILE):
    """(the fee-refund values, the SHA-256 of the file, None), or (None, None, the one problem). The problem is a
    file that cannot be read, for example an absent file. It can also be a file that is not UTF-8 YAML, or a lone
    surrogate (lone_surrogate_keys). The pure-Python YAML loader reads the escape \\ud83d as a lone surrogate, and
    libyaml refuses it. The preflight reads the file with it before any model call, and the TP-AGT-012 engine before
    the lock. The header of a TP-AGT-012 run copies declared_by and declared_on from this read.

    A top level or a fee_refund value that is present and not a mapping is a problem too. Examples are 0, an empty
    text, a list and a text. An empty file and a null value read as an empty mapping. A declared_by or declared_on
    value that is present, not null and not a text is a problem. An example is an unquoted date, which YAML reads as a
    date. The header of a TP-AGT-012 run copies the value, and the canonical form of its log cannot hold a date. So
    the problem says to put the value in quotes."""
    path = Path(path)
    try:
        raw = path.read_bytes()
        data = yamlio.load(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        return None, None, f"cannot read {path.name}: {type(e).__name__}"
    lone = lone_surrogate_keys(data)
    if lone:
        return None, None, lone_surrogate_problem(path.name, lone)
    data = {} if data is None else data
    if not isinstance(data, dict):
        return None, None, f"{path.name}: the top level is not a mapping"
    values = {} if data.get("fee_refund") is None else data["fee_refund"]
    if not isinstance(values, dict):
        return None, None, f"{path.name}: the value of 'fee_refund' is not a mapping"
    bad = [k for k in ("declared_by", "declared_on") if values.get(k) is not None and not isinstance(values[k], str)]
    if bad:
        return None, None, (f"{path.name}: the value of {', '.join(repr(k) for k in bad)} is not a text; put the value "
                            "in quotes")
    return values, hashlib.sha256(raw).hexdigest(), None


def load_risk_appetite(path=RISK_FILE):
    """(the fee-refund values, the SHA-256 of the file). A file that read_risk_appetite refuses raises RiskFileError
    with the one problem."""
    values, sha256, problem = read_risk_appetite(path)
    if problem:
        raise RiskFileError(problem)
    return values, sha256


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
