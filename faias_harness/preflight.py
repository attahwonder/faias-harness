"""Preflight: compare a target profile with the capability needs of each procedure. No network. No run.

  py -3.14 -m faias_harness.preflight --profile <profile.yaml> [--start]

It prints, for each procedure with a record, the capabilities it needs, the capabilities the profile lacks, and
a result: full, partly or none. Full: every need is present. None: the procedure needs `conversation` and the
profile lacks it, or every need is missing. Partly: anything else.
The capability needs are a DRAFT (records/capability_needs.yaml): the needs that two independent model passes
agree on, or that a third reading settled where they differed. A procedure with an open entry is not settled.

With --start, it refuses unless the isolation attestation file exists and is not empty, and the settings
provider, model_id and cap_usd are set. With a profile alone, it starts nothing, even when every condition is met.

With --settings <settings.yaml> (version 0.3 and later), it also checks the settings file of an engine run
(TP-SEC-001 on SRD-1 minimal) and prints the provider, model, host, precision, routing locks, the worst-case
estimate against the cap, and whether the key variable is present (never its value). An evidence-mode settings
file that lacks any of the five routing locks is refused. With --start it checks the signing key, makes two probe
calls (a tool-call probe and a reasoning-text probe; the judge-family check is not applicable because the engine
has no model judge), shows the result, and starts the run only after the operator types `yes`. With --probes it makes
the two probe calls and starts nothing. For a TP-SEC-006 run, the preflight verifies the signature of the M4 baseline
log against the key that `m4_baseline_pubkey` names, and refuses a key inside the repository or a log that fails. In
evidence mode the preflight and --start also refuse a baseline log whose model ID, variant, system prompt hash,
routing locks (`only`, `quantizations`) or tool definitions hash differ from the run, and name each field that
differs, or a baseline log that is not in evidence mode.
With a settings file of SRD-1 it also prints one line on the containment self-test (faias_harness/selftest): the result,
time and commit of the newest self-test log (--selftest-log, or the folders of FAIAS_HARNESS_RUNS, or runs/), and
whether its code manifest matches this checkout. The signature verifies against the public key that
--selftest-pubkey or the setting `selftest_pubkey` names; with neither, the line says "(key not pinned)". For
TP-AGT-012 and TP-DAT-009, --start refuses without a verified, passing log of this code. In evidence mode it also
refuses them without a pinned key. For other procedures the line is information only.
For TP-DAT-009 (version 0.7) it also prints the filter stage, the delegation credential, the arms and the halt
setting. The positive control (--positive-control) uses the display filter and the service credential. So --start
refuses the standard variant with either of them. The refund gate is not read, because the tool arm serves
document_search only.
For TP-SEC-002 (version 0.8) it prints the M1 bar with its reason, the baseline log and its public key, and the
declaration of the channel inputs (targets/srd1/channel_inputs.yaml): the languages and the transformation families.
--start refuses families that are not those of the item file. With no baseline log named, it prints BASELINE
REQUIRED, and --probes and --start refuse. A named baseline log is read as the engine reads it: a TP-SEC-001 log on
the full set, its signature checked against `baseline_pubkey`, and its stored scores scored again with this scorer.
The run-to-run variance of the baseline (its M4 value) is printed next to the M1 bar. In evidence mode the preflight
and --start refuse a baseline log of another target (each field that differs is named, with both values), one that
is not in evidence mode, and one whose stored scores this scorer does not give again. Explore mode allows these, and
the run label says so. The engine runs the standard variant with tools off, so --start refuses the positive control
and tools on.

With --settings <settings.yaml> of `kind: external` (version 0.5), it checks an external run instead: TP-SEC-001
against a deployed staging agent, through the channel adapter that the profile names (docs/ARCHITECTURE.md,
section 13). It prints the adapter, the endpoint host, what the channel lets the harness observe, the declared
model and version, the calls against the call budget, and whether the key variable is present (never its value).
--probes makes one channel probe (one benign message). --start makes the probe, refuses if it gets no usable
reply, and starts the run only after the operator types `yes`.
An external settings file with `procedure: TP-SEC-002` (version 0.8) runs TP-SEC-002 through the channel. The
preflight then prints the M1 bar, the baseline log and its key, and the declaration of the channel inputs that the
setting `channel_inputs` names. The baseline must be a schema 5 log of an external TP-SEC-001 run. In evidence mode
it must match the run on five declared fields (sec002_log.EXTERNAL_MATCH_FIELDS), and each field that differs is
named with both values. The probe is a recall test: one session gives the agent two words and asks for them back, and
a fresh control session sends a plain first turn and asks the same question at its second turn. It is four calls, or
six with the Direct Line start event. --start refuses Tracks A and B when the probe does not observe
session state, before the operator types `yes`. The probe's requests go to an egress log of their own, and its summary
goes into the probe block of the header.

Exit codes: 0 table printed, or a run finished; 2 start refused or profile or settings invalid; 3 start
conditions met, but a profile alone starts no run; 4 the operator did not type `yes`; 5 the run stopped at the cap
or on an error.
"""
import argparse
import os
import sys
import time
from pathlib import Path

from . import yamlio
from .profile import REPO, ProfileError, load_profile
from .settings import AGT012, DAT009, SEC002, SEC006, SettingsError, load_settings

NEEDS_FILE = REPO / "records" / "capability_needs.yaml"
RECORDS_DIR = REPO / "records"


def load_needs(path=NEEDS_FILE):
    """Return {procedure: [needed capabilities]}. Only the capabilities that both passes marked as needed count."""
    data = yamlio.load(Path(path).read_text(encoding="utf-8"))
    return {tp: list(entry["needed"]) for tp, entry in data["needs"].items()}


def open_entries(path=NEEDS_FILE):
    """Return {procedure: {pass_1: [...], pass_2: [...]}} for the procedures where the two passes differ."""
    data = yamlio.load(Path(path).read_text(encoding="utf-8"))
    return {tp: entry["open"] for tp, entry in data["needs"].items() if entry.get("open")}


def judge(needs, have):
    """Return (result, missing) for one procedure."""
    missing = [c for c in needs if c not in have]
    if not missing:
        return "full", missing
    if "conversation" in missing or len(missing) == len(needs):
        return "none", missing
    return "partly", missing


def map_summary(profile, tp):
    """Short text for the precondition map of one procedure, or '-' if the profile has none for it."""
    cats = (profile.precondition_map or {}).get(tp)
    if not cats:
        return "-"
    statuses = [c.get("status") for c in cats.values()]
    return f"{statuses.count('met')} met, {statuses.count('partly')} partly, {statuses.count('not met')} not met"


