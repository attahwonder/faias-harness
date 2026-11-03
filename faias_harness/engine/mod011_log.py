"""The run log of TP-MOD-011, Component 1, on SRD-1 minimal (schema 11): the header, the lock, the form check, the
rescore from the store file and the items line of `verify --no-repo`.

Schema 11 uses the evidence frame of schema 4 (runlog.py): the record, the star copied from it, the thresholds with
their bases, the code block, the lock over the scorer and record hashes, the Ed25519 signature, `verify`, `verify
--no-repo` and `rescore`. It adds the blocks that this procedure needs (design section 8). lock_extra gives each of them
to the lock:

  code         three files, each with its SHA-256: the scorer (mod011.py), the record and this log module
  benchmarks   for each benchmark: the allow-list entry with the digests of its digest file (mod011.entry), the
               licence, the IDs of the selected items, the seed of the item file and the cut of each item
  comparators  the SHA-256 of the private comparator file, the count for each benchmark, the first and last creation
               date, the writer families, the declared target family and training cut-off, the overlap result (its
               SHA-256, the limit and the longest run), the SHA-256 of the SME record, the match features and the
               reviewer's match tolerance. No comparator text
  decode       temperature, seed, max_tokens and k (PLAYBOOK_MOD_011.md line 29)
  probe        the template with its SHA-256 (mod011.TEMPLATE_SHA256), the variant model_probe and the SHA-256 of its
               prompt
  match_rule   the normalisation, the echo strip, the exact rule, the near-exact rule with tau and its reason, and the
               window rule
  cited        the cited benchmarks as the operator declares them, each probed or recorded with its reason
  readings     the readings of this harness (READINGS), each with the words "the harness's reading, not a ruling"
  not_in_run   what the run leaves out, each with its procedure line (NOT_IN_RUN)
  instrument   the instrument check: the result of each of the ten passages and the summary (a reading, not M1)
  isolation    the self-test log, with its SHA-256, time and manifest match (the self-test gate); the lock covers the
               hash of the self-test log only
  store        what a run record holds, and the store file (store_block)

A run record holds the arm, the benchmark, the item, the attempt, the SHA-256 of the probe, the prefix, the remainder
and the reply, the class, the ROUGE-L value, the longest run, and the facts of the model call. A benchmark record also
holds the reply, unless the settings say store_replies: hash_only. A comparator record never holds the reply, because a
reply can quote the private comparator. The store file holds the text of each comparator (its prefix and remainder)
and the reply of each run record. `rescore --replies <store>` reads it. SRD-1's trace (target/srd1_trace.jsonl) also
holds the comparator text, so it goes to the controlled evidence store with the store file.

The label (label_for) starts with "uncalibrated match rule, not a finding" and holds the limit of line 74, the model
probe note, the note on the declared benchmarks and the one-person note (ONE_PERSON). It adds "comparators from one
model family" when the comparator file names one writer family, and "decode not greedy (temperature T)" when the
temperature is above 0. The harness decides no Pass, Fail or Partial.

As in schema 4, `check` does not compare a code hash with its file, so a later edit of the code does not fail an older
log. runlog.py hands a schema 11 log to this module: SCHEMA is in runlog.CODE_SCHEMAS, runlog.lock_fixed adds
lock_extra, runlog.lock_header names LOCK_COVERS, and runlog.check_log, runlog.rescore_log and verify --no-repo call
check, rescore and summary_line.
"""
import json
from collections import Counter
from pathlib import Path

from . import mod011, runlog, sec001

SCHEMA = "faias-harness-run-log/11"
SCORER_FILE = "faias_harness/engine/mod011.py"
RECORD_FILE = "records/TP-MOD-011.yaml"
LOG_FILE = "faias_harness/engine/mod011_log.py"
ITEM_SET = mod011.TP            # the item file items/TP-MOD-011.json (mod011.ITEM_FILE)
ARMS = ("benchmark", "comparator")
VARIANT = "model_probe"
VARIANT_PURPOSE = ("SRD-1 minimal with the model-probe prompt: one sentence that says the assistant completes text, "
                   "with no rule, no bank role and no placeholder. SRD-1's rules are off, so the run probes the model "
                   "behind SRD-1's gateway, with the gateway's pins (model, host, precision, temperature and seed)")
STORE_MODES = ("full", "hash_only")
STORE_FILE = "store/store.jsonl"    # in the run folder; the operator keeps it in the controlled evidence store
TRACE_NOTE = ("SRD-1's trace (target/srd1_trace.jsonl) also holds the comparator text, so it goes to the controlled "
              "evidence store with the store file.")
STORE_README = ("This folder holds the store file of a TP-MOD-011 run: the text of each private comparator and the "
                "reply of each run record. `rescore --log <log> --replies <store file>` reads it. Keep it in the "
                "controlled evidence store. Never put it in the repository or in a public place: the comparators stay "
                f"novel only while they stay private. {TRACE_NOTE}\n")
NO_STORE = ("not rescored: the comparator replies and the comparator text are in the store file only. Rescore with "
            "--replies <store file> (the file store/store.jsonl of the run folder, from the controlled evidence store)")
# The label. The harness decides no Pass, Fail or Partial.
LABEL_BASE = "uncalibrated match rule, not a finding"
LIMIT_74 = ("absence of reproduction does not prove absence of contamination (PLAYBOOK_MOD_011.md line 74)")
MODEL_PROBE_NOTE = "model probe: SRD-1's rules are off; SRD-1's controls are not on the path"
DECLARED_NOTE = "benchmarks declared by the operator; no approval paper"
ONE_PERSON = ("one person directs the build and the review of SRD-1, so an SME check by that person is not "
              "independent")
ONE_FAMILY = "comparators from one model family"
NOT_GREEDY = "decode not greedy (temperature {t})"
LOCK_COVERS = ("procedure, thresholds with bases, settings hash, routing locks, model ID, system prompt hash, item "
               "set (the manifest of both arms with the hashes of each prefix and remainder), log module hash, "
               "benchmarks, comparators, decode, probe, match rule, cited benchmarks, readings, not_in_run, "
               "instrument check, self-test log hash, reply storage")
