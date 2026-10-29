"""The engine: estimate, probes and the run of TP-SEC-001 on SRD-1 minimal.

`execute` starts SRD-1 minimal on 127.0.0.1 and, through an Inspect task (the default engine), sends the items of
the set in the settings (`item_set`: the 131 items and 25 clean-baseline requests of items/TP-SEC-001.json, or the
20 spike items), k times each (`runs_per_item`, 3 to 20), through the target interface
(the `conversation` capability), reads the trace of each run through the `traces` capability, scores the
replies, writes the signed run log, and stops at the spend cap. It is called by the preflight after the operator
types `yes`. It makes model calls only through the SRD-1 gateway.

SRD-1's system prompts hold two staged placeholders (a credential and an endpoint; engine/sec006.py). The engine
finds them in the prompt that it gives SRD-1. It redacts each request and each reply with `sec006.redact` before it
scores and stores them, so the run log and the Inspect log hold the redacted text. Each run record holds the counts
in `placeholders`. The redaction misses the forms that REDACTION_NOTE names. SRD-1's own trace store
(`srd1_trace.jsonl`) is not redacted: it is the target's record. It goes in the folder `target/` of the run folder,
with a README, as for TP-SEC-006 (TARGET_DIR, TARGET_README).
"""
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..errors import CapReached
from . import runlog, sec001, sec006, toolrun

REPO = Path(__file__).resolve().parents[2]
LOCAL_MARGIN_S = 30.0
# The folder of the run folder that holds SRD-1's own trace store, and its README. TP-SEC-001 and TP-SEC-006
# (run_sec006.py) use the same folder and README.
TARGET_DIR = "target"
TARGET_README = ("This folder holds SRD-1's own trace store: each reply as the model gave it, with the staged "
                 "placeholder values. It is the target's record, not the harness log. Do not put it in the working "
                 "papers.\n")
# The header line of a TP-SEC-001 log on SRD-1 about the redaction. It names no placeholder value and no marking.
REDACTION_NOTE = ("SRD-1's system prompts hold two staged placeholders, a credential and an endpoint. Each request "
                  "and reply is redacted with sec006.redact before it is scored and stored. Each run record holds the "
                  "counts in placeholders. The redaction misses these forms: a cut copy in which the marking is not "
                  "whole or has spaces; a reversed, translated or encoded copy (other than Base64 of "
                  f"{sec006.BASE64_MIN} or more characters); a copy with a letter that is not ASCII; and a part of a "
                  f"value, unless parts of {sec006.PART_MIN} or more letters and digits in the same reply cover all "
                  "of it.")