def firm_value_keys(records_dir=RECORDS_DIR):
    keys = []
    for p in sorted(Path(records_dir).glob("TP-*.yaml")):
        rec = yamlio.load(p.read_text(encoding="utf-8"))
        keys += [t["firm_value_key"] for t in rec["thresholds"] if t.get("firm_value_key")]
    return keys


def declared_firm_values(profile, keys):
    """Number of keys with a non-null value in the profile's firm-values file, or None if there is no file."""
    if profile.firm_values is None or not profile.firm_values.is_file():
        return None
    data = yamlio.load(profile.firm_values.read_text(encoding="utf-8")) or {}
    values = data.get("values", {}) or {}
    return sum(1 for k in keys if values.get(k) is not None)


def attestation_problems(profile):
    att = profile.attestation_file
    if att is None:
        return ["the profile names no isolation attestation file (attestation_file)"]
    if not att.is_file():
        return [f"the isolation attestation file does not exist: {att}"]
    if not att.read_text(encoding="utf-8", errors="replace").strip():
        return [f"the isolation attestation file is empty: {att}"]
    return []


def external_start_problems(profile, settings):
    """The reasons why an external run may not start (empty list if it may)."""
    from .adapters import registry
    from .engine import external
    problems = attestation_problems(profile) + registry.problems(profile)
    if registry.is_external(profile.adapter):
        env = registry.key_env(profile)
        if env and not os.environ.get(env):
            problems.append(f"the environment variable {env} is not set")
    # The probe and the first session of the run (one item, or the first turn of a sequence). With the Direct Line
    # start event, each session is one call more.
    probe, item = external.probe_calls(settings, profile), external.calls_per_session(profile)
    if settings.call_budget < probe + item:
        what = ("the four turns of the channel probe in two sessions and one turn" if settings.procedure == SEC002
                else "the channel probe and one item")
        start = "; the Direct Line start event is one more call in each session" if item > 1 else ""
        problems.append(f"the call budget allows fewer than {probe + item} calls ({what}{start})")
    o = profile.adapter_options or {}
    if settings.procedure == SEC002 and profile.adapter == "openai-responses" and o.get("chain", True) \
            and o.get("store") is not True:
        problems.append("openai-responses with chain: true needs store: true for a TP-SEC-002 run: with store not "
                        "true the service keeps no earlier turn, so a later turn of a session cannot chain to it "
                        "(the default store: false stays for TP-SEC-001)")
    return problems


def start_problems(profile, settings=None):
    """The reasons why a run may not start. An empty list means every start condition is met.

    With `settings` (the engine settings file), provider, model_id and cap_usd come from it, and the prices, the
    key variable and the signing key are checked too. Without it, they come from the profile (version 0.2 rules).
    """
    if settings is not None and settings.kind == "external":
        return external_start_problems(profile, settings)
    problems = attestation_problems(profile)
    if settings is None:
        st = profile.settings
        for k in ("provider", "model_id"):
            if not st.get(k):
                problems.append(f"settings.{k} is not set")
        cap = st.get("cap_usd")
    else:
        for k in ("provider", "model_id"):
            if not getattr(settings, k):
                problems.append(f"settings.{k} is not set")
        for k in ("price_in_per_m", "price_out_per_m"):
            if getattr(settings, k) is None:
                problems.append(f"settings.{k} is not set")
        cap = settings.cap_usd
        if not settings.key_present():
            problems.append(f"the environment variable {settings.api_key_env} is not set")
        problems += tools_problems(settings)
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
        problems.append("settings.cap_usd is not set to a number above 0")
    return problems


def tools_problems(settings):
    """With tools on (since 0.6): the refund gate needs its declared values. With tools off: none. A TP-DAT-009 run
    reads no refund gate (its tool arm serves document_search only), so none."""
    if not getattr(settings, "tools_enabled", False) or settings.procedure == DAT009:
        return []
    from .engine import spike
    spike._srd1()
    from targets.srd1.app import gate
    return [f"{p} (targets/srd1/risk_appetite.yaml): SRD-1 does not enable its tool" for p in gate.declared_problems()]


def report(profile, needs, records_dir=RECORDS_DIR):
    """Print the preflight table. Return the counts."""
    have = set(profile.capabilities)
    print(f"Profile: {profile.name}  (adapter: {profile.adapter})")
    if profile.status:
        print(f"Status: {profile.status}")
    print(f"Capabilities offered: {', '.join(profile.capabilities) or 'none'}")
    print("Capability needs are a DRAFT: see records/capability_needs.yaml. "
          f"{len(open_entries())} of {len(needs)} procedures have an open entry (the two passes differ; not settled).")
    print()
    counts = {"full": 0, "partly": 0, "none": 0}
    for tp in sorted(needs):
        result, missing = judge(needs[tp], have)
        counts[result] += 1
        print(f"{tp}  {result:<6}  needs: {','.join(needs[tp]) or '-'}  |  missing: {','.join(missing) or '-'}"
              + (f"  |  map: {map_summary(profile, tp)}" if profile.precondition_map else ""))
    print()
    print(f"Result for {len(needs)} procedures: full {counts['full']}, partly {counts['partly']}, none {counts['none']}")
    if profile.precondition_map:
        rows = [c.get("status") for tp in needs for c in (profile.precondition_map.get(tp) or {}).values()]
        all_met = sum(1 for tp in needs if (profile.precondition_map.get(tp) or {})
                      and all(c.get("status") == "met" for c in profile.precondition_map[tp].values()))
        print(f"Precondition map: {len(rows)} category rows: {rows.count('met')} met, {rows.count('partly')} partly, "
              f"{rows.count('not met')} not met; {all_met} procedures have all categories met")
    keys = firm_value_keys(records_dir)
    declared = declared_firm_values(profile, keys)
    if declared is None:
        print(f"Firm values: no file found; {len(keys)} are needed by the thresholds with a firm basis")
    else:
        print(f"Firm values: {declared} of {len(keys)} declared")
    return counts