CALL_KEYS = ("finish_reason", "blocked", "served_host", "served_model", "host_ok", "model_requested", "prompt_tokens",
             "completion_tokens", "usage_missing", "cost_usd", "latency_ms", "seed", "retries", "retry_reasons",
             "trace_seq")
SCORE_KEYS = ("class", "rouge_l", "longest_run")
# The frame of a run record. A record holds no other key, so no key can hold a text.
RECORD_KEYS = ("type", "arm", "benchmark", "item", "attempt", "t_ns", "time", "request_sha256", "prefix_sha256",
               "remainder_sha256", "reply", "reply_sha256", "failed", "error") + SCORE_KEYS + CALL_KEYS
_READING = "; the harness's reading, not a ruling"
READINGS = [
    "the comparator arm is the chance basis: the rate at which competence and chance give the remainder of a new item "
    "of the same form (PLAYBOOK_MOD_011.md lines 18 and 42)" + _READING,
    "both arms get the guided template with the name and split of the benchmark, the identical probe of step 6 "
    "(line 31)" + _READING,
    "an item takes its highest class over its k attempts; the per-attempt rates stand beside the item rates" + _READING,
    "the ROUGE-L rule at the reviewer's tau stands in for the human near-exact label of the cited method (Golchin and "
    "Surdeanu, ICLR 2024, section 4)" + _READING,
    f"the remainder of each benchmark item and each comparator holds {mod011.MIN_REMAINDER_WORDS} words or more, and "
    f"the overlap limit is {mod011.OVERLAP_LIMIT} words, on GSM8K question text only (train and test); both are rules "
    "of this harness" + _READING,
    "the instrument check (ten public-domain passages, the same template and scorer) is outside M1; its result is in "
    "the header only" + _READING,
    "the chance basis of line 18, reading (a): the comparator arm is the subtraction basis, so M1 is the benchmark "
    "rate minus the comparator rate" + _READING,
    "the chance basis of line 18, reading (b): the comparator arm should itself sit at or near chance; the summary "
    "gives its exact and near-exact rates with Wilson intervals as comparator_reading, and the harness sets no bar for "
    "it" + _READING,
]
NOT_IN_RUN = [
    "step 1, the inventory of the evaluation evidence (PLAYBOOK_MOD_011.md line 26): a document review by the reviewer",
    "step 2, a prior contamination check (line 27): a document review by the reviewer",
    "step 11, the model-selection records (line 36): a document review by the reviewer",
    "M2, held-out performance against the cited score (line 43): Component 2 is a later build",
    "M3, the contamination check in the approval documents (line 44): a fact about documents; the reviewer records it",
    "M4, a firm-relevant held-out evaluation in the approval evidence (line 45): a fact about documents; the reviewer "
    "records it",
    "M5, model selection on leaderboard position alone (line 46): a fact about documents; the reviewer records it",
    "Component 2 (line 19, steps 8 to 10, lines 33 to 35): the held-out firm-relevant set, its run and the comparison "
    "with the cited score are a later build",
    "the general-instruction contrast and the BLEURT measure of the cited method (Golchin and Surdeanu, ICLR 2024): the "
    "procedure does not ask for them",
    "human labels or a model judge (the match rule is not calibrated against human labels)",
    "the outcome reading of lines 50 to 52: a person makes it from the log; the harness gives no outcome",
]
SCORER_STATUS = {
    "calibrated": False,
    "note": "The match rule has not been checked against replies labelled by people. Its error rates are not known. "
            "Results from this run are not evidence about the target.",
    "known_false_results": [
        "a reply that reproduces the remainder after a preface of its own (for example 'Sure, here it is:') is not "
        "exact, and is near-exact only when the ROUGE-L of its first n words reaches tau; the longest run in the "
        "record shows the reproduction",
        "a reproduction in other words (a paraphrase) is missed; absence of reproduction does not prove absence of "
        "contamination (line 74)",
        "a remainder that the model can work out from the first piece (for example a final question) can be exact "
        "without memory; the comparator arm subtracts that rate",
        "a letter outside ASCII is dropped from the words of both texts (mod011.words)",
    ],
    "outcomes": "exact, near_exact, unrelated, and no_result (an empty reply, a send error, a provider block or the "
                "finish reason error); no_result is counted apart and left out of the rates",
}


def label_for(writer_families, temperature):
    """The label of a run (see the module text)."""
    parts = [LABEL_BASE, LIMIT_74, MODEL_PROBE_NOTE, DECLARED_NOTE, ONE_PERSON]
    if len(writer_families) == 1:
        parts.append(ONE_FAMILY)
    if isinstance(temperature, (int, float)) and temperature > 0:
        parts.append(NOT_GREEDY.format(t=temperature))
    return "; ".join(parts)


def store_block(mode):
    """The store block of the header: what a run record holds with store_replies `mode` (full or hash_only)."""
    bench = ("each benchmark record holds the reply and its SHA-256" if mode == "full" else
             "each benchmark record holds the SHA-256 of the reply only (store_replies: hash_only)")
    return {"benchmark_replies": mode, "comparator_replies": "hash_only", "comparator_text": "hash_only",
            "store_file": STORE_FILE,
            "note": f"{bench}. Each comparator record holds the SHA-256 of the reply only, because a reply can quote the "
                    "private comparator. The header holds the SHA-256 of each prefix and remainder, never a comparator "
                    "text. The store file holds the text of each comparator and the reply of each run record; "
                    f"rescore --replies reads it. {TRACE_NOTE} The instrument check holds its replies in the header: "
                    "its passages are public-domain text in the repository."}


# ---------- the parts of the header ----------

def code_facts(repo):
    """The code block of a schema 11 header: the scorer and the record (runlog.code_facts), and this log module, each
    with its SHA-256."""
    out = runlog.code_facts(repo, scorer_file=SCORER_FILE, record_file=RECORD_FILE)
    out["log_file"] = LOG_FILE
    out["log_sha256"] = runlog.sha256_hex((Path(repo) / LOG_FILE).read_bytes())
    return out


