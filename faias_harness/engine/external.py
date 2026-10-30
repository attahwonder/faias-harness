"""The external run: TP-SEC-001 (and, since 0.8, TP-SEC-002) against a deployed staging agent, through a channel
adapter (version 0.5).

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

TP-SEC-002 (version 0.8, `procedure: TP-SEC-002` in the external settings) runs through the same channel, budget,
pacing and stops. It uses the item file, the pool, the encoders, the scorer and the measures of the SRD-1 engine
(run_sec002.py), in its three passes: the sequences of Tracks A and B, the cells of Track C(ii), and the isolation arm.
Each run of a sequence is one adapter session, and each turn has its own run record. A channel error that does not stop
the run ends the session: the record of that turn has the outcome no_result, and the later turns are not sent. The
channel probe is a recall test in two sessions, with a control session (session_probe), and the run needs
`session_state: observed`. The baseline is a schema 5 log of an external TP-SEC-001 run, matched on five declared
fields (sec002_log.EXTERNAL_MATCH_FIELDS). The log is schema 9 with the variant external and the blocks target, model,
budget and pacing of schema 5. The encoders, the scorer, the measures, the log, the lock and the signature run on the
operator's machine. Only the text of each turn and each cell leaves, through the channel of the profile and the egress
allow-list. The run records the SHA-256 of each request, never its text. The redaction of SRD-1's placeholders does not
run, because the target is not SRD-1.
"""
import hashlib
import re
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters import registry
from ..adapters.base import CallBudget, ChannelError, host_entry
from ..egress import EgressClient, EgressLog
from ..errors import CapReached, EgressRefused
from . import run_sec002, runlog, sec001, sec002, sec002_log
from .spike import inspect_available, run_items

REPO = Path(__file__).resolve().parents[2]
FATAL_KINDS = ("auth", "config", "key_missing")
PROBE_MESSAGE = "Hello. What can you help me with today?"
# The probe of session state of a TP-SEC-002 run is a recall test (session_probe). Its two words are plain common
# nouns, chosen at random from this fixed list with `secrets`. No word is code-like or holds a digit, so the request is
# benign. An agent that keeps no session state cannot know the pair.
RECALL_WORDS = ("badger", "basket", "bicycle", "blanket", "bucket", "candle", "carpet", "cherry", "cushion", "feather",
                "hammer", "helmet", "kettle", "kitten", "ladder", "lemon", "meadow", "mirror", "pebble", "pencil",
                "pillow", "pumpkin", "rabbit", "ribbon", "saddle", "teapot", "tunnel", "violin", "walnut", "zebra")
REMEMBER_MESSAGE = ("Hello. Please keep two words in mind for this conversation: {0} and {1}. I will ask you for them "
                    "later. For now, reply with OK.")
RECALL_MESSAGE = ("Which two words did I ask you to keep in mind earlier in this conversation? Reply with the two "
                  "words only.")
CROSS_SESSION = ("the reply in the control session (a fresh session, opened after session A) holds both words too, so "
                 "state crosses sessions: a session of the run does not start fresh")
# What a probe call records of the exchange of the adapter (last_exchange).
PROBE_CALL_FIELDS = ("channel_host", "statuses", "blocked", "block_type", "finish_reason", "latency_ms", "served_model")


def calls_per_session(profile):
    """The charged calls of one session before retries: 2 for Direct Line with the start event, else 1."""
    if profile is not None and profile.adapter == "directline" and (profile.adapter_options or {}).get("start_event"):
        return 2
    return 1


def probe_calls(settings, profile=None):
    """The charged calls of the channel probe before retries. For TP-SEC-001 it is the probe message, in one session.
    For TP-SEC-002 it is the recall test of session_probe: 3 turns in 2 sessions. The start event of Direct Line adds
    one call for each session, so the probe is then 2 or 5 calls."""
    sessions, turns = (2, 3) if settings.procedure == sec002.TP else (1, 1)
    return turns + sessions * (calls_per_session(profile) - 1)


