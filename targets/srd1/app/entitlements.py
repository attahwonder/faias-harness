"""C-07, minimal: the entitlement check (since 0.6).

Each session acts for one synthetic customer. A tool call on an account that is not that customer's is denied
before the MCP call, and the denial is written to the trace (`entitlement_denial`). The tool server trusts SRD-1's
own credential for any account, so this check is the control: it applies the customer's entitlement, not the
agent's broader one. The matrix (customer to accounts, and the customer's domain, retail or wealth) comes from the
synthetic data file. SRD-1 minimal has no retrieval stack (C-05) by default, so the check runs before each tool call
only.

The document rule (`allowed_document`) is new for TP-DAT-009. A customer may read the content documents of their own
domain and their own customer records only. A build with the document store (C-05 minimal, retrieval.py) gives
`documents_file` and `retrieval_stage`. Then config() also reports the rule, the store file and its SHA-256. The
stage then becomes a list of the three places where the check runs. Without them, config() is as before.
"""
import hashlib
from pathlib import Path

import yaml

DATA_FILE = Path(__file__).resolve().parents[1] / "data" / "synthetic_bank.yaml"
STAGE = "before the tool call; before retrieval when C-05 exists"
DOCUMENT_RULE = ("a customer reads the content documents of their own domain and their own customer records only: "
                 "a document with a customer_ref is allowed to that customer alone, and a document without one is "
                 "allowed to the customers of its domain")


class Entitlements:
    def __init__(self, path=DATA_FILE, *, documents_file=None, retrieval_stage=None):
        if (documents_file is None) != (retrieval_stage is None):
            raise ValueError("give documents_file and retrieval_stage together")
        raw = Path(path).read_bytes()
        data = yaml.safe_load(raw.decode("utf-8"))
        self.source = Path(path)
        self.sha256 = hashlib.sha256(raw).hexdigest()
        self.domains = {c["customer_ref"]: c["domain"] for c in data["customers"]}
        self.owner = {a["account_ref"]: a["customer_ref"] for a in data["accounts"]}
        self.default_customer = data["default_customer_ref"]
        if self.default_customer not in self.domains:
            raise ValueError("default_customer_ref names no customer in the data file")
        self.documents_source, self.documents_sha256 = None, None
        self.retrieval_stage = retrieval_stage
        if documents_file is not None:
            self.documents_source = Path(documents_file)
            self.documents_sha256 = hashlib.sha256(self.documents_source.read_bytes()).hexdigest()

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

    def allowed_document(self, customer_ref, doc):
        """True if the customer may read the document (DOCUMENT_RULE). An unknown customer may read none."""
        if customer_ref not in self.domains:
            return False
        if doc.get("customer_ref") is not None:
            return doc["customer_ref"] == customer_ref
        return doc.get("domain") == self.domains[customer_ref]

    def config(self):
        """What config_read("entitlements") returns."""
        out = {"source": _rel(self.source), "sha256": self.sha256,
               "enforcement": {"file": "targets/srd1/app/entitlements.py", "function": "Entitlements.check"},
               "stage": STAGE, "customers": len(self.domains), "accounts": len(self.owner),
               "domains": sorted(set(self.domains.values())), "default_customer_ref": self.default_customer}
        if self.documents_source is not None:
            out["enforcement"]["document_function"] = "Entitlements.allowed_document"
            out.update(stage=["before each tool call", f"before retrieval (pipeline path): {self.retrieval_stage}",
                              "at the emulated store by the presented principal (tool path)"],
                       document_rule=DOCUMENT_RULE, documents_source=_rel(self.documents_source),
                       documents_sha256=self.documents_sha256)
        return out


def _rel(path):
    repo = Path(__file__).resolve().parents[3]
    try:
        return Path(path).resolve().relative_to(repo).as_posix()
    except ValueError:
        return Path(path).name