def _key(arm, item_id):
    return f"{arm}:{item_id}"


def manifest(items, comparators):
    """The manifest of the two arms and the scorer keys. A row names the arm, the benchmark, the item, its pair (a
    comparator's benchmark item), the cut, the word counts and the SHA-256 of the probe. The scorer keys hold the
    SHA-256 of each prefix and remainder. No text."""
    rows, keys = [], {}
    for arm, entries in (("benchmark", [(i.id, i.benchmark, None, i.cut, i.prefix, i.remainder) for i in items]),
                         ("comparator", [(c["id"], c["benchmark"], c["pair"], c["cut"], c["prefix"], c["remainder"])
                                         for c in comparators])):
        for item_id, bench, pair, cut, prefix, remainder in entries:
            rows.append({"arm": arm, "id": item_id, "benchmark": bench, "pair": pair, "cut": cut,
                         "prefix_words": len(mod011.words(prefix)), "remainder_words": len(mod011.words(remainder)),
                         "request_sha256": runlog.sha256_hex(mod011.probe_text(bench, prefix))})
            keys[_key(arm, item_id)] = {"prefix_sha256": runlog.sha256_hex(prefix),
                                        "remainder_sha256": runlog.sha256_hex(remainder)}
    return rows, keys


def items_digest(rows, keys):
    """The items hash of a schema 11 log: the manifest and the scorer keys. It holds no text, so `verify --no-repo`
    checks it from the log alone."""
    return runlog.sha256_hex(runlog.canonical({"manifest": rows, "scorer_keys": keys}))


def thresholds_for_header(settings, repo=runlog.REPO):
    """M1 to M5 with their bases from the record. M1 holds the reviewer's bar of a material differential with its
    reason. M2 to M5 are not in the run (NOT_IN_RUN)."""
    rec = sec001.load_record(tp=mod011.TP, repo=repo)
    out = {}
    for t in rec["thresholds"]:
        out[t["measure"]] = {"text": t["text"], "basis": t["basis"], "basis_source": t["basis_source"],
                             "src": t["src"], "in_run": t["measure"] == "M1"}
    out["M1"].update(value=settings.m1_value, value_reason=settings.m1_reason,
                     value_basis="the reviewer's bar of a material differential, locked before the first item; the "
                                 "harness compares the primary and secondary differentials with it (materially_higher)")
    for m in ("M2", "M3", "M4", "M5"):
        out[m]["value_note"] = next(x for x in NOT_IN_RUN if x.startswith(f"{m},"))
    return out


def benchmarks_block(benchmarks, items, item_files, repo=runlog.REPO):
    """For each benchmark: the allow-list entry with its digests, the selected IDs, the seed and the cut of each
    item. `item_files` maps each benchmark to its item file as loaded."""
    out = {}
    for b in benchmarks:
        e = mod011.entry(b, repo)
        mine = [i for i in items if i.benchmark == b]
        gen = item_files[b].get("generated_by") or {}
        out[b] = {**e, "source_revision": "the file as fetched, pinned by source_sha256 (the source URL names the "
                                          "branch)",
                  "selected": [i.id for i in mine], "seed": gen.get("seed"), "tool": gen.get("tool"),
                  "cuts": {i.id: i.cut for i in mine}}
    return out


def comparators_block(settings, comparators, file_sha256, overlap, overlap_sha256, features):
    """The comparators block: facts of the private file, with no comparator text."""
    dates = sorted(str(c["created"]) for c in comparators)
    rows = [r for r in overlap.get("results") or [] if isinstance(r, dict)]
    return {"file_sha256": file_sha256,
            "count": dict(sorted(Counter(c["benchmark"] for c in comparators).items())),
            "created": {"first": dates[0] if dates else None, "last": dates[-1] if dates else None},
            "writer_families": sorted({str(c["writer_family"]) for c in comparators}),
            "target_family": settings.target_family,
            "training_cutoff": settings.training_cutoff, "training_cutoff_source": settings.training_cutoff_source,
            "declared_basis": "declared by the operator in the settings, from the model card; not observed",
            "overlap_result": {"sha256": overlap_sha256, "limit": mod011.OVERLAP_LIMIT,
                               "longest_run": max((r.get("longest_run") or 0 for r in rows), default=None),
                               "files": overlap.get("gsm8k_files")},
            "sme_record_sha256": settings.sme_record_sha256,
            "check": "mod011.check_comparators gave no problem before the lock",
            "match_features": features,
            "match_tolerance": {"value": settings.match_tolerance_value, "reason": settings.match_tolerance_reason,
                                "note": "recorded as the reviewer set it; the harness refuses no pair on the match "
                                        "features, and the SME record decides comparability (line 28)"},
            "note": "the comparator file is private: it stays outside the repository. The header holds the SHA-256 of "
                    f"each prefix and remainder (corpus.scorer_keys); the store file holds the text. {TRACE_NOTE}"}


def decode_block(settings):
    t = settings.temperature
    return {"temperature": t, "seed": settings.seed, "max_tokens": settings.max_tokens, "k": settings.runs_per_item,
            "greedy": t == 0,
            "note": "fixed and recorded before the first item (PLAYBOOK_MOD_011.md line 29). The gateway sends the seed "
                    "plus the number of calls made; a hosted endpoint can vary at temperature 0, and the k attempts "
                    "show that variation. The harness sets no ceiling on the temperature"}


def probe_block(system_prompt_sha256, prompt_source):
    return {"template": mod011.PROBE_TEMPLATE, "template_sha256": mod011.TEMPLATE_SHA256, "variant": VARIANT,
            "prompt_source": prompt_source, "prompt_sha256": system_prompt_sha256,
            "session": "each attempt opens a new session, sends one message and closes the session",
            "note": "both arms get the same template with the same data set name and split (line 31)"}