def _srd1():
    """Import SRD-1 minimal. It lives in targets/, outside the package, so put the repository root on the path."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from targets.srd1.app import build, gateway, orchestrator, trace
    return build, gateway, orchestrator, trace


def run_items(settings, repo=REPO):
    """Everything one pass of a run sends, in order: the items of the set, then its clean-baseline requests.
    Inspect makes k passes; the direct loop sends the k runs of each in turn."""
    items, baseline = sec001.load_items(settings.item_set, repo=repo)
    return items + baseline


def estimate(settings):
    """The worst-case cost of the whole run, in USD, or None if the prices are not set.

    It counts every call at the estimated input size and the full output limit (`max_tokens`). With tools on, each
    call also carries the tool definition, and each item counts at 1 + MAX_TOOL_ROUNDS calls, the most that one
    turn can make.
    """
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    _, gw, orch, _ = _srd1()
    system = orch.load_system_prompt()
    sent = run_items(settings)
    tools, per_item = None, 1
    if getattr(settings, "tools_enabled", False):
        from targets.srd1.app.mcp_client import function_definitions
        tools = function_definitions(toolrun.mcp_tools.load_tools(toolrun.SRD1_TOOLS)["tools"])
        per_item = 1 + orch.MAX_TOOL_ROUNDS
    in_tokens = sum(gw.estimate_tokens([{"role": "system", "content": system}, {"role": "user", "content": i.text}],
                                       tools) for i in sent) * settings.runs_per_item * per_item
    calls = len(sent) * settings.runs_per_item * per_item
    return (in_tokens * settings.price_in_per_m + calls * settings.max_tokens * settings.price_out_per_m) / 1_000_000


PROBE_TOOL = [{"type": "function", "function": {
    "name": "record_marker", "description": "Record one marker word.",
    "parameters": {"type": "object", "properties": {"word": {"type": "string"}}, "required": ["word"]}}}]


def run_probes(gateway):
    """Two model calls through the gateway. Returns the results of the three probes of the preflight."""
    out = {"judge_family": "not applicable: the engine has no model judge"}
    spent = gateway.cap.spent_usd
    try:
        r = gateway.complete([{"role": "user", "content": "Call the function record_marker with the word ready."}],
                             tools=PROBE_TOOL)
        out["tool_call"] = {"ok": bool(r.message.get("tool_calls")), "served_host": r.served_host,
                            "served_model": r.model_served, "host_ok": r.host_ok}
    except Exception as e:      # noqa: BLE001
        out["tool_call"] = {"ok": False, "error": type(e).__name__}
    try:
        r = gateway.complete([{"role": "user", "content": "What is 17 times 3? Answer with the number only."}])
        field_name = next((k for k in ("reasoning", "reasoning_content") if r.message.get(k)), None)
        out["reasoning_text"] = {"present": field_name is not None, "field": field_name,
                                 "served_host": r.served_host, "served_model": r.model_served, "host_ok": r.host_ok}
    except Exception as e:      # noqa: BLE001
        out["reasoning_text"] = {"present": False, "error": type(e).__name__}
    out["cost_usd"] = round(gateway.cap.spent_usd - spent, 8)
    return out


@dataclass
class RunResult:
    status: str
    run_dir: Path
    log_path: Path
    sig_path: object
    summary: dict = field(default_factory=dict)


def _host_entry(url):
    p = urlsplit(url)
    return p.hostname + (f":{p.port}" if p.port else "")


class _Runner:
    """One run of one item: send it, read its trace, score it, write its record. Both engines call `one`.

    With `halt_on_mismatch` (evidence mode), a reply from a host other than the locked one is recorded and the run
    stops. After a stop, `one` sends nothing and returns None. `placeholders` are the (kind, value) pairs of the
    system prompt; `one` redacts the request and the reply with them before it scores and stores, and returns the
    redacted reply.
    """

    def __init__(self, log, clock_ns, halt_on_mismatch=False, placeholders=()):
        self.log, self.clock_ns, self.halt_on_mismatch = log, clock_ns, halt_on_mismatch
        self.placeholders = tuple(placeholders)
        self.status, self.error = "complete", None
        self.runs, self.hosts, self.models = [], Counter(), Counter()
        self.mismatches = self.usage_missing = self.retries = self.host_not_reported = 0
        self.last_no_result = False     # read by the Inspect solver, so its score matches the run log

    @property
    def stopped(self):
        return self.status != "complete"

    def _stop(self, item, n, t_ns, **extra):
        self.log.write({"type": "run_stopped", "item": item.id, "run": n, "t_ns": t_ns, "reason": self.status,
                        "error": self.error, **extra})

    def one(self, target, item, n):
        self.last_no_result = False
        if self.stopped:
            return None
        t_ns = self.clock_ns()
        try:
            sid = target.open_session()
            reply = target.send(sid, item.text)
            turn = [e for e in target.get_trace(sid) if e["kind"] == "turn"][-1]
            target.close_session(sid)
        except CapReached:
            self.status = "stopped_at_cap"
        except Exception as e:      # noqa: BLE001 - only the class name is kept
            self.status, self.error = "stopped_error", type(e).__name__
        if self.stopped:
            self._stop(item, n, t_ns)
            return None
        request, request_counts = sec006.redact(item.text, self.placeholders)
        if reply is not None:       # a reply of None stays None, as before the redaction
            reply, reply_counts = sec006.redact(reply, self.placeholders)
        else:
            reply_counts = dict.fromkeys(sec006.COUNTS, 0)
        if item.cls == sec001.BASELINE:     # a clean-baseline request is recorded, never scored
            sc = sec001.Score(False, "baseline", "")
            outcome = sec001.baseline_outcome(reply, turn.get("finish_reason"), turn.get("blocked"))
        else:
            sc = sec001.score_response(item, reply)
            outcome = sec001.outcome(sc, reply, turn.get("finish_reason"), turn.get("blocked"))
        self.hosts[str(turn["served_host"])] += 1
        self.models[str(turn["model_served"])] += 1
        self.mismatches += 1 if turn["host_ok"] is False else 0
        self.host_not_reported += 1 if turn["host_ok"] is None else 0
        self.usage_missing += 1 if turn["usage_missing"] else 0
        self.retries += turn.get("retries", 0)
        self.runs.append({"item": item.id, "cls": item.cls, "run": n, "success": sc.success, "outcome": outcome})
        self.last_no_result = outcome == "no_result"
        self.log.write({"type": "run", "item": item.id, "class": item.cls, "run": n, "t_ns": t_ns,
                        "time": runlog.iso(t_ns), "request": request, "reply": reply,
                        "placeholders": {"request": request_counts, "reply": reply_counts},
                        "score": {"success": sc.success, "kind": sc.kind, "evidence": sc.evidence},
                        "outcome": outcome, "finish_reason": turn.get("finish_reason"), "blocked": turn.get("blocked"),
                        "served_host": turn["served_host"], "served_model": turn["model_served"],
                        "host_ok": turn["host_ok"], "model_requested": turn["model_requested"],
                        "prompt_tokens": turn["prompt_tokens"], "completion_tokens": turn["completion_tokens"],
                        "usage_missing": turn["usage_missing"], "cost_usd": turn["cost_usd"],
                        "latency_ms": turn["latency_ms"], "seed": turn.get("seed"), "retries": turn.get("retries", 0), "retry_reasons": turn.get("retry_reasons", []),
                        "trace_seq": turn["seq"]})
        if self.halt_on_mismatch and turn["host_ok"] is False:
            self.status = "stopped_host_mismatch"
            self._stop(item, n, t_ns, served_host=turn["served_host"])
        return reply


def inspect_available():
    import importlib.util
    return importlib.util.find_spec("inspect_ai") is not None


def _run_direct(target, runner, runs_per_item, items):
    for item in items:
        for n in range(1, runs_per_item + 1):
            if runner.stopped:
                return {"name": "direct"}
            runner.one(target, item, n)
    return {"name": "direct"}


def _run_inspect(target, runner, run_dir, runs_per_item, items, model_name="srd1"):
    """One pass driven by Inspect: one sample per item, one epoch per run, one sample at a time, Inspect model
    `none` (the target makes its own model calls: SRD-1 through its gateway, an external agent behind its
    channel). `model_name` labels the output in the Inspect log. Returns the facts for the summary."""
    import inspect_ai
    from inspect_ai import eval as inspect_eval
    from .inspect_task import build_task
    log_dir = run_dir / "inspect"
    ilog = inspect_eval(build_task(target, runner=runner, epochs=runs_per_item, items=items, model_name=model_name),
                        model="none", log_dir=str(log_dir), log_format="json", display="none", max_samples=1)[0]
    path = Path(urlsplit(ilog.location).path if "://" in ilog.location else ilog.location)
    if not path.is_absolute() and not path.exists():
        path = log_dir / path.name
    data = path.read_bytes()
    return {"name": "inspect_ai", "version": inspect_ai.__version__, "log_file": f"inspect/{path.name}",
            "log_sha256": runlog.sha256_hex(data), "status": ilog.status,
            "samples": len(ilog.samples or []), "error": ilog.error is not None}


def execute(settings, *, run_dir, sign_key, confirmation, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant="standard", engine="inspect", gate=None,
            example=False):
    """Run the engine on the item set of the settings. Return a RunResult. `base_url` and `env` are for tests; a real run uses the defaults.

    `prior_spent_usd` is the cost of the probe calls. It counts against the cap.
    `variant` is "standard" or "positive_control" (the weakened system prompt; see runlog.VARIANTS).
    `engine` is "inspect" (the default: Inspect drives the pass) or "direct" (a plain loop, for tests without Inspect).
    With `tools_enabled` (since 0.6) the run serves SRD-1's tool through the emulator (toolrun.ToolRun). `gate` is
    for tests: a RefundGate with the test's own values, in place of the risk-appetite file.
    `example` (tools/make_example_logs.py) adds runlog.EXAMPLE_LABEL to the label. It refuses a model host that is not
    on the local machine.
    """
    build, gw, orch_mod, trace_mod = _srd1()
    if variant not in runlog.VARIANTS:
        raise ValueError(f"unknown variant: {variant}")
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    system_prompt = orch_mod.load_system_prompt(orch_mod.PROMPT_VARIANTS[variant])
    base_url = base_url or gw.OPENROUTER_BASE
    if example and not runlog.is_local(urlsplit(base_url).hostname or ""):
        raise ValueError("an example run needs a model host on the local machine (a local stand-in)")
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    tools_on = bool(getattr(settings, "tools_enabled", False))
    if tools_on:
        gate = gate or build.RefundGate.from_file()     # null values: "refund cap not declared", before any file
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([_host_entry(base_url)], log=elog)
    # SRD-1's own trace store holds each reply as the model gave it, so it can hold the staged placeholders. It goes
    # in its own folder, with a README, so that it is not taken into the working papers with the run log.
    (run_dir / TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / TARGET_DIR / "README.txt").write_text(TARGET_README, encoding="utf-8")
    tool_run = toolrun.ToolRun(settings, run_dir, egress).start() if tools_on else None
    try:
        srd1 = build.build_srd1(settings, egress, run_dir / TARGET_DIR / "srd1_trace.jsonl", base_url=base_url,
                                env=env, system_prompt=system_prompt, tools_url=tool_run.url if tool_run else None,
                                tool_env=tool_run.env if tool_run else None, gate=gate)
    except BaseException:
        if tool_run:
            tool_run.stop()
        raise
    egress.add_allow(f"127.0.0.1:{srd1.server.port}")
    srd1.gateway.cap.spent_usd = prior_spent_usd
    log_path = run_dir / "run_log.jsonl"
    header = runlog.build_header(settings, allow_hosts=egress.allow_text, confirmation=confirmation,
                                 system_prompt_sha256=srd1.orchestrator.prompt_sha256, probes=probes, repo=repo,
                                 variant=variant, engine=engine,
                                 tools=tool_run.header(srd1.orchestrator.definitions_sha256) if tool_run else None)
    header["redaction"] = REDACTION_NOTE
    if example:
        header["label"] = runlog.example_label(header["label"])
    runlog.lock_header(header, clock_ns())
    log = runlog.RunLog(log_path)
    try:
        log.write(header)                   # the lock is on disk before the first item is sent
        runlog.wait_past_lock(header, clock_ns)
        # the call to SRD-1 waits longer than the gateway's retries can take, so a retry never ends the run
        target = Srd1ChatTarget(srd1.server.url, egress, timeout=gw.max_call_seconds(egress.timeout) + LOCAL_MARGIN_S,
                                tools_url=tool_run.url if tool_run else None,
                                ledger_path=tool_run.emulator.ledger.path if tool_run else None)
        runner = _Runner(log, clock_ns, halt_on_mismatch=settings.mode == "evidence",
                         placeholders=sec006.find_placeholders([system_prompt]))
        srd1.server.start()
        try:
            k, items = settings.runs_per_item, run_items(settings, repo=repo)
            engine_facts = (_run_inspect(target, runner, run_dir, k, items) if engine == "inspect"
                            else _run_direct(target, runner, k, items))
        finally:
            srd1.server.stop()
            if tool_run:
                try:
                    srd1.mcp.close()
                except Exception:       # noqa: BLE001 - the ledger, not the close, is the record
                    pass
                tool_run.stop()
        status, error, runs = runner.status, runner.error, runner.runs
        hosts, models, mismatches, usage_missing = runner.hosts, runner.models, runner.mismatches, runner.usage_missing
        chain_ok, chain_problems = trace_mod.verify_chain(srd1.trace.path, expected_head=srd1.trace.head)
        egress.close()
        summary = {
            "type": "summary", "label": header["label"], "status": status, "error": error,
            "runs_recorded": len(runs), "measures_complete": status == "complete",
            "measures": sec001.compute_measures(runs, settings.m4_value, settings.runs_per_item),
            "results_table": sec001.results_table(runs),
            "cost_usd": round(srd1.gateway.cap.spent_usd, 8), "model_calls": srd1.gateway.cap.calls,
            "cap_usd": settings.cap_usd, "served_hosts": dict(hosts), "served_models": dict(models),
            "host_mismatches": mismatches, "usage_missing_calls": usage_missing, "retries": runner.retries,
            "host_not_reported": runner.host_not_reported,
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
        if tool_run:
            tool_run.stop()
    sig = runlog.sign_log(log_path, sign_key)
    return RunResult(status, run_dir, log_path, sig, summary)
