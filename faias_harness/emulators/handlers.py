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

document_search (since TP-DAT-009) is the emulated document store of SRD-1. Its tools file is
targets/srd1/tools_retrieval.yaml. The arguments are query and, as an option, on_behalf_of. The store file has a
list `documents` and a list `customers` (targets/srd1/data/synthetic_documents.yaml). The list `customers` is the
entitlement table of the store. With on_behalf_of, the handler applies the document rule for that customer. The
customer may read the content documents of their own domain and their own records. The handler trusts the
presented principal, as a back office does. Without on_behalf_of, the call runs under the service credential, and
the handler searches every document. An unknown principal or an empty query gives `status: rejected`. The result
holds `status`, `principal` (the customer reference, or "service") and the documents found, at most TOP_K. The
scorer is lexical tf-idf over whole documents. It is a copy of the scorer of SRD-1 (targets/srd1/app/retrieval.py).
A test checks that the two put the documents in the same order. The handler reads the store once and never writes
it. The control under test is which principal SRD-1 presents (C-07 in its orchestrator). The ledger line of each
call holds the principal that the call presented and the doc_ids of the result (ledger_fields).
"""
import hashlib
import math
import re
import threading
from collections import Counter
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
    if not isinstance(data, dict) or not (isinstance(data.get("accounts"), list)
                                          or isinstance(data.get("documents"), list)):
        raise StoreError("the store must be a mapping with a list 'accounts' or a list 'documents'")
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


_TOKEN = re.compile(r"[a-z0-9]+")


class DocumentSearch:
    TOP_K = 3
    OUTPUT_SCHEMA = {
        "type": "object",
        "properties": {
            "status": {"type": "string", "enum": ["ok", "rejected"]},
            "principal": {"type": "string"},
            "documents": {"type": "array", "items": {"type": "object", "properties": {
                "doc_id": {"type": "string"}, "title": {"type": "string"}, "text": {"type": "string"}}}},
            "reason": {"type": "string"},
        },
        "required": ["status"],
    }

    def __init__(self, store):
        docs, customers = store.get("documents"), store.get("customers")
        if not isinstance(docs, list) or not docs or not isinstance(customers, list):
            raise StoreError("document_search needs a store with a list 'documents' and a list 'customers'")
        self.domains = {c.get("customer_ref"): c.get("domain") for c in customers}
        self.documents = {d.get("doc_id"): d for d in docs}
        self.tf = {i: Counter(_TOKEN.findall(f"{d.get('title')} {d.get('text')}".lower()))
                   for i, d in self.documents.items()}
        self.df = Counter(t for c in self.tf.values() for t in c)
        self.n = len(self.documents)

    def allowed(self, principal, doc):
        """The document rule of the store: own records, and the content of the principal's domain."""
        if doc.get("customer_ref") is not None:
            return doc["customer_ref"] == principal
        return doc.get("domain") == self.domains.get(principal)

    def rank(self, query, ids):
        """[(doc_id, score)] with a score above zero, high to low, then by doc_id (the scorer of retrieval.py)."""
        terms = sorted(set(_TOKEN.findall(str(query).lower())))
        out = []
        for i in ids:
            tf = self.tf[i]
            s = sum(tf[t] * (math.log((self.n + 1) / (self.df[t] + 1)) + 1) for t in terms if tf[t])
            if s > 0:
                out.append((i, s))
        return sorted(out, key=lambda x: (-x[1], x[0]))

    def call(self, args):
        """(result object, is_error)."""
        query, principal = args.get("query"), args.get("on_behalf_of")
        if not isinstance(query, str) or not query.strip():
            return {"status": "rejected", "reason": "query must be a non-empty string"}, True
        if principal is None:
            principal, ids = "service", list(self.documents)
        elif isinstance(principal, str) and principal in self.domains:
            ids = [i for i, d in self.documents.items() if self.allowed(principal, d)]
        else:
            return {"status": "rejected", "reason": "unknown principal"}, True
        found = [self.documents[i] for i, _ in self.rank(query, ids)[:self.TOP_K]]
        return {"status": "ok", "principal": principal,
                "documents": [{"doc_id": d["doc_id"], "title": d["title"], "text": d["text"]} for d in found]}, False

    @staticmethod
    def ledger_fields(args, obj):
        """The two more fields of the ledger line of a call. `principal` is the principal that the call presented.
        That is on_behalf_of, or "service" without it. `returned_doc_ids` names the documents of the result."""
        presented = args.get("on_behalf_of")
        return {"principal": "service" if presented is None else presented,
                "returned_doc_ids": [d.get("doc_id") for d in obj.get("documents") or []]}


HANDLERS = {"fee_refund": FeeRefund, "document_search": DocumentSearch}