def settings_report(settings, estimate_usd):
    """Print the settings block of the preflight. It never prints the value of the key."""
    r = settings.routing()
    print(f"Engine run: {settings.procedure} on SRD-1 minimal, item set {settings.item_set}")
    print(f"Mode: {settings.mode}")
    print(f"Settings hash: {settings.digest()}")
    print(f"Provider: {settings.provider}")
    print(f"Model: {settings.model_id or 'not set'}")
    print(f"Host (routing.only): {', '.join(settings.only) or 'not set'}")
    print(f"Precision (routing.quantizations): {', '.join(settings.quantizations) or 'not set'}")
    locks = [("only", "only" in r), ("allow_fallbacks", "allow_fallbacks" in r), ("zdr", "zdr" in r),
             ("data_collection", "data_collection" in r), ("quantizations", "quantizations" in r)]
    print("Routing locks: " + ", ".join(f"{k} {'set' if ok else 'not set'}" for k, ok in locks))
    seed = "not set" if settings.seed is None else f"{settings.seed} (+ call number)"
    temp = "not set" if settings.temperature is None else settings.temperature
    print(f"Decoding: temperature {temp}, seed {seed}, max_tokens {settings.max_tokens}")
    print(f"Runs per item: {settings.runs_per_item} (FAIAS minimum 3)")
    m4 = "not set" if settings.m4_value is None else settings.m4_value
    if settings.procedure == SEC006:
        m3 = "not set" if settings.m3_value is None else settings.m3_value
        print(f"M3 bar (reconstruction share): {m3}; reason: {settings.m3_reason or 'not set'}")
        print(f"M4 bar (material uplift): {m4}; reason: {settings.m4_reason or 'not set'}")
        print(f"M4 baseline (a TP-SEC-001 run log): {settings.m4_baseline_log or 'not named'}")
        print(f"M4 baseline public key: {settings.m4_baseline_pubkey or 'not named'}")
        print(f"Replies in the log: {settings.store_replies}")
    elif settings.procedure == AGT012:
        print("M1 and M4: from records/TP-AGT-012.yaml, not from the settings. "
              "M1 from the canvas, M4 reviewer-set; both 0")
    elif settings.procedure == DAT009:
        print("M1 to M6: from records/TP-DAT-009.yaml, not from the settings")
        print(f"Retrieval: filter stage {settings.retrieval_filter}; delegation credential "
              f"{settings.delegation_credential}; arms {', '.join(settings.arms)}; halt on crossing "
              f"{settings.halt_on_crossing}")
    elif settings.procedure == SEC002:
        m1 = "not set" if settings.m1_value is None else settings.m1_value
        print(f"M1 bar (material uplift): {m1}; reason: {settings.m1_reason or 'not set'}")
        print("M2 to M6: from records/TP-SEC-002.yaml, not from the settings")
        print(f"Baseline (a TP-SEC-001 run log on the full set): {settings.m4_baseline_log or 'not named'}")
        print(f"Baseline public key: {settings.m4_baseline_pubkey or 'not named'}")
        print(f"Replies in the log: {settings.store_replies}")
    else:
        print(f"M4 bar: {m4}; reason: {settings.m4_reason or 'not set'}")
    pin, pout = settings.price_in_per_m, settings.price_out_per_m
    print(f"Prices (USD per million tokens): in {pin if pin is not None else 'not set'}, "
          f"out {pout if pout is not None else 'not set'}")
    cap = f"USD {settings.cap_usd}" if settings.cap_usd is not None else "not set"
    if estimate_usd is None:      # with the prices set, only a TP-SEC-002 run without its item file has no estimate
        why = "prices not set" if pin is None or pout is None else "the item file is not found"
        print(f"Worst-case estimate: not available ({why}); cap: {cap}")
    else:
        note = "  WARNING: the estimate is above the cap; the run may stop at the cap" \
            if settings.cap_usd is not None and estimate_usd > settings.cap_usd else ""
        print(f"Worst-case estimate for the whole run: USD {estimate_usd:.4f}; cap: {cap}{note}")
    print(f"Key variable: {settings.api_key_env} is {'present' if settings.key_present() else 'NOT present'}")
    if settings.procedure == DAT009:
        print(f"Tools: on for the tool arm only (document_search through the MCP tool emulator; its key is generated "
              f"in the run and carried in {settings.tool_key_env})")
    elif getattr(settings, "tools_enabled", False):
        print(f"Tools: on (fee_refund through the MCP tool emulator; its key is generated in the run and carried in "
              f"{settings.tool_key_env})")
        for p in tools_problems(settings):
            print(f"  - {p}")
    if settings.procedure == AGT012:
        delays, c_runs = settings.containment_plan()
        print(f"Containment: delays {', '.join(str(d) for d in delays)} ms; {c_runs} runs per delay")
        print(f"Halt on bypass: {settings.halt_on_bypass}")
    print("Judge-family check: not applicable (the engine has no model judge)")


def external_settings_report(profile, settings, est):
    """Print the settings block of an external run. It never prints the value of a key."""
    from .adapters import registry
    from .adapters.base import host_entry
    sec002 = settings.procedure == SEC002
    print(f"Engine run: {settings.procedure} against an external target, item set {settings.item_set}")
    if registry.is_external(profile.adapter):
        entry = registry.ADAPTERS[profile.adapter]
        print(f"Target: adapter {profile.adapter}: {entry.channel}; endpoint host {host_entry(profile.endpoint) if profile.endpoint else 'not set'}")
        print(f"The harness observes: {entry.observes}")
    print(f"Mode: {settings.mode}")
    print(f"Settings hash: {settings.digest()}")
    print(f"Declared model: {settings.declared_model_id or 'not declared'}; declared target version: "
          f"{settings.declared_target_version or 'not declared'} (declared by the operator, not observed)")
    print(f"Runs per item: {settings.runs_per_item} (FAIAS minimum 3)")
    if sec002:
        m1 = "not set" if settings.m1_value is None else settings.m1_value
        print(f"M1 bar (material uplift): {m1}; reason: {settings.m1_reason or 'not set'}")
        print("M2 to M6: from records/TP-SEC-002.yaml, not from the settings")
        print(f"Baseline (a TP-SEC-001 log of an external run on the full set): "
              f"{settings.m4_baseline_log or 'not named'}")
        print(f"Baseline public key: {settings.m4_baseline_pubkey or 'not named'}")
        print("Replies in the log: full (an external run keeps each reply)")
    else:
        m4 = "not set" if settings.m4_value is None else settings.m4_value
        print(f"M4 bar: {m4}; reason: {settings.m4_reason or 'not set'}")
    if est is None:         # only a TP-SEC-002 run without its item file has no estimate
        print("Calls in the run: not available (the item file is not found)")
    elif sec002:
        print(f"Calls in the run: at most {est['calls']} (plus the channel probe: {est['probe_calls']} calls, four "
              "turns in two sessions; retries are extra)")
    else:
        n = est["probe_calls"]
        print(f"Calls in the run: {est['calls']} (plus the channel probe: {n} call{'' if n == 1 else 's'}; retries are "
              "extra)")
    mc = f", max_calls {settings.max_calls}" if settings.max_calls is not None else ""
    print(f"Budget: cap USD {settings.cap_usd}; cost per call USD {settings.cost_per_call_usd} (the operator's "
          f"estimate){mc}; call budget {settings.call_budget}")
    if est is not None:
        note = "  WARNING: the calls are above the call budget; the run will stop at the cap" \
            if est["calls"] + est["probe_calls"] > settings.call_budget else ""
        print(f"Estimated cost of the run: USD {est['cost_usd']:.4f} (the {est['probe_calls']} calls of the channel "
              f"probe are not in it){note}")
    print(f"Pacing: at least {settings.min_interval_ms} ms between calls; reply timeout {settings.reply_timeout_s} s; "
          f"stop after {settings.max_consecutive_errors} channel errors in a row")
    if sec002 and profile.adapter == "json-http":
        from .adapters import json_http
        if not json_http._has_placeholder((profile.adapter_options or {}).get("request"), "session_id"):
            print("WARNING: the json-http request holds no {session_id}, so no turn sends a session ID. The endpoint "
                  "must keep the session in another way (for example a cookie), or the channel probe shows not "
                  "observed")
    env = registry.key_env(profile) if registry.is_external(profile.adapter) else None
    if env:
        print(f"Key variable: {env} is {'present' if os.environ.get(env) else 'NOT present'}")
    else:
        print("Key variable: none needed by this adapter configuration")
    print("Judge-family check: not applicable (the engine has no model judge)")
    print("Not observed through the channel: a routing lock or a pinned model; the model, host, tokens and cost "
          "unless the response names them")


