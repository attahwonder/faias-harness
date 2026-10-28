"""The external run: TP-SEC-001 against a deployed staging agent, through a channel adapter (version 0.5).

The SRD-1 engine (spike.py) builds SRD-1 minimal in the same process and reads the model, the host, the tokens and
the cost from SRD-1's own gateway. A deployed agent offers none of that to an outside client. This engine is a
client of the agent's channel: it sends each item through the adapter that the profile names, reads the reply,
scores it with the same scorer, and computes M1 to M4 with the same formulas. What it records about each call is
what the channel showed (`last_exchange` of the adapter): the host that answered, the HTTP statuses, the time, and
any block, finish signal, model name or token use that the channel's response names.

The budget is a call budget (adapters/base.py CallBudget): floor(cap_usd / cost_per_call_usd), or max_calls if
lower. The cost per call is the operator's estimate. Every attempt that may reach the agent is charged, and the run
stops before a call that would pass the budget. The run also waits `min_interval_ms` between calls, and stops after
`max_consecutive_errors` channel errors in a row, or at the first error that names an authentication or
configuration fault.

A channel error that does not stop the run is a run with the outcome no_result. The summary counts these errors by
kind (`channel_errors`) and by class (`channel_errors_by_class`: the HTTP status, or the kind where the error has
no status), so that a channel that rejects attack items is visible. The rates do not change.

The run log is schema 5 (schema 3 before version 0.6; runlog.build_external_header). It is signed like every run log.
"""
import hashlib
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters import registry
from ..adapters.base import CallBudget, ChannelError, host_entry
from ..egress import EgressClient, EgressLog
from ..errors import CapReached, EgressRefused
from . import runlog, sec001
from .spike import inspect_available, run_items

REPO = Path(__file__).resolve().parents[2]
FATAL_KINDS = ("auth", "config", "key_missing")
PROBE_MESSAGE = "Hello. What can you help me with today?"


def calls_per_session(profile):
    """The charged calls of one session before retries: 2 for Direct Line with the start event, else 1."""
    if profile is not None and profile.adapter == "directline" and (profile.adapter_options or {}).get("start_event"):
        return 2
    return 1


def estimate(settings, repo=REPO, profile=None):
    """The calls of the whole run (one session per run of each item and baseline request; retries are extra), and
    their estimated cost from the operator's cost per call."""
    calls = len(run_items(settings, repo=repo)) * settings.runs_per_item * calls_per_session(profile)
    return {"calls": calls, "cost_usd": round(calls * settings.cost_per_call_usd, 6),
            "call_budget": settings.call_budget}


class Pacer:
    """Waits so that two calls are at least `min_interval_ms` apart."""

    def __init__(self, min_interval_ms, sleep=time.sleep, clock=time.monotonic):
        self.gap = min_interval_ms / 1000
        self.sleep, self.clock = sleep, clock
        self.last = None

    def wait(self):
        if self.last is not None and self.gap > 0:
            left = self.gap - (self.clock() - self.last)
            if left > 0:
                self.sleep(left)
        self.last = self.clock()


def make_egress(profile, log=None):
    return EgressClient(registry.allow_hosts(profile), log=log)


def make_target(profile, settings, egress, budget, *, env=None, sleep=time.sleep, clock=time.monotonic):
    """The adapter for the profile, with the budget and the pacing on every attempt that may reach the agent."""
    pacer = Pacer(settings.min_interval_ms, sleep=sleep, clock=clock)

    def before_send():
        budget.charge()          # raises CapReached before a call that would pass the budget; nothing is sent
        pacer.wait()

    return registry.build(profile, egress, reply_timeout_s=settings.reply_timeout_s, before_send=before_send,
                          env=env, sleep=sleep, clock=clock)


def channel_probe(target):
    """One benign message through the channel, in its own session. Shows that the channel answers before a run
    spends its budget. The probe call counts against the budget."""
    out = {"message": PROBE_MESSAGE}
    try:
        sid = target.open_session()
        try:
            reply = target.send(sid, PROBE_MESSAGE)
        finally:
            target.close_session(sid)
        ex = target.last_exchange
        out.update({"ok": bool((reply or "").strip()), "reply_start": (reply or "")[:200],
                    "channel_host": ex.get("channel_host"), "statuses": ex.get("statuses"),
                    "latency_ms": ex.get("latency_ms"), "served_model": ex.get("served_model"),
                    "finish_reason": ex.get("finish_reason"), "blocked": ex.get("blocked")})
    except ChannelError as e:
        out.update({"ok": False, "error": e.kind, "status": e.status, "detail": str(e)})
    except CapReached:
        out.update({"ok": False, "error": "CapReached"})
    except EgressRefused as e:
        out.update({"ok": False, "error": "EgressRefused", "detail": str(e)})
    except Exception as e:          # noqa: BLE001 - only the class name is kept
        out.update({"ok": False, "error": type(e).__name__})
    return out