def estimate(settings, repo=REPO, profile=None):
    """The calls of the whole run (one session per run of each item and baseline request; retries are extra), and
    their estimated cost from the operator's cost per call. `probe_calls`: the calls of the channel probe.

    For TP-SEC-002, a run of a sequence is one session with a call for each turn; a cell and an isolation item are one
    session of one turn. The isolation arm counts at its worst case, as in run_sec002.planned_calls. The start event of
    Direct Line is one more call for each session, not for each turn."""
    extra = calls_per_session(profile) - 1
    if settings.procedure == sec002.TP:
        seqs, cells, _ = sec002.load_items(repo)
        alone = len(cells) + len(run_sec002.isolation_plan(seqs))         # sessions of one turn each
        sessions = len(seqs) + alone
        calls = (sum(len(s.turns) for s in seqs) + alone + sessions * extra) * settings.runs_per_item
    else:
        calls = len(run_items(settings, repo=repo)) * settings.runs_per_item * (1 + extra)
    return {"calls": calls, "cost_usd": round(calls * settings.cost_per_call_usd, 6),
            "call_budget": settings.call_budget, "probe_calls": probe_calls(settings, profile)}


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


def recall_pair():
    """Two different words of RECALL_WORDS, chosen at random with `secrets`."""
    first = secrets.choice(RECALL_WORDS)
    return first, secrets.choice([w for w in RECALL_WORDS if w != first])


def _holds(reply, word):
    """True when `reply` holds `word` as a whole word, in any case."""
    return re.search(r"\b" + re.escape(word) + r"\b", reply or "", re.I) is not None


def _error(e):
    """What a probe call records of a failed call: the kind and HTTP status of a channel error, else the class name."""
    if isinstance(e, ChannelError):
        return {"error": e.kind, "status": e.status, "detail": str(e)}
    if isinstance(e, EgressRefused):
        return {"error": "EgressRefused", "detail": str(e)}
    return {"error": "CapReached" if isinstance(e, CapReached) else type(e).__name__}


def _exchange(target):
    """PROBE_CALL_FIELDS of the last exchange of the adapter, and for json-http whether the response named the
    session (session_path_found)."""
    ex = dict(getattr(target, "last_exchange", {}) or {})
    out = {k: ex.get(k) for k in PROBE_CALL_FIELDS}
    if "session_path_found" in ex:
        out["session_path_found"] = ex["session_path_found"]
    return out


def _probe_session(target, session, texts):
    """Send `texts` in order, in one new adapter session. Return a record of each call: the session, the turn, the
    exchange and the reply (or the error). A failed call ends the session. A failed open_session is recorded at turn 1
    with step open_session."""
    try:
        sid = target.open_session()
    except Exception as e:          # noqa: BLE001 - recorded by kind or class name only
        return [{"session": session, "turn": 1, "step": "open_session", **_error(e)}]
    calls = []
    try:
        for turn, text in enumerate(texts, 1):
            rec = {"session": session, "turn": turn}
            calls.append(rec)
            try:
                reply = target.send(sid, text) or ""
            except Exception as e:  # noqa: BLE001 - recorded by kind or class name only
                rec.update(_exchange(target), **_error(e))
                break
            rec.update(_exchange(target), reply=reply)
    finally:
        try:
            target.close_session(sid)
        except Exception:           # noqa: BLE001 - a failed close does not change the replies
            pass
    return calls


def _recall_outcome(a, b, words):
    """(session_state, reason) of the recall test from the call records of session A and of the control session B.
    "observed" only when the reply to turn 2 of session A holds both words and the reply in session B does not hold
    both. Every "not observed" has a reason."""
    first = a[0]
    if "error" in first:
        return "not observed", (f"session A, turn 1 failed ({first['error']}"
                                + (f", HTTP {first['status']}" if first.get("status") else "") + "): no word was given")
    second = a[1] if len(a) > 1 else {}
    if "error" in second:
        return "not observed", (f"session A, turn 2 failed ({second['error']}"
                                + (f", HTTP {second['status']}" if second.get("status") else "") + ")")
    if len(second.get("holds") or []) < len(words):
        held = ", ".join(second.get("holds") or []) or "none"
        if second.get("blocked"):
            why = f"was blocked (block type {second.get('block_type')})"
        elif second.get("reply_empty"):
            why = f"is empty (finish reason {second.get('finish_reason')})"
        else:
            why = f"does not hold both words (it holds: {held})"
        reason = f"the reply to turn 2 of session A {why}; a target that keeps no session state cannot know the words"
        if first.get("session_path_found") is False:
            reason += ("; json-http: session_path is set, and the first response did not hold it, so the later turns "
                       "sent no session ID")
        return "not observed", reason
    control = b[0] if b else {"error": "not sent"}
    if "error" in control:
        return "not observed", (f"the control session failed ({control['error']}): the probe cannot tell the state of one "
                                "session from state that crosses sessions")
    if len(control.get("holds") or []) == len(words):
        return "not observed", CROSS_SESSION
    return "observed", None


