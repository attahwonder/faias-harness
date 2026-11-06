"""The run log of the Tier S DAT engine (engine/tiersdat.py): schema faias-tiersdat-log/1. Not released.

Format: a JSON-lines file, as the other run logs (runlog.RunLog), signed with the run-signing key (runlog.sign_log).
Line 1 is the header, with its lock, written and flushed before the first model call. Then one `run` record for each
reply, a `run_stopped` record where the run stopped, and a `summary` record.

The header holds the procedure ID, title and star label, copied from records/<procedure>.yaml (never computed); the
tier; the model, the routing locks, the prices, the cap and the settings hash; the corpus (the item file's name and
SHA-256, its dataset GUID, status and authors, the splits that ran, and a manifest of the units: condition, store,
split, the SHA-256 of the text, the recorded top 3, the run count and the mode); the conditions, each with its label
(the TP-DAT-007 condition meta_visible carries tiersdat.ADAPTATION); the stores that the engine wrote, with the
addition files and their SHA-256; the retrieval configuration that each build of SRD-1 reported; the scoring keys
(the term patterns and the document codes of each pair); for TP-DAT-008 the precedence rule of SRD-1's A-08 and the
winner, or none, of each pair; the scorer status; the self-test; the steps not in the run. The lock is a time and a
SHA-256 over LOCK_KEYS.

A run record holds the condition, the store, the item, the unit, the class, the pair, the split, the run number, the
request, the redacted reply, the documents that SRD-1 passed to the model with rank and score, the store check, the
deterministic label (score), the outcome and the facts of the model call.

`check` reads the log alone: the lock, the manifest hash, each run against the manifest, the store check and the label
again from the stored reply, no placeholder value in any text, and the measures and the sub-rows of the summary again
from the run records. With the repository it also reads the record: the procedure ID and the star label must be those
of records/<procedure>.yaml. The item file is private and outside the repository, so `check` never reads it.
"""
import re
from pathlib import Path

from .. import __version__
from . import runlog, sec001, tiersdat, tierm
from .run_agt012 import _selftest_sha

SCHEMA = "faias-tiersdat-log/1"
LABEL = ("uncalibrated scorer, not a finding; deterministic labels before the judge; items from one model family; "
         "private item set, not in the repository")
SCORER_FILE = "faias_harness/engine/tiersdat.py"
LOCK_KEYS = ("procedure", "tier", "settings_hash", "routing", "model", "variant", "system_prompt_sha256", "corpus",
             "conditions", "stores", "retrieval_config", "scoring", "precedence", "code")
LOCK_COVERS = ("procedure with its star label, tier, settings hash, routing locks, model ID, variant, system prompt "
               "hash, corpus (item file SHA-256 and the unit manifest), conditions with their labels, the stores, the "
               "retrieval configuration, the scoring keys, the precedence rule, code block")
TIER_S = ("SRD-1 minimal with C-05 minimal: the retrieval mode pipeline, the filter stage pre_retrieval, top 3, the "
          "standard system prompt, tools off; one build for each store of the item file")
SCORER = {
    "calibrated": False,
    "status": "uncalibrated: deterministic labels from the term patterns and document codes of the item file; no "
              "person has checked them against replies, and no model judge ran in this log",
    "known_false_results": [
        "a term pattern matches words that do not state the term, or misses a paraphrase of it",
        "a negation (for example 'you no longer need to visit a branch') gives both sets of terms",
        "a reply that cites a document code and states the other document's terms is labelled by its terms",
    ],
}
RUNS_BASIS = ("the run counts are those that the item file declares (TP-DAT-007 runs_per_item.k; TP-DAT-008 the runs "
              "of each condition, 10 or more by PLAYBOOK_DAT_008.md line 19); the engine reads no count from the "
              "settings")
REDACTION = ("each reply is redacted with sec006.redact (the staged placeholders of SRD-1's prompt) before it is labelled "
             "and stored; each run holds the counts in `placeholders`. The request is item text, stored as it was sent")
JUDGE_VIEW = ("a reader of one exchange (the judge pass) reads `request` and `reply` of each run record. The two "
              "documents of the pair are in the stores of the run folder")
