"""The run log of the Tier S AGT engine (engine/tiers_agt.py): schema faias-tiers-agt-log/1.

Format: a JSON-lines file, as the other run logs (runlog.RunLog), signed with the run-signing key (runlog.sign_log).
Line 1 is the header, with its lock, written and flushed before the first model call. Then one `run` record for each
run of each item, a `run_stopped` record where the run stopped, and a `summary` record.

The header holds the procedure ID, title and star label, copied from records/<procedure>.yaml (never computed); the
tier and the build; the model, the routing locks, the prices, the cap and the settings hash; the corpus (the item
file's and the store's names and SHA-256, the dataset GUID, status and authors, the splits that ran, k and its basis,
and a manifest of the items with the SHA-256 of each turn and the scoring inputs of each item); the tools block of
the tool emulator; the refund gate and the retrieval as SRD-1 states them; the self-test; the scorer status; the
measures that the run computes and those it does not; the store mode; the judge view; the steps not in the run. The
lock is a time and a SHA-256 over LOCK_KEYS. The label says "Model propensity, not control" and, with k below 10, that
k is below the preferred run count of PLAYBOOK_AGT.md line 45.

A run record holds the item, its skeleton, class, arm and split, the run number, each turn (its request and reply, or
their SHA-256), the `tool_call` lines of the trace with their turn, the ledger lines of the run and the facts of the
scorer (tiers_agt_score). It also holds `request` and `reply` (or their SHA-256): the judge view, as in the Tier M log.

`check` reads the log alone: the lock, the manifest hash, each run against the manifest, the store mode, the facts
again from the stored lines (tiers_agt_score.score), the measures and scores again from the run records
(tiers_agt_score.summarise), no placeholder value in any text, and the summary counts. With the repository it also
reads the record. The item file and the store are private and outside the repository, so `check` never reads them:
`verify` says "repository item set: not checked". `rescore` scores each run again from its stored lines; it needs no
reply text, so it works with hash_only too.
"""
import re
import time
from pathlib import Path

from .. import __version__
from . import runlog, sec001, tierm, tiers_agt
from .tiers_agt_score import PROPENSITY, score, summarise

SCHEMA = "faias-tiers-agt-log/1"
LABEL = ("uncalibrated scorer, not a finding; Model propensity, not control; private item set and store, not in the "
         "repository")
SCORER_FILE = "faias_harness/engine/tiers_agt_score.py"
ENGINE_FILES = ("faias_harness/engine/tiers_agt.py", "faias_harness/engine/tiers_agt_log.py",
                "targets/srd1/app/build.py", "targets/srd1/app/orchestrator.py")
LOCK_KEYS = ("procedure", "tier", "settings_hash", "routing", "model", "variant", "system_prompt_sha256", "corpus",
             "tools", "gate", "store_replies", "code")
LOCK_COVERS = ("procedure with its star label, tier and build, settings hash, routing locks, model ID, variant, system "
               "prompt hash, corpus (item file and store SHA-256, item manifest with the scoring inputs), tools block, "
               "refund gate, reply storage, code block")
SCORER = {
    "calibrated": False,
    "status": "uncalibrated: deterministic rules on SRD-1's trace and the emulator's ledger (the rubrics of the draft "
              "item sets, section 2); no person has checked the rules against runs; the text criteria are not judged "
              "in this log",
    "known_false_results": [
        "a refund decision is read from arguments.fee_id; a call that names the fee another way is not a decision on it",
        "a check counts only when a document_search result returned the document; a model that reads the document "
        "another way is scored as not checked",
        "TP-AGT-001: a followed trap that a reply flagged is still 'trap followed' here; the flag needs the judge",
        "TP-AGT-001: the order of a search and a decision in one tool round is the order of the calls, not of what "
        "the model saw",
    ],
}
STORE_NOTES = {
    "full": "each turn holds its request and its reply, and each tool line its arguments (redacted: the staged "
            "placeholders of SRD-1's prompt); the log holds private item text, so keep it outside the repository",
    "hash_only": "each turn holds the SHA-256 of its request and of its reply; tool lines keep identifiers and "
                 "amounts, and the SHA-256 of other argument text; the facts can be scored again from the lines",
}
JUDGE_VIEW = ("a reader of one exchange (the judge pass) reads `request` and `reply` of each run record: the "
              "conversation before the last reply as a transcript, and the last reply. Each turn's own reply is in "
              "`turns`. With store_replies hash_only there is nothing to judge")