@dataclass
class ExternalResult:
    status: str
    run_dir: Path
    log_path: Path
    sig_path: object
    summary: dict = field(default_factory=dict)


class _ExternalRunner:
    """One run of one item through the adapter. Both engines (Inspect and the direct loop) call `one`."""

    def __init__(self, log, clock_ns, budget, settings):
        self.log, self.clock_ns, self.budget, self.settings = log, clock_ns, budget, settings
        self.status, self.error = "complete", None
        self.runs, self.hosts, self.models, self.channel_errors = [], Counter(), Counter(), Counter()
        self.channel_error_classes = Counter()      # by HTTP status, or by kind where the error has no status
        self.retries = self.consecutive_errors = 0
        self.last_no_result = False     # read by the Inspect solver, so its score matches the run log

    @property
    def stopped(self):
        return self.status != "complete"

    def _stop(self, item, n, t_ns, **extra):
        self.log.write({"type": "run_stopped", "item": item.id, "run": n, "t_ns": t_ns, "reason": self.status,
                        "error": self.error, **extra})

    def _record(self, item, n, t_ns, reply, ex, calls, channel_error=None):
        if channel_error is not None:      # no reply: not scored as a defence, left out of the rates
            sc = sec001.Score(False, "baseline", "") if item.cls == sec001.BASELINE else sec001.score_response(item, "")
            outcome = "no_result"
        elif item.cls == sec001.BASELINE:
            sc = sec001.Score(False, "baseline", "")
            outcome = sec001.baseline_outcome(reply, ex.get("finish_reason"), ex.get("blocked"))
        else:
            sc = sec001.score_response(item, reply)
            outcome = sec001.outcome(sc, reply, ex.get("finish_reason"), ex.get("blocked"))
        self.hosts[str(ex.get("channel_host"))] += 1
        self.models[str(ex.get("served_model"))] += 1
        self.retries += ex.get("retries", 0)
        self.runs.append({"item": item.id, "cls": item.cls, "run": n, "success": sc.success, "outcome": outcome})
        self.last_no_result = outcome == "no_result"
        channel = {k: ex[k] for k in ("statuses", "ids", "activity_types", "polls", "task_state", "task_reads", "status")
                   if k in ex}
        self.log.write({"type": "run", "item": item.id, "class": item.cls, "run": n, "t_ns": t_ns,
                        "time": runlog.iso(t_ns), "request": item.text, "reply": reply,
                        "score": {"success": sc.success, "kind": sc.kind, "evidence": sc.evidence},
                        "outcome": outcome, "finish_reason": ex.get("finish_reason"), "blocked": ex.get("blocked"),
                        "block_type": ex.get("block_type"), "served_host": ex.get("channel_host"),
                        "served_model": ex.get("served_model"), "usage": ex.get("usage"),
                        "calls": calls, "cost_usd_estimate": round(calls * self.settings.cost_per_call_usd, 8),
                        "latency_ms": ex.get("latency_ms"), "retries": ex.get("retries", 0),
                        "retry_reasons": ex.get("retry_reasons", []), "channel": channel,
                        "channel_error": channel_error})

    def one(self, target, item, n):
        self.last_no_result = False
        if self.stopped:
            return None
        t_ns = self.clock_ns()
        calls_before = self.budget.calls
        reply, channel_error = None, None
        try:
            sid = target.open_session()
            try:
                reply = target.send(sid, item.text)
            finally:
                try:
                    target.close_session(sid)
                except Exception:          # noqa: BLE001 - a failed close does not change the reply
                    pass
        except CapReached:
            self.status = "stopped_at_cap"
        except ChannelError as e:
            if e.kind in FATAL_KINDS:
                self.status, self.error = "stopped_channel_error", e.kind
            else:
                channel_error = {"kind": e.kind, "status": e.status}
        except EgressRefused:
            self.status, self.error = "stopped_error", "EgressRefused"
        except Exception as e:            # noqa: BLE001 - only the class name is kept
            self.status, self.error = "stopped_error", type(e).__name__
        if self.stopped:
            self._stop(item, n, t_ns)
            return None
        ex = dict(getattr(target, "last_exchange", {}) or {})
        calls = self.budget.calls - calls_before
        if channel_error is not None:
            self.channel_errors[channel_error["kind"]] += 1
            st = channel_error["status"]
            self.channel_error_classes[f"http_{st}" if st is not None else channel_error["kind"]] += 1
            self.consecutive_errors += 1
            self._record(item, n, t_ns, "", ex, calls, channel_error)
            if self.consecutive_errors >= self.settings.max_consecutive_errors:
                self.status, self.error = "stopped_errors", channel_error["kind"]
                self._stop(item, n, t_ns, consecutive_errors=self.consecutive_errors)
            return ""
        self.consecutive_errors = 0
        self._record(item, n, t_ns, reply or "", ex, calls)
        return reply or ""


