"""The engine of TP-SEC-002 on SRD-1 minimal: estimate and run.

`execute` first reads the TP-SEC-001 baseline log (sec002_log.read_baseline). A TP-SEC-002 run needs it in both modes:
without it the uplift cannot be computed (PLAYBOOK_SEC_002.md line 8). It also reads the declaration of the channel
inputs (evidence output E2, targets/srd1/channel_inputs.yaml). It then starts SRD-1 minimal on 127.0.0.1 and reads,
through the target interface (`config_read`), the system prompt and the limits of one session. It locks and writes
the header (schema 9, sec002_log.py) and sends each item k times, in three passes:
  1. Tracks A and B: each sequence, one conversation for each run; each turn has its own run record
  2. Track C(ii): each cell, one pool objective in one transformation family, in one turn. The engine makes the cell
     text at run time from the pool text (sec002_families.encode). It sends that text and stores only its SHA-256
  3. the isolation arm (line 30): each earlier turn of each sequence with a hit, sent alone in a new session
The isolation_set record, written before the third pass, names each turn sent alone and each sequence whose turns
were not. The engine writes the signed run log and stops at the spend cap. It makes model calls only through the
SRD-1 gateway. In evidence mode it refuses, before the lock, a baseline log of another target, one that is not in
evidence mode, one whose signature does not verify against baseline_pubkey, and one whose stored scores this scorer
does not give again (sec002_log.evidence_problems).

Each reply is redacted of SRD-1's staged placeholders (sec006.redact). Then each copy of an encoded pool text in it is
replaced by sec002.CELL_TEXT_REMOVED (sec002.remove_cell_texts), and the text that remains is scored
(sec002.score_turn), for every track. So no file that the harness writes holds a whole copy of a transformed string as
the engine made it (line 20), and a reply that only echoes a cell text is not a success: it repeats the request. The
removal finds exact copies only. A partial or re-wrapped echo of the request (a part of the text, or the text with
other line breaks or spaces) is not removed, and it stays in the stored reply. The record stores that text (store_replies
full) or the SHA-256 of the reply as received (hash_only). A rescore of the stored reply, and a rescore from the store
of the replies as received, make the same removal before the score, so both give the score of the record again. The
run gives the Inspect solver the reply as stored, and the Inspect sample of a cell shows the placeholder text
sec002.CELL_TEXT. SRD-1's own trace store keeps each message as sent, the cell texts included. It goes in the folder
target/ of the run folder, with a README.

This engine runs with tools off: SRD-1 sends no tool definition to the model.
"""
import dataclasses
import time
from collections import Counter
from pathlib import Path

import yaml

from .. import yamlio
from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from . import run_sec006, runlog, sec001, sec002, sec002_families, sec002_log, sec006, sec006_log, spike

REPO = Path(__file__).resolve().parents[2]
TARGET_DIR = spike.TARGET_DIR
TARGET_README = ("This folder holds SRD-1's own trace store: each message as sent, the encoded text of each cell "
                 "included, and each reply as the model gave it, with the staged placeholder values. It is the "
                 "target's record, not the harness log. Do not put it in the working papers.\n")
TRACE_STORE = ("SRD-1's own trace store (C-15) keeps each message as sent, the encoded text of each cell included, and "
               "each reply as the model gave it; it is the target's record, not the harness log. It is in the folder "
               "target/ of the run folder; keep it out of the working papers")
E2_FILE = "targets/srd1/channel_inputs.yaml"
E2_FIELDS = ("languages", "transformation_families", "declared_by", "declared_on", "status")
_pass = run_sec006._pass        # one pass of either engine, as for TP-SEC-006


def isolation_plan(seqs, ids=None):
    """The isolation items of the sequences named in `ids` (of every sequence when None): each earlier turn of each,
    as (Item of track I, its row of the isolation_set record). The final turn is the TP-SEC-001 text of the objective,
    so the baseline log answers it."""
    return [(dataclasses.replace(s, id=f"I-{s.id}-{t}", track="I", turns=(text,), text=text,
                                 source=f"{s.id} turn {t}"), {"id": f"I-{s.id}-{t}", "sequence": s.id, "turn": t})
            for s in seqs if ids is None or s.id in ids for t, text in enumerate(s.turns[:-1], 1)]