TARGET_README = ("This folder holds SRD-1's own trace store: each customer turn, each reply and each tool call as the "
                 "model gave it. In a Tier S bench run the customer turns are private item text. Keep this folder "
                 "with the run log, outside the repository. It is the target's record, not the harness log.\n")
NOT_IN_RUN = [
    "the text criteria of the rubric (flags, reasons, hand-off offers, acknowledgements): for the judge pass or human "
    "labellers; not judged in this log",
    "a reference prompt other than the standard prompt (the rule 6 proposal of the item sets, and the TP-AGT-010 "
    "process map in the prompt): not registered; DECISIONS_WAVE2 D-A1 runs the standard prompt first",
    "the tools hand_off, payment_instruction and complaint_case: not in SRD-1 minimal",
    "SME certification of traps and edge cases, and process-owner sign-off: not done",
    "the steps of the procedure that need a firm, its data or its production system",
]


def label(k, procedure):
    note = tiers_agt.runs_note(k, procedure)
    return LABEL if note is None else f"{LABEL}; {note}"


def unit_entry(u):
    return {"unit": u.id, "skeleton": u.skeleton, "class": u.cls, "arm": u.arm, "split": u.split,
            "customer_ref": u.customer_ref, "turns": len(u.turns),
            "turn_sha256": [runlog.sha256_hex(t) for t in u.turns], "spec": u.spec}


def code_block(procedure, repo):
    out = runlog.code_facts(repo, scorer_file=SCORER_FILE, record_file=f"records/{procedure}.yaml")
    out["files"] = {f: runlog.sha256_hex((Path(repo) / f).read_bytes()) for f in ENGINE_FILES}
    return out


def build_header(settings, *, record, data, units, allow_hosts, system_prompt_sha256, prompt_source, tools, gate,
                 retrieval, isolation, confirmation, probes=None, repo=tiers_agt.REPO, engine="inspect"):
    """The header, without the lock (lock_header adds it)."""
    tp, k = record["tp"], settings.runs_per_item
    manifest = [unit_entry(u) for u in units]
    not_in_run = list(NOT_IN_RUN)
    if "dev" not in settings.splits:
        not_in_run.append("the items of the dev split (the cell runs the private split only)")
    retr = {k2: retrieval.get(k2) for k2 in ("enabled", "mode", "stage", "documents_sha256", "top_k", "scorer")}
    retr["credential"] = (retrieval.get("tool_path") or {}).get("credential")
    return {
        "type": "header", "schema": SCHEMA, "label": label(k, tp), "engine_version": __version__,
        "code": code_block(tp, repo),
        "procedure": {"tp": tp, "title": record.get("title"), "irq": record.get("irq"), "star": record["star"],
                      "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
                      "faias_version": sec001.FAIAS_VERSION, "source": record.get("source"),
                      "class": record.get("class")},
        "tier": {"name": "S", "build": tiers_agt.BUILDS[tp]},
        "mode": settings.mode, "provider": settings.provider, "model": {"pinned_id": settings.model_id},
        "routing": settings.routing(),
        "prices_per_million_tokens": {"in": settings.price_in_per_m, "out": settings.price_out_per_m},
        "cap_usd": settings.cap_usd, "max_tokens": settings.max_tokens, "temperature": settings.temperature,
        "seed": settings.seed, "settings_hash": settings.digest(),
        "corpus": {"item_set": tiers_agt.ITEM_SET, "items_file": Path(settings.items_file).name,
                   "items_sha256": settings.items_sha256, "store_file": Path(settings.store_file).name,
                   "store_sha256": settings.store_sha256, "dataset_guid": data.get("dataset_guid"),
                   "item_status": data.get("status"), "item_authors": data.get("authors"),
                   "item_labels": data.get("labels"), "splits_run": list(settings.splits), "items": len(units),
                   "runs_per_item": k, "runs_per_item_basis": tiers_agt.RUNS_SOURCE,
                   "manifest": manifest, "units_sha256": runlog.sha256_hex(runlog.canonical(manifest)),
                   "scoring": "deterministic rules on the trace and the ledger (tiers_agt_score.py); no model judge "
                              "in this log"},
        "tools": tools, "gate": {k2: gate.get(k2) for k2 in ("enabled", "layer", "cap_gbp", "session_limit_gbp",
                                                             "source", "sha256", "module", "function")},
        "retrieval": retr,
        "variant": {"name": tiers_agt.VARIANT, "purpose": "the standard system prompt of SRD-1 minimal",
                    "prompt_source": prompt_source},
        "run_engine": runlog._engine_facts(engine),
        "scorer": SCORER,
        "labels": [PROPENSITY] + ([tiers_agt.runs_note(k, tp)] if tiers_agt.runs_note(k, tp) else []),
        "system_prompt_sha256": system_prompt_sha256,
        "isolation": {"selftest_time": (isolation or {}).get("time"), "commit": (isolation or {}).get("commit"),
                      "code_manifest_match": (isolation or {}).get("manifest_matches"),
                      "pinned": (isolation or {}).get("pinned")},
        "store_replies": {"mode": settings.store_replies, "note": STORE_NOTES[settings.store_replies]},
        "judge_view": JUDGE_VIEW,
        "egress": {"allow": sorted(allow_hosts), "limit": runlog.EGRESS_LIMIT},
        "not_in_run": not_in_run, "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each turn (calls)",
    }


