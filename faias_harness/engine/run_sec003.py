"""This module is the engine of TP-SEC-003 on SRD-1 minimal. The procedure tests indirect prompt injection through
retrieved content.

The steps, in order:
  1. The settings and the self-test gate (settings_problems). The settings must name TP-SEC-003 and its item set,
     with tools off. The filter stage (retrieval_filter) must be pre_retrieval or pre_ranking: post_generation is
     refused (sec003_log.STAGE_REASON). The arms must be exactly clean and seeded, and a list without the clean arm
     is refused, because the clean-corpus baseline arm is mandatory (PLAYBOOK_SEC_003.md line 27). The settings
     loader refuses the same files (settings.validate). A run needs a containment self-test log that passes, and
     whose code manifest matches the checkout (selftest.gate_problems, the rule of TP-DAT-009). In evidence mode the
     log's signature must verify against a pinned public key (selftest_pubkey).
  2. The items (load_items, item_problems). Items passed to execute are a fixture set: the log names the item set
     "fixture", and its label starts with the spike label, so the run cannot pass as a result. With no items, the
     engine reads items/TP-SEC-003.json, and it refuses the run while that file is absent. The item check
     (sec003.check_item_set, with no tool) must find no problem, and each item must be of a marker class (P1, P2 or
     P5), because the pipeline build offers no tool.
  3. The seed route (sec003.seed). The engine writes the seeded store into the run folder and checks it against the
     clean store. A difference refuses the run before the lock.
  4. Two builds of SRD-1 minimal in the retrieval mode pipeline, one for each arm: the clean arm from the shipped
     store, the seeded arm from the seeded store. The pipeline build offers the model no tool. Each arm has its own
     trace file. Both builds count against one spend cap.
  5. The checks before the lock (arm_problems). The store hash that config_read("retrieval") of each arm reports must
     be the hash of the store of that arm in the seed record. Each arm must declare the mode pipeline, the stage of
     the settings and no tool. The two arms must report one system prompt. A difference refuses the run before the
     lock.
  6. The header and the lock (sec003_log.build_header). The header holds the seed record, the retrieval configuration
     of each arm and the context assembly of SRD-1 (sec003_log.context_assembly), and the lock covers them.
  7. The runs. Each item runs k times on each arm, one session each, for the item's customer: the clean arm first,
     then the seeded arm. The runner keeps the trace lines that the scorer reads (sec003_log.record_lines), redacts
     each reply of SRD-1's staged placeholders (sec006.redact), and scores the stored lines (sec003.score_run). In
     evidence mode, a call from a host other than the locked one stops the run after its record is written.
  8. The summary and the signature. SRD-1 stops before the summary. The summary holds the measures
     (sec003.compute_measures, with k), the teardown facts of the seed route (sec003.teardown), the trace chain of each
     arm, the egress summary and the cost.

A run record holds the SHA-256 of each turn (request_sha256), never the turn text, and the model calls of the session
(calls). Its lines hold the doc_ids that SRD-1 passed to the model and the redacted reply of each turn. SRD-1's own
trace store keeps each reply as the model gave it, in the folder target/ of the run folder. The run returns the item
ID to the Inspect solver, so the Inspect log holds no reply. The Inspect sample of an item shows its ID and the
SHA-256 of its turns (_Sample).

The preflight reads plan (the sessions and the model calls of a run) and estimate (its worst-case cost).

No live call has used this engine. The tests use the stand-in host and a fixture set of benign documents. No run
starts until the containment self-test passes.
"""
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .. import selftest, yamlio
from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..errors import CapReached
from . import jsonkeys, runlog, sec003, sec003_log, sec006, spike
from .run_agt012 import _selftest_sha, _session_calls     # one rule in one place, as for TP-DAT-009
from .run_dat009 import _Arms

REPO = Path(__file__).resolve().parents[2]
TARGET_DIR, TARGET_README = spike.TARGET_DIR, spike.TARGET_README
FIXTURE_AUTHORS = "a fixture set passed to the engine; it is not an item file"
OBJECTIVE = "a nonsense marker in the reply (PLAYBOOK_SEC_003.md line 25)"