def print_external_probe(probe, calls):
    """The probe block of the preflight. `calls`: the charged calls of the probe before retries (probe_calls)."""
    check = probe.get("session_check")
    s = "" if calls == 1 else "s"
    print(f"Channel probe (a recall test: four turns in two sessions; {calls} calls):" if check
          else f"Channel probe ({calls} call{s}):")
    if probe.get("ok"):
        print(f"  reply received from {probe.get('channel_host')} in {probe.get('latency_ms')} ms"
              + (f"; model named: {probe['served_model']}" if probe.get("served_model") else ""))
        print(f"  reply start: {probe.get('reply_start')!r}")
    else:
        print(f"  no usable reply (error: {probe.get('error') or 'empty reply'}"
              + (f", HTTP {probe['status']}" if probe.get("status") else "") + ")"
              + (f": {probe['detail']}" if probe.get("detail") else ""))
    if check:
        first = (check.get("calls") or [{}])[0]
        if "error" not in first:
            print(f"  turn 1 of session A: blocked {first.get('blocked')}; block type {first.get('block_type')}")
        held = {c.get("session"): ", ".join(c.get("holds") or []) or "none" for c in check.get("calls") or []
                if c.get("turn") == 2}
        print(f"  session state: {probe.get('session_state')} (the words: {', '.join(check.get('words') or [])}; "
              f"turn 2 of session A holds: {held.get('A', 'not sent')}; turn 2 of the control session holds: "
              f"{held.get('B', 'not sent')}; history held by: {check.get('history_held_by')})")
        if check.get("reason"):
            print(f"  reason: {check['reason']}")


def _default_external_executor(**kw):
    from .engine import external
    return external.execute(**kw)


