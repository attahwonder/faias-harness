"""The run log of the Tier M engine (engine/tierm.py): schema faias-tierm-log/1.

Format: a JSON-lines file, as the other run logs (runlog.RunLog), signed with the run-signing key (runlog.sign_log).
Line 1 is the header, with its lock, written and flushed before the first model call. Then one `run` record for each
run of each unit, a `run_stopped` record where the run stopped, and a `summary` record.

The header holds the procedure ID, title and star label, copied from records/<procedure>.yaml (never computed); the
tier; the model, the routing locks, the prices, the cap and the settings hash; the corpus (the item file's name and
SHA-256, its dataset GUID, status and authors, the splits that ran, k, and a manifest of the units with the SHA-256 of
each turn, the checks and the scoring fields (`meta`) of each unit); the checks that the item file declares, as declared; the store mode; the scorer
status ("uncalibrated"); "item outcomes, measure not computed"; what a reader of one exchange (the judge pass) reads; the
steps not in the run. The lock is a time and a SHA-256 over LOCK_KEYS.

A run record holds the unit, its item, the variant it is of, its class and split, the run number, and each turn: its
request (or its SHA-256), its reply (or its SHA-256 with store_replies hash_only), the placeholder counts, the checks
and the facts of the model call. It also holds `request` and `reply` (or their SHA-256): the judge view of tierm.py
step 6. `score` holds the checks of the last reply, `outcome` replied, partial or no_result, and `judge` the criteria
that the item names for a judge (`scoring.judge`), with "not judged in this log".

`check` reads the log alone: the lock, the manifest hash, each run against the manifest (unit, split, turns, the SHA-256
of each request, the check names), the store mode, the checks again on each stored reply (full only), no placeholder
value in any text, and the summary's item outcomes against the records. With the repository it also reads the record:
the procedure ID and the star label must be those of records/<procedure>.yaml. The item file is private and outside the
repository, so it is never read by `check`: `verify` says "repository item set: not checked".
"""
import re
from pathlib import Path

from .. import __version__
from . import runlog, sec001, tierm

SCHEMA = "faias-tierm-log/1"
LABEL = "uncalibrated scorer, not a finding; item outcomes, measure not computed; private item set, not in the repository"
MEASURE_STATUS = "item outcomes, measure not computed"
SCORER_FILE = "faias_harness/engine/tierm.py"
LOCK_KEYS = ("procedure", "tier", "settings_hash", "routing", "model", "variant", "system_prompt_sha256", "corpus",
             "checks", "store_replies", "code")
LOCK_COVERS = ("procedure with its star label, tier, settings hash, routing locks, model ID, variant, system prompt "
               "hash, corpus (item file SHA-256 and the unit manifest), declared checks, reply storage, code block")
TIER_M = ("SRD-1 minimal with its standard system prompt, tools off and retrieval off: one model call for each customer "
          "turn")
SCORER = {
    "calibrated": False,
    "status": "uncalibrated: the deterministic checks that the item file declares give item outcomes; no measure of "
              "the procedure is computed and no person has checked the checks against replies",
    "known_false_results": [
        "a pattern check matches words that do not carry the behaviour, or misses a paraphrase of it",
        "a decision extractor gives NONE for a reply that states its decision in another form, and reads a decision "
        "from a reply that only quotes the answer instruction",
    ],
}
MEASURES = {"status": MEASURE_STATUS,
            "note": "the engine writes item-level outcomes only; for a procedure with a measure scorer "
                    "(tierm_measures.py), the bench report computes the measures from this log afterwards"}
JUDGE_VIEW = ("a reader of one exchange (the judge pass, which runs after the run and writes its own signed log) reads "
              "`request` and `reply` of each run record: for one turn, the customer text and the reply; for more "
              "turns, the conversation before the last reply as a transcript, and the last reply. Each turn's own reply "
              "is in `turns`. With store_replies hash_only the record holds SHA-256 values only, and there is nothing "
              "to judge")
STORE_NOTES = {
    "full": "each turn holds its request and its reply (redacted: the staged placeholders of SRD-1's prompt); the log "
            "holds private item text, so keep it outside the repository",
    "hash_only": "each turn holds the SHA-256 of its request and of its reply, not the text; the checks ran on the "
                 "reply before it was hashed, and the log cannot be checked again from the replies",
}
REDACTION = ("each reply is redacted with sec006.redact (the staged placeholders of SRD-1's prompt) before the checks "
             "and before it is stored; each turn holds the counts in `placeholders`. The customer turns are item text "
             "and are stored as they were sent")
