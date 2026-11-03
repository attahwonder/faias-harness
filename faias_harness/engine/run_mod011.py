"""This module is the engine of TP-MOD-011, Component 1, on SRD-1 minimal. The procedure probes a model for memorised
benchmark items: the model gets the first piece of an item and is asked for the second piece.

The steps, in order:
  1. The settings, the variant and the self-test gate (settings_problems). The settings must name TP-MOD-011 and its
     item set, with tools off and benchmarks of the allow-list (mod011.ALLOW_LIST). The variant must be model_probe.
     A run needs a containment self-test log that passes, and whose code manifest matches the checkout
     (selftest.gate_problems, the rule of TP-DAT-009). In evidence mode the log's signature must verify against a
     pinned public key (selftest_pubkey).
  2. The files (file_problems). The item file of each benchmark (mod011.benchmark_problems), the instrument file
     (mod011.instrument_problems) and the private comparator file: it must be named, outside the repository, present
     and of the SHA-256 that the settings name, and mod011.check_comparators must find no problem with the overlap
     result that the settings name. Each problem refuses the run before SRD-1 is built.
  3. SRD-1 minimal with the model-probe prompt (orchestrator.PROMPT_VARIANTS["model_probe"]), tools off and retrieval
     off. The engine reads the prompt through config_read and checks its SHA-256 and its file.
  4. The instrument check, before the lock (instrument_check). Each of the ten passages of the instrument file gets one
     attempt with the template of the items, the data set name mod011.INSTRUMENT_DATASET and the title of its work as
     the split. score_completion scores each reply. The result goes in the header, not in M1. A cap reached, an error
     of SRD-1 or, in evidence mode, a host other than the locked one refuses the run before the lock.
  5. The header and the lock (mod011_log.build_header). Then the store file: the text of each comparator first.
  6. The two arms: the benchmark arm, then the comparator arm. Each item runs k times. Each attempt opens a new
     session, sends one message (mod011.probe_text) and closes the session. The runner reads the turn line of the
     trace and scores the reply (mod011.score_completion with the reviewer's tau). A send error gives no_result, and
     the run goes on. A run record holds the hashes, the score and the call facts; a benchmark record also holds the
     reply with store_replies full. The store file gets each reply. In evidence mode, a call from a host other than
     the locked one stops the run after its record is written.
  7. The summary and the signature. SRD-1 stops before the summary. The summary holds M1 for each benchmark
     (mod011_log.measures), the outcomes, the store file with its SHA-256, the trace, the egress summary and the cost.

The Inspect sample of an item holds its arm, its ID and the SHA-256 of its probe (_Sample), so the Inspect log holds no
comparator text. The runner gives the Inspect solver the reply of a benchmark record with store_replies full, and else
the reply hash only. SRD-1's own trace (the folder target/ of the run) keeps each message and each reply, so it holds the
comparator text too; its README says to keep it with the store file (TARGET_README).

The preflight reads plan (the attempts of a run) and estimate (its worst-case cost). No live call has used this engine.
The tests use the stand-in host. No run starts until the containment self-test passes.
"""
import json
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from .. import selftest
from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..errors import CapReached
from . import mod011, mod011_log, runlog, spike
from .run_agt012 import _selftest_sha     # one rule in one place, as for TP-DAT-009

REPO = Path(__file__).resolve().parents[2]
TARGET_DIR = spike.TARGET_DIR
# SRD-1's trace keeps each message and each reply, so in this run it also holds the comparator text.
TARGET_README = ("This folder holds SRD-1's own trace store: each probe message and each reply as the model gave it. "
                 "In a TP-MOD-011 run the probes of the comparator arm hold the text of the private comparators, so "
                 "keep this folder with the store file in the controlled evidence store. It is the target's record, "
                 "not the harness log. Do not put it in the working papers or in the repository.\n")
OBJECTIVE = "the remainder of the item, word for word (PLAYBOOK_MOD_011.md line 18)"


@dataclass(frozen=True)
class _Probe:
    """One item of an arm: what the runner sends and what the scorer reads."""
    arm: str
    id: str
    benchmark: str
    prefix: str
    remainder: str
    text: str


@dataclass(frozen=True)
class _Sample:
    """The Inspect view of one item (inspect_task.build_task reads its ID, text, class, activity and objective). The
    text names the arm, the item and the SHA-256 of its probe, so the Inspect log holds no comparator text."""
    id: str
    cls: str
    activity: str
    objective: str
    text: str