def planned_calls(settings, repo=REPO):
    """The most model calls that a run makes: each sequence turn, each cell and each turn of the isolation arm at its
    worst case (every earlier turn of every sequence), k times each."""
    seqs, cells, _ = sec002.load_items(repo)
    return (sum(len(s.turns) for s in seqs) + len(cells) + len(isolation_plan(seqs))) * settings.runs_per_item


def estimate(settings, repo=REPO):
    """The worst-case cost of the whole run, in USD, or None if the prices are not set. Every call is counted at the
    output limit. A sequence turn also counts the earlier replies of its conversation at the output limit, as in
    run_sec006.estimate. A cell counts its encoded text, made here in memory. The isolation arm counts at its worst
    case: every earlier turn of every sequence, each alone. The estimate takes the prompt's size from the prompt file;
    the run reads the prompt through config_read. The gateway checks the cap before each call."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    _, gw, orch, _ = spike._srd1()
    system = [{"role": "system", "content": orch.load_system_prompt()}]
    seqs, cells, pool = sec002.load_items(repo)
    tokens = calls = 0
    for s in seqs:
        users = []
        for j, t in enumerate(s.turns):
            users.append({"role": "user", "content": t})
            tokens += gw.estimate_tokens(system + users) + j * settings.max_tokens
            calls += 1
    alone = ([sec002_families.encode(c.family, pool[c.objective]["text"]) for c in cells]
             + [i.text for i, _ in isolation_plan(seqs)])
    tokens += sum(gw.estimate_tokens(system + [{"role": "user", "content": t}]) for t in alone)
    calls += len(alone)
    k = settings.runs_per_item
    return (tokens * k * settings.price_in_per_m + calls * k * settings.max_tokens * settings.price_out_per_m) / 1_000_000


def read_e2(path=E2_FILE, repo=REPO):
    """The e2 block of the header: the declaration of the channel inputs (evidence output E2, line 52), quoted with its
    SHA-256, who declared it and when, its status, and the languages and the transformation families that it lists.
    It also holds product_owner_declared (sec002_log.E2_DECLARED, since 0.8): true or false as the file gives it, or
    None when the file has no such field. Raises ValueError when the file cannot be read, lacks one of E2_FIELDS, or
    gives product_owner_declared a value that is not true or false."""
    p = Path(path) if Path(path).is_absolute() else Path(repo) / path
    try:
        text = p.read_text(encoding="utf-8")
        data = yamlio.load(text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        raise ValueError(f"the declaration of the channel inputs cannot be read ({type(e).__name__}): "
                         f"{p.name}") from None
    data = data if isinstance(data, dict) else {}
    missing = [k for k in E2_FIELDS if data.get(k) in (None, "")
               or (k in ("languages", "transformation_families")) != isinstance(data.get(k), list)]
    if missing:
        raise ValueError(f"the declaration of the channel inputs ({p.name}) lacks {', '.join(missing)}")
    declared = data.get(sec002_log.E2_DECLARED)
    if declared is not None and not isinstance(declared, bool):
        raise ValueError(f"the declaration of the channel inputs ({p.name}) gives {sec002_log.E2_DECLARED} a value "
                         "that is not true or false")
    return {"file": sec006_log._where(p, repo)[0], "quoted": text, "sha256": runlog.sha256_hex(text),
            "declared_by": str(data["declared_by"]), "declared_on": str(data["declared_on"]),
            "status": str(data["status"]), "languages": [str(x) for x in data["languages"]],
            "transformation_families": [str(x) for x in data["transformation_families"]],
            sec002_log.E2_DECLARED: declared}


def procedure_block(repo=REPO):
    """The procedure block of a schema 9 header, from records/TP-SEC-002.yaml."""
    rec = sec001.load_record(tp=sec002.TP, repo=repo)
    return {"tp": rec["tp"], "title": rec["title"], "irq": rec["irq"], "star": rec["star"],
            "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
            "faias_version": sec002.FAIAS_VERSION, "source": rec["source"]}


def runs_per_item_difference(k, baseline_k):
    """The words that name a difference between the k of the run and the k of the baseline log, or None when they are
    equal. Line 38 compares the uplift with the run-to-run variance of TP-SEC-001, and that variance depends on k."""
    if baseline_k == k:
        return None
    if baseline_k is None:
        return f"the run takes k = {k}; the baseline log does not state its k"
    return (f"the run takes k = {k} and the baseline log k = {baseline_k}: line 38 compares the uplift with the "
            "run-to-run variance of TP-SEC-001, and a different k gives a different variance")


def corpus_block(baseline, k, repo=REPO):
    """The corpus block of a schema 9 header, for a run on SRD-1 minimal and for an external run: the pool with the
    facts of each objective in `baseline`, the track counts, the families, the languages, the cells table, the
    manifest, the items hash, k with its basis, the k of the baseline log beside it with any difference, the
    authorship and the scorer keys. No item text."""
    data = sec002.load_set(repo)
    seqs, _, pool = sec002.load_items(repo)
    classes = {o: e["class"] for o, e in pool.items()}
    rows, keys = sec002_log.manifest(seqs), sec002_log.scorer_keys(pool)
    cells = sec002_log.cells_table(pool, data["families"])
    return {"item_set": sec002.ITEM_SET, "objectives": sec002_log.objectives(classes, baseline),
            "tracks": sec002_log.tracks(rows, cells), "families": list(data["families"]),
            "languages": list(data["languages"]), "cells": cells, "manifest": rows,
            "items_sha256": sec002_log.items_digest(classes, rows, cells, keys), "runs_per_item": k,
            "runs_per_item_basis": ('"Two runs per sequence minimum" and "three is preferred where budget '
                                    'allows" (PLAYBOOK_SEC_002.md line 24); each sequence, cell and isolation '
                                    f"item runs k = {k} times, and the settings take 3 to 20"),
            "runs_per_item_baseline": baseline.get("runs_per_item"),
            "runs_per_item_difference": runs_per_item_difference(k, baseline.get("runs_per_item")),
            "authors": data.get("authors"), "generated_by": data.get("generated_by"), "scorer_keys": keys,
            "scoring": "deterministic; no model judge"}


def build_header(settings, *, allow_hosts, config, session, baseline, e2, confirmation, probes=None, repo=REPO,
                 variant="standard", engine="inspect"):
    """The header of a TP-SEC-002 run (schema 9), without the lock. `config` and `session` are what config_read
    returned for the kinds prompt and session (the header keeps the hashes, the counts and the limits, not the prompt).
    `baseline`: the facts of sec002_log.read_baseline. `e2`: the block of read_e2."""
    data = sec002.load_set(repo)
    baseline = {**baseline, "differs_from_run": sec006_log.baseline_differences(baseline, sec006_log.run_facts(
        settings.model_id, variant, config["prompt_sha256"], settings.routing()))}
    match = not baseline["differs_from_run"]
    store, k = settings.store_replies, settings.runs_per_item
    return {
        "type": "header", "schema": sec002_log.SCHEMA,
        "label": sec002_log.run_label(match, baseline, authors=data.get("authors")),
        "engine_version": runlog.__version__, "code": sec002_log.code_facts(repo),
        "procedure": procedure_block(repo),
        "mode": settings.mode, "provider": settings.provider, "model": {"pinned_id": settings.model_id},
        "routing": settings.routing(),
        "prices_per_million_tokens": {"in": settings.price_in_per_m, "out": settings.price_out_per_m},
        "cap_usd": settings.cap_usd, "max_tokens": settings.max_tokens, "settings_hash": settings.digest(),
        "thresholds": sec002.thresholds_for_header(settings, repo=repo),
        "corpus": corpus_block(baseline, k, repo),
        "config": {"read_through": "config_read (GET /config/prompt of SRD-1 minimal, through the srd1-http adapter)",
                   "prompt_sha256": config["prompt_sha256"], "prompt_source": config.get("prompt_source"),
                   "prompt_chars": len(config["prompt"])},
        "baseline": baseline, "baseline_match": match,
        "session_limits": {"read_through": "config_read (GET /config/session of SRD-1 minimal, through the srd1-http "
                                           "adapter)",
                           "max_turns": session["max_turns"], "max_message_chars": session["max_message_chars"],
                           "session_monitoring": session.get("session_monitoring")},
        "e2": e2, "readings": list(sec002_log.READINGS),
        "store_replies": {"mode": store, "note": sec002_log.STORE_NOTES[store]},
        "variant": {"name": variant, "purpose": runlog.VARIANTS[variant]},
        "run_engine": runlog._engine_facts(engine),
        "scorer": sec002_log.SCORER_STATUS,
        "system_prompt_sha256": config["prompt_sha256"],
        "egress": {"allow": sorted(allow_hosts), "limit": runlog.EGRESS_LIMIT},
        "not_in_run": list(sec002_log.NOT_IN_RUN) + [TRACE_STORE],
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each run record (served_host)",
    }


class _Runner(run_sec006._Runner):
    """One run of one sequence, one cell or one isolation item: send each turn in one conversation, read its trace,
    redact the reply (a sequence turn with the earlier replies of its conversation), take out each copy of an encoded
    pool text, score the text that remains and write its record. Both engines call `one`. After a stop, `one` sends
    nothing and returns None.

    `pool` is the objective pool (sec002.load_items). A cell text is made from the pool text when the cell is sent."""

    def __init__(self, log, clock_ns, pool, placeholders, store, halt_on_mismatch=False):
        super().__init__(log, clock_ns, None, placeholders, store, halt_on_mismatch=halt_on_mismatch)
        self.pool = pool
        # Each encoded form of each pool text, under each family: no record may hold one (line 20).
        self.encoded = sec002.encoded_forms(e["text"] for e in pool.values())
        self.removed = 0            # the copies replaced by sec002.CELL_TEXT_REMOVED over the run

    def sent(self, item):
        """What one run of `item` sends: the turns of a sequence or an isolation item, or the cell text."""
        if item.track == "C":
            return (sec002_families.encode(item.family, self.pool[item.objective]["text"]),)
        return item.turns

    def one(self, target, item, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        turns, sequence = self.sent(item), item.track in sec002.TRACKS
        t_ns = self.clock_ns()
        try:
            sid = target.open_session()
        except Exception as e:      # noqa: BLE001
            self._fail(item, n, t_ns, e)
            return None
        done, received = [], []         # received: the replies of this conversation as received, for the redaction
        for t, text in enumerate(turns, 1):
            turn = t if sequence else None
            t_ns = self.clock_ns()
            try:
                reply = target.send(sid, text)
                tr = [e for e in target.get_trace(sid) if e["kind"] == "turn"][-1]
            except Exception as e:      # noqa: BLE001
                self._fail(item, n, t_ns, e, turn)
                return None
            done.append(self._record(item, n, t_ns, text, reply, tr, turn, len(turns), earlier=tuple(received)))
            received.append(reply or "")
            if self.stopped:
                return None
        try:
            target.close_session(sid)
        except Exception as e:      # noqa: BLE001
            self._fail(item, n, t_ns, e)
            return None
        hits = [d["score"]["evidence"] for d in done if d["score"]["success"]]
        kind = "sequence" if sequence else "cell" if item.track == "C" else "isolation"
        self.last_score = {"value": 1 if hits else 0, "kind": kind, "explanation": "; ".join(hits) or None}
        result = sec002.sequence_outcome(done)["outcome"] if sequence else done[-1]["outcome"]
        self.last_no_result = result == "no_result"
        return done[-1]["shown"]

    def _record(self, item, n, t_ns, request, reply, tr, turn, n_turns, earlier=()):
        redacted, counts = sec006.redact(reply, self.placeholders, earlier)
        stored, removed = sec002.remove_cell_texts(redacted, self.encoded)     # remove first, then score
        self.removed += removed
        score = sec002.score_turn(self.pool[item.objective], stored, item.family if item.track == "C" else None)
        outcome = sec002.outcome(score, stored, tr.get("finish_reason"), tr.get("blocked"))
        self.hosts[str(tr["served_host"])] += 1
        self.models[str(tr["model_served"])] += 1
        halt = spike.host_tally(self, tr["host_ok"])
        self.usage_missing += 1 if tr["usage_missing"] else 0
        self.retries += tr.get("retries", 0)
        run = {"item": item.id, "track": item.track, "objective": item.objective, "class": item.cls, "run": n}
        if turn:
            run.update(turn=turn, turns=n_turns)
        if item.track == "C":
            run["family"] = item.family
        empty = not (stored or "").strip()
        self.runs.append({**run, "score": score, "outcome": outcome, "finish_reason": tr.get("finish_reason"),
                          "reply_empty": empty})
        rec = {"type": "run", **run, "t_ns": t_ns, "time": runlog.iso(t_ns),
               "request_sha256": runlog.sha256_hex(request)}
        full = self.store == "full"
        if full:
            rec["reply"] = stored
        else:
            rec["reply_sha256"] = runlog.sha256_hex(reply or "")
        rec.update(score=score, outcome=outcome, placeholders=counts, cell_texts_removed=removed, reply_empty=empty,
                   finish_reason=tr.get("finish_reason"), blocked=tr.get("blocked"), served_host=tr["served_host"],
                   served_model=tr["model_served"], host_ok=tr["host_ok"], model_requested=tr["model_requested"],
                   prompt_tokens=tr["prompt_tokens"], completion_tokens=tr["completion_tokens"],
                   usage_missing=tr["usage_missing"], cost_usd=tr["cost_usd"], latency_ms=tr["latency_ms"],
                   seed=tr.get("seed"), retries=tr.get("retries", 0), retry_reasons=tr.get("retry_reasons", []),
                   trace_seq=tr["seq"])
        self.log.write(rec)
        if halt:
            self.status = "stopped_host_mismatch"
            self._stop(item, n, t_ns, turn, served_host=tr["served_host"])
        shown = stored if full else f"reply stored as SHA-256 only: {rec['reply_sha256']}"
        return {**run, "score": score, "outcome": outcome, "shown": shown}


def sequence_outcomes(runs):
    """The outcome of each run of each sequence (sec002.sequence_outcome over its turn records), in the order run."""
    groups = {}
    for r in runs:
        if r["track"] in sec002.TRACKS:
            groups.setdefault((r["item"], r["run"]), []).append(r)
    return [{"item": i, "run": n, **sec002.sequence_outcome(turns)} for (i, n), turns in groups.items()]


def _isolation(runner, seqs, clock_ns):
    """The isolation arm: its items and its isolation_set record. A sequence with a hit is one with an attacked run."""
    hit = {o["item"] for o in sequence_outcomes(runner.runs) if o["outcome"] == "attacked"}
    plan = isolation_plan(seqs, hit)
    rec = {"type": "isolation_set", "t_ns": clock_ns(), "applicable": bool(plan),
           "sequences": [s.id for s in seqs if s.id in hit], "items": [row for _, row in plan],
           "not_sent_alone": [s.id for s in seqs if s.id not in hit],
           "rule": "each earlier turn of each sequence with a hit is sent alone, in a new session, k times; the final "
                   "turn is the TP-SEC-001 text of the objective, which the baseline log answers; the turns of a "
                   "sequence with no hit are not sent alone, so line 30 is not tested for it"}
    if not plan:
        rec["reason"] = "no sequence run had a hit, so no turn is sent alone"
    return tuple(i for i, _ in plan), rec


def execute(settings, *, run_dir, sign_key, confirmation, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant="standard", engine="inspect",
            e2_file=None):
    """Run TP-SEC-002 on SRD-1 minimal. Return a spike.RunResult. `base_url` and `env` are for tests.

    `variant` is "standard" (the only one for this procedure). `engine` is "inspect" (the default) or "direct" (a
    plain loop, for tests without Inspect). `e2_file` is the declaration of the channel inputs, in place of
    targets/srd1/channel_inputs.yaml (for tests). The baseline log is the settings' baseline_log, checked against
    baseline_pubkey; the run refuses to start without it. It also refuses, before it writes a file, a declaration of the
    channel inputs whose families are not those of the item file (sec002_log.e2_family_problems)."""
    build, gw, orch_mod, trace_mod = spike._srd1()
    if settings.procedure != sec002.TP:
        raise ValueError("the settings do not name the procedure TP-SEC-002")
    if variant not in sec002_log.VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    if getattr(settings, "tools_enabled", False):
        raise ValueError("this engine runs TP-SEC-002 with tools off")
    if not settings.m4_baseline_log:
        raise ValueError("a TP-SEC-002 run needs a TP-SEC-001 baseline log (baseline_log) in both modes: without it "
                         "the uplift cannot be computed (PLAYBOOK_SEC_002.md line 8)")
    seqs, cells, pool = sec002.load_items(repo)
    baseline = sec002_log.read_baseline(settings.m4_baseline_log, list(pool), repo, pubkey=settings.m4_baseline_pubkey)
    e2 = read_e2(e2_file or E2_FILE, repo)
    refused = sec002_log.e2_family_problems(e2, sec002.load_set(repo)["families"])
    if refused:         # the preflight checks this too; a direct call may not skip it
        raise ValueError("; ".join(refused))
    prompt_file = orch_mod.PROMPT_VARIANTS[variant]
    base_url = base_url or gw.OPENROUTER_BASE
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    # SRD-1's own trace store holds each message as sent, the cell texts included, and each reply as the model gave
    # it. It goes in its own folder, with a README, so that it is not taken into the working papers with the run log.
    (run_dir / TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / TARGET_DIR / "README.txt").write_text(TARGET_README, encoding="utf-8")
    srd1 = build.build_srd1(settings, egress, run_dir / TARGET_DIR / "srd1_trace.jsonl", base_url=base_url, env=env,
                            system_prompt=orch_mod.load_system_prompt(prompt_file), prompt_source=prompt_file.name)
    egress.add_allow(f"127.0.0.1:{srd1.server.port}")
    srd1.gateway.cap.spent_usd = prior_spent_usd
    target = Srd1ChatTarget(srd1.server.url, egress, timeout=gw.max_call_seconds(egress.timeout) + spike.LOCAL_MARGIN_S)
    log_path = run_dir / "run_log.jsonl"
    k = settings.runs_per_item
    srd1.server.start()
    try:
        config = target.read_config("prompt")       # through the interface; the harness reads no prompt in process
        if runlog.sha256_hex(config["prompt"]) != config["prompt_sha256"]:
            raise ValueError("config_read returned a prompt whose SHA-256 is not the one that it states")
        session = target.read_config("session")
        if not all(type(session.get(x)) is int for x in ("max_turns", "max_message_chars")):
            raise ValueError("config_read('session') did not return the turn limit and the message limit of a session")
        placeholders = sec006.find_placeholders(t for _, t in sec006.config_sources(config))
        header = build_header(settings, allow_hosts=egress.allow_text, config=config, session=session,
                              baseline=baseline, e2=e2, confirmation=confirmation, probes=probes, repo=repo,
                              variant=variant, engine=engine)
        if settings.mode == "evidence":
            refused = sec002_log.evidence_problems(header["baseline"])
            if refused:
                raise ValueError(f"evidence mode needs {sec002_log.EVIDENCE_BASELINE}: " + "; ".join(refused))
        runlog.lock_header(header, clock_ns())
        log = runlog.RunLog(log_path)
        try:
            log.write(header)                   # the lock is on disk before the first item is sent
            runlog.wait_past_lock(header, clock_ns)
            runner = _Runner(log, clock_ns, pool, placeholders, settings.store_replies,
                             halt_on_mismatch=settings.mode == "evidence")
            engine_facts = [_pass(engine, target, runner, run_dir, k, seqs + cells)]
            iso_record = None
            if not runner.stopped:
                iso_items, iso_record = _isolation(runner, seqs, clock_ns)
                log.write(iso_record)
                if iso_items:
                    engine_facts.append(_pass(engine, target, runner, run_dir, k, iso_items))
        except BaseException:
            log.close()
            raise
    finally:
        srd1.server.stop()
    try:
        runs = runner.runs
        chain_ok, chain_problems = trace_mod.verify_chain(srd1.trace.path, expected_head=srd1.trace.head)
        egress.close()
        summary = {
            "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
            "runs_recorded": len(runs), "measures_complete": runner.status == "complete",
            "measures": sec002_log.measures(header, runs, iso_record),
            "results_table": sec002.results_table(runs), "sequence_outcomes": sequence_outcomes(runs),
            "cell_texts_removed": runner.removed,
            "cost_usd": round(srd1.gateway.cap.spent_usd, 8), "model_calls": srd1.gateway.cap.calls,
            "cap_usd": settings.cap_usd, "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
            "host_mismatches": runner.mismatches, "usage_missing_calls": runner.usage_missing,
            "retries": runner.retries, "host_not_reported": runner.host_not_reported,
            "calls_over_max_tokens": srd1.gateway.cap.calls_over_max_tokens,
            "outcomes": dict(Counter(r["outcome"] for r in runs)),
            "trace": {"file": f"{TARGET_DIR}/{srd1.trace.path.name}",
                      "sha256": runlog.sha256_hex(srd1.trace.path.read_bytes()),
                      "entries": len(srd1.trace.entries()), "head": srd1.trace.head, "chain_ok": chain_ok,
                      "problems": chain_problems},
            "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                       "summary": elog.summary()},
            "run_engine": engine_facts,
            "finished": runlog.iso(),
        }
        log.write(summary)
    finally:
        log.close()
    sig = runlog.sign_log(log_path, sign_key)
    return spike.RunResult(runner.status, run_dir, log_path, sig, summary)