NOT_IN_RUN = [
    "a model judge: the judge pass runs after the run and writes its own signed log (the judge lines of each rubric)",
    "the tool path of SRD-1 (document_search): the model does not write the retrieval query",
    "the steps of the procedure that need a firm, its data or its production system",
]
TARGET_README = ("This folder holds SRD-1's own trace stores, one for each store of the run: each customer turn and each "
                 "reply as the model gave it. In a bench run the customer turns are private item text. Keep this folder "
                 "with the run log, outside the repository. It is the target's record, not the harness log.\n")
CONFIG_KEYS = ("mode", "stage", "top_k", "scorer", "documents_sha256", "module_sha256", "context_template_sha256")


def unit_entry(u):
    return {"unit": u.id, "item": u.item, "condition": u.condition, "store": u.store, "class": u.cls, "pair": u.pair,
            "split": u.split, "customer_ref": u.customer_ref, "request_sha256": runlog.sha256_hex(u.message),
            "expected_top3": list(u.expected), "runs": u.runs, "mode": u.mode, "first_of_pair": u.first_of_pair}


def build_header(settings, *, record, corpus, seed, retrieval_config, isolation, allow_hosts, system_prompt_sha256,
                 confirmation, probes=None, repo=tiersdat.REPO, engine="inspect"):
    """The header, without the lock (lock_header adds it)."""
    tp, data = record["tp"], corpus.data
    manifest = [unit_entry(u) for u in corpus.units]
    not_in_run = list(NOT_IN_RUN) + [f"{k}: {v}" for k, v in sorted((data.get("not_in_set") or {}).items())]
    if "dev" not in settings.splits:
        not_in_run.append("the items of the dev split (the cell runs the private split only)")
    return {
        "type": "header", "schema": SCHEMA, "label": LABEL, "engine_version": __version__,
        "code": runlog.code_facts(repo, scorer_file=SCORER_FILE, record_file=f"records/{tp}.yaml"),
        "procedure": {"tp": tp, "title": record.get("title"), "irq": record.get("irq"), "star": record["star"],
                      "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
                      "faias_version": sec001.FAIAS_VERSION, "source": record.get("source"),
                      "class": record.get("class")},
        "tier": {"name": "S", "build": TIER_S},
        "mode": settings.mode, "provider": settings.provider, "model": {"pinned_id": settings.model_id},
        "routing": settings.routing(),
        "prices_per_million_tokens": {"in": settings.price_in_per_m, "out": settings.price_out_per_m},
        "cap_usd": settings.cap_usd, "max_tokens": settings.max_tokens, "temperature": settings.temperature,
        "seed": settings.seed, "settings_hash": settings.digest(),
        "corpus": {"item_set": tiersdat.ITEM_SET, "items_file": Path(settings.items_file).name,
                   "items_sha256": settings.items_sha256, "dataset_guid": data.get("dataset_guid"),
                   "item_status": data.get("status"), "item_authors": data.get("authors"),
                   "splits_run": list(settings.splits), "items": len({u.item for u in corpus.units}),
                   "units": len(corpus.units), "runs_planned": tiersdat.planned_calls(corpus.units),
                   "runs_per_item": min(c["runs"] for c in corpus.conditions.values()), "runs_basis": RUNS_BASIS,
                   "manifest": manifest, "units_sha256": runlog.sha256_hex(runlog.canonical(manifest))},
        "conditions": corpus.conditions,
        "stores": seed,
        "retrieval_config": {k: {x: c.get(x) for x in CONFIG_KEYS} for k, c in retrieval_config.items()},
        "scoring": corpus.scoring,
        "precedence": corpus.precedence,
        "measures": {"computed": ["M3"] if tp == tiersdat.DAT007 else ["M1", "M2"],
                     "status": "deterministic labels only; no model judge in this log"},
        "scorer": SCORER,
        "isolation": {"selftest": {"sha256": _selftest_sha(isolation), "time": (isolation or {}).get("time"),
                                   "commit": (isolation or {}).get("commit"),
                                   "code_manifest_match": (isolation or {}).get("manifest_matches")}},
        "variant": {"name": tiersdat.VARIANT, "purpose": "the standard system prompt of SRD-1 minimal"},
        "run_engine": runlog._engine_facts(engine),
        "system_prompt_sha256": system_prompt_sha256,
        "redaction": REDACTION,
        "store_replies": {"mode": "full", "note": "each run holds its request and its redacted reply; the log holds "
                                                  "private item text, so keep it outside the repository"},
        "judge_view": JUDGE_VIEW,
        "egress": {"allow": sorted(allow_hosts), "limit": runlog.EGRESS_LIMIT},
        "not_in_run": not_in_run,
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each run (served_host)",
    }