def _probes(items, comparators):
    out = {"benchmark": [_Probe("benchmark", i.id, i.benchmark, i.prefix, i.remainder,
                                mod011.probe_text(i.benchmark, i.prefix)) for i in items],
           "comparator": [_Probe("comparator", c["id"], c["benchmark"], c["prefix"], c["remainder"],
                                 mod011.probe_text(c["benchmark"], c["prefix"])) for c in comparators]}
    return out


def _sample(p):
    return _Sample(p.id, p.arm, "model probe", OBJECTIVE,
                   f"TP-MOD-011 {p.arm} item {p.id}; the probe is held by its SHA-256 only: {runlog.sha256_hex(p.text)}")


def settings_problems(settings):
    """Step 1, the settings. Returns the reasons that the settings cannot run TP-MOD-011 (empty if none)."""
    out = []
    if settings.procedure != mod011.TP:
        out.append("the settings do not name the procedure TP-MOD-011")
    if settings.item_set != mod011_log.ITEM_SET:
        out.append(f"the settings name the item set {settings.item_set}; a TP-MOD-011 run names {mod011_log.ITEM_SET}")
    if settings.tools_enabled:
        out.append("tools are on; TP-MOD-011 probes the model alone, with no tool")
    if not settings.benchmarks:
        out.append("the settings name no benchmark")
    out += [f"the benchmark {b!r} is not on the allow-list of TP-MOD-011 ({', '.join(mod011.ALLOW_LIST)}); a new "
            "entry is a code change, with its own licence fetch" for b in settings.benchmarks
            if b not in mod011.ALLOW_LIST]
    return out


def variant_problems(variant):
    if variant != mod011_log.VARIANT:
        return [f"the variant {variant} does not apply to TP-MOD-011: the engine runs SRD-1 minimal with the "
                "model-probe prompt (the variant model_probe) only"]
    return []


def _read_json(path):
    return json.loads(Path(path).read_bytes().decode("utf-8"))


def _inside(path, repo):
    p, r = Path(path).resolve(), Path(repo).resolve()
    return p == r or r in p.parents


def comparator_problems(settings, items, repo=REPO):
    """The private comparator file of the settings: (problems, comparators, file SHA-256, overlap result, overlap
    SHA-256). The file must be named, outside the repository, present and of the SHA-256 of comparator_sha256, and
    mod011.check_comparators must find no problem. An overlap result that is absent or cannot be read counts as no
    overlap result, which check_comparators names."""
    path = settings.comparator_file
    if not path:
        return ["no comparator file is named (comparator_file)"], None, None, None, None
    if _inside(path, repo):
        return ["the comparator file is inside the repository; the comparators are private, so keep the file in the "
                "controlled evidence store"], None, None, None, None
    if not Path(path).is_file():
        return ["the comparator file is absent"], None, None, None, None
    raw = Path(path).read_bytes()
    file_sha = runlog.sha256_hex(raw)
    if file_sha != settings.comparator_sha256:
        return ["the comparator file does not have the SHA-256 that the settings name (comparator_sha256)"], None, \
            None, None, None
    try:
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        return ["the comparator file is not JSON"], None, None, None, None
    overlap, overlap_sha = None, None
    if settings.overlap_result and Path(settings.overlap_result).is_file():
        try:
            overlap_raw = Path(settings.overlap_result).read_bytes()
            overlap, overlap_sha = json.loads(overlap_raw.decode("utf-8")), runlog.sha256_hex(overlap_raw)
        except ValueError:
            overlap = None
    problems = mod011.check_comparators(
        data, items, file_sha256=file_sha, overlap=overlap, training_cutoff=settings.training_cutoff,
        target_family=settings.target_family, evidence=settings.mode == "evidence",
        sme_record_sha256=settings.sme_record_sha256, repo=repo)
    return problems, (data.get("comparators") if isinstance(data, dict) else None), file_sha, overlap, overlap_sha


def load_items(settings, repo=REPO):
    """The items of each benchmark of the settings, and the item file of each as loaded. A ValueError with the problems
    of mod011.benchmark_problems."""
    items, files = [], {}
    for b in settings.benchmarks:
        items += mod011.load_items(b, repo)
        files[b] = _read_json(Path(repo) / mod011.ALLOW_LIST[b]["item_file"])
    return tuple(items), files


