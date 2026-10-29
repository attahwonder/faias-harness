"""This module is the engine of TP-DAT-009 on SRD-1 minimal. The procedure tests the retrieval entitlement boundary.

The steps, in order:
  1. The self-test gate. A run needs a containment self-test log that passes. Its code manifest must match the
     checkout. The executor checks it with selftest.gate_problems, the same rule as the preflight. In evidence mode
     the log's signature must verify against a pinned public key (selftest_pubkey).
  2. The variant rule (dat009.variant_problems). The standard variant needs the filter stage `pre_retrieval` or
     `pre_ranking`, and the credential `user`. The positive control always takes the display filter and the service
     credential.
  3. Two builds of SRD-1 minimal, one for each arm. The pipeline arm retrieves in SRD-1 (C-05 minimal) and sends no
     tool. The tool arm retrieves through the tool document_search only, served by the harness's MCP tool emulator
     from targets/srd1/tools_retrieval.yaml. Each arm has its own trace file. The tool arm has the tool ledger. Both
     builds count against one spend cap.
  4. The matrix check. The harness derives the entitlement matrix from the two data files (dat009.expected_matrix),
     not from SRD-1. It compares the customers, domains, accounts and file hashes with config_read("entitlements")
     and config_read("retrieval") of each arm. It also compares the stage and the credential that each arm declares
     with the ones that it built. A difference refuses the run before the lock.
  5. The header and the lock (dat009_log). The lock covers the matrix, the inventory and the retrieval
     configuration of each arm. It also covers the delegation block and the self-test log hash.
  6. The runs. Each item runs k times on each arm, one session each, for the item's customer. The runner scores each
     run from SRD-1's trace lines and the emulator's ledger lines (dat009.score_run). It reads the reply only for the
     canary and the fact phrases. It writes and prints a record of type `crossing` at once. The run continues, unless
     halt_on_crossing is set.
  7. The summary and the signature. SRD-1 and the emulator stop before the summary, so the summary states the
     teardown as done.

A run record holds the request of a tier 1 or tier 2 item. For a tier 3 item, it holds the SHA-256 of each turn.
Each tool argument in its lines is a SHA-256, except on_behalf_of where SRD-1 set it. The customer's message in its
lines is a SHA-256 too. Each reply in its lines is redacted of the staged placeholders of SRD-1's system prompt. The
reply of a tier 3 item also loses each copy of a turn of the item. The run returns the item ID to
the Inspect solver, so the Inspect log holds no reply. The Inspect sample of a tier 3 item shows its technique and
hash only (dat009.Item.text).

No live call has used this engine. The tests use the stand-in host and the local emulator. No run starts until the
containment self-test passes. Each run needs one `yes`.
"""
import time
from collections import Counter
from pathlib import Path

from .. import selftest
from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..emulators import mcp_tools
from ..errors import CapReached
from . import dat009, dat009_log, runlog, sec006, spike, toolrun
from .run_agt012 import _selftest_sha, _session_calls, _turn_facts     # one rule in one place, as for TP-AGT-012

REPO = Path(__file__).resolve().parents[2]
TARGET_DIR, TARGET_README = spike.TARGET_DIR, spike.TARGET_README
TOOLS_FILE = REPO / "targets" / "srd1" / "tools_retrieval.yaml"
# A run record keeps the trace lines of these kinds, which are the lines that the scorer reads.
RECORD_KINDS = ("retrieval", "entitlement_denial", "tool_call", "turn", "turn_error")


def plan(settings, repo=REPO, items=None):
    """(sessions, the most model calls, items) of a TP-DAT-009 run. Each item runs k times on each arm. A turn of
    the pipeline arm makes one model call. A turn of the tool arm makes at most 1 + MAX_TOOL_ROUNDS calls."""
    items = dat009.load_items(repo) if items is None else items
    _, _, orch, _ = spike._srd1()
    k, arms = settings.runs_per_item, tuple(settings.arms)
    turns = sum(len(i.turns) for i in items) * k
    per_turn = {"pipeline": 1, "tool": 1 + orch.MAX_TOOL_ROUNDS}
    return len(items) * k * len(arms), sum(turns * per_turn[a] for a in arms), items


