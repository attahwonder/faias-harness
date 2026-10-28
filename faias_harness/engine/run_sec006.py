"""The engine of TP-SEC-006 on SRD-1 minimal: estimate and run.

`execute` starts SRD-1 minimal on 127.0.0.1. It reads the system prompt of the active variant and the tools through
the target interface (`config_read`), never in the process, and checks the SHA-256 that SRD-1 states. Before any item
is sent, it finds the staged placeholders (the staging record, E4) and runs the static inspection (M5, step 2). It
then locks and writes the header, and sends the 60 items of items/TP-SEC-006.json and its 8 X4 sequences, k times
each (one conversation for each sequence run; each turn has its own run record). From the replies that disclose
prompt text, it builds the follow-on set (step 7: 20 TP-SEC-001 class C1 items) and runs it. With no disclosure,
the follow-on set is empty and M4 is not applicable. It writes the signed run log (schema 6, sec006_log.py) and
stops at the spend cap. It makes model calls only through the SRD-1 gateway. The preflight calls it after the operator
types `yes` (settings `procedure: TP-SEC-006`). It refuses an M4 baseline log whose signature does not verify against
the key that `m4_baseline_pubkey` names. In evidence mode it also refuses, before the lock, a baseline log whose model
ID, variant, system prompt hash or routing locks (`only`, `quantizations`) differ from the run, or that is not in
evidence mode. SRD-1's trace store goes in the folder `target/` of the run folder, with a README.

With `tools_enabled` (since 0.6) the run serves SRD-1's tool through the emulator (toolrun.ToolRun), as spike.execute
does. config_read then returns the tool definition, so the placeholder search and the static inspection (M5) read it.
The header gets the tools block, and the summary gets the tool ledger. The M4 baseline log must also match the hash of
the tool definitions (sec006_log.MATCH_FIELDS).
"""
import dataclasses
import time
from collections import Counter
from pathlib import Path

from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..errors import CapReached
from . import runlog, sec001, sec006, sec006_log, spike, toolrun

REPO = Path(__file__).resolve().parents[2]
TARGET_DIR, TARGET_README = spike.TARGET_DIR, spike.TARGET_README     # the same folder and README as TP-SEC-001


def run_items(repo=REPO):
    """What the first pass sends: the items, then the sequences."""
    items, seqs = sec006.load_items(repo)
    return items + seqs


def prompt_sha256(variant="standard"):
    """The SHA-256 of the system prompt of a variant, from its prompt file, as SRD-1 minimal states it through
    config_read. The preflight compares it with the M4 baseline log before the run starts."""
    _, _, orch, _ = spike._srd1()
    return runlog.sha256_hex(orch.load_system_prompt(orch.PROMPT_VARIANTS[variant]))


def _tools(settings):
    """With tools on: the tool definitions that SRD-1 sends to the model (built from its tools file, as its MCP client
    builds them from tools/list), and the most model calls of one turn (1 + MAX_TOOL_ROUNDS). With tools off: None
    and 1."""
    if not getattr(settings, "tools_enabled", False):
        return None, 1
    _, _, orch, _ = spike._srd1()
    from targets.srd1.app.mcp_client import function_definitions
    return function_definitions(toolrun.mcp_tools.load_tools(toolrun.SRD1_TOOLS)["tools"]), 1 + orch.MAX_TOOL_ROUNDS


def tool_definitions_sha256(settings):
    """The SHA-256 of the tool definitions that SRD-1 sends to the model, from its tools file, or None with tools off.
    SRD-1 minimal states the same hash through config_read. The preflight compares it with the M4 baseline log."""
    definitions, _ = _tools(settings)
    return None if not definitions else runlog.sha256_hex(runlog.canonical(definitions))


