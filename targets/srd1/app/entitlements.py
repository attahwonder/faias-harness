"""C-07, minimal: the entitlement check (since 0.6).

Each session acts for one synthetic customer. A tool call on an account that is not that customer's is denied
before the MCP call, and the denial is written to the trace (`entitlement_denial`). The tool server trusts SRD-1's
own credential for any account, so this check is the control: it applies the customer's entitlement, not the
agent's broader one. The matrix (customer to accounts, and the customer's domain, retail or wealth) comes from the
synthetic data file. SRD-1 minimal has no retrieval stack (C-05), so the check runs before each tool call only.
"""
import hashlib
from pathlib import Path

import yaml

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "synthetic_bank.yaml"
STAGE = "before the tool call; before retrieval when C-05 exists"


class Entitlements:
    def __init__(self, path=DATA_FILE):
        raw = Path(path).read_bytes()
        data = yaml.safe_load(raw.decode("utf-8"))
        self.source = Path(path)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.domains = {c["customer_ref"]: c["domain"] for c in data["customers"]}
        self.owner = {a["account_ref"]: a["customer_ref"] for a in data["accounts"]}
        self.default_customer = data["default_customer_ref"]
        if self.default_customer not in self.domains:
            raise ValueError("default_customer_ref names no customer in the data file")

    def known(self, customer_ref):
        return customer_ref in self.domains

    def check(self, customer_ref, account_ref):
        """None if the customer may act on the account, else the reason for the denial."""
        if customer_ref not in self.domains:
            return "unknown customer"
        if not isinstance(account_ref, str) or account_ref not in self.owner:
            return "unknown account"
        if self.owner[account_ref] != customer_ref:
            return "the account is not the customer's"
        return None

    def config(self):
        """What config_read("entitlements") returns."""
        return {"source": _rel(self.source), "sha256": self.sha256,
                "enforcement": {"file": "targets/srd1/app/entitlements.py", "function": "Entitlements.check"},
                "stage": STAGE, "customers": len(self.domains), "accounts": len(self.owner),
                "domains": sorted(set(self.domains.values())), "default_customer_ref": self.default_customer}


def _rel(path):
    repo = Path(__file__).resolve().parents[3]
    try:
        return Path(path).resolve().relative_to(repo).as_posix()
    except ValueError:
        return Path(path).name