def run_external(a, profile, settings, *, input_fn, executor, inspect_check):
    """The preflight of an external run: report, probe, ask for `yes`, run. Returns the exit code."""
    from .adapters import registry
    from .adapters.base import CallBudget
    from .engine import external
    reasons = registry.problems(profile)
    sec002 = settings.procedure == SEC002
    est = None if sec002 and _sec002_items() is None else external.estimate(settings, profile=profile)
    print()
    external_settings_report(profile, settings, est)
    if reasons:
        print()
        print("PROFILE CANNOT DRIVE AN EXTERNAL RUN. No run starts:")
        for p in reasons:
            print(f"  - {p}")
        return 2
    label, base_problems, target = external.sec001.LABELS.get(settings.item_set), [], None
    if sec002:          # the declaration of the channel inputs and the baseline, as for a run on SRD-1 minimal
        target = external.target_block(profile, settings)
        lines = sec002_channel_inputs(settings.channel_inputs)[0]
        label, more, base_problems = sec002_baseline(settings, target=target)
        for line in lines + more:
            print(line)
    if not a.start and not a.probes:
        if base_problems and settings.mode == "evidence":
            print()
            print("BASELINE REFUSED. No run starts. Fix these points first:")
            for p in base_problems:
                print(f"  - {p}")
            return 2
        print("Channel probe: not run (use --probes, or --start)")
        return 0
    print()
    problems = external_start_problems(profile, settings)
    if sec002 and not settings.m4_baseline_log:
        problems.append(BASELINE_REQUIRED)          # --probes and --start: the run has no measure without it
    if a.start:
        from .engine import runlog
        problems += runlog.key_problems(a.sign_key) if a.sign_key else ["no signing key given (--sign-key)"]
        missing = inspect_check()
        if missing:
            problems.append(missing)
        if a.positive_control:
            problems.append("--positive-control does not apply to an external run: the harness cannot weaken a "
                            "deployed agent; run a second staging agent with no rules, with its own profile")
        if sec002:
            problems += sec002_start_problems(settings, target=target)
        elif settings.m4_value is None or not settings.m4_reason:
            problems.append("reviewer_values.M4 needs a value and a reason before a run (verify refuses a log without them)")
    # The probe of an external run reaches the firm's agent, so it needs the isolation attestation too.
    if problems:
        print("START REFUSED. No run starts. Fix these points first:" if a.start else "PROBES REFUSED. Fix these points first:")
        for p in problems:
            print(f"  - {p}")
        return 2
    budget = CallBudget(settings.cap_usd, settings.cost_per_call_usd, settings.max_calls)
    from .egress import EgressLog
    elog = EgressLog()          # the probe's requests, as the run's egress.jsonl holds the run's; its summary is kept
    egress = external.make_egress(profile, log=elog)
    try:
        probe = external.channel_probe(external.make_target(profile, settings, egress, budget), session_check=sec002)
    finally:
        egress.close()
    probe["egress"] = {"summary": elog.summary()}       # in the probe block of the header
    print_external_probe(probe, external.probe_calls(settings, profile))
    if not a.start:
        return 0
    if not probe.get("ok"):
        print()
        print("START REFUSED: the channel probe got no usable reply. Fix the channel first.")
        return 2
    refused = external.sec002_start_problems(settings, {"channel": probe}) if sec002 else []
    if refused:         # before the operator types yes: Tracks A and B need the session state
        print()
        print("START REFUSED. No run starts. Fix these points first:")
        for p in refused:
            print(f"  - {p}")
        return 2
    print()
    print("Start conditions are met.")
    sends = f"sends at most {est['calls']} calls" if sec002 else f"sends {est['calls']} calls"
    print(f"Item set {settings.item_set}. This run {sends} ({settings.runs_per_item} runs per item) "
          f"through the {profile.adapter} channel to {profile.endpoint}. Call budget {settings.call_budget} "
          f"({budget.calls} used by the probe); estimated cost USD {est['cost_usd']:.4f}; cap USD {settings.cap_usd}. "
          f"The result is labelled '{label}'.")
    answer = input_fn("Type yes to start the run (anything else cancels): ").strip()
    if answer != "yes":
        print("Run not started: `yes` was not typed.")
        return 4
    confirmation = {"typed": "yes", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    run_dir = Path(a.run_dir) if a.run_dir else REPO / "runs" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    result = executor(settings=settings, profile=profile, run_dir=run_dir, sign_key=a.sign_key,
                      confirmation=confirmation, probes={"channel": probe}, prior_calls=budget.calls)
    s = result.summary
    print(f"Run {result.status}. Calls {s.get('calls')} of a budget of {s.get('call_budget')}; estimated cost USD "
          f"{s.get('cost_usd_estimate')}. Label: {s.get('label')}.")
    print(f"Log: {result.log_path}\nSignature: {result.sig_path}")
    return 0 if result.status == "complete" else 5


def print_probes(probes):
    tc, rt = probes.get("tool_call", {}), probes.get("reasoning_text", {})
    print("Probes (two model calls):")
    print(f"  tool-call probe: {'ok' if tc.get('ok') else 'no tool call returned'}"
          + (f" (error: {tc['error']})" if tc.get("error") else ""))
    print(f"  reasoning-text probe: {'reasoning text present (' + str(rt.get('field')) + ')' if rt.get('present') else 'no reasoning text'}"
          + (f" (error: {rt['error']})" if rt.get("error") else ""))
    print(f"  judge-family check: {probes.get('judge_family')}")
    print(f"  probe cost: USD {probes.get('cost_usd')}")


def _default_gateway(settings):
    from .egress import EgressClient
    from .engine import spike
    build, gw, _, _ = spike._srd1()
    return gw.Gateway(settings, EgressClient([spike._host_entry(gw.OPENROUTER_BASE)]))


def _default_executor(**kw):
    proc = kw["settings"].procedure
    if proc == SEC006:
        from .engine import run_sec006
        return run_sec006.execute(**kw)
    if proc == AGT012:
        from .engine import run_agt012
        return run_agt012.execute(**kw)
    if proc == DAT009:
        from .engine import run_dat009
        return run_dat009.execute(**kw)
    if proc == SEC002:
        from .engine import run_sec002
        return run_sec002.execute(**kw)
    from .engine import spike
    return spike.execute(**kw)


def sec006_baseline(settings, variant="standard"):
    """The check of the M4 baseline log of a TP-SEC-006 run: (run label, lines to print, problems). A log that cannot
    be read, a public key file inside the repository, and a signature that does not verify are problems. In evidence
    mode, a log whose model ID, variant, system prompt hash, routing locks or tool definitions hash (from the tools
    file, with tools on) differ from the run is a problem too:
    each field that differs is named, with both values. So is a log that is not in evidence mode. In explore mode a
    difference is allowed and printed, and the run label adds "baseline from another target". A baseline on the spike
    set is labelled in both modes."""
    from .engine import run_sec006, sec006_log
    if not settings.m4_baseline_log:
        return sec006_log.run_label(None), [], []
    try:
        base = sec006_log.read_baseline(settings.m4_baseline_log, pubkey=settings.m4_baseline_pubkey)
    except ValueError as e:
        return sec006_log.run_label(None), [f"M4 baseline check: {e}"], [str(e)]
    sig = base["signature"]
    lines = [f"M4 baseline signature: verified (public key SHA-256 {sig['public_key_sha256']})" if sig["verified"]
             else f"M4 baseline signature: not checked ({sig['reason']})",
             f"M4 baseline mode: {base['mode']}; item set: {base['item_set']}"]
    base["differs_from_run"] = sec006_log.baseline_differences(base, sec006_log.run_facts(
        settings.model_id, variant, run_sec006.prompt_sha256(variant), settings.routing(),
        run_sec006.tool_definitions_sha256(settings)))
    match = not base["differs_from_run"]
    label = sec006_log.run_label(match, base)
    if base.get("item_set") != sec006_log.FULL_SET:
        lines.append(f"  The baseline is not on the full TP-SEC-001 set. The run label adds '{sec006_log.ON_SPIKE}'.")
    if match:
        lines.append("M4 baseline match: yes (the same model ID, variant, system prompt hash, routing locks and tool "
                     "definitions hash as the run)")
    else:
        lines += ["M4 baseline match: no"] + [f"  - {p}" for p in
                                              sec006_log.difference_problems(base["differs_from_run"])]
    if settings.mode == "evidence":
        problems = sec006_log.evidence_problems(base)
        return label, lines, problems + ([f"evidence mode needs {sec006_log.EVIDENCE_BASELINE}"] if problems else [])
    if not match:
        lines.append(f"  Explore mode allows this. The run label adds '{sec006_log.ANOTHER_TARGET}'.")
    return label, lines, []


def sec006_start_problems(settings, variant="standard"):
    """What a TP-SEC-006 run needs besides the TP-SEC-001 conditions: the M3 and M4 values with their reasons (verify
    refuses a log without them), and a readable M4 baseline log where one is named, with a signature that verifies
    where a key is named. In evidence mode, the baseline log must be of the same model, variant, system prompt,
    routing locks and tool definitions as the run, and in evidence mode itself (sec006_baseline)."""
    problems = [f"reviewer_values.{m} needs a value and a reason before a run (verify refuses a log without them)"
                for m, v, r in (("M3", settings.m3_value, settings.m3_reason), ("M4", settings.m4_value, settings.m4_reason))
                if v is None or not r]
    return problems + sec006_baseline(settings, variant)[2]


def agt012_start_problems(settings):
    """What a TP-AGT-012 run needs besides the SRD-1 conditions and the self-test gate. The item file must be
    found, and agt012.check_item_set must find no problem in it with the declared values. The run drives the tool,
    so tools must be on."""
    from .engine import agt012, run_agt012
    from targets.srd1.app.gate import load_risk_appetite
    problems = []
    if not (REPO / agt012.ITEM_FILE).is_file():
        problems.append(f"the item file {agt012.ITEM_FILE} is not found")
    else:
        values, _ = load_risk_appetite()
        problems += [f"the item file: {p}" for p in
                     run_agt012.item_set_problems(REPO, values.get("cap_gbp"), values.get("session_limit_gbp"))]
    if not getattr(settings, "tools_enabled", False):
        problems.append("TP-AGT-012 needs tools_enabled: true")
    return problems


def agt012_sends(settings):
    """The count part of the start line of a TP-AGT-012 run. It gives the sessions that the run opens and the most
    model calls they make. The function run_agt012.plan gives both. The estimate uses it too."""
    from .engine import run_agt012
    sessions, calls, _ = run_agt012.plan(settings)
    return f"opens {sessions} sessions and sends at most {calls} model calls"


def dat009_start_problems(settings, variant="standard"):
    """What a TP-DAT-009 run needs besides the SRD-1 conditions and the self-test gate. It needs the item file and
    tools on for the tool arm. Its variant must agree with the stage and the credential (dat009.variant_problems).
    The engine checks the same rule."""
    from .engine import dat009
    problems = []
    if not (REPO / dat009.ITEM_FILE).is_file():
        problems.append(f"the item file {dat009.ITEM_FILE} is not found")
    if not getattr(settings, "tools_enabled", False):
        problems.append("TP-DAT-009 needs tools_enabled: true")
    return problems + dat009.variant_problems(variant, settings.retrieval_filter, settings.delegation_credential)


def dat009_sends(settings):
    """The count part of the start line of a TP-DAT-009 run (run_dat009.plan gives both counts)."""
    from .engine import run_dat009
    sessions, calls, _ = run_dat009.plan(settings)
    return f"opens {sessions} sessions on {len(settings.arms)} arms and sends at most {calls} model calls"


BASELINE_REQUIRED = ("BASELINE REQUIRED: a TP-SEC-002 run compares each track with a TP-SEC-001 run log on the full "
                     "set, in both modes; name the log in baseline_log and its public key file in baseline_pubkey")


def _sec002_items():
    """The item file of TP-SEC-002 (sec002.ITEM_FILE), or None when it is not found."""
    from .engine import sec002
    return sec002.load_set() if (REPO / sec002.ITEM_FILE).is_file() else None


def sec002_channel_inputs(path=None):
    """The declaration of the channel inputs of a TP-SEC-002 run (run_sec002.read_e2): (lines to print, problems). The
    transformation families that it lists must be those of the item file, because the run makes its cells from the
    item file and its header quotes the declaration. The engine does not compare the two. `path`: the declaration of
    an external run (its setting channel_inputs); without it, the declaration of SRD-1 (run_sec002.E2_FILE)."""
    from .engine import run_sec002, sec002
    try:
        e2 = run_sec002.read_e2(path or run_sec002.E2_FILE)
    except ValueError as e:
        return [f"Channel inputs (E2): {e}"], [str(e)]
    declared = e2["transformation_families"]
    lines = [f"Channel inputs (E2, {e2['file']}): languages {', '.join(e2['languages']) or 'none'}; transformation "
             f"families {', '.join(declared) or 'none'}",
             f"  declared by {e2['declared_by']} on {e2['declared_on']}; status: {e2['status']}"]
    data = _sec002_items()
    if data is None:
        missing = f"the item file {sec002.ITEM_FILE} is not found"
        return lines + [f"Item file: {sec002.ITEM_FILE} is not found"], [missing]
    families = list(data["families"])
    if sorted(families) != sorted(declared):
        p = (f"the declaration of the channel inputs ({e2['file']}) lists the transformation families "
             f"{', '.join(declared) or 'none'}, and the item file lists {', '.join(families) or 'none'}: the two "
             "lists must hold the same families")
        return lines + [f"  - {p}"], [p]
    return lines + ["  The item file has the same transformation families."], []


def sec002_baseline(settings, variant="standard", target=None):
    """The check of the baseline log of a TP-SEC-002 run: (run label, lines to print, problems). With no log named,
    the line is BASELINE REQUIRED, and main refuses --probes and --start. A named log is read as the engine reads it
    (sec002_log.read_baseline): a TP-SEC-001 log on the full set, its signature checked against baseline_pubkey, and
    its stored scores scored again with this scorer. A log that cannot be read, a key file inside the repository and a
    signature that does not verify are problems in both modes. In evidence mode, a log of another target (each field
    that differs is named, with both values), a log that is not in evidence mode and a log whose stored scores this
    scorer does not give again are problems too. In explore mode they are printed, and the run label says so. The run
    label takes its authorship part from the item file. It is None when no run can start: with no baseline log, with
    no item file, with a baseline log that cannot be read, or with an authors value that is not declared
    (sec002_start_problems refuses each of them).

    `target`: the target block of an external run (external.target_block). The baseline must then be a schema 5 log of
    an external TP-SEC-001 run. It is compared on the five declared fields of sec002_log.EXTERNAL_MATCH_FIELDS, and
    the label says that the comparison is on declared fields only."""
    from .engine import run_sec006, sec002, sec002_log, sec006_log
    if not settings.m4_baseline_log:
        return None, [BASELINE_REQUIRED], []
    data = _sec002_items()
    if data is None:
        return None, ["Baseline check: not made (the pool of the item file names the baseline items, and the item "
                      "file is not found)"], []
    external = target is not None
    try:
        base = sec002_log.read_baseline(settings.m4_baseline_log, [o["id"] for o in data["objectives"]],
                                        pubkey=settings.m4_baseline_pubkey, external=external)
    except ValueError as e:
        return None, [f"Baseline check: {e}"], [str(e)]
    sig, variance = base["signature"], base.get("M4_per_run_variance")
    lines = [f"Baseline signature: verified (public key SHA-256 {sig['public_key_sha256']})" if sig["verified"]
             else f"Baseline signature: not checked ({sig['reason']})",
             f"Baseline mode: {base['mode']}; item set: {base['item_set']}",
             f"Baseline rescore with this scorer: {base['mismatches']} of {base['runs_rescored']} runs score "
             "differently now",
             f"M1 bar: {'not set' if settings.m1_value is None else settings.m1_value}; the run-to-run variance of the "
             f"baseline (its M4 value): {'not computed' if variance is None else variance}"]
    if external:
        base["differs_from_run"] = sec002_log.external_differences(base, sec002_log.external_run_facts(target))
    else:
        base["differs_from_run"] = sec006_log.baseline_differences(base, sec006_log.run_facts(
            settings.model_id, variant, run_sec006.prompt_sha256(variant), settings.routing()))
    match = not base["differs_from_run"]
    authors = data.get("authors")
    label = (None if sec002.authorship_problem(authors)
             else sec002_log.run_label(match, base, authors=authors, external=external))
    if match and external:
        lines.append("Baseline match: yes (the same profile hash, adapter, endpoint host, declared model and declared "
                     f"version as the run; {sec002_log.EXTERNAL_MATCH})")
    elif match:
        lines.append("Baseline match: yes (the same model ID, variant, system prompt hash, routing locks and tool "
                     "definitions hash as the run)")
    else:
        lines += ["Baseline match: no"] + [f"  - {p}" for p in
                                           sec002_log.difference_problems(base["differs_from_run"])]
    if settings.mode == "evidence":
        problems = sec002_log.evidence_problems(base)
        needs = sec002_log.EVIDENCE_BASELINE_EXTERNAL if external else sec002_log.EVIDENCE_BASELINE
        return label, lines, problems + ([f"evidence mode needs {needs}"] if problems else [])
    if not match:
        lines.append(f"  Explore mode allows this. The run label adds '{sec002_log.ANOTHER_TARGET}'.")
    if base["mismatches"]:
        lines.append(f"  Explore mode allows this. The run label adds '{sec002_log.RESCORED_DIFFERS}'.")
    return label, lines, []


def sec002_start_problems(settings, variant="standard", target=None):
    """What a TP-SEC-002 run needs besides the SRD-1 conditions: the M1 bar with its reason (verify refuses a log
    without them), the item file and a declaration of the channel inputs with its families (sec002_channel_inputs),
    an authors value of the item file that is declared (sec002.AUTHORSHIP; the run label takes its part from it), and a
    baseline log that sec002_baseline accepts. The engine runs the standard variant with tools off and raises on
    anything else, so the preflight refuses the positive control and tools on first. `target`: the target block of an
    external run, which has no variant and no tools of SRD-1 (run_external refuses --positive-control itself), and
    whose declaration of the channel inputs is the setting channel_inputs."""
    from .engine import sec002
    problems = []
    if settings.m1_value is None or not settings.m1_reason:
        problems.append("reviewer_values.M1 needs a value and a reason before a run (verify refuses a log without them)")
    data = _sec002_items()
    if data is not None and sec002.authorship_problem(data.get("authors")):
        problems.append(f"the item file: {sec002.authorship_problem(data.get('authors'))}")
    if target is None and variant != "standard":
        problems.append(f"the variant {variant} does not apply to TP-SEC-002: the engine runs SRD-1 minimal with its "
                        "system prompt (the standard variant) only")
    if getattr(settings, "tools_enabled", False):
        problems.append("TP-SEC-002 runs with tools off (SRD-1 sends no tool definition to the model): set "
                        "tools_enabled to false")
    e2 = sec002_channel_inputs(settings.channel_inputs if target is not None else None)[1]
    return problems + e2 + sec002_baseline(settings, variant, target)[2]


def _inspect_missing():
    from .engine import spike
    return None if spike.inspect_available() else "the run engine inspect_ai is not installed"


def main(argv=None, *, input_fn=input, gateway_factory=_default_gateway, executor=_default_executor,
         inspect_check=_inspect_missing, external_executor=_default_external_executor):
    ap = argparse.ArgumentParser(prog="faias_harness.preflight", description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True, help="path to a target profile YAML")
    ap.add_argument("--settings", help="path to the settings YAML of an engine run")
    ap.add_argument("--start", action="store_true",
                    help="check the start conditions; with --settings, probe, ask for `yes` and run")
    ap.add_argument("--probes", action="store_true", help="with --settings: make the two probe calls and start nothing")
    ap.add_argument("--sign-key", help="with --settings and --start: the run-signing private key (outside the repository)")
    ap.add_argument("--run-dir", help="with --settings and --start: where the run files go (default runs/<time>)")
    ap.add_argument("--positive-control", action="store_true",
                    help="with --settings and --start: run SRD-1 with the weakened system prompt, which the "
                         "harness must find failing (TP-AGT-012: run the prompt-only gate, which a bypass must pass; "
                         "TP-DAT-009: run the display-layer filter and the service credential, which must give "
                         "crossings)")
    ap.add_argument("--variant", choices=("standard", "positive_control", "prompt_free_gate"),
                    help="with --settings: the run variant; prompt_free_gate applies to TP-AGT-012 only")
    ap.add_argument("--selftest-log", help="with --settings: a containment self-test log, or a folder that holds one "
                                           "(default: the folders of FAIAS_HARNESS_RUNS, or runs/)")
    ap.add_argument("--selftest-pubkey", help="with --settings: the public key file that the self-test log's signature "
                                              "must verify against (default: selftest_pubkey in the settings; without "
                                              "one, the key is not pinned)")
    a = ap.parse_args(argv)
    try:
        profile = load_profile(a.profile)
    except ProfileError as e:
        print("PROFILE INVALID:")
        for p in e.problems:
            print(f"  - {p}")
        return 2
    settings = None
    if (a.probes or a.sign_key or a.run_dir or a.positive_control or a.variant) and not a.settings:
        print("--probes, --sign-key, --run-dir, --positive-control and --variant need --settings")
        return 2
    if a.variant and a.positive_control:
        print("--variant and --positive-control cannot be given together")
        return 2
    if a.settings:
        try:
            settings = load_settings(a.settings)
        except SettingsError as e:
            print("SETTINGS INVALID. No run starts:")
            for p in e.problems:
                print(f"  - {p}")
            return 2
    report(profile, load_needs())
    if settings is not None and settings.kind == "external":
        return run_external(a, profile, settings, input_fn=input_fn, executor=external_executor,
                            inspect_check=inspect_check)
    est = None
    variant = a.variant or ("positive_control" if a.positive_control else "standard")
    if variant == "prompt_free_gate" and (settings is None or settings.procedure != AGT012):
        print("--variant prompt_free_gate applies to the procedure TP-AGT-012 only")
        return 2
    sec006_label, sec002_label, base_problems, isolation = None, None, [], None
    if settings is not None:
        print()
        from . import selftest
        from .engine import run_agt012, run_dat009, run_sec002, run_sec006, spike
        if settings.procedure == SEC006:
            est = run_sec006.estimate(settings)
        elif settings.procedure == AGT012:
            est = run_agt012.estimate(settings)
        elif settings.procedure == DAT009:
            est = run_dat009.estimate(settings)
        elif settings.procedure == SEC002:
            est = run_sec002.estimate(settings) if _sec002_items() is not None else None
        else:
            est = spike.estimate(settings)
        settings_report(settings, est)
        isolation = selftest.current(selftest_log=a.selftest_log,
                                     pubkey=a.selftest_pubkey or settings.selftest_pubkey)
        print(selftest.preflight_line(isolation))
        if settings.procedure == SEC006:
            sec006_label, lines, base_problems = sec006_baseline(settings, variant)
            for line in lines:
                print(line)
        if settings.procedure == SEC002:
            lines = sec002_channel_inputs()[0]
            sec002_label, more, base_problems = sec002_baseline(settings, variant)
            for line in lines + more:
                print(line)
    if not a.start and not a.probes:
        if settings is not None:
            if base_problems and settings.mode == "evidence":
                print()
                print(f"{'M4 ' if settings.procedure == SEC006 else ''}BASELINE REFUSED. No run starts. Fix these "
                      "points first:")
                for p in base_problems:
                    print(f"  - {p}")
                return 2
            print("Probes: not run (use --probes, or --start)")
        return 0
    print()
    problems = start_problems(profile, settings)
    if settings is not None and settings.procedure == SEC002 and not settings.m4_baseline_log:
        problems.append(BASELINE_REQUIRED)          # --probes and --start: the run has no measure without it
    if settings is not None and a.start:
        from .engine import runlog
        problems += runlog.key_problems(a.sign_key) if a.sign_key else ["no signing key given (--sign-key)"]
        missing = inspect_check()
        if missing:
            problems.append(missing)
        if settings.procedure == SEC006:
            problems += sec006_start_problems(settings, variant)
        if settings.procedure == AGT012:
            problems += agt012_start_problems(settings)
        if settings.procedure == DAT009:
            problems += dat009_start_problems(settings, variant)
        if settings.procedure == SEC002:
            problems += sec002_start_problems(settings, variant)
        from . import selftest
        problems += selftest.gate_problems(settings.procedure, isolation, settings.mode)   # TP-AGT-012, TP-DAT-009
    if a.probes and not a.start:
        problems = [p for p in problems if "attestation" not in p and "signing" not in p]
    if problems:
        print("START REFUSED. No run starts. Fix these points first:" if a.start else "PROBES REFUSED. Fix these points first:")
        for p in problems:
            print(f"  - {p}")
        return 2
    if settings is None:
        print("Start conditions are met.")
        print("No run starts: a profile alone names no run. Give --settings to run the engine.")
        return 3
    from .engine import spike
    gateway = gateway_factory(settings)
    probes = spike.run_probes(gateway)
    if hasattr(getattr(gateway, "egress", None), "close"):
        gateway.egress.close()
    print_probes(probes)
    if not a.start:
        return 0
    print()
    print("Start conditions are met.")
    if settings.procedure == SEC006:
        from .engine import run_sec006
        sends, label = f"sends at most {run_sec006.planned_calls(settings)} model calls", sec006_label
    elif settings.procedure == AGT012:
        from .engine import agt012_log
        sends, label = agt012_sends(settings), agt012_log.LABEL
    elif settings.procedure == DAT009:
        from .engine import dat009_log
        sends, label = dat009_sends(settings), dat009_log.LABEL
    elif settings.procedure == SEC002:
        from .engine import run_sec002
        sends, label = f"sends at most {run_sec002.planned_calls(settings)} model calls", sec002_label
    else:
        sends = f"sends {len(spike.run_items(settings)) * settings.runs_per_item} model calls"
        label = spike.sec001.LABELS[settings.item_set]
    print(f"Item set {settings.item_set}. This run {sends} ({settings.runs_per_item} runs per item) to {settings.model_id} on {', '.join(settings.only) or 'any host'}. "
          f"Worst-case cost USD {est:.4f}; cap USD {settings.cap_usd}. The result is labelled '{label}'.")
    if variant == "positive_control" and settings.procedure == AGT012:
        print("Variant: positive control (the prompt-only gate; the orchestrator gate is off). A bypass must pass.")
    elif variant == "positive_control" and settings.procedure == DAT009:
        print("Variant: positive control (the display-layer filter on the pipeline arm and the service credential on "
              "the tool arm). The harness must find crossings on both paths.")
    elif variant == "positive_control":
        print("Variant: positive control (the weakened system prompt, no rules). The harness must find successes.")
    elif variant == "prompt_free_gate":
        print("Variant: prompt_free_gate (the weakened prompt, the orchestrator gate on). Every gated attempt must hold.")
    answer = input_fn("Type yes to start the run (anything else cancels): ").strip()
    if answer != "yes":
        print("Run not started: `yes` was not typed.")
        return 4
    confirmation = {"typed": "yes", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    run_dir = Path(a.run_dir) if a.run_dir else REPO / "runs" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    exec_kwargs = dict(settings=settings, run_dir=run_dir, sign_key=a.sign_key, confirmation=confirmation,
                       probes=probes, prior_spent_usd=gateway.cap.spent_usd, variant=variant)
    if settings.procedure == AGT012:
        exec_kwargs.update(selftest_log=a.selftest_log, selftest_pubkey=a.selftest_pubkey,
                           delays_ms=settings.containment_plan()[0])
    if settings.procedure == DAT009:
        exec_kwargs.update(selftest_log=a.selftest_log, selftest_pubkey=a.selftest_pubkey)
    result = executor(**exec_kwargs)
    s = result.summary
    print(f"Run {result.status}. Calls {s.get('model_calls')}, cost USD {s.get('cost_usd')}. Label: {s.get('label')}.")
    print(f"Log: {result.log_path}\nSignature: {result.sig_path}")
    return 0 if result.status == "complete" else 5


if __name__ == "__main__":
    sys.exit(main())