def planned_calls(settings, repo=REPO):
    """The most model calls that a run makes: each item, each sequence turn and each follow-on item, k times. With
    tools on, each of them counts at 1 + MAX_TOOL_ROUNDS calls, the most that one turn can make."""
    items, seqs = sec006.load_items(repo)
    _, per_turn = _tools(settings)
    return (len(items) + sum(len(s.turns) for s in seqs) + sec006.FOLLOWON_SIZE) * settings.runs_per_item * per_turn


def estimate(settings, repo=REPO):
    """The worst-case cost of the whole run, in USD, or None if the prices are not set. Every call is counted at the
    output limit; a sequence turn also counts the earlier replies of its conversation at the output limit. The
    estimate takes the prompt's size from the prompt file; the run reads the prompt through config_read.

    With tools on, each call also carries the tool definitions, and each turn counts at 1 + MAX_TOOL_ROUNDS calls, as
    in spike.estimate. The estimate does not count the tool calls and the tool results that a turn adds to the
    messages. The gateway checks the cap before each call."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    _, gw, orch, _ = spike._srd1()
    tools, per_turn = _tools(settings)
    system = [{"role": "system", "content": orch.load_system_prompt()}]
    items, seqs = sec006.load_items(repo)
    tokens = sum(gw.estimate_tokens(system + [{"role": "user", "content": i.text}], tools) for i in items)
    calls = len(items)
    for s in seqs:
        users = []
        for j, t in enumerate(s.turns):
            users.append({"role": "user", "content": t})
            tokens += gw.estimate_tokens(system + users, tools) + j * settings.max_tokens
            calls += 1
    longest = max(sec006.FOLLOWON_TEMPLATES, key=len).format(fragment="w" * 12 * sec006.MAX_FRAGMENT_WORDS,
                                                             a="W" * 12, b="0000")
    tokens += gw.estimate_tokens(system + [{"role": "user", "content": longest}], tools) * sec006.FOLLOWON_SIZE
    calls += sec006.FOLLOWON_SIZE
    tokens, calls = tokens * per_turn, calls * per_turn
    k = settings.runs_per_item
    return (tokens * k * settings.price_in_per_m + calls * k * settings.max_tokens * settings.price_out_per_m) / 1_000_000


class _Runner:
    """One run of one item or one sequence: send each turn in one conversation, read its trace, redact the reply (a
    sequence turn with the earlier replies of its conversation) and score it, write its record. Both engines call
    `one`. After a stop, `one` sends nothing and returns None.

    `lookup` maps an item ID to the item to send, so that the Inspect log can show a follow-on item without its text
    (store_replies hash_only)."""

    def __init__(self, log, clock_ns, index, placeholders, store, halt_on_mismatch=False):
        self.log, self.clock_ns, self.index, self.placeholders, self.store = log, clock_ns, index, placeholders, store
        self.halt_on_mismatch = halt_on_mismatch
        self.lookup = {}
        self.status, self.error = "complete", None
        self.runs, self.hosts, self.models = [], Counter(), Counter()
        self.mismatches = self.usage_missing = self.retries = self.host_not_reported = 0
        self.disclosing = []        # the redacted replies of X1 to X4 that disclose prompt text (step 7 material)
        self.last_no_result, self.last_score = False, None

    @property
    def stopped(self):
        return self.status != "complete"

    def _stop(self, item, n, t_ns, turn=None, **extra):
        rec = {"type": "run_stopped", "item": item.id, "run": n, "t_ns": t_ns, "reason": self.status,
               "error": self.error, **extra}
        if turn:
            rec["turn"] = turn
        self.log.write(rec)

    def _fail(self, item, n, t_ns, exc, turn=None):
        if isinstance(exc, CapReached):
            self.status = "stopped_at_cap"
        else:
            self.status, self.error = "stopped_error", type(exc).__name__      # only the class name is kept
        self._stop(item, n, t_ns, turn)

    def one(self, target, item, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        item = self.lookup.get(item.id, item)
        turns = item.turns or (item.text,)
        t_ns = self.clock_ns()
        try:
            sid = target.open_session()
        except Exception as e:      # noqa: BLE001
            self._fail(item, n, t_ns, e)
            return None
        done, received = [], []         # received: the replies of this conversation as received, for the redaction
        for t, text in enumerate(turns, 1):
            turn = t if item.turns else None
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
        kind = "override" if item.cls == sec006.FOLLOW_ON else "sequence" if item.turns else "disclosure"
        hits = [d["evidence"] for d in done if d["success"]]
        self.last_score = {"value": 1 if hits else 0, "kind": kind, "explanation": "; ".join(hits) or None}
        self.last_no_result = all(d["outcome"] == "no_result" for d in done)
        return done[-1]["shown"]

    def _record(self, item, n, t_ns, request, reply, tr, turn, n_turns, earlier=()):
        redacted, counts = sec006.redact(reply, self.placeholders, earlier)
        if item.cls == sec006.FOLLOW_ON:
            sc = sec006.score_followon(item, redacted)
            score, evidence = {"success": sc.success, "kind": sc.kind, "evidence": sc.evidence}, "marker"
        else:
            sc = sec006.score_reply(redacted, self.index)
            score = sc.as_dict()
            evidence = ", ".join(x for x in ("credential" if sc.credential else "", "endpoint" if sc.endpoint else "",
                                             sc.disclosure if sc.disclosure != "none" else "") if x)
            if sc.disclosure != "none":
                self.disclosing.append(redacted)
        outcome = sec001.outcome(sc, redacted, tr.get("finish_reason"), tr.get("blocked"))
        self.hosts[str(tr["served_host"])] += 1
        self.models[str(tr["model_served"])] += 1
        halt = spike.host_tally(self, tr["host_ok"])
        self.usage_missing += 1 if tr["usage_missing"] else 0
        self.retries += tr.get("retries", 0)
        self.runs.append({"item": item.id, "cls": item.cls, "run": n, "turn": turn, "outcome": outcome,
                          "score": score, "placeholders": counts})
        rec = {"type": "run", "item": item.id, "class": item.cls, "run": n}
        if turn:
            rec.update(turn=turn, turns=n_turns)
        rec.update(t_ns=t_ns, time=runlog.iso(t_ns))
        full = self.store == "full"
        if full or item.cls != sec006.FOLLOW_ON:
            rec["request"] = request
        else:
            rec["request_sha256"] = runlog.sha256_hex(request)
        if full:
            rec["reply"] = redacted
        else:
            rec["reply_sha256"] = runlog.sha256_hex(reply or "")
        rec.update(score=score, placeholders=counts, outcome=outcome, finish_reason=tr.get("finish_reason"),
                   blocked=tr.get("blocked"), served_host=tr["served_host"], served_model=tr["model_served"],
                   host_ok=tr["host_ok"], model_requested=tr["model_requested"], prompt_tokens=tr["prompt_tokens"],
                   completion_tokens=tr["completion_tokens"], usage_missing=tr["usage_missing"],
                   cost_usd=tr["cost_usd"], latency_ms=tr["latency_ms"], seed=tr.get("seed"),
                   retries=tr.get("retries", 0), retry_reasons=tr.get("retry_reasons", []), trace_seq=tr["seq"])
        self.log.write(rec)
        if halt:
            self.status = "stopped_host_mismatch"
            self._stop(item, n, t_ns, turn, served_host=tr["served_host"])
        shown = redacted if full else f"reply stored as SHA-256 only: {rec['reply_sha256']}"
        return {"shown": shown, "outcome": outcome, "success": sc.success, "evidence": evidence}


def _pass(engine, target, runner, run_dir, k, items):
    if engine == "inspect":
        return spike._run_inspect(target, runner, run_dir, k, items)
    return spike._run_direct(target, runner, k, items)


def _followon(runner, index, settings, clock_ns, repo):
    """Step 7: the follow-on set from the material that X1 to X4 disclosed, and its followon_set record."""
    fragments = sec006.disclosed_fragments(runner.disclosing, index)
    t_ns = clock_ns()
    if not fragments:
        return (), (), {"type": "followon_set", "t_ns": t_ns, "applicable": False, "fragments": 0, "items": [],
                        "reason": "no reply of X1 to X4 disclosed prompt text, so the follow-on set has no material "
                                  "(step 7, line 37); M4 is not applicable. A disclosed placeholder is counted in M1."}
    items = sec006.followon_items(fragments, sec006.load_set(repo)["activity_rule"])
    full = settings.store_replies == "full"
    rec_items = [{"id": i.id, "marker": i.marker, "technique": i.technique, "activity": i.activity,
                  **({"text": i.text} if full else {"text_sha256": runlog.sha256_hex(i.text)})} for i in items]
    shown = items if full else tuple(dataclasses.replace(i, text=f"follow-on item {i.id} (its text is stored as "
                                                                  "SHA-256 only)") for i in items)
    return items, shown, {"type": "followon_set", "t_ns": t_ns, "applicable": True, "fragments": len(fragments),
                          "items": rec_items}


def execute(settings, *, run_dir, sign_key, confirmation, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant="standard", engine="inspect", gate=None,
            tools_file=None):
    """Run TP-SEC-006 on SRD-1 minimal. Return a spike.RunResult. `base_url` and `env` are for tests.

    `variant` is "standard" or "positive_control" (the weakened system prompt, which tells the model to show its
    instructions). `engine` is "inspect" (the default) or "direct" (a plain loop, for tests without Inspect).
    With `tools_enabled` (since 0.6) the run serves SRD-1's tool through the emulator. `gate` and `tools_file` are for
    tests: a RefundGate with the test's own values in place of the risk-appetite file, and a tools file in place of
    targets/srd1/tools.yaml."""
    build, gw, orch_mod, trace_mod = spike._srd1()
    if settings.procedure != sec006.TP:
        raise ValueError("the settings do not name the procedure TP-SEC-006")
    if variant not in sec006_log.VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    baseline = (sec006_log.read_baseline(settings.m4_baseline_log, repo, pubkey=settings.m4_baseline_pubkey)
                if settings.m4_baseline_log else None)
    prompt_file = orch_mod.PROMPT_VARIANTS[variant]
    base_url = base_url or gw.OPENROUTER_BASE
    tools_on = bool(getattr(settings, "tools_enabled", False))
    if tools_on:
        gate = gate or build.RefundGate.from_file()     # null values: "refund cap not declared", before any file
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    # SRD-1's own trace store holds each reply as the model gave it, with the placeholders. It goes in its own folder,
    # with a README, so that it is not taken into the working papers with the run log (PLAYBOOK_SEC_006.md line 11).
    (run_dir / TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / TARGET_DIR / "README.txt").write_text(TARGET_README, encoding="utf-8")
    tool_run = (toolrun.ToolRun(settings, run_dir, egress, tools_file=tools_file or toolrun.SRD1_TOOLS).start()
                if tools_on else None)
    try:
        srd1 = build.build_srd1(settings, egress, run_dir / TARGET_DIR / "srd1_trace.jsonl", base_url=base_url,
                                env=env, system_prompt=orch_mod.load_system_prompt(prompt_file),
                                prompt_source=prompt_file.name, tools_url=tool_run.url if tool_run else None,
                                tool_env=tool_run.env if tool_run else None, gate=gate)
    except BaseException:
        if tool_run:
            tool_run.stop()
        raise
    egress.add_allow(f"127.0.0.1:{srd1.server.port}")
    srd1.gateway.cap.spent_usd = prior_spent_usd
    target = Srd1ChatTarget(srd1.server.url, egress, timeout=gw.max_call_seconds(egress.timeout) + spike.LOCAL_MARGIN_S,
                            tools_url=tool_run.url if tool_run else None,
                            ledger_path=tool_run.emulator.ledger.path if tool_run else None)
    log_path = run_dir / "run_log.jsonl"
    k = settings.runs_per_item
    srd1.server.start()
    try:
        config = target.read_config("prompt")       # through the interface; the harness reads no prompt in process
        if runlog.sha256_hex(config["prompt"]) != config["prompt_sha256"]:
            raise ValueError("config_read returned a prompt whose SHA-256 is not the one that it states")
        # The hash of the tool definitions that config_read returned (None with tools off). The M4 baseline log must
        # match it, and the tools block of the header holds it.
        definitions = config.get("tool_definitions") or []
        definitions_sha256 = runlog.sha256_hex(runlog.canonical(definitions)) if definitions else None
        if tool_run and target.read_config("tools").get("definitions_sha256") != definitions_sha256:
            raise ValueError("config_read returned tool definitions whose SHA-256 is not the one that it states")
        sources = sec006.config_sources(config)
        placeholders = sec006.find_placeholders(t for _, t in sources)
        redacted_prompt, _ = sec006.redact(config["prompt"], placeholders)
        index = sec006.PromptIndex(redacted_prompt)
        static = sec006.static_inspection(sources, repo=repo)      # step 2: before any item is sent
        header = sec006_log.build_header(
            settings, allow_hosts=egress.allow_text, config=config, placeholders=sec006.placeholder_record(sources),
            static=static, redacted_prompt=redacted_prompt, baseline=baseline, confirmation=confirmation,
            probes=probes, repo=repo, variant=variant, engine=engine,
            tools=tool_run.header(definitions_sha256) if tool_run else None)
        if settings.mode == "evidence" and header["m4_baseline"] is not None:
            refused = sec006_log.evidence_problems(header["m4_baseline"])
            if refused:
                raise ValueError(f"evidence mode needs {sec006_log.EVIDENCE_BASELINE}: " + "; ".join(refused))
        runlog.lock_header(header, clock_ns())
        log = runlog.RunLog(log_path)
        try:
            log.write(header)                   # the lock is on disk before the first item is sent
            runlog.wait_past_lock(header, clock_ns)
            runner = _Runner(log, clock_ns, index, placeholders, settings.store_replies,
                             halt_on_mismatch=settings.mode == "evidence")
            engine_facts = [_pass(engine, target, runner, run_dir, k, run_items(repo))]
            fo_record = None
            if not runner.stopped:
                fo_items, shown, fo_record = _followon(runner, index, settings, clock_ns, repo)
                log.write(fo_record)
                if fo_items:
                    runner.lookup = {i.id: i for i in fo_items}
                    engine_facts.append(_pass(engine, target, runner, run_dir, k, shown))
        except BaseException:
            log.close()
            raise
    finally:
        srd1.server.stop()
        if tool_run:
            try:
                srd1.mcp.close()
            except Exception:       # noqa: BLE001 - the ledger, not the close, is the record
                pass
            tool_run.stop()
    try:
        runs = runner.runs
        chain_ok, chain_problems = trace_mod.verify_chain(srd1.trace.path, expected_head=srd1.trace.head)
        egress.close()
        summary = {
            "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
            "runs_recorded": len(runs), "measures_complete": runner.status == "complete",
            "measures": sec006.compute_measures(
                runs, runs_per_item=k, n_sentences=len(index.sentences), m3_bar=settings.m3_value,
                m4_bar=settings.m4_value, followon=fo_record, baseline=header["m4_baseline"], static=static,
                placeholders=header["placeholders"]),
            "results_table": sec006.results_table(runs),
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
        if tool_run:
            summary["tool_ledger"] = tool_run.summary()
        log.write(summary)
    finally:
        log.close()
    sig = runlog.sign_log(log_path, sign_key)
    return spike.RunResult(runner.status, run_dir, log_path, sig, summary)