NOT_IN_RUN = [
    "a measure of the procedure: the engine records item outcomes only (the bench report computes the measures of "
    "a procedure with a scorer from this log)",
    "a model judge: the judge pass runs after the run and writes its own signed log",
    "Tier S: tools and retrieval are off",
    "the steps of the procedure that need a firm, its data or its production system",
]
TARGET_README = ("This folder holds SRD-1's own trace store: each customer turn and each reply as the model gave it. In "
                 "a Tier M bench run the customer turns are private item text. Keep this folder with the run log, "
                 "outside the repository. It is the target's record, not the harness log.\n")


def unit_entry(u):
    return {"unit": u.id, "item": u.item, "variant_of": u.variant_of, "class": u.cls, "split": u.split,
            "turns": len(u.turns), "turn_sha256": [runlog.sha256_hex(t) for t in u.turns], "checks": list(u.checks),
            "meta": dict(u.meta)}


def build_header(settings, *, record, data, units, checks, allow_hosts, system_prompt_sha256, confirmation,
                 probes=None, repo=tierm.REPO, engine="inspect"):
    """The header, without the lock (lock_header adds it)."""
    tp = record["tp"]
    manifest = [unit_entry(u) for u in units]
    k = settings.runs_per_item
    not_in_run = list(NOT_IN_RUN)
    if "dev" not in settings.splits:
        not_in_run.append("the items of the dev split (the cell runs the private split only)")
    return {
        "type": "header", "schema": SCHEMA, "label": LABEL, "engine_version": __version__,
        "code": runlog.code_facts(repo, scorer_file=SCORER_FILE, record_file=f"records/{tp}.yaml"),
        "procedure": {"tp": tp, "title": record.get("title"), "irq": record.get("irq"), "star": record["star"],
                      "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
                      "faias_version": sec001.FAIAS_VERSION, "source": record.get("source"),
                      "class": record.get("class")},
        "tier": {"name": "M", "build": TIER_M},
        "mode": settings.mode, "provider": settings.provider, "model": {"pinned_id": settings.model_id},
        "routing": settings.routing(),
        "prices_per_million_tokens": {"in": settings.price_in_per_m, "out": settings.price_out_per_m},
        "cap_usd": settings.cap_usd, "max_tokens": settings.max_tokens, "temperature": settings.temperature,
        "seed": settings.seed, "settings_hash": settings.digest(),
        "corpus": {"item_set": tierm.ITEM_SET, "items_file": Path(settings.items_file).name,
                   "items_sha256": settings.items_sha256, "dataset_guid": data.get("dataset_guid"),
                   "item_status": data.get("status"), "item_authors": data.get("authors"),
                   "splits_run": list(settings.splits), "items": len({u.item for u in units}), "units": len(units),
                   "runs_per_item": k,
                   "runs_per_item_basis": "set in the bench file (3 to 20); the engine reads no count from the procedure",
                   "manifest": manifest, "units_sha256": runlog.sha256_hex(runlog.canonical(manifest)),
                   "scoring": "the deterministic checks that the item file declares, on each reply; item outcomes "
                              "only; no measure; no model judge in this log. Each manifest entry's `meta` holds the "
                              "item fields that the measure scorer of the procedure reads (tierm_measures.FIELDS), "
                              "in both store modes"},
        "checks": checks,
        "measures": MEASURES,
        "variant": {"name": tierm.VARIANT, "purpose": "the standard system prompt of SRD-1 minimal"},
        "run_engine": runlog._engine_facts(engine),
        "scorer": SCORER,
        "system_prompt_sha256": system_prompt_sha256,
        "redaction": REDACTION,
        "store_replies": {"mode": settings.store_replies, "note": STORE_NOTES[settings.store_replies]},
        "judge_view": JUDGE_VIEW,
        "egress": {"allow": sorted(allow_hosts), "limit": runlog.EGRESS_LIMIT},
        "not_in_run": not_in_run,
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each turn (served_host)",
    }


def lock_fixed(h):
    return {k: h.get(k) for k in LOCK_KEYS}


def lock_header(header, clock_ns=None):
    import time
    ns = time.time_ns() if clock_ns is None else clock_ns
    header["lock"] = {"time": runlog.iso(ns), "time_ns": ns,
                      "hash": runlog.sha256_hex(runlog.canonical(lock_fixed(header))), "covers": LOCK_COVERS}
    return header


def _inc(d, key):
    d[str(key)] = d.get(str(key), 0) + 1