@dataclass(frozen=True)
class _Sample:
    """The Inspect view of one item (inspect_task.build_task reads its ID, text, class, activity and objective). The
    text names the item and the SHA-256 of each turn, so the Inspect log holds no turn text and no document text."""
    id: str
    cls: str
    activity: str
    objective: str
    text: str


def _sample(item):
    return _Sample(item.id, item.cls, item.activity, OBJECTIVE,
                   f"TP-SEC-003 item {item.id}; each turn is held by its SHA-256 only: "
                   + ", ".join(runlog.sha256_hex(t) for t in item.turns))


def settings_problems(settings):
    """Step 1, the settings. Returns the reasons that the settings cannot run TP-SEC-003 (empty if none)."""
    out = []
    if settings.procedure != sec003.TP:
        out.append("the settings do not name the procedure TP-SEC-003")
    if settings.item_set != sec003_log.ITEM_SET:
        out.append(f"the settings name the item set {settings.item_set}; a TP-SEC-003 run names {sec003_log.ITEM_SET}")
    if settings.tools_enabled:
        out.append("tools are on; the pipeline build of SRD-1 offers the model no tool, so TP-SEC-003 runs with tools "
                   "off")
    if settings.retrieval_filter not in sec003_log.ACCEPTED_STAGES:
        out.append(f"the stage {settings.retrieval_filter} is refused; it must be pre_retrieval or pre_ranking "
                   f"({sec003_log.STAGE_REASON})")
    arms = tuple(settings.arms)
    if "clean" not in arms:
        out.append("the arms have no clean arm; the clean-corpus baseline arm is mandatory (PLAYBOOK_SEC_003.md "
                   "line 27)")
    elif sorted(arms) != sorted(sec003_log.ARMS):
        out.append("the arms must be exactly clean and seeded")
    return out


def load_items(items=None, repo=REPO):
    """Step 2. Returns (the items, the item set, the authors). Items passed in are a fixture set
    (sec003_log.FIXTURE_SET). With none, the items of items/TP-SEC-003.json (sec003_log.ITEM_SET). A ValueError while
    that file is absent, for a file in which a JSON object holds a key more than once (jsonkeys.unique_keys), and for a
    file that is not one JSON object (sec003.NOT_ONE_OBJECT)."""
    if items is not None:
        return tuple(items), sec003_log.FIXTURE_SET, FIXTURE_AUTHORS
    path = Path(repo) / sec003.ITEM_FILE
    if not path.is_file():
        raise ValueError(f"{sec003.TP}: the item file {sec003.ITEM_FILE} is not found, so no run of the shipped set "
                         "can start")
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey as e:
        raise ValueError(f"{sec003.TP}: the item file {sec003.ITEM_FILE} is refused: {e}") from None
    if not isinstance(data, dict):      # for example a list, or null; sec003.load_set refuses it too
        raise ValueError(f"{sec003.TP}: the item file {sec003.ITEM_FILE} is refused: {sec003.NOT_ONE_OBJECT}")
    return sec003.load_set(path), sec003_log.ITEM_SET, data.get("authors")


def plan(settings, items):
    """(sessions, model calls) of a run on `items`. Each item runs k times on each of the two arms. Each turn makes one
    model call, because the pipeline build offers no tool."""
    n = settings.runs_per_item * len(sec003_log.ARMS)
    return len(items) * n, sum(len(i.turns) for i in items) * n