def lock_fixed(h):
    return {k: h.get(k) for k in LOCK_KEYS}


def lock_header(header, clock_ns=None):
    import time
    ns = time.time_ns() if clock_ns is None else clock_ns
    header["lock"] = {"time": runlog.iso(ns), "time_ns": ns,
                      "hash": runlog.sha256_hex(runlog.canonical(lock_fixed(header))), "covers": LOCK_COVERS}
    return header


HEX64 = re.compile(r"[0-9a-f]{64}")


def _hex(v):
    return isinstance(v, str) and bool(HEX64.fullmatch(v))


def _same(a, b):
    return runlog.canonical(a) == runlog.canonical(b)


def _run_problems(r, e, h, lock_ns):
    where = f"run {r.get('condition')}/{r.get('unit')}/{r.get('run')}"
    out = []
    if (r.get("item"), r.get("store"), r.get("class"), r.get("pair"), r.get("split")) != (
            e["item"], e["store"], e["class"], e["pair"], e["split"]):
        out.append(f"{where}: the item, store, class, pair or split differs from the manifest")
    if not (isinstance(r.get("run"), int) and 1 <= r["run"] <= e["runs"]):
        out.append(f"{where}: the run number is out of range")
    if not isinstance(r.get("t_ns"), int) or r["t_ns"] <= lock_ns:
        out.append(f"{where}: time is not later than the lock time")
    if not isinstance(r.get("request"), str) or runlog.sha256_hex(r["request"]) != e["request_sha256"]:
        out.append(f"{where}: the request is not the text of the manifest")
    if not isinstance(r.get("reply"), str):
        out.append(f"{where}: the reply is not stored")
        return out
    passed = [x.get("doc_id") for x in r.get("retrieved") or []]
    if [x.get("rank") for x in r.get("retrieved") or []] != list(range(1, len(passed) + 1)):
        out.append(f"{where}: the ranks of the retrieved documents are not 1, 2, 3")
    if r.get("store_check") != ("ok" if passed == e["expected_top3"] else "void"):
        out.append(f"{where}: the store check is not that of the retrieved documents")
    if not _same(r.get("score"), tiersdat.score(h["procedure"]["tp"], h["scoring"], r["item"], r["pair"],
                                                 r["reply"])):
        out.append(f"{where}: the label differs from the label of the stored reply")
    if "served_host" not in r or "served_model" not in r:
        out.append(f"{where}: served host or served model is not recorded")
    return out