def lock_fixed(h):
    return {k: h.get(k) for k in LOCK_KEYS}


def lock_header(header, clock_ns=None):
    ns = time.time_ns() if clock_ns is None else clock_ns
    header["lock"] = {"time": runlog.iso(ns), "time_ns": ns,
                      "hash": runlog.sha256_hex(runlog.canonical(lock_fixed(header))), "covers": LOCK_COVERS}
    return header


def _outcome(replies):
    empty = [not (r or "").strip() for r, _ in replies]
    return "no_result" if all(empty) else "replied" if not any(empty) else "partial"


def run_record(unit, n, t0, replies, turn_lines, lines, ledger, exhausted, facts, store):
    """The `run` record of one run."""
    full = store == "full"
    turns = []
    for t, (text, (reply, counts)) in enumerate(zip(unit.turns, replies), 1):
        e = {"turn": t}
        e.update({"request": text} if full else {"request_sha256": runlog.sha256_hex(text)})
        e.update({"reply": reply} if full else {"reply_sha256": runlog.sha256_hex(reply)})
        tl = turn_lines[t - 1] if t - 1 < len(turn_lines) else {}
        e.update(placeholders=counts, model_calls=tl.get("model_calls"), tool_rounds=tl.get("tool_rounds"),
                 calls=tl.get("calls") or [])
        turns.append(e)
    rec = {"type": "run", "item": unit.id, "skeleton": unit.skeleton, "class": unit.cls, "arm": unit.arm,
           "split": unit.split, "run": n, "t_ns": t0, "time": runlog.iso(t0), "turns": turns,
           "tool_lines": lines, "ledger_lines": ledger, "rounds_exhausted": exhausted}
    view = tierm.transcript(list(unit.turns), [r for r, _ in replies])
    rec.update({"request": view, "reply": replies[-1][0]} if full else
               {"request_sha256": runlog.sha256_hex(view), "reply_sha256": runlog.sha256_hex(replies[-1][0])})
    rec.update(facts=facts, score=facts, outcome=_outcome(replies),
               judge={"status": "not judged in this log",
                      "criteria": list(unit.judge) if unit.judge is not None else None})
    return rec


def reconciliation(records):
    out = {"unmatched_trace": 0, "unmatched_ledger": 0}
    for r in records:
        for k in out:
            out[k] += ((r.get("facts") or {}).get("reconciliation") or {}).get(k, 0)
    return out


HEX64 = re.compile(r"[0-9a-f]{64}")


def _hex(v):
    return isinstance(v, str) and bool(HEX64.fullmatch(v))


def _turn_problems(where, t, want, full):
    out = []
    if full:
        if not isinstance(t.get("request"), str) or runlog.sha256_hex(t["request"]) != want:
            out.append(f"{where}: the request is not the text of the manifest")
        if not isinstance(t.get("reply"), str) or "reply_sha256" in t:
            out.append(f"{where}: a full record holds the reply and no reply hash")
    else:
        if "request" in t or "reply" in t:
            out.append(f"{where}: a hash_only record holds text")
        if t.get("request_sha256") != want or not _hex(t.get("reply_sha256")):
            out.append(f"{where}: the request hash is not that of the manifest, or the reply hash is missing")
    return out


def _free_text_problems(where, lines, full):
    """With hash_only, a tool or ledger line keeps no argument text but the identifiers, the amount and one hash."""
    if full:
        return []
    for x in lines:
        args = x.get("arguments") or {}
        if set(args) - set(tiers_agt.ID_ARGS) - {"other_sha256"}:
            return [f"{where}: a hash_only record keeps argument text"]
    return []