def session_probe(target, pair=None):
    """The probe of session state of a TP-SEC-002 run: a recall test with a control session, 3 calls before retries.
    Session A, turn 1 gives the agent two words of RECALL_WORDS (REMEMBER_MESSAGE); turn 2 asks for them back
    (RECALL_MESSAGE). Session B, a fresh session opened after session A, asks the same question as its first turn: the
    control. `session_state` is "observed" only when the reply to turn 2 of session A holds both words (whole words, in
    any case) and the reply in session B does not hold both. When session B holds both, it is "not observed", because
    state crosses sessions. Every "not observed" has a `reason`. Each call records its HTTP statuses, block, block
    type, finish reason and time, and which words its reply holds. `history_held_by` names who holds the history of a
    session, from the adapter: the service, or the harness (generic-http resends the history with each turn). Session
    B is sent only when both turns of session A got a reply. Each call counts against the budget. The limit: a target
    that keeps state only in a way that the recall question does not reach shows as not observed."""
    words = list(pair or recall_pair())
    remember = REMEMBER_MESSAGE.format(*words)
    a = _probe_session(target, "A", [remember, RECALL_MESSAGE])
    b = _probe_session(target, "B", [RECALL_MESSAGE]) if len(a) == 2 and not any("error" in c for c in a) else []
    for c in a + b:
        reply = c.pop("reply", None)
        if reply is not None:
            c.update(reply_start=reply[:200], reply_empty=not reply.strip(), holds=[w for w in words if _holds(reply, w)])
    state, reason = _recall_outcome(a, b, words)
    out = {"words": words, "messages": {"remember": remember, "recall": RECALL_MESSAGE},
           "history_held_by": getattr(target, "history_held_by", None), "calls": a + b, "session_state": state}
    if "session_path_found" in a[0]:
        out["session_path"] = {"set": True, "in_first_response": a[0]["session_path_found"]}
    if reason:
        out["reason"] = reason
    return out


def channel_probe(target, session_check=False, pair=None):
    """One benign message through the channel, in its own session. Shows that the channel answers before a run
    spends its budget. The probe call counts against the budget. With `session_check` (TP-SEC-002), the probe is the
    recall test of session_probe, and its first call (session A, turn 1) is the benign message: the result holds
    `session_state` and `session_check`. `pair`: the two words, for tests; a real probe draws them."""
    if session_check:
        check = session_probe(target, pair)
        first = check["calls"][0]
        out = {"message": check["messages"]["remember"]}
        if "error" in first:
            out.update(ok=False, **{k: first[k] for k in ("error", "status", "detail") if k in first})
        else:
            out.update(ok=not first["reply_empty"], reply_start=first["reply_start"],
                       **{k: first.get(k) for k in PROBE_CALL_FIELDS if k not in ("block_type",)})
        out.update(session_state=check["session_state"], session_check=check)
        return out
    out = {"message": PROBE_MESSAGE}
    try:
        sid = target.open_session()
        try:
            reply = target.send(sid, PROBE_MESSAGE)
            ex = dict(target.last_exchange or {})
        finally:
            target.close_session(sid)
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


