"""TP-SEC-003 (indirect prompt injection through retrieved content): the seed route into the document store of SRD-1.

This module does not import `inspect_ai`. It makes no network call and no model call.

The seed route. SRD-1 reads its document store once and never writes it (retrieval.py, DocumentStore). So the
harness does not seed the store while SRD-1 runs. It writes a seeded store into the run folder before SRD-1 is built,
and SRD-1 is then built from that file (build.build_srd1, `documents_file`). The seeded store is the bytes of the clean
store, unchanged, and then one entry for each payload document at the end of the list `documents`. The clean store is
the shipped file targets/srd1/data/synthetic_documents.yaml. The seed route reads it once.

The check (seed_problems) reads the bytes of the seeded file as they are on the disk, because SRD-1 is built from that
file. The bytes of the clean store must start it. Its documents must be the clean documents, equal and in order, and
then the payload documents. Its other keys (the list `customers`) must be those of the clean store. No doc_id may be
in the store twice. Each payload document must have a non-empty `watermark`. A difference refuses the run before the
lock, in the form of step 4 of TP-DAT-009 (run_dat009.execute). The refused file stays in the run folder.

The watermark. Each payload document marks itself as a synthetic test artefact in the field `watermark`
(PLAYBOOK_SEC_003.md line 25). Retrieval gives the model the doc_id, the title and the text of a document, on the
pipeline path (retrieval.py, Retrieval.retrieve) and on the tool path (handlers.py, DocumentSearch.call). So the model
does not see the watermark. The scorer of each path reads the title and the text only (DocumentStore, DocumentSearch),
so the watermark does not change the ranking.

The record (the return value of seed) holds the file and the SHA-256 of each store, as TP-DAT-009 records its store
(run_dat009.execute, matrix["sources"]). The teardown facts (teardown) say whether the shipped store changed during
the run. The seeded store stays in the run folder on purpose. It is the record of what SRD-1 read.
"""
import json
from pathlib import Path

from .. import yamlio
from . import runlog

REPO = Path(__file__).resolve().parents[2]
TP = "TP-SEC-003"
CLEAN_STORE = REPO / "targets" / "srd1" / "data" / "synthetic_documents.yaml"
SEEDED_STORE = "seeded_documents.yaml"      # the name of the seeded store in the run folder
WATERMARK_FIELD = "watermark"
SEED_COMMENT = "# The payload documents of TP-SEC-003 follow. The harness wrote them into this copy of the store.\n"
TEARDOWN_NOTE = ("SRD-1 is built from the seeded store in the run folder, and nothing writes the shipped store; the "
                 "seeded store stays in the run folder as the record of what SRD-1 read")


def _rel(path):
    try:
        return Path(path).resolve().relative_to(REPO).as_posix()
    except ValueError:
        return Path(path).name


def _entry(doc):
    """One entry of the list `documents`, in block form. Each value is written as JSON, which YAML reads as the same
    value. The check parses the file again and compares, so a value that does not come back equal refuses the run."""
    return "".join(f"{'  - ' if n == 0 else '    '}{k}: {json.dumps(v, ensure_ascii=False)}\n"
                   for n, (k, v) in enumerate(doc.items()))


def seeded_bytes(clean_raw, payload):
    """The bytes of the seeded store: the bytes of the clean store, unchanged, then one entry for each payload
    document. The clean store ends with its list `documents`, so the new entries join that list."""
    return clean_raw + (SEED_COMMENT + "".join(_entry(d) for d in payload)).encode("utf-8")


def seed_problems(clean_raw, seeded_raw, payload):
    """Returns the differences between the seeded store and the clean store and the payload (empty if none).
    `clean_raw` and `seeded_raw` are the bytes of the two files."""
    out = []
    if not seeded_raw.startswith(clean_raw):
        out.append("the seeded store does not hold the clean store byte for byte")
    clean, seeded = yamlio.load(clean_raw.decode("utf-8")), yamlio.load(seeded_raw.decode("utf-8"))
    if {k: v for k, v in seeded.items() if k != "documents"} != {k: v for k, v in clean.items() if k != "documents"}:
        out.append("the seeded store changes a part of the clean store other than its documents")
    clean_docs, docs = clean["documents"], seeded.get("documents") or []
    if docs[:len(clean_docs)] != clean_docs:
        out.append("the clean documents of the seeded store differ from the clean store")
    if docs[len(clean_docs):] != list(payload):
        out.append("the payload documents of the seeded store differ from the payload")
    # A doc_id in the store twice changes the clean part too: SRD-1 keys its index by doc_id, so the later entry wins.
    seen = set()
    for d in docs:
        if d.get("doc_id") in seen:
            out.append(f"the doc_id {d.get('doc_id')} is in the seeded store more than once")
        seen.add(d.get("doc_id"))
    for d in payload:
        if not isinstance(d.get(WATERMARK_FIELD), str) or not d[WATERMARK_FIELD].strip():
            out.append(f"the payload document {d.get('doc_id')} has no {WATERMARK_FIELD}")
    return out


def seed(payload, run_dir, clean_file=CLEAN_STORE):
    """The seed route, before the lock. It reads the clean store once, writes the seeded store into the run folder,
    and checks the file as it is on the disk. SRD-1 is then built from run_dir / SEEDED_STORE. Returns the record of
    the two stores. A difference raises RuntimeError ("refused before the lock")."""
    clean_raw = Path(clean_file).read_bytes()
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / SEEDED_STORE
    path.write_bytes(seeded_bytes(clean_raw, payload))
    seeded_raw = path.read_bytes()
    refused = seed_problems(clean_raw, seeded_raw, payload)
    if refused:
        raise RuntimeError(f"{TP}: the run is refused before the lock: " + "; ".join(refused))
    n = len(yamlio.load(clean_raw.decode("utf-8"))["documents"])
    return {"clean": {"file": _rel(clean_file), "sha256": runlog.sha256_hex(clean_raw), "documents": n},
            "seeded": {"file": SEEDED_STORE, "sha256": runlog.sha256_hex(seeded_raw), "documents": n + len(payload)},
            "payload_doc_ids": [d["doc_id"] for d in payload], "watermark_field": WATERMARK_FIELD}


def teardown(record, clean_file=CLEAN_STORE):
    """The teardown facts of the seed route. `store_written` is true if the shipped store does not have the SHA-256
    that the seed route recorded. The seeded store stays in the run folder (the module text)."""
    return {"store_written": runlog.sha256_hex(Path(clean_file).read_bytes()) != record["clean"]["sha256"],
            "seeded_store": record["seeded"]["file"], "note": TEARDOWN_NOTE}
