"""The bench report: one signed report over the member run logs of a bench (faias_harness/bench.py).

Format: a JSON-lines file, `bench_report.jsonl`, with the schema `faias-bench-report/1`. Line 1 is the header. The
bench runner writes it, with its lock, and flushes it to disk before the first cell runs. The header holds the bench
settings hash, the bench cap, the models (model ID, prices, routing locks), the matrix of cells (procedure, tier, model,
item set, run folder and the settings hash of each cell), the operator's typed confirmation and the probe results of
each model. The lock is a time and a hash over the bench settings hash, the mode, the cap, the models, the cells and the
code block. Then the report holds one `row` record for each cell, in the order of the matrix, and a `summary` record.
The signature is Ed25519 over the exact bytes of the report (runlog.sign_log), with the run-signing key of the cells.

A row of a cell that ran holds the path of its member log and the SHA-256 of the log's bytes. It copies from the log
the procedure, the star label (from the log header, never computed), the model, the variant, the item set, the settings
hash, the log's own label, the scorer status, the run status and the list of steps that the run leaves out
(`not_in_run`, where the log has one). For each measure of the log summary (M1, M2 and so on) it gives the rate, the
count of successes and items where the measure holds them, k (the runs per item) and a Wilson 95% interval on the item
rate. With 0 successes the row says to report the upper bound only. A measure with no count of successes and items has
no interval, and the row says so. Every row carries the label LABEL. A row of a log whose scorer is not calibrated
also carries UNCALIBRATED. The report has no composite score.

A row of a cell that did not run says "not run" (a cell after the cell that stopped the bench), "refused" (a check
just before the cell refused it) or "error" (the engine raised before it signed a log). Each gives the reason.

`verify` checks the signature of the report, its form and its lock. For each row of a cell that ran, it checks that
the member log is at the run folder of its cell, that its SHA-256 is the one in the row, that the member log passes
runlog.verify_log, that its signature has the public key of the report, that its settings hash is that of the cell,
and that the row is the row that this module builds from the log. A cell that is "not run" must have no run log. A
cell with the status "error" can hold an unsigned log, and the report does not rely on it. The module makes no network
call.
"""
import base64
import json
import time
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from .. import __version__
from . import runlog, sec001

REPO = Path(__file__).resolve().parents[2]
SCHEMA = "faias-bench-report/1"
REPORT_NAME = "bench_report.jsonl"
LABEL = "Model benchmark result, not FAIAS assurance"
UNCALIBRATED = "Uncalibrated scorer, not a finding"
STAR_NOTE = ("the star label of each row is the method maturity of the FAIAS procedure, copied from the header of the "
             "member log; it is not a grade of the model, and the bench never computes or raises it")
COMPOSITE = "none: each row is one procedure, one tier and one model, and the report does not combine rows"
INTERVAL = ("Wilson 95% interval on the item rate (successes over items, as the measure counts them); with 0 successes, "
            "report the upper bound only, never 0%")
ZERO_NOTE = "0 successes: report the upper bound of the interval only, never 0%"
NO_PAIR_NOTE = "no interval: the measure gives no count of successes and items"
RAN, NOT_RUN, REFUSED, ERROR = "run", "not run", "refused", "error"
ROW_STATES = (RAN, NOT_RUN, REFUSED, ERROR)
# The count pairs of a measure, as (successes, items). TP-SEC-001 and TP-SEC-006 measures hold both.
COUNT_PAIRS = (("successful_items", "items"),)
CODE_FILES = ("faias_harness/bench.py", "faias_harness/engine/benchreport.py")
LOCK_KEYS = ("bench_settings_sha256", "mode", "cap_usd", "models", "cells", "code")
LOCK_COVERS = "bench settings hash, mode, bench cap, models, cells (with the settings hash of each), code block"


def code_block(repo=REPO):
    """What code wrote the report: the engine version, the git commit (null when git does not answer) and the SHA-256
    of the bench runner and of this module."""
    repo = Path(repo)
    return {"engine_version": __version__, "git_commit": runlog.git_commit(repo),
            "files": {f: runlog.sha256_hex((repo / f).read_bytes()) for f in CODE_FILES}}


def build_header(*, bench_sha256, bench_file, mode, cap_usd, models, cells, confirmation, probes, repo=REPO):
    """The header of a bench report, without the lock (lock_header adds it)."""
    return {
        "type": "header", "schema": SCHEMA, "label": LABEL, "engine_version": __version__, "code": code_block(repo),
        "bench_settings_sha256": bench_sha256, "bench_file": bench_file, "mode": mode, "cap_usd": cap_usd,
        "models": models, "cells": cells, "star_note": STAR_NOTE, "composite": COMPOSITE, "interval": INTERVAL,
        "row_labels": {"every_row": LABEL, "uncalibrated_scorer": UNCALIBRATED},
        "owner_confirmation": confirmation, "probes": probes,
    }


