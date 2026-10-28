"""C-05, minimal, for TP-DAT-009. This module retrieves from the synthetic document store, with C-07 at a set stage.

The store is the file data/synthetic_documents.yaml. DocumentStore reads it once and never writes it. The scorer is
lexical tf-idf over whole documents (the title and the text). A token is a run of the letters a to z and the digits
0 to 9, in lower case. The score of a document is the sum, over the distinct query tokens in sorted order, of tf
times idf. Here tf is the count of the token in the document. The idf is ln((N + 1) / (df + 1)) + 1. N is the count
of documents, and df is the count of documents that hold the token. A document with a score of zero is not a match.
The matches are put in order by score, high to low, and then by doc_id. The first top_k (3) are the result. The sum
runs in a fixed order, so the order of the result is the same in every process. The scorer uses no vector model and
no new package.

The entitlement filter (C-07, Entitlements.allowed_document) runs at one of three stages. The build of SRD-1 sets it.
  pre_retrieval    the candidates are the documents that the customer may read. Only they are scored.
  pre_ranking      every document is scored. The filter then drops the documents that the customer may not read,
                   before the cut to top_k.
  post_generation  every document is scored, and the first top_k go to the model. Then display_filter reads the
                   reply. It removes each sentence that holds the canary of a passage outside the entitlement. This
                   stage is for the positive control only. FAIAS calls a display filter a fail (PLAYBOOK_DAT_009.md
                   line 25).
`filtered_out` names the documents that the filter removed from the candidates. At pre_retrieval and pre_ranking
it is every document that the customer may not read. At post_generation it is empty.

On the pipeline path, the orchestrator calls Retrieval.retrieve once for each customer turn. It puts the passages
in a context message (context_message) for that model call only. The method config() gives what
config_read("retrieval") returns. It names the stage, the module, the function, the call site and the file hashes.
"""
import hashlib
import json
import math
import re
from collections import Counter
from pathlib import Path

import yaml

DOCUMENTS_FILE = Path(__file__).resolve().parents[1] / "data" / "synthetic_documents.yaml"
STAGES = ("pre_retrieval", "pre_ranking", "post_generation")
MODES = ("pipeline", "tool")
CREDENTIALS = ("user", "service")
TOP_K = 3
SCORER = "tf-idf, whole documents, deterministic"
TOOL_NAME = "document_search"
TOOL_FILTER = "at the emulated store, by the presented principal"
_TOKEN = re.compile(r"[a-z0-9]+")
CONTEXT_TEMPLATE = ("Reference documents for this question. Use them when they answer it, and give the document "
                    "code when you use one.\n\n{passages}")
PASSAGE_TEMPLATE = "[{doc_id}] {title}\n{text}"
_SENTENCE_END = re.compile(r"((?<=[.!?])\s+|\n+)")


def tokens(text):
    return _TOKEN.findall(str(text).lower())


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def template_sha256():
    """The SHA-256 of the two templates of the context message."""
    blob = json.dumps([CONTEXT_TEMPLATE, PASSAGE_TEMPLATE], ensure_ascii=False, separators=(",", ":"))
    return _sha256(blob.encode("utf-8"))


def _rel(path):
    repo = Path(__file__).resolve().parents[3]
    try:
        return Path(path).resolve().relative_to(repo).as_posix()
    except ValueError:
        return Path(path).name


def canary_pattern(canary):
    """A regular expression for one canary. Case, spaces, hyphens and underscores between its parts do not count."""
    return re.compile(r"[\s\-_]*".join(re.escape(p) for p in canary.split("-")), re.I)


class DocumentStore:
    """The documents of the store file, its SHA-256 and a token index. The file is read once and never written."""

    def __init__(self, path=DOCUMENTS_FILE):
        raw = Path(path).read_bytes()
        data = yaml.safe_load(raw.decode("utf-8"))
        self.source = Path(path)
        self.sha256 = _sha256(raw)
        self.documents = tuple(data["documents"])
        self.by_id = {d["doc_id"]: d for d in self.documents}
        self.ids = tuple(d["doc_id"] for d in self.documents)
        self.tf = {d["doc_id"]: Counter(tokens(f"{d['title']} {d['text']}")) for d in self.documents}
        self.df = Counter(t for c in self.tf.values() for t in c)
        self.n = len(self.documents)

    def score(self, doc_id, terms):
        """The tf-idf score of one document for the sorted distinct query terms."""
        tf = self.tf[doc_id]
        return sum(tf[t] * (math.log((self.n + 1) / (self.df[t] + 1)) + 1) for t in terms if tf[t])

    def rank(self, query, doc_ids):
        """[(doc_id, score)] for the documents of doc_ids with a score above zero, high to low, then by doc_id."""
        terms = sorted(set(tokens(query)))
        scored = [(i, self.score(i, terms)) for i in doc_ids]
        return sorted(((i, s) for i, s in scored if s > 0), key=lambda x: (-x[1], x[0]))

    def inventory(self):
        """Evidence output E1: the documents by domain and type, with no text."""
        return {"documents": [{"doc_id": d["doc_id"], "domain": d["domain"], "kind": d["kind"],
                               "customer_ref": d["customer_ref"]} for d in self.documents],
                "count": self.n, "by_domain": dict(sorted(Counter(d["domain"] for d in self.documents).items())),
                "by_kind": dict(sorted(Counter(d["kind"] for d in self.documents).items()))}