def estimate(settings, repo=REPO):
    """The worst-case cost of a TP-DAT-009 run in USD, or None if the prices are not set. It counts the calls of
    plan(), each at the full output limit. The input of a pipeline call is the system prompt and the item's turns. It
    also holds a context message of the three longest documents. The input of the call after tool round r of the tool
    arm adds r such search results. A model that asks for more than one search in a round can pass this estimate. The
    cap still stops the run."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    _, calls, items = plan(settings, repo)
    _, gw, orch, _ = spike._srd1()
    from targets.srd1.app import retrieval as rt
    from targets.srd1.app.mcp_client import function_definitions
    system = {"role": "system", "content": orch.load_system_prompt()}
    longest = sorted(rt.DocumentStore().documents, key=lambda d: len(d["title"]) + len(d["text"]),
                     reverse=True)[:rt.TOP_K]
    context = {"role": "system", "content": rt.context_message(
        [{"doc_id": d["doc_id"], "title": d["title"], "text": d["text"]} for d in longest])}
    result = {"role": "tool", "content": context["content"]}
    tools = function_definitions(mcp_tools.load_tools(TOOLS_FILE)["tools"])
    in_tokens = 0
    for i in items:
        user = {"role": "user", "content": " ".join(i.turns)}
        n = len(i.turns) * settings.runs_per_item
        if "pipeline" in settings.arms:
            in_tokens += gw.estimate_tokens([system, context, user]) * n
        if "tool" in settings.arms:
            in_tokens += sum(gw.estimate_tokens([system, user] + [result] * r, tools)
                             for r in range(1 + orch.MAX_TOOL_ROUNDS)) * n
    return (in_tokens * settings.price_in_per_m + calls * settings.max_tokens * settings.price_out_per_m) / 1_000_000


class _Runner:
    """One run of one item on one arm. It opens a session for the item's customer and sends the item's turns. Then
    it reads the trace and the new ledger lines, scores the run and writes the record. Both engines call `one`.
    Call `use` before each arm. After a stop, `one` sends nothing.

    With `halt_on_mismatch` (evidence mode), a call from a host other than the locked one stops the run. The call is
    recorded first, as in the other engines (spike.host_tally). The calls are those of every `turn` and `turn_error`
    line (run_agt012._session_calls). `tools` is the run's tool list (the header's `tools.names`), and a record holds
    a name that is not in it as its SHA-256. `target_turns` maps each item to its target turns (the manifest)."""

    def __init__(self, log, matrix, keys, placeholders, clock_ns, halt_on_crossing=False, halt_on_mismatch=False,
                 tools=(), target_turns=None):
        self.log, self.matrix, self.keys, self.placeholders = log, matrix, keys, tuple(placeholders)
        self.clock_ns, self.halt_on_crossing, self.halt_on_mismatch = clock_ns, halt_on_crossing, halt_on_mismatch
        self.tools, self.known_refs = tuple(tools), tuple(matrix.get("customers") or ())
        self.target_turns = dict(target_turns or {})
        self.status, self.error = "complete", None
        self.facts, self.crossings, self.hosts, self.models = [], 0, Counter(), Counter()
        self.calls, self.mismatches, self.host_not_reported = 0, 0, 0
        self.last_no_result, self.last_score = False, None
        self.arm, self.emulator = None, None

    @property
    def stopped(self):
        return self.status != "complete"

    def use(self, arm, emulator=None):
        """The arm of the next runs, and the emulator whose ledger they read (None for the pipeline arm)."""
        self.arm, self.emulator = arm, emulator

    def _stop(self, item, n, t_ns, **extra):
        self.log.write({"type": "run_stopped", "item": item.id, "arm": self.arm, "run": n, "t_ns": t_ns,
                        "reason": self.status, "error": self.error, **extra})

    def _lines(self, trace, item):
        """The lines of the record, which are the kinds that the scorer reads. Each tool argument and customer
        message is its SHA-256, and each reply is redacted. A reply of a tier 3 item also loses each copy of a turn
        of the item (dat009.redact_turns). Returns (lines, the redaction counts of each reply, the tier 3 turns
        removed from each reply)."""
        lines = dat009.hash_arguments([e for e in trace if e.get("kind") in RECORD_KINDS], self.tools, self.known_refs)
        counts, removed, earlier = [], [], []
        for line in lines:
            if line.get("kind") == "turn" and isinstance(line.get("reply"), str):
                raw = line["reply"]
                line["reply"], c = sec006.redact(raw, self.placeholders, earlier=tuple(earlier))
                line["reply"], n = dat009.redact_turns(line["reply"], item.turns if item.tier == "T3" else ())
                counts.append(c)
                removed.append(n)
                earlier.append(raw)
        return lines, counts, removed

    def one(self, target, item, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        t_ns = self.clock_ns()
        before = self.emulator.ledger.seq if self.emulator is not None else 0
        turn_error, trace = False, []
        try:
            sid = target.open_session(item.customer_ref)
            for text in item.turns:
                try:
                    target.send(sid, text)
                except CapReached:
                    raise               # the cap stops the run, as in the other engines
                except Exception:       # noqa: BLE001 - a send error: no result; the class is not kept here
                    turn_error = True
                    break
            trace = target.get_trace(sid)
            target.close_session(sid)
        except CapReached:
            self.status = "stopped_at_cap"
        except Exception as e:          # noqa: BLE001 - only the class name is kept
            self.status, self.error = "stopped_error", type(e).__name__
        if self.stopped:
            self._stop(item, n, t_ns)
            return None
        ledger = [] if self.emulator is None else dat009.hash_arguments(
            [x for x in mcp_tools.read_ledger(self.emulator.ledger.path, tool=dat009.TOOL) if x["seq"] > before],
            self.tools, self.known_refs)
        lines, counts, removed = self._lines(trace, item)
        _, blocked, finish_reason = _turn_facts(lines)
        calls, unreported = _session_calls(lines)     # the calls of the turn_error lines too
        facts = dat009.score_run(item.customer_ref, self.arm, lines, ledger, self.matrix, self.keys,
                                 turn_error=turn_error, target_turns=self.target_turns.get(item.id, ()))
        self.facts.append({"item": item.id, "tier": item.tier, "direction": item.direction, "arm": self.arm,
                           "run": n, **facts})
        halt, served = False, None
        for c in calls:
            self.hosts[str(c.get("served_host"))] += 1
            self.models[str(c.get("model_served"))] += 1
            self.calls += 1
            if spike.host_tally(self, c.get("host_ok")) and not halt:
                halt, served = True, c.get("served_host")
        for _ in range(unreported):
            spike.host_tally(self, None)
        rec = {"type": "run", "item": item.id, "tier": item.tier, "direction": item.direction,
               "technique": item.technique, "arm": self.arm, "customer_ref": item.customer_ref,
               "target_doc_ids": list(item.target_doc_ids), "run": n, "t_ns": t_ns, "time": runlog.iso(t_ns)}
        if item.tier == "T3":           # PLAYBOOK_DAT_009.md line 23 allows the technique category only, never the text
            rec["request_sha256"] = [runlog.sha256_hex(t) for t in item.turns]
        else:
            rec["request"] = list(item.turns)
        rec.update(calls=calls, trace_lines=lines, ledger_lines=ledger, turn_error=turn_error, blocked=blocked,
                   finish_reason=finish_reason, placeholders=counts, tier3_turns_removed=removed, **facts)
        self.log.write(rec)
        self.last_no_result = facts["outcome"] == "no_result"
        self.last_score = {"value": 1 if facts["crossing"] else 0, "kind": facts["outcome"],
                           "explanation": "; ".join(f"{d['where']}: {', '.join(d['doc_ids'])}"
                                                    for d in facts["crossing_detail"]) or None}
        if facts["crossing"]:
            self.crossings += 1
            self.log.write({"type": "crossing", "item": item.id, "run": n, "arm": self.arm, "tier": item.tier,
                            "direction": item.direction, "crossing_detail": facts["crossing_detail"],
                            "t_ns": self.clock_ns(),
                            "escalated": f"printed at {runlog.iso()}; the engagement lead is the operator"})
            print(f"CROSSING {item.id}/{self.arm}/{n}: {facts['crossing_detail']} (escalated at once; "
                  f"{'the run stops' if self.halt_on_crossing else 'the run continues'})")
            if self.halt_on_crossing:
                self.status = "stopped_on_crossing"
        if halt and not self.stopped:
            self.status = "stopped_host_mismatch"
            self._stop(item, n, t_ns, served_host=served)
        return item.id          # with a non-None value, the Inspect solver marks the sample sent and uses last_score


def matrix_problems(matrix, bank_sha256, documents_sha256, doc_ids, configs, emulator_store_sha256=None):
    """Step 2 (PLAYBOOK_DAT_009.md line 31). Returns the differences between the harness's matrix and the reports of
    each arm of SRD-1 (empty if none). `configs` maps each arm to `{"retrieval": ..., "entitlements": ...}` from
    config_read."""
    out = []
    customers = matrix["customers"]
    domains = sorted({c["domain"] for c in customers.values()})
    accounts = sum(len(c["accounts"]) for c in customers.values())
    for arm, cfg in configs.items():
        ent, ret = cfg.get("entitlements") or {}, cfg.get("retrieval") or {}
        if ent.get("sha256") != bank_sha256:
            out.append(f"{arm}: the bank file that SRD-1 read differs from the one that the harness read")
        if ent.get("documents_sha256") != documents_sha256 or ret.get("documents_sha256") != documents_sha256:
            out.append(f"{arm}: the document store that SRD-1 read differs from the one that the harness read")
        if ent.get("customers") != len(customers) or ent.get("accounts") != accounts or ent.get("domains") != domains:
            out.append(f"{arm}: the customers, accounts or domains of SRD-1 differ from the harness's matrix")
        inv = sorted(d.get("doc_id") for d in ((ret.get("inventory") or {}).get("documents") or []))
        if inv != sorted(doc_ids):
            out.append(f"{arm}: the document inventory of SRD-1 differs from the store that the harness read")
    if emulator_store_sha256 is not None and emulator_store_sha256 != documents_sha256:
        out.append("tool: the tool emulator's store differs from the store that the harness read")
    return out


def declaration_problems(configs, stage, credential):
    """Step 3. Returns the differences between the declaration of each arm and the build of the harness (empty if
    none)."""
    out = []
    for arm, cfg in configs.items():
        ret = cfg.get("retrieval") or {}
        if ret.get("mode") != arm:
            out.append(f"{arm}: SRD-1 declares the retrieval mode {ret.get('mode')}")
        if ret.get("stage") != stage:
            out.append(f"{arm}: SRD-1 declares the stage {ret.get('stage')}; the harness built {stage}")
        if (ret.get("tool_path") or {}).get("credential") != credential:
            out.append(f"{arm}: SRD-1 declares the credential {(ret.get('tool_path') or {}).get('credential')}; the "
                       f"harness built {credential}")
    return out


class _Arms:
    """The builds of SRD-1, one for each arm, and the tool emulator. `stop` stops each part once."""

    def __init__(self):
        self.builds, self.started, self.tool_run, self.stopped = {}, set(), None, False

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        for arm, b in self.builds.items():
            if arm in self.started:
                b.server.stop()
            if b.mcp is not None:
                try:
                    b.mcp.close()
                except Exception:       # noqa: BLE001
                    pass
        if self.tool_run is not None:
            self.tool_run.stop()


def execute(settings, *, run_dir, sign_key, confirmation, items=None, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant="standard", engine="inspect",
            selftest_log=None, selftest_pubkey=None, srd1_bank_file=None, srd1_documents_file=None):
    """Run TP-DAT-009 on SRD-1 minimal. Returns a spike.RunResult. `items` overrides the shipped item file (tests pass
    their own). `selftest_log` names a self-test log or a folder. `selftest_pubkey` names the public key file of
    that log (the default is the setting selftest_pubkey). `base_url`, `env`, `srd1_bank_file` and
    `srd1_documents_file` are for tests. The last two are the data files that SRD-1 is built with. The harness's
    matrix always comes from the data files of `repo`."""
    build, gw, orch_mod, trace_mod = spike._srd1()
    problems = dat009.variant_problems(variant, settings.retrieval_filter, settings.delegation_credential)
    if problems:
        raise ValueError("; ".join(problems))
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    isolation_status = selftest.current(repo=repo, selftest_log=selftest_log,
                                        pubkey=selftest_pubkey or settings.selftest_pubkey)
    gate_problems = selftest.gate_problems(dat009.TP, isolation_status, settings.mode)
    if gate_problems:
        raise RuntimeError("TP-DAT-009 is gated: " + "; ".join(gate_problems))
    if items is None:
        items = dat009.load_items(repo)
    data = Path(repo) / "targets" / "srd1" / "data"
    bank, documents, bank_sha, docs_sha = dat009.load_data(data / "synthetic_bank.yaml",
                                                           data / "synthetic_documents.yaml")
    matrix = dat009.expected_matrix(bank, documents)
    matrix["sources"] = {"bank": {"file": "targets/srd1/data/synthetic_bank.yaml", "sha256": bank_sha},
                         "documents": {"file": "targets/srd1/data/synthetic_documents.yaml", "sha256": docs_sha}}
    keys = dat009.scorer_keys(documents)
    stage, credential = dat009.arm_settings(variant, settings.retrieval_filter, settings.delegation_credential)
    arms_run = tuple(settings.arms)
    base_url = base_url or gw.OPENROUTER_BASE
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    (run_dir / TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / TARGET_DIR / "README.txt").write_text(TARGET_README, encoding="utf-8")
    arms = _Arms()
    log, runner = None, None
    try:
        if "tool" in arms_run:
            arms.tool_run = toolrun.ToolRun(settings, run_dir, egress, tools_file=TOOLS_FILE).start()
        cap = None
        build_files = {k: v for k, v in (("data_file", srd1_bank_file), ("documents_file", srd1_documents_file)) if v}
        for arm in arms_run:
            tr = arms.tool_run if arm == "tool" else None
            b = build.build_srd1(settings, egress, run_dir / TARGET_DIR / f"srd1_trace_{arm}.jsonl", base_url=base_url,
                                 env=env, tools_url=tr.url if tr else None, tool_env=tr.env if tr else None,
                                 retrieval_mode=arm, retrieval_filter=stage, delegation_credential=credential,
                                 cap=cap, **build_files)
            cap = b.gateway.cap             # the second build counts against the same cap
            arms.builds[arm] = b
            egress.add_allow(f"127.0.0.1:{b.server.port}")
        cap.spent_usd = prior_spent_usd
        targets = {}
        timeout = gw.max_call_seconds(egress.timeout) + spike.LOCAL_MARGIN_S
        for arm, b in arms.builds.items():
            b.server.start()
            arms.started.add(arm)
            tr = arms.tool_run if arm == "tool" else None
            targets[arm] = Srd1ChatTarget(b.server.url, egress, timeout=timeout, tools_url=tr.url if tr else None,
                                          ledger_path=tr.emulator.ledger.path if tr else None)
        # Steps 1 to 3 come before the lock. They check the inventory, the matrix and the declared filter placement.
        configs, prompts = {}, {}
        for arm, t in targets.items():
            configs[arm] = {"retrieval": t.read_config("retrieval"), "entitlements": t.read_config("entitlements")}
            p = t.read_config("prompt")
            prompts[arm] = p.get("prompt_sha256")
            configs[arm]["prompt"] = {"prompt_sha256": p.get("prompt_sha256"), "prompt_source": p.get("prompt_source"),
                                      "runtime_templates": p.get("runtime_templates")}
        emu_sha = arms.tool_run.emulator.store_sha256 if arms.tool_run else None
        refused = (matrix_problems(matrix, bank_sha, docs_sha, [d["doc_id"] for d in documents], configs, emu_sha)
                   + declaration_problems(configs, stage, credential))
        if len(set(prompts.values())) != 1:
            refused.append("the two arms of SRD-1 report different system prompts")
        if refused:
            raise RuntimeError("TP-DAT-009: the run is refused before the lock: " + "; ".join(refused))
        first = arms.builds[arms_run[0]]
        tool_b = arms.builds.get("tool")
        defs_sha = tool_b.orchestrator.definitions_sha256 if tool_b else None
        delegation = {"credential": credential, "credential_setting": settings.delegation_credential,
                      "tool": dat009.TOOL if tool_b else None, "definitions_sha256": defs_sha,
                      "principal_rule": "the orchestrator of SRD-1 sets on_behalf_of to the session's customer with "
                                        "the credential user, and sends none with service; a value that the model "
                                        "gave is not used (principal_overridden)" if tool_b else
                                        "the tool arm is not in this run"}
        isolation = {"selftest": {"sha256": _selftest_sha(isolation_status),
                                  "time": (isolation_status or {}).get("time"),
                                  "commit": (isolation_status or {}).get("commit"),
                                  "code_manifest_match": (isolation_status or {}).get("manifest_matches")}}
        emulator = None if not arms.tool_run else {
            "name": "faias-tool-emulator", "version": runlog.__version__, "store_sha256": emu_sha,
            "tools_file": TOOLS_FILE.name, "probe": "none: the matrix check reads the store hash; no probe call"}
        header = dat009_log.build_header(
            settings, allow_hosts=egress.allow_text, items=items, isolation=isolation, entitlement_matrix=matrix,
            document_inventory=configs[arms_run[0]]["retrieval"].get("inventory"), retrieval_config=configs,
            delegation=delegation, emulator=emulator, tools=arms.tool_run.header(defs_sha) if arms.tool_run else None,
            arms=arms_run, scorer_keys=keys, system_prompt_sha256=prompts[arms_run[0]], confirmation=confirmation,
            probes=probes, repo=repo, variant=variant, engine=engine)
        runlog.lock_header(header, clock_ns())
        log = runlog.RunLog(run_dir / "run_log.jsonl")
        log.write(header)
        runlog.wait_past_lock(header, clock_ns)
        runner = _Runner(log, matrix, keys, sec006.find_placeholders([first.orchestrator.system_prompt]), clock_ns,
                         halt_on_crossing=settings.halt_on_crossing, halt_on_mismatch=settings.mode == "evidence",
                         tools=(header.get("tools") or {}).get("names") or (),
                         target_turns={m["id"]: m["target_turns"] for m in header["corpus"]["manifest"]})
        engine_runs = {}
        for arm in arms_run:
            if runner.stopped:
                break
            runner.use(arm, arms.tool_run.emulator if arm == "tool" else None)
            if engine == "inspect":
                engine_runs[arm] = spike._run_inspect(targets[arm], runner, run_dir, settings.runs_per_item, items,
                                                      model_name=f"dat009-{arm}")
            else:
                engine_runs[arm] = spike._run_direct(targets[arm], runner, settings.runs_per_item, items)
        arms.stop()                         # the teardown, before the summary states it
        declared = configs["pipeline"]["retrieval"].get("stage") if "pipeline" in arms_run else None
        summary = _summary(runner, settings, arms, elog, engine, engine_runs, declared, variant, arms_run,
                           data / "synthetic_documents.yaml", docs_sha)
        log.write(summary)
    finally:
        if log is not None:
            log.close()
        arms.stop()
        egress.close()
    sig = runlog.sign_log(run_dir / "run_log.jsonl", sign_key)
    return spike.RunResult(runner.status, run_dir, run_dir / "run_log.jsonl", sig, summary)


def _summary(runner, settings, arms, elog, engine, engine_runs, declared_stage, variant, arms_run, store_path,
             store_sha):
    facts = runner.facts
    measures = dat009.compute_measures(facts, declared_stage=declared_stage, runs_per_item=settings.runs_per_item)
    trace_mod = __import__("targets.srd1.app.trace", fromlist=["verify_chain"])
    per_arm = {}
    for arm, b in arms.builds.items():
        ok, problems = trace_mod.verify_chain(b.trace.path, expected_head=b.trace.head)
        per_arm[arm] = {"trace": {"file": f"{TARGET_DIR}/{b.trace.path.name}",
                                  "sha256": runlog.sha256_hex(b.trace.path.read_bytes()),
                                  "entries": len(b.trace.entries()), "head": b.trace.head, "chain_ok": ok,
                                  "problems": problems},
                        "tool_ledger": arms.tool_run.summary() if arm == "tool" and arms.tool_run else None,
                        "run_engine": engine_runs.get(arm)}
    emu = arms.tool_run.emulator if arms.tool_run else None
    cap = next(iter(arms.builds.values())).gateway.cap
    out = {
        "type": "summary", "label": dat009_log.LABEL, "variant": variant, "status": runner.status,
        "error": runner.error, "runs_recorded": len(facts), "measures_complete": runner.status == "complete",
        "measures": measures, "results_table": dat009.results_table(facts),
        "denial_reconciliation": {k: measures["M5"][k] for k in ("denials_expected", "denials_logged", "reconciled",
                                                                 "runs_unreconciled")},
        "crossings": runner.crossings, "cost_usd": round(cap.spent_usd, 8), "model_calls": cap.calls,
        "cap_usd": settings.cap_usd, "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
        "host_mismatches": runner.mismatches, "host_not_reported": runner.host_not_reported,
        "arms": per_arm,
        "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                   "summary": elog.summary()},
        "run_engine": runlog._engine_facts(engine),
        "teardown": {"srd1_stopped": arms.stopped, "store_written": runlog.sha256_hex(Path(store_path).read_bytes())
                     != store_sha,
                     "emulator_store_written": None if emu is None else
                     runlog.sha256_hex(Path(emu.store_path).read_bytes()) != emu.store_sha256,
                     "note": "SRD-1 is built from files for each run and stopped before this summary; the store is "
                             "a file that nothing writes"},
        "finished": runlog.iso(),
    }
    if variant == "positive_control":
        out["positive_control"] = dat009.control_result(facts, arms=arms_run)
    return out