def lock_fixed(header):
    return {k: header.get(k) for k in LOCK_KEYS}


def lock_header(header, clock_ns=None):
    ns = time.time_ns() if clock_ns is None else clock_ns
    header["lock"] = {"time": runlog.iso(ns), "time_ns": ns,
                      "hash": runlog.sha256_hex(runlog.canonical(lock_fixed(header))), "covers": LOCK_COVERS}
    return header


def measure_row(m, k):
    """One measure of a member log summary as a row cell: the value field and its value, the successes and items
    (COUNT_PAIRS), k and the Wilson 95% interval."""
    b = m["overall"] if isinstance(m, dict) and isinstance(m.get("overall"), dict) else m
    if not isinstance(b, dict):
        return {"value_field": None, "rate": b, "successes": None, "n": None, "k": k, "wilson95": None,
                "note": NO_PAIR_NOTE}
    name = next((f for f in runlog.RESULT_VALUES if f in b), None)
    out = {"value_field": name, "rate": b.get(name) if name else None, "successes": None, "n": None, "k": k,
           "wilson95": None}
    for c_key, n_key in COUNT_PAIRS:
        c, n = b.get(c_key), b.get(n_key)
        if isinstance(c, int) and isinstance(n, int) and not isinstance(c, bool) and not isinstance(n, bool):
            out.update(successes=c, n=n, wilson95=sec001._wilson95(c, n))
            if n and c == 0:
                out["note"] = ZERO_NOTE
            break
    else:
        out["note"] = NO_PAIR_NOTE
    return out


def row_from_log(log_path, cell, report_dir):
    """The row of a cell that ran, built from its member log. `cell` is the cell entry of the header."""
    log_path = Path(log_path)
    recs = runlog.read_log(log_path)
    h = recs[0]
    s = recs[-1] if recs[-1].get("type") == "summary" else {}
    corpus, scorer = h.get("corpus") or {}, h.get("scorer") or {}
    k = corpus.get("runs_per_item", cell.get("runs_per_item"))
    calibrated = scorer.get("calibrated") is True
    measures = {name: measure_row(m, k) for name, m in sorted((s.get("measures") or {}).items(),
                                                              key=lambda x: (len(x[0]), x[0]))
                if runlog.MEASURE_KEY.fullmatch(name)}
    row = {
        "type": "row", "cell": cell["cell"], "status": RAN,
        "log": log_path.relative_to(report_dir).as_posix(), "log_sha256": runlog.sha256_hex(log_path.read_bytes()),
        "procedure": (h.get("procedure") or {}).get("tp"), "star": (h.get("procedure") or {}).get("star"),
        "tier": cell["tier"], "model_id": (h.get("model") or {}).get("pinned_id"),
        "variant": (h.get("variant") or {}).get("name"), "item_set": corpus.get("item_set"),
        "settings_hash": h.get("settings_hash"), "label": h.get("label"),
        "labels": [LABEL] + ([] if calibrated else [UNCALIBRATED]), "scorer_calibrated": calibrated,
        "run_status": s.get("status") or "no summary", "measures": measures,
        "not_in_run": h.get("not_in_run", "not stated in the member log"),
    }
    if "measure_status" in s:       # a Tier M engine log (tierm_log.py): item outcomes, no measure
        row.update(measure_status=s["measure_status"], item_outcomes=s.get("item_outcomes"))
    return row


def row_not_run(cell, status, reason):
    return {"type": "row", "cell": cell["cell"], "status": status, "procedure": cell["procedure"],
            "tier": cell["tier"], "model_id": cell["model_id"], "labels": [LABEL], "reason": reason}


class BenchReport:
    """Writes the report: the header with its lock first (flushed to disk), a row for each cell, the summary, then the
    signature."""

    def __init__(self, folder):
        self.dir = Path(folder)
        self.path = self.dir / REPORT_NAME
        self.log = None

    def open(self, header, clock_ns=None):
        lock_header(header, clock_ns)
        self.log = runlog.RunLog(self.path)
        self.log.write(header)
        return header

    def write(self, record):
        self.log.write(record)

    def finish(self, summary, sign_key):
        self.log.write(summary)
        self.log.close()
        return runlog.sign_log(self.path, sign_key)


def _signature(path, sig_path, pubkey_path):
    """(ok, message, public key PEM of the signature file)."""
    try:
        s = json.loads(Path(sig_path).read_bytes().decode("utf-8"))
        pem = s["public_key_pem"]
        pub = serialization.load_pem_public_key(Path(pubkey_path).read_bytes() if pubkey_path else pem.encode("ascii"))
        pub.verify(base64.b64decode(s["signature_b64"]), Path(path).read_bytes())
    except InvalidSignature:
        return False, "the signature does not match the file", None
    except Exception as e:      # noqa: BLE001
        return False, f"cannot verify ({type(e).__name__})", None
    return True, "signature valid", pem