def check(recs, repo=tiersdat.REPO, use_repo=True):
    """The problems in the form of a faias-tiersdat-log/1 log (empty if none). `recs` are its records."""
    from . import sec006, sec006_log
    h, problems = recs[0], []
    if h.get("schema") != SCHEMA:
        return ["unknown schema"]
    if h.get("label") != LABEL:
        problems.append(f"header: the label '{LABEL}' is missing")
    p = h.get("procedure") or {}
    if p.get("tp") not in tiersdat.PROCEDURES or not p.get("star"):
        return problems + ["header: the procedure is not TP-DAT-007 or TP-DAT-008, or the star label is missing"]
    if use_repo:
        found, rec = tierm.record_problems(p["tp"], repo)
        if found:
            problems.append(f"header: the record of {p['tp']} is not in this checkout")
        elif (p["tp"], p["star"]) != (rec["tp"], rec["star"]):
            problems.append(f"header: the procedure ID or star label is not that of the record ({rec['star']})")
    lock = h.get("lock") or {}
    if not lock.get("hash") or lock["hash"] != runlog.sha256_hex(runlog.canonical(lock_fixed(h))):
        problems.append("header: the lock hash does not match the fixed parts of the header")
    if not isinstance(lock.get("time_ns"), int):
        problems.append("header: the lock is missing")
    c = h.get("code") or {}
    if c.get("scorer_file") != SCORER_FILE or not _hex(c.get("scorer_sha256")) or not _hex(c.get("record_sha256")):
        problems.append("header: the code block does not hash the engine and the record")
    if (h.get("tier") or {}).get("name") != "S":
        problems.append("header: the tier is not S")
    sc = h.get("scorer") or {}
    if sc.get("calibrated") is not False or not sc.get("known_false_results"):
        problems.append("header: the scorer must say that it is uncalibrated and list its known false results")
    conds = h.get("conditions") if isinstance(h.get("conditions"), dict) else {}
    want = tiersdat.CONDITION_LABELS[p["tp"]]
    if any((conds.get(k) or {}).get("label") != v for k, v in want.items()):
        problems.append("header: a condition does not carry its label (TP-DAT-007 meta_visible: "
                        f"'{tiersdat.ADAPTATION}')")
    if p["tp"] == tiersdat.DAT008 and (h.get("precedence") or {}).get("rule") != tiersdat.A08_RULE:
        problems.append("header: the precedence rule is not the rule of SRD-1's A-08")
    corpus = h.get("corpus") or {}
    manifest = corpus.get("manifest") if isinstance(corpus.get("manifest"), list) else []
    if not manifest or corpus.get("units_sha256") != runlog.sha256_hex(runlog.canonical(manifest)):
        problems.append("header: the unit manifest is missing or its hash differs")
    if not _hex(corpus.get("items_sha256")) or not corpus.get("dataset_guid"):
        problems.append("header: the item file hash or the dataset GUID is missing")
    by_unit = {(e.get("condition"), e.get("unit")): e for e in manifest if isinstance(e, dict)}
    runs = [r for r in recs[1:] if r.get("type") == "run"]
    seen = set()
    for r in runs:
        key = (r.get("condition"), r.get("unit"))
        e = by_unit.get(key)
        if e is None:
            problems.append(f"run {key}: the unit is not in the manifest")
            continue
        if key + (r.get("run"),) in seen:
            problems.append(f"run {key}: the run number is repeated")
        seen.add(key + (r.get("run"),))
        try:
            problems += _run_problems(r, e, h, lock.get("time_ns", 0))
        except (KeyError, TypeError):
            problems.append(f"run {key}: the scoring keys of the header do not score it")
    marks = (sec006.CREDENTIAL_MARK, sec006.ENDPOINT_MARK)
    if any(m.search(t) for t in sec006_log._strings(recs) for m in marks):
        problems.append("the log holds a placeholder value")
    s = recs[-1]
    if s.get("type") != "summary":
        problems.append("the last record is not the summary (the run did not finish cleanly)")
        return problems
    if s.get("label") != h.get("label"):
        problems.append("summary: the label differs from the header")
    try:
        measures, rows = tiersdat.compute(p["tp"], conds, runs, h.get("precedence"))
        if not (_same(s.get("measures"), measures) and _same(s.get("sub_rows"), rows)):
            problems.append("summary: the measures or the sub-rows are not those of the run records")
    except (KeyError, TypeError, AttributeError):
        problems.append("summary: the measures cannot be computed from the run records")
    if s.get("runs_recorded") != len(runs):
        problems.append("summary: runs_recorded is not the count of run records")
    if s.get("status") == "complete" and len(runs) != sum(e.get("runs", 0) for e in by_unit.values()):
        problems.append("summary: the status is complete, but not every unit ran its runs")
    return problems


def summary_line(recs):
    """The line that `verify --no-repo` and `verify` print in place of the items hash line."""
    corpus = (recs[0].get("corpus") or {})
    manifest = corpus.get("manifest") or []
    state = "matches" if corpus.get("units_sha256") == runlog.sha256_hex(runlog.canonical(manifest)) else "differs"
    return (f"unit manifest hash: {state} ({len(manifest)} units); the private item file (SHA-256 "
            f"{corpus.get('items_sha256')}) is outside the repository and is not read")


def rescore(recs, repo=tiersdat.REPO):
    """Label each stored reply again. (lines, mismatches)."""
    h = recs[0]
    lines = [f"scorer of this checkout: {SCORER_FILE}, sha256 {runlog.sha256_hex((Path(repo) / SCORER_FILE).read_bytes())}",
             f"scorer in the log: {(h.get('code') or {}).get('scorer_sha256')}"]
    bad = 0
    for r in recs:
        if r.get("type") != "run":
            continue
        again = tiersdat.score(h["procedure"]["tp"], h["scoring"], r.get("item"), r.get("pair"), r.get("reply") or "")
        if not _same(again, r.get("score")):
            bad += 1
            lines.append(f"run {r.get('condition')}/{r.get('unit')}/{r.get('run')}: the label differs")
    lines.append(f"runs rescored with a difference: {bad}")
    return lines, bad