def file_problems(settings, repo=REPO):
    """Step 2. The problems of the item files, the instrument file and the comparator file (empty if none)."""
    out = []
    for b in settings.benchmarks:
        if b in mod011.ALLOW_LIST:
            out += mod011.benchmark_problems(b, repo)
    out += mod011.instrument_problems(repo)
    if out:
        return out
    items, _ = load_items(settings, repo)
    return comparator_problems(settings, items, repo)[0]


def start_problems(settings, variant="model_probe", repo=REPO):
    """Steps 1 and 2 without the self-test gate: what the preflight refuses before --start, and what the engine refuses
    before SRD-1 is built."""
    out = settings_problems(settings) + variant_problems(variant)
    return out + (file_problems(settings, repo) if not settings_problems(settings) else [])


def plan(settings, items):
    """The most model calls of a run: one for each instrument passage, and k for each item of each arm. A comparator
    pairs with one item, so an arm has at most as many comparators as items."""
    return mod011.INSTRUMENT_COUNT + 2 * len(items) * settings.runs_per_item


def estimate(settings, repo=REPO):
    """The worst-case cost of a run in USD, or None if the prices are not set. Each call counts at the full output
    limit. Its input is the model-probe prompt and the probe message. The comparator probes are read from the
    comparator file when it has the SHA-256 of the settings; otherwise each counts at the longest message that SRD-1
    takes (MAX_MESSAGE_CHARS). Raises ValueError when an item file or the instrument file is refused."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    _, gw, orch, _ = spike._srd1()
    system = {"role": "system", "content": orch.load_system_prompt(orch.PROMPT_VARIANTS[mod011_log.VARIANT])}

    def tokens(text):
        return gw.estimate_tokens([system, {"role": "user", "content": text}])
    items, _ = load_items(settings, repo)
    comps = None
    if settings.comparator_file and Path(settings.comparator_file).is_file() and not _inside(settings.comparator_file,
                                                                                               repo):
        raw = Path(settings.comparator_file).read_bytes()
        if runlog.sha256_hex(raw) == settings.comparator_sha256:
            try:
                comps = [c for c in json.loads(raw.decode("utf-8"))["comparators"]]
            except (ValueError, KeyError, TypeError):
                comps = None
    comp_tokens = (sum(tokens(mod011.probe_text(c["benchmark"], c["prefix"])) for c in comps) if comps
                   else len(items) * tokens("w" * orch.MAX_MESSAGE_CHARS))
    k = settings.runs_per_item
    in_tokens = (sum(tokens(mod011.instrument_probe_text(p)) for p in mod011.load_instrument(repo))
                 + k * (sum(tokens(mod011.probe_text(i.benchmark, i.prefix)) for i in items) + comp_tokens))
    calls = plan(settings, items)
    return (in_tokens * settings.price_in_per_m + calls * settings.max_tokens * settings.price_out_per_m) / 1_000_000


# ---------- one attempt ----------

def _attempt(target, text):
    """One attempt: a new session, one message, the trace, then close. Returns (the turn line or None, the class name
    of a send error or None). CapReached and an error outside the send are raised."""
    sid = target.open_session()
    error = None
    try:
        target.send(sid, text)
    except CapReached:
        raise
    except Exception as e:      # noqa: BLE001 - a send error: the attempt has no result, and only the class is kept
        error = type(e).__name__
    trace = target.get_trace(sid)
    target.close_session(sid)
    turns = [e for e in trace if e.get("kind") == "turn"]
    return (turns[-1] if turns else None), error


def _scored(turn, error, prefix, remainder, tau):
    """(reply, failed, score) of one attempt. It has no result after a send error, with no turn line, after a provider
    block and with the finish reason error."""
    reply = (turn or {}).get("reply")
    failed = (error is not None or turn is None or bool(turn.get("blocked"))
              or turn.get("finish_reason") == "error")
    return reply, failed, mod011.score_completion(reply, prefix, remainder, tau, failed=failed)


def _call_facts(turn):
    t = turn or {}
    return {"finish_reason": t.get("finish_reason"), "blocked": t.get("blocked"), "served_host": t.get("served_host"),
            "served_model": t.get("model_served"), "host_ok": t.get("host_ok"),
            "model_requested": t.get("model_requested"), "prompt_tokens": t.get("prompt_tokens"),
            "completion_tokens": t.get("completion_tokens"), "usage_missing": t.get("usage_missing"),
            "cost_usd": t.get("cost_usd"), "latency_ms": t.get("latency_ms"), "seed": t.get("seed"),
            "retries": t.get("retries", 0) if turn else 0, "retry_reasons": t.get("retry_reasons", []),
            "trace_seq": t.get("seq")}


class _Runner:
    """The attempts of the run. `one` runs one attempt of one item of the current arm (`use`), writes its record and
    its store line, and keeps its class for M1. Both engines call `one`, with the item or its Inspect view. After a
    stop, `one` sends nothing.

    With `halt_on_mismatch` (evidence mode), a call from a host other than the locked one stops the run. The call is
    recorded first, as in the other engines (spike.host_tally)."""

    def __init__(self, clock_ns, tau, store_mode, halt_on_mismatch=False):
        self.clock_ns, self.tau, self.store_mode, self.halt_on_mismatch = clock_ns, tau, store_mode, halt_on_mismatch
        self.log, self.store, self.arm, self.items = None, None, None, {}
        self.status, self.error = "complete", None
        self.records, self.hosts, self.models = [], Counter(), Counter()
        self.mismatches = self.host_not_reported = self.usage_missing = self.retries = 0
        self.last_no_result, self.last_score = False, None

    @property
    def stopped(self):
        return self.status != "complete"

    def use(self, arm, probes):
        self.arm, self.items = arm, {p.id: p for p in probes}

    def tally(self, turn):
        """Count the call of a turn line. Returns True when the run must stop (spike.host_tally)."""
        if turn is None:
            return False
        self.hosts[str(turn.get("served_host"))] += 1
        self.models[str(turn.get("model_served"))] += 1
        self.usage_missing += 1 if turn.get("usage_missing") else 0
        self.retries += turn.get("retries", 0)
        return spike.host_tally(self, turn.get("host_ok"))

    def _stop(self, p, n, t_ns, **extra):
        self.log.write({"type": "run_stopped", "arm": self.arm, "item": p.id, "attempt": n, "t_ns": t_ns,
                        "reason": self.status, "error": self.error, **extra})

    def one(self, target, item, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        p = self.items[item.id]
        t_ns = self.clock_ns()
        try:
            turn, error = _attempt(target, p.text)
        except CapReached:
            self.status = "stopped_at_cap"
        except Exception as e:      # noqa: BLE001 - only the class name is kept
            self.status, self.error = "stopped_error", type(e).__name__
        if self.stopped:
            self._stop(p, n, t_ns)
            return None
        reply, failed, score = _scored(turn, error, p.prefix, p.remainder, self.tau)
        halt = self.tally(turn)
        reply_sha = runlog.sha256_hex(reply or "")
        rec = {"type": "run", "arm": p.arm, "benchmark": p.benchmark, "item": p.id, "attempt": n, "t_ns": t_ns,
               "time": runlog.iso(t_ns), "request_sha256": runlog.sha256_hex(p.text),
               "prefix_sha256": runlog.sha256_hex(p.prefix), "remainder_sha256": runlog.sha256_hex(p.remainder)}
        if p.arm == "benchmark" and self.store_mode == "full":
            rec["reply"] = reply
        rec.update(reply_sha256=reply_sha, failed=failed, error=error, **score, **_call_facts(turn))
        self.log.write(rec)
        self.store.write({"type": "reply", "arm": p.arm, "item": p.id, "attempt": n, "reply": reply or ""})
        self.records.append({"arm": p.arm, "benchmark": p.benchmark, "item": p.id, "attempt": n,
                             "class": score["class"]})
        self.last_no_result = score["class"] == "no_result"
        self.last_score = {"value": 1 if score["class"] == "exact" else 0, "kind": score["class"],
                           "explanation": None if score["rouge_l"] is None else
                           f"ROUGE-L {score['rouge_l']:.4f}, longest run {score['longest_run']}"}
        if halt:
            self.status = "stopped_host_mismatch"
            self._stop(p, n, t_ns, served_host=turn.get("served_host"))
        return reply if "reply" in rec and reply is not None else f"reply stored as SHA-256 only: {reply_sha}"


def instrument_check(target, runner, passages, repo=REPO):
    """Step 4: one attempt for each passage, before the lock. Returns the instrument block of the header. A cap
    reached, an error outside a send or, in evidence mode, a host other than the locked one raises RuntimeError: the
    run is refused before the lock."""
    rows = []
    for p in passages:
        text = mod011.instrument_probe_text(p)
        try:
            turn, error = _attempt(target, text)
        except CapReached:
            raise RuntimeError(f"{mod011.TP}: the cap was reached in the instrument check; the run is refused before "
                               "the lock") from None
        except Exception as e:      # noqa: BLE001 - only the class name is kept
            raise RuntimeError(f"{mod011.TP}: the instrument check stopped on an error ({type(e).__name__}); the run "
                               "is refused before the lock") from None
        reply, failed, score = _scored(turn, error, p.prefix, p.remainder, runner.tau)
        if runner.tally(turn):
            raise RuntimeError(f"{mod011.TP}: in the instrument check a host other than the locked one served a "
                               f"call ({turn.get('served_host')}); the run is refused before the lock")
        rows.append({"id": p.id, "title": p.title, "author": p.author, "year": p.year,
                     "request_sha256": runlog.sha256_hex(text), "prefix_sha256": runlog.sha256_hex(p.prefix),
                     "remainder_sha256": runlog.sha256_hex(p.remainder), "reply": reply,
                     "reply_sha256": runlog.sha256_hex(reply or ""), "failed": failed, "error": error, **score,
                     **_call_facts(turn)})
    raw = (Path(repo) / mod011.INSTRUMENT_FILE).read_bytes()
    return {"file": mod011.INSTRUMENT_FILE, "sha256": runlog.sha256_hex(raw), "dataset": mod011.INSTRUMENT_DATASET,
            "split": "the title of each work", "passages": rows,
            "summary": mod011.instrument_summary([r["class"] for r in rows]),
            "note": "a rule of this harness, in the role of the positive control of TP-SEC-001: well-known public-domain "
                    "openings probed with the template of the items, before the arms. A high exact rate shows that the "
                    "probe and the scorer work; a low rate points to a refusal or a broken probe. It does not show a "
                    "refusal on benchmark items only. It is a reading, not part of M1"}


class _Store:
    """The store file: one JSON line for each comparator text and each reply, flushed as it is written."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        (self.path.parent / "README.txt").write_text(mod011_log.STORE_README, encoding="utf-8")
        self._f, self.lines = open(self.path, "wb"), 0

    def write(self, entry):
        self._f.write(runlog.canonical(entry).encode("utf-8") + b"\n")
        self._f.flush()
        self.lines += 1

    def close(self):
        self._f.close()


