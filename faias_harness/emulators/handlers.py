"""Built-in handlers of the MCP tool emulator (mcp_tools.py).

A tool in a tools file can name a handler in place of a fixed result: `result: {handler: fee_refund}`. The handler
computes a synthetic result from a synthetic store by a fixed rule. It reads the store once, when the emulator
starts, keeps it in memory and never writes it. It acts on no system. The handlers form a fixed set (HANDLERS); a
tools file cannot add one.

fee_refund: the emulated back office of a fee refund. Arguments: account_ref, fee_id, amount_gbp and, optionally,
reason. A known fee of that account with an amount equal to the fee gives `status: recorded` and a new refund
reference (REF-SYN-0001, then REF-SYN-0002, and so on). An unknown account, an unknown fee or another amount gives
`status: rejected` with the reason, as an error result. So does an amount that is not a finite number, or that is
not above zero. A store whose fee amount is not a finite number above zero is refused (StoreError). A second call for the same fee records a second refund: the
tool is not idempotent, and the ledger shows both. The handler checks no cap and no entitlement. It trusts the
credential of the caller, as a back office trusts the agent's service credential. The controls are the caller's:
in SRD-1 minimal, C-07 (entitlements) and C-09 (the refund gate) in its orchestrator.

The store is a YAML file with a list `accounts`. Each account has `account_ref`, `customer_ref` and `fees`. Each fee
has `fee_id` and `amount_gbp` (targets/srd1/data/synthetic_bank.yaml).
"""
import hashlib
import math
import threading
from pathlib import Path

import yaml


class StoreError(ValueError):
    pass


def load_store(path):
    """(data, sha256 of the file). The file is read once."""
    try:
        raw = Path(path).read_bytes()
    except OSError as e:
        raise StoreError(f"cannot read the store ({type(e).__name__})") from None
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise StoreError(f"cannot read the store ({type(e).__name__})") from None
    if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
        raise StoreError("the store must be a mapping with a list 'accounts'")
    return data, hashlib.sha256(raw).hexdigest()


def _number(v):
    """A finite int or float, not a bool. NaN and the infinities are not numbers here."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return isinstance(v, int) or math.isfinite(v)


class FeeRefund:
    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["recorded", "rejected"]},
            "refund_ref": {"type": "string"},
            "account_ref": {"type": "string"},
            "fee_id": {"type": "string"},
            "amount_gbp": {"type": "number"},
            "reason": {"type": "string"},
        },
        "required": ["status"],
    }

    def __init__(self, store):
        self.fees = {}
        for a in store.get("accounts") or []:
            for f in a.get("fees") or []:
                fee = f.get("amount_gbp")
                if not _number(fee) or fee <= 0:   # a NaN fee would equal every amount
                    raise StoreError(f"fee {f.get('fee_id')}: amount_gbp must be a finite number above zero")
                self.fees[(a.get("account_ref"), f.get("fee_id"))] = fee
        self.accounts = {a.get("account_ref") for a in store.get("accounts") or []}
        self._n = 0
        self._lock = threading.Lock()

    def call(self, args):
        """(result object, is_error)."""
        account, fee, amount = args.get("account_ref"), args.get("fee_id"), args.get("amount_gbp")
        if not isinstance(account, str) or account not in self.accounts:
            return {"status": "rejected", "reason": "no such account"}, True
        if not isinstance(fee, str) or (account, fee) not in self.fees:
            return {"status": "rejected", "reason": "no such fee"}, True
        if not _number(amount):
            return {"status": "rejected", "reason": "amount_gbp must be a number"}, True
        if amount <= 0:
            return {"status": "rejected", "reason": "amount_gbp must be above zero"}, True
        try:
            differs = abs(float(amount) - float(self.fees[(account, fee)])) > 0.005
        except OverflowError:       # an integer too large for a float differs from every fee
            differs = True
        if differs:
            return {"status": "rejected", "reason": "the amount differs from the fee"}, True
        with self._lock:
            self._n += 1
            ref = f"REF-SYN-{self._n:04d}"
        return {"status": "recorded", "refund_ref": ref, "account_ref": account, "fee_id": fee,
                "amount_gbp": float(self.fees[(account, fee)])}, False


HANDLERS = {"fee_refund": FeeRefund}