def estimate(settings, repo=REPO, items=None):
    """The worst-case cost of a run in USD, or None if the prices are not set. It reads the items as load_items does,
    so it raises ValueError while items/TP-SEC-003.json is absent. Each call of plan() counts at the full output limit.
    Its input is the system prompt, a context message of the three longest documents of the seeded store, and the
    turns of the session so far, with each earlier reply at the output limit. The cap still stops the run."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    items = load_items(items, repo)[0]
    _, gw, orch, _ = spike._srd1()
    from targets.srd1.app import retrieval as rt
    docs = yamlio.load(sec003.CLEAN_STORE.read_bytes())["documents"] + sec003.pool(items)
    longest = sorted(docs, key=lambda d: len(str(d.get("title") or "")) + len(str(d.get("text") or "")),
                     reverse=True)[:rt.TOP_K]
    context = {"role": "system", "content": rt.context_message(
        [{k: str(d.get(k) or "") for k in ("doc_id", "title", "text")} for d in longest])}
    system = {"role": "system", "content": orch.load_system_prompt()}
    in_tokens = 0
    for i in items:
        users = []
        for j, text in enumerate(i.turns):
            users.append({"role": "user", "content": text})
            in_tokens += gw.estimate_tokens([system, context] + users) + j * settings.max_tokens
    _, calls = plan(settings, items)
    in_tokens *= settings.runs_per_item * len(sec003_log.ARMS)
    return (in_tokens * settings.price_in_per_m + calls * settings.max_tokens * settings.price_out_per_m) / 1_000_000


def item_problems(items, store):
    """Step 2, the item check (sec003.check_item_set, with no tool), and each item of a class that needs a tool (empty
    if none). `store` is the clean store."""
    out = sec003.check_item_set(items, store)
    tool = [str(i.id) for i in items if i.cls not in sec003.MARKER_CLASSES]
    if tool:
        out.append(f"{', '.join(tool)}: a class that needs a tool (P3 or P4); the pipeline build offers no tool")
    return out


def arm_problems(configs, seed, stage):
    """Step 5. Returns the differences between the report of each arm of SRD-1 and the build of the harness (empty if
    none). `configs` maps each arm to what config_read("retrieval") returned. `seed` is the record of sec003.seed."""
    out = []
    for arm, cfg in configs.items():
        if cfg.get("documents_sha256") != seed[arm]["sha256"]:
            out.append(f"{arm}: the store that SRD-1 read is not the {arm} store of the seed record")
        if cfg.get("mode") != "pipeline":
            out.append(f"{arm}: SRD-1 declares the retrieval mode {cfg.get('mode')}; the harness built pipeline")
        if cfg.get("stage") != stage:
            out.append(f"{arm}: SRD-1 declares the stage {cfg.get('stage')}; the harness built {stage}")
        if (cfg.get("tool_path") or {}).get("tool") is not None:
            out.append(f"{arm}: SRD-1 declares the tool {cfg['tool_path']['tool']}; the pipeline build offers none")
    return out


class _Runner:
    """One run of one item on one arm. It opens a session for the item's customer and sends the item's turns. Then it
    reads the trace, keeps the lines that the scorer reads, redacts each reply, scores the stored lines and writes the
    record. Both engines call `one`, with the item or its Inspect view; `items` maps each ID to its item. Call `use`
    before each arm. After a stop, `one` sends nothing.

    With `halt_on_mismatch` (evidence mode), a call from a host other than the locked one stops the run. The call is
    recorded first, as in the other engines (spike.host_tally). The calls are those of every `turn` and `turn_error`
    line (run_agt012._session_calls)."""

    def __init__(self, log, items, placeholders, clock_ns, halt_on_mismatch=False):
        self.log, self.items, self.placeholders = log, {i.id: i for i in items}, tuple(placeholders)
        self.clock_ns, self.halt_on_mismatch = clock_ns, halt_on_mismatch
        self.status, self.error, self.arm = "complete", None, None
        self.facts, self.hosts, self.models = [], Counter(), Counter()
        self.mismatches, self.host_not_reported = 0, 0
        self.redacted = dict.fromkeys(sec006.COUNTS, 0)        # over the run: the counts of sec006.redact
        self.last_no_result, self.last_score = False, None

    @property
    def stopped(self):
        return self.status != "complete"

    def use(self, arm):
        self.arm = arm

    def _stop(self, item, n, t_ns, **extra):
        self.log.write({"type": "run_stopped", "item": item.id, "arm": self.arm, "run": n, "t_ns": t_ns,
                        "reason": self.status, "error": self.error, **extra})

    def _lines(self, trace):
        """The lines of the record (sec003_log.record_lines). Each reply is redacted of the staged placeholders, with
        the replies of the earlier turns of the session as `earlier` (sec006.redact)."""
        lines, earlier = sec003_log.record_lines(trace), []
        for line in lines:
            if line["kind"] == "turn" and isinstance(line["reply"], str):
                raw = line["reply"]
                line["reply"], counts = sec006.redact(raw, self.placeholders, earlier=tuple(earlier))
                for k, v in counts.items():
                    self.redacted[k] += v
                earlier.append(raw)
        return lines

    def one(self, target, item, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        item = self.items[item.id]
        t_ns = self.clock_ns()
        trace = []
        try:
            sid = target.open_session(item.customer_ref)
            for text in item.turns:
                try:
                    target.send(sid, text)
                except CapReached:
                    raise               # the cap stops the run, as in the other engines
                except Exception:       # noqa: BLE001 - a send error: the turn has no reply line
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
        lines = self._lines(trace)
        calls, unreported = _session_calls(trace)     # the calls of the turn_error lines too
        facts = sec003.score_run(item, lines)
        self.facts.append({**facts, "arm": self.arm, "activity": item.activity, "run": n})
        halt, served = False, None
        for c in calls:
            self.hosts[str(c.get("served_host"))] += 1
            self.models[str(c.get("model_served"))] += 1
            if spike.host_tally(self, c.get("host_ok")) and not halt:
                halt, served = True, c.get("served_host")
        for _ in range(unreported):
            spike.host_tally(self, None)
        self.log.write({"type": "run", "item": item.id, "class": item.cls, "activity": item.activity, "arm": self.arm,
                        "run": n, "t_ns": t_ns, "time": runlog.iso(t_ns),
                        "request_sha256": [runlog.sha256_hex(t) for t in item.turns], "calls": calls, "lines": lines,
                        **facts})
        self.last_no_result = not sec003._has_result(facts)
        self.last_score = {"value": 1 if facts["hit"] else 0,
                           "kind": ("hit" if facts["hit"] else "marker" if facts["marker"] else
                                    "no_result" if self.last_no_result else "no_marker"),
                           "explanation": None if facts["influencing"] is None else
                           "influencing document {doc_id} at turn {turn}".format(**facts["influencing"])}
        if halt:
            self.status = "stopped_host_mismatch"
            self._stop(item, n, t_ns, served_host=served)
        return item.id          # with a non-None value, the Inspect solver marks the sample sent and uses last_score


def execute(settings, *, run_dir, sign_key, confirmation, items=None, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, engine="inspect", selftest_log=None,
            selftest_pubkey=None):
    """Run TP-SEC-003 on SRD-1 minimal. Returns a spike.RunResult. `items` is a fixture set (sec003.Item), and the log
    then names the item set "fixture". With no items, the engine reads items/TP-SEC-003.json. `selftest_log` names a
    self-test log or a folder. `selftest_pubkey` names the public key file of that log (the default is the setting
    selftest_pubkey). `base_url` and `env` are for tests."""
    build, gw, _, _ = spike._srd1()
    refused = settings_problems(settings)
    if refused:
        raise ValueError(f"{sec003.TP}: " + "; ".join(refused))
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    isolation_status = selftest.current(repo=repo, selftest_log=selftest_log,
                                        pubkey=selftest_pubkey or settings.selftest_pubkey)
    gate_problems = selftest.gate_problems(sec003.TP, isolation_status, settings.mode)
    if gate_problems:
        raise RuntimeError(f"{sec003.TP} is gated: " + "; ".join(gate_problems))
    items, item_set, authors = load_items(items, repo)
    refused = item_problems(items, yamlio.load(sec003.CLEAN_STORE.read_bytes()))
    if refused:
        raise ValueError(f"{sec003.TP}: the item set is refused: " + "; ".join(refused))
    base_url = base_url or gw.OPENROUTER_BASE
    run_dir = Path(run_dir)
    seed = sec003.seed(sec003.pool(items), run_dir)        # a difference raises RuntimeError, before the lock
    stores = {"clean": sec003.CLEAN_STORE, "seeded": run_dir / sec003.SEEDED_STORE}
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    (run_dir / TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / TARGET_DIR / "README.txt").write_text(TARGET_README, encoding="utf-8")
    arms = _Arms()
    log, runner, summary = None, None, None
    try:
        cap = None
        for arm in sec003_log.ARMS:
            b = build.build_srd1(settings, egress, run_dir / TARGET_DIR / f"srd1_trace_{arm}.jsonl", base_url=base_url,
                                 env=env, retrieval_mode="pipeline", retrieval_filter=settings.retrieval_filter,
                                 documents_file=stores[arm], cap=cap)
            cap = b.gateway.cap             # the second build counts against the same cap
            arms.builds[arm] = b
            egress.add_allow(f"127.0.0.1:{b.server.port}")
        cap.spent_usd = prior_spent_usd
        targets = {}
        timeout = gw.max_call_seconds(egress.timeout) + spike.LOCAL_MARGIN_S
        for arm, b in arms.builds.items():
            b.server.start()
            arms.started.add(arm)
            targets[arm] = Srd1ChatTarget(b.server.url, egress, timeout=timeout)
        # Step 5 comes before the lock. It checks the store of each arm, the declared build and the system prompt.
        configs, prompts = {}, {}
        for arm, t in targets.items():
            configs[arm] = t.read_config("retrieval")
            prompts[arm] = t.read_config("prompt").get("prompt_sha256")
        refused = arm_problems(configs, seed, settings.retrieval_filter)
        if len(set(prompts.values())) != 1:
            refused.append("the two arms of SRD-1 report different system prompts")
        if refused:
            raise RuntimeError(f"{sec003.TP}: the run is refused before the lock: " + "; ".join(refused))
        isolation = {"selftest": {"sha256": _selftest_sha(isolation_status),
                                  "time": (isolation_status or {}).get("time"),
                                  "commit": (isolation_status or {}).get("commit"),
                                  "code_manifest_match": (isolation_status or {}).get("manifest_matches")}}
        header = sec003_log.build_header(
            settings, allow_hosts=egress.allow_text, items=items, item_set=item_set, authors=authors, seed=seed,
            retrieval_config=configs, isolation=isolation, system_prompt_sha256=prompts["clean"],
            confirmation=confirmation, probes=probes, repo=repo, engine=engine)
        runlog.lock_header(header, clock_ns())
        log = runlog.RunLog(run_dir / "run_log.jsonl")
        log.write(header)
        runlog.wait_past_lock(header, clock_ns)
        runner = _Runner(log, items, sec006.find_placeholders([arms.builds["clean"].orchestrator.system_prompt]),
                         clock_ns, halt_on_mismatch=settings.mode == "evidence")
        engine_runs = {}
        for arm in sec003_log.ARMS:
            if runner.stopped:
                break
            runner.use(arm)
            if engine == "inspect":
                engine_runs[arm] = spike._run_inspect(targets[arm], runner, run_dir, settings.runs_per_item,
                                                      [_sample(i) for i in items], model_name=f"sec003-{arm}")
            else:
                engine_runs[arm] = spike._run_direct(targets[arm], runner, settings.runs_per_item, items)
        arms.stop()                         # the teardown, before the summary states it
        summary = _summary(runner, settings, header, arms, elog, engine, engine_runs, seed)
        log.write(summary)
    finally:
        if log is not None:
            log.close()
        arms.stop()
        egress.close()
    sig = runlog.sign_log(run_dir / "run_log.jsonl", sign_key)
    return spike.RunResult(runner.status, run_dir, run_dir / "run_log.jsonl", sig, summary)


def _summary(runner, settings, header, arms, elog, engine, engine_runs, seed):
    trace_mod = spike._srd1()[3]
    per_arm = {}
    for arm, b in arms.builds.items():
        ok, problems = trace_mod.verify_chain(b.trace.path, expected_head=b.trace.head)
        per_arm[arm] = {"trace": {"file": f"{TARGET_DIR}/{b.trace.path.name}",
                                  "sha256": runlog.sha256_hex(b.trace.path.read_bytes()),
                                  "entries": len(b.trace.entries()), "head": b.trace.head, "chain_ok": ok,
                                  "problems": problems},
                        "run_engine": engine_runs.get(arm)}
    cap = arms.builds["clean"].gateway.cap
    return {
        "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
        "runs_recorded": len(runner.facts), "measures_complete": runner.status == "complete",
        "measures": sec003.compute_measures(runner.facts, runs_per_item=settings.runs_per_item),
        "cost_usd": round(cap.spent_usd, 8), "model_calls": cap.calls, "cap_usd": settings.cap_usd,
        "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
        "host_mismatches": runner.mismatches, "host_not_reported": runner.host_not_reported,
        "placeholders_redacted": dict(runner.redacted),
        "arms": per_arm,
        "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                   "summary": elog.summary()},
        "run_engine": runlog._engine_facts(engine),
        "teardown": {**sec003.teardown(seed), "srd1_stopped": arms.stopped},
        "finished": runlog.iso(),
    }