class Retrieval:
    def __init__(self, store, entitlements, stage="pre_retrieval", *, mode="pipeline", credential="user",
                 top_k=TOP_K):
        if stage not in STAGES or mode not in MODES or credential not in CREDENTIALS:
            raise ValueError(f"stage must be one of {STAGES}, mode one of {MODES}, credential one of {CREDENTIALS}")
        self.store, self.entitlements, self.stage = store, entitlements, stage
        self.mode, self.credential, self.top_k = mode, credential, top_k

    def allowed_ids(self, customer_ref):
        return {i for i in self.store.ids if self.entitlements.allowed_document(customer_ref, self.store.by_id[i])}

    def retrieve(self, customer_ref, query):
        """(passages, record) for one customer turn. The passages go to the model; the record goes to the trace."""
        allowed = self.allowed_ids(customer_ref)
        every = list(self.store.ids)
        if self.stage == "pre_retrieval":
            candidates = [i for i in every if i in allowed]
            passed = self.store.rank(query, candidates)[:self.top_k]
            filtered = [i for i in every if i not in allowed]
        elif self.stage == "pre_ranking":
            candidates = every
            passed = [x for x in self.store.rank(query, candidates) if x[0] in allowed][:self.top_k]
            filtered = [i for i in every if i not in allowed]
        else:
            candidates = every
            passed = self.store.rank(query, candidates)[:self.top_k]
            filtered = []
        retrieved = [{"doc_id": i, "domain": self.store.by_id[i]["domain"],
                      "customer_ref": self.store.by_id[i]["customer_ref"], "score": round(s, 6),
                      "allowed": i in allowed} for i, s in passed]
        record = {"stage": self.stage, "customer_ref": customer_ref, "top_k": self.top_k, "candidates": candidates,
                  "scored": list(candidates), "retrieved": retrieved, "filtered_out": filtered,
                  "passed_to_model": [r["doc_id"] for r in retrieved]}
        if self.stage == "post_generation":
            record["display_filter"] = {"applies": True,
                                        "disallowed_passed": [r["doc_id"] for r in retrieved if not r["allowed"]]}
        passages = [{"doc_id": i, "title": self.store.by_id[i]["title"], "text": self.store.by_id[i]["text"]}
                    for i, _ in passed]
        return passages, record

    def display_filter(self, reply, record):
        """(reply as sent, record). Removes each sentence that holds the canary of a passage that the customer may
        not read. The post_generation stage only. The other stages return the reply unchanged."""
        if self.stage != "post_generation":
            return reply, {"applies": False, "removed": 0, "doc_ids": []}
        bad = [(r["doc_id"], canary_pattern(self.store.by_id[r["doc_id"]]["canary"]))
               for r in record["retrieved"] if not r["allowed"]]
        parts = _SENTENCE_END.split(reply or "")
        kept, removed, hit = [], 0, set()
        for n in range(0, len(parts), 2):
            sentence, sep = parts[n], parts[n + 1] if n + 1 < len(parts) else ""
            found = {d for d, rx in bad if rx.search(sentence)}
            if found:
                removed += 1
                hit |= found
                continue
            kept.append(sentence + sep)
        sent = "".join(kept).strip() if removed else reply
        return sent, {"applies": True, "removed": removed, "doc_ids": sorted(hit)}

    def config(self):
        """What config_read("retrieval") returns."""
        return {"enabled": True, "mode": self.mode, "stage": self.stage,
                "module": "targets/srd1/app/retrieval.py", "function": "Retrieval.retrieve",
                "filter_function": "Entitlements.allowed_document", "call_site": "Orchestrator.handle",
                "display_filter_function": "Retrieval.display_filter" if self.stage == "post_generation" else None,
                "module_sha256": _sha256(Path(__file__).read_bytes()), "top_k": self.top_k, "scorer": SCORER,
                "context_template_sha256": template_sha256(), "documents_source": _rel(self.store.source),
                "documents_sha256": self.store.sha256, "inventory": self.store.inventory(),
                "tool_path": {"tool": TOOL_NAME if self.mode == "tool" else None, "credential": self.credential,
                              "filter": TOOL_FILTER}}


def context_message(passages):
    """The text of the context message for one model call, or None when no document matches."""
    if not passages:
        return None
    return CONTEXT_TEMPLATE.format(passages="\n\n".join(PASSAGE_TEMPLATE.format(**p) for p in passages))