def outcomes(records):
    """The item outcomes of the run records, by split: runs, units, replies, the outcome of each run and, for each
    check, the replies that matched (a pattern check) or the count of each value and route (a decision check)."""
    out, units = {}, {}
    for r in records:
        sp = str(r.get("split"))
        b = out.setdefault(sp, {"runs": 0, "units": 0, "replies": 0, "run_outcomes": {}, "checks": {}})
        units.setdefault(sp, set()).add(r.get("item"))
        b["runs"] += 1
        _inc(b["run_outcomes"], r.get("outcome"))
        for t in r.get("turns") or []:
            b["replies"] += 1
            for name, res in sorted((t.get("checks") or {}).items()):
                c = b["checks"].setdefault(name, {})
                if isinstance(res, dict) and "matched" in res:
                    c["replies"] = c.get("replies", 0) + 1
                    c["matched"] = c.get("matched", 0) + (1 if res["matched"] else 0)
                else:
                    res = res if isinstance(res, dict) else {}
                    _inc(c.setdefault("values", {}), res.get("value"))
                    _inc(c.setdefault("routes", {}), res.get("route"))
    for sp, b in out.items():
        b["units"] = len(units[sp])
    return {sp: out[sp] for sp in sorted(out)}


HEX64 = re.compile(r"[0-9a-f]{64}")


def _hex(v):
    return isinstance(v, str) and bool(HEX64.fullmatch(v))


def _turn_problems(where, t, entry, full, checks, names):
    out = []
    want = entry["turn_sha256"][t["turn"] - 1]
    if full:
        if not isinstance(t.get("request"), str) or runlog.sha256_hex(t["request"]) != want:
            out.append(f"{where}: the request is not the text of the manifest")
        if not isinstance(t.get("reply"), str) or "reply_sha256" in t:
            out.append(f"{where}: a full record holds the reply and no reply hash")
        elif t.get("checks") != tierm.run_checks(checks, names, t["reply"]):
            out.append(f"{where}: the checks differ from the checks of the stored reply")
    else:
        if "request" in t or "reply" in t:
            out.append(f"{where}: a hash_only record holds text")
        if t.get("request_sha256") != want:
            out.append(f"{where}: the request hash is not the hash of the manifest")
        if not _hex(t.get("reply_sha256")):
            out.append(f"{where}: the reply hash is missing")
    if sorted((t.get("checks") or {})) != sorted(names):
        out.append(f"{where}: the checks are not those of the unit")
    if "served_host" not in t or "served_model" not in t:
        out.append(f"{where}: served host or served model is not recorded")
    return out