def match_rule_block(settings):
    return {"normalisation": "the words of a text: its NFKC form in lower case, letters and digits only, each run of "
                             "other characters as one space (mod011.words); a letter outside ASCII is dropped",
            "echo_strip": "an echoed 'First piece' label, an echoed prefix and a 'Second piece' label are removed "
                          "from the start of the reply, in that order",
            "exact": "the reply starts with the words of the remainder",
            "near_exact": "not exact, and the ROUGE-L F1 over words of the remainder and the first n words of the "
                          "reply is at or above tau",
            "window": "n is the word count of the remainder; the longest run of remainder words is over the whole "
                      "reply",
            "unrelated": "any other reply with text",
            "no_result": "an empty reply, a send error, a provider block or the finish reason error; left out of the "
                         "rates",
            "tau": {"value": settings.tau_value, "reason": settings.tau_reason},
            "calibrated": False}


def cited_block(settings):
    declared = [dict(c) for c in settings.cited]
    return {"declared": declared, "basis": "declared by the operator in the settings; no approval paper exists for "
                                           "SRD-1 (A-15 is planned), so the cited list is not observed",
            "probed": sorted({c["benchmark"] for c in declared} & set(settings.benchmarks)),
            "not_probed": [{"benchmark": c["benchmark"], "reason": "not on the allow-list"} for c in declared
                           if c["benchmark"] not in mod011.ALLOW_LIST],
            "probed_not_cited": sorted(set(settings.benchmarks) - {c["benchmark"] for c in declared})}


def build_header(settings, *, allow_hosts, items, item_files, comparators, comparator_sha256, overlap,
                 overlap_sha256, instrument, isolation, system_prompt_sha256, prompt_source, confirmation,
                 probes=None, repo=runlog.REPO, engine="inspect"):
    """The header of a TP-MOD-011 run, without the lock (runlog.lock_header adds it). `items` is the tuple of
    mod011.Item, `comparators` the list of comparator rows that passed mod011.check_comparators, `overlap` the overlap
    result as loaded, `instrument` the instrument block (the result of the check) and `isolation` the self-test log."""
    rec = sec001.load_record(tp=mod011.TP, repo=repo)
    rows, keys = manifest(items, comparators)
    features = mod011.match_features(items, comparators)
    fams = sorted({str(c["writer_family"]) for c in comparators})
    return {
        "type": "header", "schema": SCHEMA, "label": label_for(fams, settings.temperature),
        "engine_version": runlog.__version__, "code": code_facts(repo),
        "procedure": {"tp": rec["tp"], "title": rec["title"], "irq": rec["irq"], "star": rec["star"],
                      "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
                      "faias_version": sec001.FAIAS_VERSION, "source": rec["source"]},
        "mode": settings.mode, "provider": settings.provider, "model": {"pinned_id": settings.model_id},
        "routing": settings.routing(),
        "prices_per_million_tokens": {"in": settings.price_in_per_m, "out": settings.price_out_per_m},
        "cap_usd": settings.cap_usd, "max_tokens": settings.max_tokens, "settings_hash": settings.digest(),
        "thresholds": thresholds_for_header(settings, repo),
        "corpus": {"item_set": ITEM_SET, "items": len(rows), "arms": list(ARMS), "runs_per_item": settings.runs_per_item,
                   "manifest": rows, "scorer_keys": keys, "items_sha256": items_digest(rows, keys),
                   "scoring": "deterministic: mod011.score_completion on each reply; no model judge"},
        "benchmarks": benchmarks_block(settings.benchmarks, items, item_files, repo),
        "comparators": comparators_block(settings, comparators, comparator_sha256, overlap, overlap_sha256, features),
        "decode": decode_block(settings), "probe": probe_block(system_prompt_sha256, prompt_source),
        "match_rule": match_rule_block(settings), "cited": cited_block(settings),
        "readings": list(READINGS), "not_in_run": list(NOT_IN_RUN), "instrument": instrument,
        "isolation": isolation, "store": store_block(settings.store_replies),
        "variant": {"name": VARIANT, "purpose": VARIANT_PURPOSE},
        "run_engine": runlog._engine_facts(engine), "scorer": SCORER_STATUS,
        "system_prompt_sha256": system_prompt_sha256,
        "egress": {"allow": sorted(allow_hosts), "limit": runlog.EGRESS_LIMIT},
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each run record (served_host) and in each instrument row",
    }


def lock_extra(header):
    """The parts of a schema 11 header that the lock covers besides those of schema 4."""
    out = {k: header.get(k) for k in ("benchmarks", "comparators", "decode", "probe", "match_rule", "cited", "readings",
                                      "not_in_run", "instrument", "store")}
    out["log_sha256"] = (header.get("code") or {}).get("log_sha256")
    out["selftest_sha256"] = ((header.get("isolation") or {}).get("selftest") or {}).get("sha256")
    return out


# ---------- checks on the log ----------

def _block(h, name):
    return h.get(name) if isinstance(h.get(name), dict) else {}


def _hex(v):
    return bool(runlog.HEX64.fullmatch(str(v)))


def _corpus_parts(h):
    corpus = _block(h, "corpus")
    rows = [m for m in corpus.get("manifest") or [] if isinstance(m, dict)]
    keys = corpus.get("scorer_keys") if isinstance(corpus.get("scorer_keys"), dict) else {}
    return rows, keys


def _row_ok(m, keys):
    k = keys.get(_key(m.get("arm"), m.get("id")))
    return (m.get("arm") in ARMS and isinstance(m.get("id"), str) and m.get("benchmark") in mod011.ALLOW_LIST
            and (m.get("pair") is None if m["arm"] == "benchmark" else isinstance(m.get("pair"), str))
            and type(m.get("cut")) is int and type(m.get("remainder_words")) is int
            and m["remainder_words"] >= mod011.MIN_REMAINDER_WORDS and _hex(m.get("request_sha256"))
            and isinstance(k, dict) and _hex(k.get("prefix_sha256")) and _hex(k.get("remainder_sha256")))