def execute(settings, *, run_dir, sign_key, confirmation, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant="model_probe", engine="inspect",
            selftest_log=None, selftest_pubkey=None):
    """Run TP-MOD-011, Component 1, on SRD-1 minimal. Returns a spike.RunResult. `selftest_log` names a self-test log
    or a folder. `selftest_pubkey` names the public key file of that log (the default is the setting
    selftest_pubkey). `base_url` and `env` are for tests."""
    build, gw, orch_mod, trace_mod = spike._srd1()
    refused = settings_problems(settings) + variant_problems(variant)
    if refused:
        raise ValueError(f"{mod011.TP}: " + "; ".join(refused))
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    isolation_status = selftest.current(repo=repo, selftest_log=selftest_log,
                                        pubkey=selftest_pubkey or settings.selftest_pubkey)
    gate = selftest.gate_problems(mod011.TP, isolation_status, settings.mode)
    if gate:
        raise RuntimeError(f"{mod011.TP} is gated: " + "; ".join(gate))
    refused = file_problems(settings, repo)
    if refused:
        raise ValueError(f"{mod011.TP}: the run is refused: " + "; ".join(refused))
    items, item_files = load_items(settings, repo)
    _, comparators, comp_sha, overlap, overlap_sha = comparator_problems(settings, items, repo)
    passages = mod011.load_instrument(repo)
    probes_by_arm = _probes(items, comparators)
    prompt_file = orch_mod.PROMPT_VARIANTS[mod011_log.VARIANT]
    base_url = base_url or gw.OPENROUTER_BASE
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    (run_dir / TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / TARGET_DIR / "README.txt").write_text(TARGET_README, encoding="utf-8")
    srd1 = build.build_srd1(settings, egress, run_dir / TARGET_DIR / "srd1_trace.jsonl", base_url=base_url, env=env,
                            system_prompt=orch_mod.load_system_prompt(prompt_file), prompt_source=prompt_file.name)
    egress.add_allow(f"127.0.0.1:{srd1.server.port}")
    srd1.gateway.cap.spent_usd = prior_spent_usd
    target = Srd1ChatTarget(srd1.server.url, egress, timeout=gw.max_call_seconds(egress.timeout) + spike.LOCAL_MARGIN_S)
    runner = _Runner(clock_ns, settings.tau_value, settings.store_replies, halt_on_mismatch=settings.mode == "evidence")
    log, store, summary, engine_runs, stopped = None, None, None, {}, False
    srd1.server.start()
    try:
        # Step 3, before the lock: the prompt that SRD-1 states through config_read.
        config = target.read_config("prompt")
        if (runlog.sha256_hex(config.get("prompt") or "") != config.get("prompt_sha256")
                or config.get("prompt_source") != prompt_file.name):
            raise RuntimeError(f"{mod011.TP}: SRD-1 states a prompt that is not the model-probe prompt, or whose "
                               "SHA-256 differs; the run is refused before the lock")
        instrument = instrument_check(target, runner, passages, repo)
        isolation = {"selftest": {"sha256": _selftest_sha(isolation_status),
                                  "time": (isolation_status or {}).get("time"),
                                  "commit": (isolation_status or {}).get("commit"),
                                  "code_manifest_match": (isolation_status or {}).get("manifest_matches")}}
        header = mod011_log.build_header(
            settings, allow_hosts=egress.allow_text, items=items, item_files=item_files, comparators=comparators,
            comparator_sha256=comp_sha, overlap=overlap, overlap_sha256=overlap_sha, instrument=instrument,
            isolation=isolation, system_prompt_sha256=config["prompt_sha256"], prompt_source=prompt_file.name,
            confirmation=confirmation, probes=probes, repo=repo, engine=engine)
        runlog.lock_header(header, clock_ns())
        log = runlog.RunLog(run_dir / "run_log.jsonl")
        log.write(header)
        runlog.wait_past_lock(header, clock_ns)
        store = _Store(run_dir / mod011_log.STORE_FILE)
        for c in comparators:
            store.write({"type": "comparator", "id": c["id"], "prefix": c["prefix"], "remainder": c["remainder"]})
        runner.log, runner.store = log, store
        for arm in mod011_log.ARMS:
            if runner.stopped:
                break
            runner.use(arm, probes_by_arm[arm])
            if engine == "inspect":
                engine_runs[arm] = spike._run_inspect(target, runner, run_dir, settings.runs_per_item,
                                                      [_sample(p) for p in probes_by_arm[arm]],
                                                      model_name=f"mod011-{arm}")
            else:
                engine_runs[arm] = spike._run_direct(target, runner, settings.runs_per_item, probes_by_arm[arm])
        srd1.server.stop()              # the teardown, before the summary
        stopped = True
        store.close()
        summary = _summary(runner, settings, header, srd1, trace_mod, elog, engine_runs, store)
        log.write(summary)
    finally:
        if store is not None:
            store.close()
        if log is not None:
            log.close()
        if not stopped:
            srd1.server.stop()
        egress.close()
    sig = runlog.sign_log(run_dir / "run_log.jsonl", sign_key)
    return spike.RunResult(runner.status, run_dir, run_dir / "run_log.jsonl", sig, summary)