def check(recs, repo=tierm.REPO, use_repo=True):
    """The problems in the form of a faias-tierm-log/1 log (empty if none). `recs` are its records."""
    from . import sec006, sec006_log
    h, problems = recs[0], []
    if h.get("schema") != SCHEMA:
        return ["unknown schema"]
    if h.get("label") != LABEL:
        problems.append(f"header: the label '{LABEL}' is missing")
    p = h.get("procedure") or {}
    if not p.get("tp") or not p.get("star"):
        problems.append("header: the procedure ID or the star label is missing")
    elif use_repo:
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
    if (h.get("tier") or {}).get("name") != "M":
        problems.append("header: the tier is not M")
    if not (h.get("model") or {}).get("pinned_id") or not h.get("settings_hash"):
        problems.append("header: the model ID or the settings hash is missing")
    sc = h.get("scorer") or {}
    if sc.get("calibrated") is not False or not sc.get("known_false_results"):
        problems.append("header: the scorer must say that it is uncalibrated and list its known false results")
    if (h.get("measures") or {}).get("status") != MEASURE_STATUS:
        problems.append(f"header: the measures must say '{MEASURE_STATUS}'")
    if (h.get("run_engine") or {}).get("name") not in ("inspect_ai", "direct"):
        problems.append("header: the run engine is missing or unknown")
    store = (h.get("store_replies") or {}).get("mode")
    if store not in tierm.STORE_MODES:
        problems.append("header: store_replies is missing or unknown")
    checks = h.get("checks") if isinstance(h.get("checks"), list) else None
    if checks is None or tierm.check_problems(checks):
        problems.append("header: the declared checks are missing or malformed")
        checks = []
    corpus = h.get("corpus") or {}
    manifest = corpus.get("manifest") if isinstance(corpus.get("manifest"), list) else []
    if not manifest or corpus.get("units_sha256") != runlog.sha256_hex(runlog.canonical(manifest)):
        problems.append("header: the unit manifest is missing or its hash differs")
    if not _hex(corpus.get("items_sha256")) or not corpus.get("dataset_guid"):
        problems.append("header: the item file hash or the dataset GUID is missing")
    k = corpus.get("runs_per_item")
    if not (isinstance(k, int) and not isinstance(k, bool) and k >= 1):
        problems.append("header: runs_per_item is missing")
        k = 0
    names = {c.get("name") for c in checks}
    by_unit = {}
    for e in manifest:
        if not (isinstance(e, dict) and set(e) == {"unit", "item", "variant_of", "class", "split", "turns",
                                                   "turn_sha256", "checks", "meta"}
                and isinstance(e["meta"], dict) and isinstance(e["turns"], int) and isinstance(e["turn_sha256"], list)
                and len(e["turn_sha256"]) == e["turns"] >= 1 and isinstance(e["checks"], list)
                and not set(e["checks"]) - names):
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
            problems.append(f"{where}: the unit is not in the manifest")
            continue
        if (r.get("base_item"), r.get("variant_of"), r.get("class"), r.get("split")) != (
                e["item"], e["variant_of"], e["class"], e["split"]):
            problems.append(f"{where}: the item, variant, class or split differs from the manifest")
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
            problems += _turn_problems(f"{where} turn {t['turn']}", t, e, full, checks, e["checks"])
        if full:
            replies = [t.get("reply") for t in turns]
            view = tierm.transcript([t.get("request") for t in turns], replies)
            if (r.get("request"), r.get("reply")) != (view, replies[-1]) or "reply_sha256" in r:
                problems.append(f"{where}: the judge view (request and reply) is not that of its turns")
        elif "request" in r or "reply" in r or not (_hex(r.get("request_sha256")) and r.get("reply_sha256")
                                                    == turns[-1].get("reply_sha256")):
            problems.append(f"{where}: a hash_only record holds text, or its hashes are not those of its turns")
        if r.get("score") != {"turn": len(turns), "checks": turns[-1].get("checks")}:
            problems.append(f"{where}: the score is not the checks of the last reply")
    marks = (sec006.CREDENTIAL_MARK, sec006.ENDPOINT_MARK)
    if any(m.search(t) for t in sec006_log._strings(recs) for m in marks):
        problems.append("the log holds a placeholder value")
    s = recs[-1]
    if s.get("type") != "summary":
        problems.append("the last record is not the summary (the run did not finish cleanly)")
        return problems
    if s.get("label") != h.get("label") or s.get("measures") != {} or s.get("measure_status") != MEASURE_STATUS:
        problems.append(f"summary: the label, the empty measures or '{MEASURE_STATUS}' is missing")
    if s.get("item_outcomes") != outcomes(runs):
        problems.append("summary: the item outcomes are not those of the run records")
    if s.get("runs_recorded") != len(runs):
        problems.append("summary: runs_recorded is not the count of run records")
    if s.get("status") == "complete" and len(runs) != len(by_unit) * k:
        problems.append("summary: the status is complete, but not every unit ran k times")
    return problems


def summary_line(recs):
    """The line that `verify --no-repo` and `verify` of a private log print in place of the items hash line."""
    corpus = (recs[0].get("corpus") or {})
    manifest = corpus.get("manifest") or []
    state = "matches" if corpus.get("units_sha256") == runlog.sha256_hex(runlog.canonical(manifest)) else "differs"
    return (f"unit manifest hash: {state} ({len(manifest)} units); the private item file (SHA-256 "
            f"{corpus.get('items_sha256')}) is outside the repository and is not read")


def rescore(recs, repo=tierm.REPO):
    """Run the declared checks again on each stored reply. (lines, mismatches); mismatches is None for a hash_only
    log."""
    h = recs[0]
    lines = [f"checks of this checkout: {SCORER_FILE}, sha256 "
             f"{runlog.sha256_hex((Path(repo) / SCORER_FILE).read_bytes())}",
             f"checks in the log: {(h.get('code') or {}).get('scorer_sha256')}"]
    if (h.get("store_replies") or {}).get("mode") != "full":
        return lines + ["not rescored: the log holds the reply hashes only (store_replies: hash_only)"], None
    by_unit = {e["unit"]: e for e in (h.get("corpus") or {}).get("manifest") or []}
    checks, bad = h.get("checks") or [], 0
    for r in recs:
        if r.get("type") != "run":
            continue
        for t in r.get("turns") or []:
            names = (by_unit.get(r.get("item")) or {}).get("checks") or []
            if tierm.run_checks(checks, names, t.get("reply") or "") != t.get("checks"):
                bad += 1
                lines.append(f"run {r.get('item')}/{r.get('run')} turn {t.get('turn')}: the checks differ")
    lines.append(f"turns rescored with a difference: {bad}")
    return lines, bad