def _instrument_problems(h):
    ins = _block(h, "instrument")
    rows = [p for p in ins.get("passages") or [] if isinstance(p, dict)]
    ok = (ins.get("file") == mod011.INSTRUMENT_FILE and _hex(ins.get("sha256"))
          and ins.get("dataset") == mod011.INSTRUMENT_DATASET and len(rows) == mod011.INSTRUMENT_COUNT
          and all(p.get("class") in mod011.CLASSES and _hex(p.get("reply_sha256"))
                  and runlog.sha256_hex(p.get("reply") or "") == p.get("reply_sha256") for p in rows))
    if not ok:
        return ["header: the instrument block (the file, its SHA-256, the data set and the class and reply of each of "
                f"the {mod011.INSTRUMENT_COUNT} passages) is missing or incomplete"]
    if ins.get("summary") != mod011.instrument_summary([p["class"] for p in rows]):
        return ["header: the instrument summary does not match the class of each passage"]
    return []


def _block_problems(h, rows):
    """The blocks of design section 8, read from the header alone."""
    out = []
    bench = _block(h, "benchmarks")
    by_bench = Counter(m["benchmark"] for m in rows if m.get("arm") == "benchmark")
    if (set(bench) != set(by_bench) or not all(
            isinstance(bench[b], dict) and _hex(bench[b].get("item_sha256")) and _hex(bench[b].get("source_sha256"))
            and _hex(bench[b].get("licence_sha256")) and bench[b].get("licence")
            and bench[b].get("selected") == [m["id"] for m in rows if m.get("arm") == "benchmark"
                                             and m.get("benchmark") == b]
            and bench[b].get("cuts") == {m["id"]: m["cut"] for m in rows if m.get("arm") == "benchmark"
                                         and m.get("benchmark") == b} for b in bench)):
        out.append("header: the benchmarks block (the entry with its digests, the licence, the selected IDs and the "
                   "cuts of each benchmark) does not match the manifest")
    comp = _block(h, "comparators")
    count = Counter(m["benchmark"] for m in rows if m.get("arm") == "comparator")
    if (not _hex(comp.get("file_sha256")) or comp.get("count") != dict(sorted(count.items()))
            or not isinstance(comp.get("writer_families"), list) or not comp["writer_families"]
            or not _hex(_block(comp, "overlap_result").get("sha256"))
            or not isinstance(comp.get("match_features"), dict)):
        out.append("header: the comparators block (the file hash, the count for each benchmark, the writer families, "
                   "the overlap result and the match features) is missing or does not match the manifest")
    elif h.get("mode") == "evidence" and not _hex(comp.get("sme_record_sha256")):
        out.append("header: evidence mode: the comparators block has no SHA-256 of the SME record")
    dec = _block(h, "decode")
    if (dec.get("k") != _block(h, "corpus").get("runs_per_item") or dec.get("max_tokens") != h.get("max_tokens")
            or dec.get("greedy") != (dec.get("temperature") == 0)):
        out.append("header: the decode block (temperature, seed, max_tokens and k) does not match the header")
    pr = _block(h, "probe")
    if (not isinstance(pr.get("template"), str) or runlog.sha256_hex(pr["template"]) != pr.get("template_sha256")
            or pr.get("variant") != VARIANT or pr.get("prompt_sha256") != h.get("system_prompt_sha256")):
        out.append("header: the probe block (the template with its SHA-256, the variant model_probe and the prompt "
                   "hash) is missing or does not match")
    mr = _block(h, "match_rule")
    tau = _block(mr, "tau")
    if (not all(isinstance(mr.get(k), str) for k in ("normalisation", "echo_strip", "exact", "near_exact", "window"))
            or "value" not in tau or mr.get("calibrated") is not False):
        out.append("header: the match_rule block (the normalisation, the rules, the window and tau) is missing")
    elif h.get("mode") == "evidence" and (tau.get("value") is None or not tau.get("reason")):
        out.append("header: evidence mode: tau has no value or no reason")
    if not isinstance(_block(h, "cited").get("declared"), list):
        out.append("header: the cited block is missing")
    return out + _instrument_problems(h)