def _summary(runner, settings, header, srd1, trace_mod, elog, engine_runs, store):
    chain_ok, chain_problems = trace_mod.verify_chain(srd1.trace.path, expected_head=srd1.trace.head)
    cap = srd1.gateway.cap
    return {
        "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
        "runs_recorded": len(runner.records), "measures_complete": runner.status == "complete",
        "measures": {"M1": mod011_log.measures(header, runner.records)},
        "outcomes": {a: dict(Counter(r["class"] for r in runner.records if r["arm"] == a)) for a in mod011_log.ARMS},
        "cost_usd": round(cap.spent_usd, 8), "model_calls": cap.calls, "cap_usd": settings.cap_usd,
        "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
        "host_mismatches": runner.mismatches, "host_not_reported": runner.host_not_reported,
        "usage_missing_calls": runner.usage_missing, "retries": runner.retries,
        "calls_over_max_tokens": cap.calls_over_max_tokens,
        "store": {"file": mod011_log.STORE_FILE, "sha256": runlog.sha256_hex(store.path.read_bytes()),
                  "lines": store.lines},
        "trace": {"file": f"{TARGET_DIR}/{srd1.trace.path.name}",
                  "sha256": runlog.sha256_hex(srd1.trace.path.read_bytes()),
                  "entries": len(srd1.trace.entries()), "head": srd1.trace.head, "chain_ok": chain_ok,
                  "problems": chain_problems, "note": mod011_log.TRACE_NOTE},
        "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                   "summary": elog.summary()},
        "run_engine": engine_runs,
        "finished": runlog.iso(),
    }