def check(recs, repo=tiers_agt.REPO, use_repo=True):
    """The problems in the form of a faias-tiers-agt-log/1 log (empty if none). `recs` are its records."""
    from . import sec006, sec006_log
    h, problems = recs[0], []
    if h.get("schema") != SCHEMA:
        return ["unknown schema"]
    p = h.get("procedure") or {}
    tp = p.get("tp")
    if tp not in tiers_agt.PROCEDURES or not p.get("star"):
        problems.append("header: the procedure ID or the star label is missing or not of this engine")
        return problems
    if use_repo:
        found, rec = tierm.record_problems(tp, repo)
        if found:
            problems.append(f"header: the record of {tp} is not in this checkout")
        elif (tp, p["star"]) != (rec["tp"], rec["star"]):
            problems.append(f"header: the procedure ID or star label is not that of the record ({rec['star']})")
    lock = h.get("lock") or {}
    if not lock.get("hash") or lock["hash"] != runlog.sha256_hex(runlog.canonical(lock_fixed(h))):
        problems.append("header: the lock hash does not match the fixed parts of the header")
    if not isinstance(lock.get("time_ns"), int):
        problems.append("header: the lock is missing")
    c = h.get("code") or {}
    if c.get("scorer_file") != SCORER_FILE or not _hex(c.get("scorer_sha256")) or not _hex(c.get("record_sha256")):
        problems.append("header: the code block does not hash the scorer and the record")
    if (h.get("tier") or {}).get("name") != "S":
        problems.append("header: the tier is not S")
    if not (h.get("model") or {}).get("pinned_id") or not h.get("settings_hash"):
        problems.append("header: the model ID or the settings hash is missing")
    sc = h.get("scorer") or {}
    if sc.get("calibrated") is not False or not sc.get("known_false_results"):
        problems.append("header: the scorer must say that it is uncalibrated and list its known false results")
    if PROPENSITY not in (h.get("labels") or []):
        problems.append(f"header: the label '{PROPENSITY}' is missing")
    if (h.get("run_engine") or {}).get("name") not in ("inspect_ai", "direct"):
        problems.append("header: the run engine is missing or unknown")
    store = (h.get("store_replies") or {}).get("mode")
    if store not in tiers_agt.STORE_MODES:
        problems.append("header: store_replies is missing or unknown")
    corpus = h.get("corpus") or {}
    manifest = corpus.get("manifest") if isinstance(corpus.get("manifest"), list) else []
    if not manifest or corpus.get("units_sha256") != runlog.sha256_hex(runlog.canonical(manifest)):
        problems.append("header: the item manifest is missing or its hash differs")
    if not (_hex(corpus.get("items_sha256")) and _hex(corpus.get("store_sha256")) and corpus.get("dataset_guid")):
        problems.append("header: the item file hash, the store hash or the dataset GUID is missing")
    if not (h.get("tools") or {}).get("store_sha256") == corpus.get("store_sha256"):
        problems.append("header: the emulator's store is not the store of the corpus")
    k = corpus.get("runs_per_item")
    if not (isinstance(k, int) and not isinstance(k, bool) and k >= tiers_agt.RUNS_MIN):
        problems.append(f"header: runs_per_item is missing or below {tiers_agt.RUNS_MIN}")
        k = 0
    if h.get("label") != label(k, tp):
        problems.append("header: the label is not the label of the engine for its runs per item")
    by_unit = {}
    for e in manifest:
        if not (isinstance(e, dict) and isinstance(e.get("turns"), int) and isinstance(e.get("turn_sha256"), list)
                and len(e["turn_sha256"]) == e["turns"] >= 1 and isinstance(e.get("spec"), dict)):
            problems.append("header: a manifest entry is malformed")
            continue
        by_unit[e["unit"]] = e
    full, seen = store == "full", set()
    runs = [r for r in recs[1:] if r.get("type") == "run"]
    for r in runs:
        u, n = r.get("item"), r.get("run")
        where = f"run {u}/{n}"
        e = by_unit.get(u)
        if e is None:
            problems.append(f"{where}: the item is not in the manifest")
            continue
        if (r.get("skeleton"), r.get("class"), r.get("arm"), r.get("split")) != (
                e["skeleton"], e["class"], e["arm"], e["split"]):
            problems.append(f"{where}: the skeleton, class, arm or split differs from the manifest")
        if not (isinstance(n, int) and 1 <= n <= k) or (u, n) in seen:
            problems.append(f"{where}: the run number is out of range or repeated")
        seen.add((u, n))
        if not isinstance(r.get("t_ns"), int) or r["t_ns"] <= lock.get("time_ns", 0):
            problems.append(f"{where}: time is not later than the lock time")
        turns = r.get("turns") if isinstance(r.get("turns"), list) else []
        if [t.get("turn") for t in turns] != list(range(1, e["turns"] + 1)):
            problems.append(f"{where}: the turns are not those of the manifest")
            continue
        for t in turns:
            problems += _turn_problems(f"{where} turn {t['turn']}", t, e["turn_sha256"][t["turn"] - 1], full)
            if not isinstance(t.get("calls"), list):
                problems.append(f"{where} turn {t['turn']}: the model calls of the turn are not recorded")
        lines, ledger = r.get("tool_lines"), r.get("ledger_lines")
        if not (isinstance(lines, list) and isinstance(ledger, list) and isinstance(r.get("rounds_exhausted"), list)):
            problems.append(f"{where}: the tool lines, the ledger lines or the exhausted turns are missing")
            continue
        problems += _free_text_problems(where, lines + ledger, full)
        if full:
            replies = [t.get("reply") for t in turns]
            view = tierm.transcript([t.get("request") for t in turns], replies)
            if (r.get("request"), r.get("reply")) != (view, replies[-1]) or "reply_sha256" in r:
                problems.append(f"{where}: the judge view (request and reply) is not that of its turns")
        elif "request" in r or "reply" in r or r.get("reply_sha256") != turns[-1].get("reply_sha256"):
            problems.append(f"{where}: a hash_only record holds text, or its hashes are not those of its turns")
        again = score(tp, e["spec"], lines, ledger, {"replies": len(turns), "rounds_exhausted": r["rounds_exhausted"]})
        if r.get("facts") != again or r.get("score") != again:
            problems.append(f"{where}: the facts differ from the facts of its stored lines")
    marks = (sec006.CREDENTIAL_MARK, sec006.ENDPOINT_MARK)
    if any(m.search(t) for t in sec006_log._strings(recs) for m in marks):
        problems.append("the log holds a placeholder value")
    s = recs[-1]
    if s.get("type") != "summary":
        problems.append("the last record is not the summary (the run did not finish cleanly)")
        return problems
    measures, not_computed, scores = summarise(tp, runs, by_unit)
    if (s.get("measures"), s.get("measures_not_computed"), s.get("scores")) != (measures, not_computed, scores):
        problems.append("summary: the measures or scores are not those of the run records")
    if s.get("label") != h.get("label") or s.get("reconciliation") != reconciliation(runs):
        problems.append("summary: the label or the reconciliation is not that of the header and the run records")
    if s.get("runs_recorded") != len(runs):
        problems.append("summary: runs_recorded is not the count of run records")
    if s.get("status") == "complete" and len(runs) != len(by_unit) * k:
        problems.append("summary: the status is complete, but not every item ran k times")
    if (s.get("store_file") or {}).get("unchanged") is not True:
        problems.append("summary: the store file is not stated unchanged")
    return problems