def _run_direct(target, runner, runs_per_item, items):
    for item in items:
        for n in range(1, runs_per_item + 1):
            if runner.stopped:
                return {"name": "direct"}
            runner.one(target, item, n)
    return {"name": "direct"}


def target_facts(profile):
    """What the header says about the target. The profile itself may hold firm information: only its hash is kept."""
    entry = registry.ADAPTERS[profile.adapter]
    return {"adapter": profile.adapter, "channel": entry.channel, "observes": entry.observes,
            "endpoint_host": host_entry(profile.endpoint), "profile_name": profile.name,
            "profile_sha256": hashlib.sha256(Path(profile.path).read_bytes()).hexdigest(),
            "capabilities_used": ["conversation"]}


def execute(settings, profile, *, run_dir, sign_key, confirmation, probes=None, engine="inspect",
            clock_ns=time.time_ns, repo=REPO, prior_calls=0, env=None, sleep=time.sleep, clock=time.monotonic):
    """Run TP-SEC-001 against the external target of the profile. Return an ExternalResult.

    `prior_calls` is the number of probe calls already made; they count against the budget.
    `env`, `sleep` and `clock` are for tests; a real run uses the defaults.
    """
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    problems = registry.problems(profile)
    if problems:
        raise ValueError("the profile cannot drive an external run: " + "; ".join(problems))
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = make_egress(profile, log=elog)
    budget = CallBudget(settings.cap_usd, settings.cost_per_call_usd, settings.max_calls, calls=prior_calls)
    target = make_target(profile, settings, egress, budget, env=env, sleep=sleep, clock=clock)
    header = runlog.build_external_header(settings, target=target_facts(profile), allow_hosts=egress.allow_text,
                                          confirmation=confirmation, probes=probes, repo=repo, engine=engine)
    runlog.lock_header(header, clock_ns())
    log_path = run_dir / "run_log.jsonl"
    log = runlog.RunLog(log_path)
    try:
        log.write(header)                   # the lock is on disk before the first item is sent
        runner = _ExternalRunner(log, clock_ns, budget, settings)
        k, items = settings.runs_per_item, run_items(settings, repo=repo)
        if engine == "inspect":
            from .spike import _run_inspect
            engine_facts = _run_inspect(target, runner, run_dir, k, items, model_name=profile.adapter)
        else:
            engine_facts = _run_direct(target, runner, k, items)
        egress.close()          # also closed in `finally` if the run raises; closing twice is harmless
        runs = runner.runs
        summary = {
            "type": "summary", "label": sec001.LABELS[settings.item_set], "status": runner.status,
            "error": runner.error, "runs_recorded": len(runs), "measures_complete": runner.status == "complete",
            "measures": sec001.compute_measures(runs, settings.m4_value, settings.runs_per_item),
            "results_table": sec001.results_table(runs),
            "calls": budget.calls, "probe_calls": prior_calls, "call_budget": budget.call_budget,
            "cost_usd_estimate": budget.spent_usd, "cap_usd": settings.cap_usd,
            "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
            "channel_errors": dict(runner.channel_errors),
            "channel_errors_by_class": dict(runner.channel_error_classes), "retries": runner.retries,
            "outcomes": dict(Counter(r["outcome"] for r in runs)),
            "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                       "summary": elog.summary()},
            "run_engine": engine_facts,
            "finished": runlog.iso(),
        }
        log.write(summary)
    finally:
        egress.close()
        log.close()
    sig = runlog.sign_log(log_path, sign_key)
    return ExternalResult(runner.status, run_dir, log_path, sig, summary)