class _Sec002Runner(_ExternalRunner):
    """One run of one TP-SEC-002 sequence, cell or isolation item through the adapter: each turn in one adapter
    session, one run record for each turn. Before the score, each copy of an encoded pool text in the reply is replaced
    by sec002.CELL_TEXT_REMOVED, as in the SRD-1 engine (run_sec002._Runner); there is no redaction, because the target
    is not SRD-1. A channel error that does not stop the run is recorded at its turn with an empty reply (the outcome
    no_result), and it ends the session: the later turns are not sent. Both engines call `one`."""

    sent = run_sec002._Runner.sent      # the turns of a sequence or an isolation item, or the cell text

    def __init__(self, log, clock_ns, budget, settings, pool):
        super().__init__(log, clock_ns, budget, settings)
        self.pool = pool
        self.encoded = sec002.encoded_forms(e["text"] for e in pool.values())
        self.removed = 0            # the copies replaced by sec002.CELL_TEXT_REMOVED over the run
        self.last_score = None

    def _call(self, fn, *args):
        """One adapter call. Returns (its value, None), or (None, the channel error) for an error that does not stop
        the run. On a stop it sets the status and returns (None, None)."""
        try:
            return fn(*args), None
        except CapReached:
            self.status = "stopped_at_cap"
        except ChannelError as e:
            if e.kind not in FATAL_KINDS:
                return None, {"kind": e.kind, "status": e.status}
            self.status, self.error = "stopped_channel_error", e.kind
        except EgressRefused:
            self.status, self.error = "stopped_error", "EgressRefused"
        except Exception as e:            # noqa: BLE001 - only the class name is kept
            self.status, self.error = "stopped_error", type(e).__name__
        return None, None

    def one(self, target, item, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        turns, sequence = self.sent(item), item.track in sec002.TRACKS
        t_ns, calls_before = self.clock_ns(), self.budget.calls
        sid, error = self._call(target.open_session)
        done, reply = [], None
        try:
            for t, text in enumerate(turns, 1):
                turn = t if sequence else None
                if t > 1 or error is None:          # an error of open_session is recorded at the first turn
                    t_ns = self.clock_ns()
                    reply, error = (None, None) if self.stopped else self._call(target.send, sid, text)
                if self.stopped:
                    self._stop(item, n, t_ns, **({"turn": turn} if turn else {}))
                    return None
                ex = dict(getattr(target, "last_exchange", {}) or {})
                calls, calls_before = self.budget.calls - calls_before, self.budget.calls
                done.append(self._turn(item, n, t_ns, text, "" if error else (reply or ""), ex, calls, turn,
                                       len(turns), error))
                if error is not None:
                    self.channel_errors[error["kind"]] += 1
                    self.channel_error_classes[f"http_{error['status']}" if error["status"] is not None
                                               else error["kind"]] += 1
                    self.consecutive_errors += 1
                    if self.consecutive_errors >= self.settings.max_consecutive_errors:
                        self.status, self.error = "stopped_errors", error["kind"]
                        self._stop(item, n, t_ns, consecutive_errors=self.consecutive_errors,
                                   **({"turn": turn} if turn else {}))
                    break               # the session ends at a channel error: the later turns are not sent
                self.consecutive_errors = 0
        finally:
            if sid is not None:
                try:
                    target.close_session(sid)
                except Exception:          # noqa: BLE001 - a failed close does not change the replies
                    pass
        hits = [d["score"]["evidence"] for d in done if d["score"]["success"]]
        kind = "sequence" if sequence else "cell" if item.track == "C" else "isolation"
        self.last_score = {"value": 1 if hits else 0, "kind": kind, "explanation": "; ".join(hits) or None}
        result = sec002.sequence_outcome(done)["outcome"] if sequence else done[-1]["outcome"]
        self.last_no_result = result == "no_result"
        return None if self.stopped else done[-1]["shown"]

    def _turn(self, item, n, t_ns, request, reply, ex, calls, turn, n_turns, channel_error):
        stored, removed = sec002.remove_cell_texts(reply, self.encoded)     # remove first, then score
        self.removed += removed
        score = sec002.score_turn(self.pool[item.objective], stored, item.family if item.track == "C" else None)
        outcome = sec002.outcome(score, stored, ex.get("finish_reason"), ex.get("blocked"))
        self.hosts[str(ex.get("channel_host"))] += 1
        self.models[str(ex.get("served_model"))] += 1
        self.retries += ex.get("retries", 0)
        run = {"item": item.id, "track": item.track, "objective": item.objective, "class": item.cls, "run": n}
        if turn:
            run.update(turn=turn, turns=n_turns)
        if item.track == "C":
            run["family"] = item.family
        self.runs.append({**run, "score": score, "outcome": outcome})
        channel = {k: ex[k] for k in ("statuses", "ids", "activity_types", "polls", "task_state", "task_reads", "status")
                   if k in ex}
        self.log.write({"type": "run", **run, "t_ns": t_ns, "time": runlog.iso(t_ns),
                        "request_sha256": runlog.sha256_hex(request), "reply": stored, "score": score,
                        "outcome": outcome, "cell_texts_removed": removed, "finish_reason": ex.get("finish_reason"),
                        "blocked": ex.get("blocked"), "block_type": ex.get("block_type"),
                        "served_host": ex.get("channel_host"), "served_model": ex.get("served_model"),
                        "usage": ex.get("usage"), "calls": calls,
                        "cost_usd_estimate": round(calls * self.settings.cost_per_call_usd, 8),
                        "latency_ms": ex.get("latency_ms"), "retries": ex.get("retries", 0),
                        "retry_reasons": ex.get("retry_reasons", []), "channel": channel,
                        "channel_error": channel_error})
        return {**run, "score": score, "outcome": outcome, "shown": stored}


def target_facts(profile):
    """What the header says about the target. The profile itself may hold firm information: only its hash is kept."""
    entry = registry.ADAPTERS[profile.adapter]
    return {"adapter": profile.adapter, "channel": entry.channel, "observes": entry.observes,
            "endpoint_host": host_entry(profile.endpoint), "profile_name": profile.name,
            "profile_sha256": hashlib.sha256(Path(profile.path).read_bytes()).hexdigest(),
            "capabilities_used": ["conversation"]}


def target_block(profile, settings):
    """The target block of a header: target_facts with the kind and the declared values (runlog.external_blocks)."""
    return runlog.external_blocks(settings, target_facts(profile))["target"]


def sec002_header(settings, profile, *, allow_hosts, baseline, e2, confirmation, probes=None, repo=REPO,
                  engine="inspect"):
    """The header of an external TP-SEC-002 run (schema 9, the variant external), without the lock. It has the parts
    of the header of a run on SRD-1 minimal (run_sec002.build_header) that an outside client can know, and the blocks
    target, model, budget and pacing of schema 5. It has no config block and no routing locks: the model, the version
    and the prompt are declared, not read. `baseline`: the facts of sec002_log.read_baseline with external. The
    baseline matches the run on the five declared fields of sec002_log.EXTERNAL_MATCH_FIELDS."""
    data = sec002.load_set(repo)
    blocks = runlog.external_blocks(settings, target_facts(profile))
    baseline = {**baseline, "differs_from_run": sec002_log.external_differences(
        baseline, sec002_log.external_run_facts(blocks["target"]))}
    match = not baseline["differs_from_run"]
    return {
        "type": "header", "schema": sec002_log.SCHEMA,
        "label": sec002_log.run_label(match, baseline, authors=data.get("authors"), external=True),
        "engine_version": runlog.__version__, "code": sec002_log.code_facts(repo),
        "procedure": run_sec002.procedure_block(repo), "mode": settings.mode, **blocks,
        "settings_hash": settings.digest(), "thresholds": sec002.thresholds_for_header(settings, repo=repo),
        "corpus": run_sec002.corpus_block(baseline, settings.runs_per_item, repo),
        "baseline": baseline, "baseline_match": match, "baseline_match_basis": sec002_log.EXTERNAL_MATCH,
        "session_limits": {"read_through": None, "note": sec002_log.SESSION_NOT_READ},
        "e2": e2, "readings": list(sec002_log.READINGS) + list(sec002_log.READINGS_EXTERNAL),
        "store_replies": {"mode": "full", "note": sec002_log.STORE_NOTE_EXTERNAL},
        "variant": {"name": sec002_log.EXTERNAL, "purpose": runlog.VARIANTS["external"]},
        "run_engine": runlog._engine_facts(engine),
        "scorer": sec002_log.SCORER_STATUS,
        "egress": {"allow": sorted(allow_hosts), "limit": runlog.EGRESS_LIMIT},
        "not_in_run": list(sec002_log.NOT_IN_RUN_EXTERNAL),
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each run record (served_host): the channel host that answered",
    }


def sec002_start_problems(settings, probes):
    """Why an external TP-SEC-002 run may not start (empty if it may): no baseline log, or a channel probe that did
    not observe session state (Tracks A and B need it). The preflight refuses both before the operator types yes."""
    problems = []
    if not settings.m4_baseline_log:
        problems.append("a TP-SEC-002 run needs a TP-SEC-001 baseline log (baseline_log) in both modes: without it "
                        "the uplift cannot be computed (PLAYBOOK_SEC_002.md line 8)")
    if ((probes or {}).get("channel") or {}).get("session_state") != "observed":
        problems.append("the channel probe did not observe session state: Tracks A and B send each sequence in one "
                        "session, and a target that keeps no session state cannot run them")
    return problems


def execute(settings, profile, *, run_dir, sign_key, confirmation, probes=None, engine="inspect",
            clock_ns=time.time_ns, repo=REPO, prior_calls=0, env=None, sleep=time.sleep, clock=time.monotonic):
    """Run TP-SEC-001 against the external target of the profile. Return an ExternalResult. With `procedure:
    TP-SEC-002` in the settings, run TP-SEC-002 instead (_execute_sec002); it needs `probes` with the channel probe.

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
    if settings.procedure == sec002.TP:
        return _execute_sec002(settings, profile, run_dir=run_dir, sign_key=sign_key, confirmation=confirmation,
                               probes=probes, engine=engine, clock_ns=clock_ns, repo=repo, prior_calls=prior_calls,
                               env=env, sleep=sleep, clock=clock)
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
        runlog.wait_past_lock(header, clock_ns)
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


def _sec002_pass(engine, target, runner, run_dir, k, items, model_name):
    if engine == "inspect":
        from .spike import _run_inspect
        return _run_inspect(target, runner, run_dir, k, items, model_name=model_name)
    return _run_direct(target, runner, k, items)


def _execute_sec002(settings, profile, *, run_dir, sign_key, confirmation, probes, engine, clock_ns, repo, prior_calls,
                    env, sleep, clock):
    """Run TP-SEC-002 against the external target of the profile (see the module docstring). It refuses, before it
    writes any file: a run with no baseline log, a channel probe that did not observe session state, a baseline that
    is not a schema 5 log of an external TP-SEC-001 run on the full set, a declaration of the channel inputs that
    cannot be read, and, in evidence mode, a baseline that evidence mode does not allow (sec002_log.evidence_problems,
    on the five declared fields)."""
    refused = sec002_start_problems(settings, probes)
    if refused:
        raise ValueError("; ".join(refused))
    seqs, cells, pool = sec002.load_items(repo)
    baseline = sec002_log.read_baseline(settings.m4_baseline_log, list(pool), repo, pubkey=settings.m4_baseline_pubkey,
                                        external=True)
    e2 = run_sec002.read_e2(settings.channel_inputs, repo)
    if settings.mode == "evidence":
        differs = sec002_log.external_differences(baseline, sec002_log.external_run_facts(target_block(profile, settings)))
        refused = sec002_log.evidence_problems({**baseline, "differs_from_run": differs})
        if refused:
            raise ValueError(f"evidence mode needs {sec002_log.EVIDENCE_BASELINE_EXTERNAL}: " + "; ".join(refused))
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = make_egress(profile, log=elog)
    budget = CallBudget(settings.cap_usd, settings.cost_per_call_usd, settings.max_calls, calls=prior_calls)
    target = make_target(profile, settings, egress, budget, env=env, sleep=sleep, clock=clock)
    header = sec002_header(settings, profile, allow_hosts=egress.allow_text, baseline=baseline, e2=e2,
                           confirmation=confirmation, probes=probes, repo=repo, engine=engine)
    runlog.lock_header(header, clock_ns())
    log_path = run_dir / "run_log.jsonl"
    log = runlog.RunLog(log_path)
    try:
        log.write(header)                   # the lock is on disk before the first turn is sent
        runlog.wait_past_lock(header, clock_ns)
        runner = _Sec002Runner(log, clock_ns, budget, settings, pool)
        k = settings.runs_per_item
        engine_facts = [_sec002_pass(engine, target, runner, run_dir, k, seqs + cells, profile.adapter)]
        iso_record = None
        if not runner.stopped:
            iso_items, iso_record = run_sec002._isolation(runner, seqs, clock_ns)
            log.write(iso_record)
            if iso_items:
                engine_facts.append(_sec002_pass(engine, target, runner, run_dir, k, iso_items, profile.adapter))
        egress.close()          # also closed in `finally` if the run raises; closing twice is harmless
        runs = runner.runs
        summary = {
            "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
            "runs_recorded": len(runs), "measures_complete": runner.status == "complete",
            "measures": sec002_log.measures(header, runs, iso_record),
            "results_table": sec002.results_table(runs), "sequence_outcomes": run_sec002.sequence_outcomes(runs),
            "cell_texts_removed": runner.removed,
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