def _header_problems(h, repo, use_repo):
    problems = []
    p = h.get("procedure") or {}
    if use_repo:
        rec = sec001.load_record(tp=mod011.TP, repo=repo)
        if p.get("tp") != rec["tp"]:
            problems.append("header: procedure ID differs from the record")
        if p.get("star") != rec["star"]:
            problems.append(f"header: star label is not the label of the record ({rec['star']})")
    elif p.get("tp") != mod011.TP or not p.get("star"):
        problems.append("header: the procedure ID or the star label is missing")
    problems += runlog._code_problems(h)
    code = h.get("code") or {}
    if (code.get("scorer_file"), code.get("record_file"), code.get("log_file")) != (SCORER_FILE, RECORD_FILE,
                                                                                     LOG_FILE) \
            or not _hex(code.get("log_sha256")):
        problems.append("header: the code block does not name the TP-MOD-011 scorer, record and log module with their "
                        "hashes")
    if not (h.get("model") or {}).get("pinned_id"):
        problems.append("header: model ID is missing")
    if not h.get("settings_hash"):
        problems.append("header: settings hash is missing")
    th = h.get("thresholds") or {}
    for m in ("M1", "M2", "M3", "M4", "M5"):
        if not (th.get(m) or {}).get("basis") or not (th.get(m) or {}).get("basis_source"):
            problems.append(f"header: threshold {m} has no basis")
    if h.get("mode") == "evidence" and ((th.get("M1") or {}).get("value") is None
                                        or not (th.get("M1") or {}).get("value_reason")):
        problems.append("header: evidence mode: the M1 bar has no value or no reason")
    if not (h.get("scorer") or {}).get("known_false_results"):
        problems.append("header: the scorer must list its known false results")
    if (h.get("run_engine") or {}).get("name") not in ("inspect_ai", "direct"):
        problems.append("header: the run engine is missing or unknown")
    if (h.get("variant") or {}).get("name") != VARIANT:
        problems.append(f"header: the variant is not {VARIANT}")
    comp = _block(h, "comparators")
    want = label_for(comp.get("writer_families") or [], _block(h, "decode").get("temperature"))
    if h.get("label") not in (want, runlog.example_label(want)):
        problems.append(f"header: the label '{want}' is missing")
    problems += runlog.example_problems(h)
    selftest = _block(_block(h, "isolation"), "selftest")
    if not _hex(selftest.get("sha256")) or not selftest.get("time") or selftest.get("code_manifest_match") is not True:
        problems.append("header: isolation.selftest is missing or incomplete (the self-test log hash, its time, and a "
                        "code manifest that matches)")
    store = _block(h, "store")
    if store.get("benchmark_replies") not in STORE_MODES or store != store_block(store["benchmark_replies"]):
        problems.append("header: the store block is not the schema 11 store block")
    readings = h.get("readings") if isinstance(h.get("readings"), list) else []
    problems += [f"header: a reading is missing from the readings block: {x.split(';')[0]}" for x in READINGS
                 if x not in readings]
    nir = h.get("not_in_run") if isinstance(h.get("not_in_run"), list) else []
    problems += [f"header: a line is missing from not_in_run: {x.split(':')[0]}" for x in NOT_IN_RUN if x not in nir]
    lock = h.get("lock") or {}
    if not lock.get("hash") or not isinstance(lock.get("time_ns"), int):
        problems.append("header: the lock is missing")
    corpus = _block(h, "corpus")
    rows, keys = _corpus_parts(h)
    if corpus.get("item_set") != ITEM_SET or corpus.get("arms") != list(ARMS):
        problems.append("header: the item set is not TP-MOD-011, or the arms are not the benchmark arm and the "
                        "comparator arm")
    if type(corpus.get("runs_per_item")) is not int or corpus["runs_per_item"] < 1:
        problems.append("header: runs_per_item is missing")
    bad = [f"{m.get('arm')}:{m.get('id')}" for m in rows if not _row_ok(m, keys)]
    if bad:
        problems.append(f"header: the manifest rows {', '.join(bad)} have no valid arm, benchmark, pair, cut, word "
                        "counts or hashes")
    ids = Counter(_key(m.get("arm"), m.get("id")) for m in rows)
    if any(n > 1 for n in ids.values()) or set(keys) != set(ids) or corpus.get("items") != len(rows):
        problems.append("header: the manifest and the scorer keys do not name each item of each arm once")
    if corpus.get("items_sha256") != items_digest(rows, keys):
        problems.append("header: the items hash differs from the manifest and the scorer keys in the log")
    if use_repo:
        problems += _repo_problems(h, rows, keys, repo)
    return problems + _block_problems(h, rows)


def _repo_problems(h, rows, keys, repo):
    """With the repository: the benchmark rows and the instrument hash are those of the files of the checkout."""
    out = []
    try:
        texts = {i.id: i for b in {m["benchmark"] for m in rows if m.get("arm") == "benchmark"}
                 for i in mod011.load_items(b, repo)}
    except (OSError, ValueError):
        return ["header: the item file of a benchmark cannot be read from the repository, or it is refused"]
    for m in rows:
        if m.get("arm") != "benchmark":
            continue
        i, k = texts.get(m.get("id")), keys.get(_key("benchmark", m.get("id"))) or {}
        if i is None or (k.get("prefix_sha256"), k.get("remainder_sha256")) != (runlog.sha256_hex(i.prefix),
                                                                                runlog.sha256_hex(i.remainder)):
            out.append(f"header: the benchmark item {m.get('id')} differs from the item file in the repository")
    path = Path(repo) / mod011.INSTRUMENT_FILE
    if not path.is_file() or runlog.sha256_hex(path.read_bytes()) != _block(h, "instrument").get("sha256"):
        out.append(f"header: the instrument hash differs from {mod011.INSTRUMENT_FILE} in the repository")
    return out


def _run_problems(r, rows, keys, lock, k, store):
    where = f"run {r.get('arm')}/{r.get('item')}/{r.get('attempt')}"
    m = rows.get((r.get("arm"), r.get("item")))
    if m is None:
        return [f"{where}: the item is not in the manifest of its arm"]
    out = []
    key = keys.get(_key(m["arm"], m["id"])) or {}
    if (r.get("benchmark"), r.get("request_sha256"), r.get("prefix_sha256"), r.get("remainder_sha256")) != (
            m["benchmark"], m["request_sha256"], key.get("prefix_sha256"), key.get("remainder_sha256")):
        out.append(f"{where}: the benchmark or a hash differs from the manifest")
    if type(r.get("attempt")) is not int or not 1 <= r["attempt"] <= (k if type(k) is int else 0):
        out.append(f"{where}: the attempt number is not between 1 and runs_per_item")
    extra = sorted(str(x) for x in r if x not in RECORD_KEYS)
    if extra:
        out.append(f"{where}: the record holds a key that is not in the frame of a run record ({', '.join(extra)})")
    missing = [x for x in RECORD_KEYS if x not in r and x != "reply"]
    if missing:
        out.append(f"{where}: {', '.join(missing)} missing")
    if not _hex(r.get("reply_sha256")):
        out.append(f"{where}: the reply hash is missing")
    holds = "reply" in r
    if m["arm"] == "comparator" and holds:
        out.append(f"{where}: a comparator record holds the reply (it holds reply_sha256 only)")
    elif m["arm"] == "benchmark" and holds != (store == "full"):
        out.append(f"{where}: a benchmark record must hold the reply with store_replies full, and only then")
    elif holds and runlog.sha256_hex(r.get("reply") or "") != r.get("reply_sha256"):
        out.append(f"{where}: the reply does not have the reply hash")
    cls, rouge, run = r.get("class"), r.get("rouge_l"), r.get("longest_run")
    if cls not in mod011.CLASSES or (cls == "no_result") != (rouge is None) or (rouge is None) != (run is None) \
            or not (rouge is None or isinstance(rouge, (int, float)) and 0 <= rouge <= 1):
        out.append(f"{where}: the class, the ROUGE-L value or the longest run is not of the form of score_completion")
    if not isinstance(r.get("t_ns"), int) or r["t_ns"] <= lock.get("time_ns", 0):
        out.append(f"{where}: time is not later than the lock time")
    return out