def check(path, report_pem, pubkey_path=None, repo=REPO, use_repo=True):
    """The problems in the form of a bench report and in its member logs (empty if none)."""
    path = Path(path)
    try:
        recs = runlog.read_log(path)
    except (OSError, ValueError) as e:
        return [f"cannot read the report: {type(e).__name__}"]
    if not recs or recs[0].get("type") != "header":
        return ["the first line is not the header"]
    h, problems = recs[0], []
    if h.get("schema") != SCHEMA:
        problems.append("header: unknown schema")
    if h.get("label") != LABEL:
        problems.append(f"header: the label '{LABEL}' is missing")
    lock = h.get("lock") or {}
    if not lock.get("hash") or lock["hash"] != runlog.sha256_hex(runlog.canonical(lock_fixed(h))):
        problems.append("header: the lock hash does not match the fixed parts of the header")
    if not runlog.HEX64.fullmatch(str(h.get("bench_settings_sha256"))):
        problems.append("header: the bench settings hash is missing")
    if (h.get("owner_confirmation") or {}).get("typed") != "yes":
        problems.append("header: the typed confirmation is missing")
    cells = h.get("cells") if isinstance(h.get("cells"), list) else []
    rows = [r for r in recs if r.get("type") == "row"]
    if recs[-1].get("type") != "summary":
        problems.append("the last record is not the summary (the bench did not finish cleanly)")
    if [r.get("cell") for r in rows] != [c.get("cell") for c in cells]:
        problems.append("the rows are not one for each cell of the header, in its order")
        return problems
    for row, cell in zip(rows, cells):
        where = f"cell {cell.get('cell')} ({cell.get('procedure')}, tier {cell.get('tier')}, {cell.get('model_id')})"
        log = path.parent / str(cell.get("run_dir")) / "run_log.jsonl"
        if LABEL not in (row.get("labels") or []):
            problems.append(f"{where}: the row does not carry the label '{LABEL}'")
        if row.get("status") not in ROW_STATES:
            problems.append(f"{where}: the row status is unknown")
            continue
        if row["status"] != RAN:
            # A cell after the stop sent nothing, so its folder holds no log. An engine that raised can leave an
            # unsigned log; the report does not rely on it.
            if row["status"] == NOT_RUN and log.exists():
                problems.append(f"{where}: the row says '{row['status']}', but the run folder holds a run log")
            if not row.get("reason"):
                problems.append(f"{where}: the row gives no reason")
            continue
        if row.get("log") != log.relative_to(path.parent).as_posix():
            problems.append(f"{where}: the row names a log outside the run folder of its cell")
            continue
        if not log.is_file():
            problems.append(f"{where}: the member log is missing ({cell.get('run_dir')})")
            continue
        if runlog.sha256_hex(log.read_bytes()) != row.get("log_sha256"):
            problems.append(f"{where}: the member log does not have the SHA-256 in the row")
        sig = log.with_name(log.name + ".sig")
        ok, msg = runlog.verify_log(log, sig, pubkey_path, repo=repo, use_repo=use_repo)
        if not ok:
            problems.append(f"{where}: the member log fails verify: {msg.splitlines()[0]}")
            continue
        if _signature(log, sig, pubkey_path)[2] != report_pem:
            problems.append(f"{where}: the member log is signed with a key other than the key of the report")
        member = runlog.read_log(log)[0]
        if member.get("settings_hash") != cell.get("settings_hash"):
            problems.append(f"{where}: the settings hash of the member log is not that of the cell")
        if row != row_from_log(log, cell, path.parent):
            problems.append(f"{where}: the row differs from the row that its member log gives")
    s = recs[-1] if recs[-1].get("type") == "summary" else {}
    counts = {st: sum(1 for r in rows if r.get("status") == st) for st in ROW_STATES}
    if s and (s.get("cells_planned"), s.get("cells_run")) != (len(cells), counts[RAN]):
        problems.append("summary: the cell counts do not match the rows")
    return problems


def verify(path, sig_path=None, pubkey_path=None, repo=REPO, use_repo=True):
    """(ok, message). The signature of the report, then its form and each member log (check)."""
    path = Path(path)
    sig_path = Path(sig_path) if sig_path else path.with_name(path.name + ".sig")
    ok, msg, pem = _signature(path, sig_path, pubkey_path)
    if not ok:
        return False, f"FAIL: {msg}"
    problems = check(path, pem, pubkey_path, repo=repo, use_repo=use_repo)
    if problems:
        return False, "FAIL: the signature is valid but the report or a member log is not: " + "; ".join(problems)
    pin = "pinned public key" if pubkey_path else "public key taken from the signature file (not pinned)"
    rows = [r for r in runlog.read_log(path) if r.get("type") == "row"]
    ran = sum(1 for r in rows if r.get("status") == RAN)
    return True, (f"PASS: signature valid ({pin}); report form ok; {ran} member logs verified "
                  f"({'with' if use_repo else 'without'} the repository); {len(rows) - ran} cells did not run\n"
                  f"label: {LABEL}")