def summary_line(recs):
    """The line that `verify --no-repo` and `verify` print in place of the items hash line."""
    corpus = recs[0].get("corpus") or {}
    manifest = corpus.get("manifest") or []
    state = "matches" if corpus.get("units_sha256") == runlog.sha256_hex(runlog.canonical(manifest)) else "differs"
    return (f"item manifest hash: {state} ({len(manifest)} items); the private item file (SHA-256 "
            f"{corpus.get('items_sha256')}) and store (SHA-256 {corpus.get('store_sha256')}) are outside the "
            "repository and are not read")


def rescore(recs, repo=tiers_agt.REPO):
    """Score each run again from its stored tool and ledger lines. (lines, mismatches)."""
    h = recs[0]
    tp = (h.get("procedure") or {}).get("tp")
    lines = [f"scorer of this checkout: {SCORER_FILE}, sha256 {runlog.sha256_hex((Path(repo) / SCORER_FILE).read_bytes())}",
             f"scorer in the log: {(h.get('code') or {}).get('scorer_sha256')}"]
    by_unit = {e.get("unit"): e for e in (h.get("corpus") or {}).get("manifest") or []}
    bad = 0
    for r in recs:
        if r.get("type") != "run":
            continue
        e = by_unit.get(r.get("item"))
        again = None if e is None else score(tp, e["spec"], r.get("tool_lines") or [], r.get("ledger_lines") or [],
                                             {"replies": len(r.get("turns") or []),
                                              "rounds_exhausted": r.get("rounds_exhausted") or []})
        if again != r.get("facts"):
            bad += 1
            lines.append(f"run {r.get('item')}/{r.get('run')}: the facts differ")
    lines.append(f"runs rescored with a difference: {bad}")
    return lines, bad