def check(recs, repo=runlog.REPO, use_repo=True):
    """The problems in the form of a schema 11 log (empty if none). `use_repo` false reads nothing from the record, the
    item file or the instrument file of the checkout."""
    h = recs[0]
    problems = _header_problems(h, repo, use_repo)
    rows, keys = _corpus_parts(h)
    by_id = {(m["arm"], m["id"]): m for m in rows if _row_ok(m, keys)}
    lock, k = h.get("lock") or {}, _block(h, "corpus").get("runs_per_item")
    store = _block(h, "store").get("benchmark_replies")
    seen = set()
    for r in recs:
        if r.get("type") != "run":
            continue
        key = runlog.canonical([r.get("arm"), r.get("item"), r.get("attempt")])
        if key in seen:
            problems.append(f"run {r.get('arm')}/{r.get('item')}/{r.get('attempt')}: a second record for this arm, "
                            "item and attempt")
        seen.add(key)
        problems += _run_problems(r, by_id, keys, lock, k, store)
    if recs[-1].get("type") != "summary":
        problems.append("the last record is not the summary (the run did not finish cleanly)")
    else:
        problems += _summary_problems(recs[-1], recs, by_id, k)
    return problems


def _summary_problems(summary, recs, by_id, k):
    """The summary holds M1 for each benchmark of the manifest. When its status is complete, each item of each arm
    has runs_per_item records: one problem for each arm that has fewer, with the count."""
    out = []
    m1 = _block(_block(summary, "measures"), "M1")
    if set(m1) != {m["benchmark"] for m in by_id.values()} or not all(
            isinstance(v, dict) and all(x in v for x in ("primary", "secondary", "comparator_reading"))
            for v in m1.values()):
        out.append("summary: M1 is missing for a benchmark (primary, secondary and comparator_reading)")
    if summary.get("status") == "complete" and type(k) is int and k >= 1:
        for a in ARMS:
            want = sum(1 for m in by_id.values() if m["arm"] == a) * k
            got = {(r["item"], r["attempt"]) for r in recs if r.get("type") == "run" and r.get("arm") == a
                   and (a, r.get("item")) in by_id and type(r.get("attempt")) is int and 1 <= r["attempt"] <= k}
            if len(got) < want:
                out.append(f"summary: the status is complete, but the {a} arm has {len(got)} of {want} records "
                           "(runs_per_item for each item of the manifest)")
    return out


# ---------- rescore from the store file ----------

def read_store(path):
    """Read a store file. Return the comparator texts {id: (line, prefix, remainder)} and a list of (line number,
    (arm, item, attempt), reply). Raise ValueError for a line that is not of the format, with its number."""
    comps, replies = {}, []
    for n, line in enumerate(Path(path).read_bytes().decode("utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            raise ValueError(f"store line {n} is not JSON") from None
        kind = e.get("type") if isinstance(e, dict) else None
        if kind == "comparator" and all(isinstance(e.get(k), str) for k in ("id", "prefix", "remainder")):
            if e["id"] in comps:
                raise ValueError(f"store line {n} is a second line for the comparator {e['id']}")
            comps[e["id"]] = (n, e["prefix"], e["remainder"])
        elif (kind == "reply" and e.get("arm") in ARMS and isinstance(e.get("item"), str)
              and type(e.get("attempt")) is int and isinstance(e.get("reply"), str)):
            replies.append((n, (e["arm"], e["item"], e["attempt"]), e["reply"]))
        else:
            raise ValueError(f"store line {n} is not a comparator line or a reply line of the store format")
    return comps, replies


def _texts(h, comps, repo):
    """{(arm, item): (prefix, remainder)} for the manifest, and the refused comparator lines. Benchmark texts come
    from the item file of the checkout, comparator texts from the store. Each text must have the hash of the log.
    Raises ValueError when the item file of the checkout is not that of the log."""
    rows, keys = _corpus_parts(h)
    items = {i.id: i for b in {m["benchmark"] for m in rows if m.get("arm") == "benchmark"}
             for i in mod011.load_items(b, repo)}
    texts, refused = {}, []
    for m in rows:
        k = keys.get(_key(m.get("arm"), m.get("id"))) or {}
        want = (k.get("prefix_sha256"), k.get("remainder_sha256"))
        if m.get("arm") == "benchmark":
            i = items.get(m.get("id"))
            if i is None or (runlog.sha256_hex(i.prefix), runlog.sha256_hex(i.remainder)) != want:
                raise ValueError(f"the item {m.get('id')} of the item file of this checkout does not have the hashes "
                                 "of the log")
            texts[("benchmark", m["id"])] = (i.prefix, i.remainder)
        elif m.get("id") not in comps:
            refused.append(f"comparator {m.get('id')}: the store has no comparator line for it")
        else:
            n, prefix, remainder = comps[m["id"]]
            if (runlog.sha256_hex(prefix), runlog.sha256_hex(remainder)) != want:
                refused.append(f"store line {n} (comparator {m['id']}): its text does not have the hashes of the log")
            else:
                texts[("comparator", m["id"])] = (prefix, remainder)
    names = {m.get("id") for m in rows if m.get("arm") == "comparator"}
    refused += [f"store line {n} (comparator {c}): the log has no comparator with this ID"
                for c, (n, _, _) in comps.items() if c not in names]
    return texts, refused


def _instrument_rescored(h, repo, tau):
    """The mismatches of the instrument rows, scored again from the instrument file of the checkout and the replies
    in the header. Raises ValueError when the file is not that of the log."""
    ins = _block(h, "instrument")
    path = Path(repo) / mod011.INSTRUMENT_FILE
    if not path.is_file() or runlog.sha256_hex(path.read_bytes()) != ins.get("sha256"):
        raise ValueError(f"{mod011.INSTRUMENT_FILE} of this checkout does not have the hash of the log")
    passages = {p.id: p for p in mod011.load_instrument(repo)}
    out, classes = [], []
    for row in ins.get("passages") or []:
        p = passages.get(row.get("id"))
        if p is None:
            out.append(f"instrument/{row.get('id')}: the passage is not in {mod011.INSTRUMENT_FILE}")
            continue
        now = mod011.score_completion(row.get("reply"), p.prefix, p.remainder, tau, failed=bool(row.get("failed")))
        classes.append(now["class"])
        if {k: row.get(k) for k in SCORE_KEYS} != now:
            out.append(f"instrument/{row.get('id')}: recorded {runlog.canonical({k: row.get(k) for k in SCORE_KEYS})}; "
                       f"now {runlog.canonical(now)}")
    if mod011.instrument_summary(classes) != ins.get("summary"):
        out.append("instrument summary: it differs from the classes scored now")
    return out


def rescore(recs, repo=runlog.REPO, replies=None):
    """Score each reply again with the scorer of this checkout and compare: the class, the ROUGE-L value and the
    longest run of each run record and of each instrument row, then M1 of the summary. A comparator record holds the
    reply hash only, so `replies` must name the store file. Without it, nothing is rescored. Returns (lines,
    differences), or (lines, None) when nothing is rescored. A difference is a refused store line, a run record with no
    store line, or a changed score or measure."""
    h, runs = recs[0], [r for r in recs if r.get("type") == "run"]
    used = runlog.sha256_hex((Path(repo) / SCORER_FILE).read_bytes())
    logged = (h.get("code") or {}).get("scorer_sha256")
    lines = [f"scorer used: {SCORER_FILE} of this checkout, sha256 {used}",
             f"scorer in the log: sha256 {logged} ({'equal to' if logged == used else 'differs from'} the scorer used)"]
    if replies is None:
        return lines + [NO_STORE], None
    tau = _block(_block(h, "match_rule"), "tau").get("value")
    try:
        comps, entries = read_store(replies)
        texts, refused = _texts(h, comps, repo)
        mismatches = _instrument_rescored(h, repo, tau)
    except OSError as e:
        return lines + [f"not rescored: a file cannot be read ({type(e).__name__})"], None
    except ValueError as e:
        return lines + [f"not rescored: {e}"], None
    lines.append("texts from: the item file and the instrument file of this checkout, and the comparator lines of the "
                 "store; each text has the hash of the log")
    by_key = {(r.get("arm"), r.get("item"), r.get("attempt")): r for r in runs}
    seen, verified = set(), {}
    for n, key, reply in entries:
        name, r = f"store line {n} ({'/'.join(map(str, key))})", by_key.get(key)
        if r is None:
            refused.append(f"{name}: the log has no run record with this key")
            continue
        if key in seen:
            refused.append(f"{name}: a second reply line for this run record")
        elif runlog.sha256_hex(reply) != r.get("reply_sha256"):
            refused.append(f"{name}: its SHA-256 is not the reply hash that the log holds for this run")
        else:
            verified[key] = reply
        seen.add(key)
    missing = [f"{'/'.join(map(str, key))}: the store has no reply line for this run record" for key in by_key
               if key not in seen]
    now_records, rescored = [], 0
    for key, r in by_key.items():
        text = texts.get(key[:2])
        if key not in verified or text is None:
            continue
        now = mod011.score_completion(verified[key], *text, tau, failed=bool(r.get("failed")))
        rescored += 1
        now_records.append({"arm": key[0], "benchmark": r.get("benchmark"), "item": key[1], "attempt": key[2],
                            "class": now["class"]})
        if {k: r.get(k) for k in SCORE_KEYS} != now:
            mismatches.append(f"{'/'.join(map(str, key))}: recorded "
                              f"{runlog.canonical({k: r.get(k) for k in SCORE_KEYS})}; now {runlog.canonical(now)}")
    summary = recs[-1] if recs[-1].get("type") == "summary" else None
    if summary is not None and rescored == len(runs):
        now_m1 = measures(h, now_records)
        for b in sorted(set(now_m1) | set(_block(_block(summary, "measures"), "M1"))):
            if runlog.canonical(now_m1.get(b)) != runlog.canonical(summary["measures"]["M1"].get(b)):
                mismatches.append(f"summary M1 {b}: it differs from the classes scored now")
    else:
        lines.append("summary M1: not compared (the log has no summary, or not every run record was rescored)")
    differences = refused + missing + mismatches
    lines += [f"store: {len(comps)} comparator lines, {len(entries)} reply lines; refused: {len(refused)}; run records "
              f"with no reply line: {len(missing)}",
              f"runs rescored: {rescored} of {len(runs)}; mismatches: {len(mismatches)}",
              f"differences: {len(differences)}"]
    return (lines + ["REFUSED " + x for x in refused] + ["NOT IN STORE " + x for x in missing]
            + ["MISMATCH " + x for x in mismatches]), differences


def measures(h, records):
    """M1 of each benchmark of the manifest, from records with arm, benchmark, item, attempt and class
    (mod011.compute_m1, with k, the M1 bar and tau of the header)."""
    rows, _ = _corpus_parts(h)
    k = _block(h, "corpus").get("runs_per_item")
    bar = _block(_block(h, "thresholds"), "M1").get("value")
    tau = _block(_block(h, "match_rule"), "tau").get("value")
    return {b: mod011.compute_m1([r for r in records if r.get("benchmark") == b], runs_per_item=k, bar=bar, tau=tau)
            for b in sorted({m.get("benchmark") for m in rows})}


def summary_line(recs):
    """For `verify --no-repo`: the items hash line of a schema 11 log, and how the comparator text is checked."""
    h = recs[0]
    same = _block(h, "corpus").get("items_sha256") == items_digest(*_corpus_parts(h))
    return (f"items hash: {'matches' if same else 'differs'} (the manifest and the scorer keys in the log); comparator "
            "text is checked by hash only: the log holds the SHA-256 of each comparator prefix, remainder, probe and "
            "reply, never the text, and the store file holds the text (rescore --replies)")
