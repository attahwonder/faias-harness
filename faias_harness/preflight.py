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
has no model judge), shows the result, and starts the run only after the owner types `yes`. With --probes it makes
the two probe calls and starts nothing. For a TP-SEC-006 run, the preflight verifies the signature of the M4 baseline
log against the key that `m4_baseline_pubkey` names, and refuses a key inside the repository or a log that fails. In
evidence mode the preflight and --start also refuse a baseline log whose model ID, variant, system prompt hash or
routing locks (`only`, `quantizations`) differ from the run, and name each field that differs, or a baseline log that
is not in evidence mode.
With a settings file of SRD-1 it also prints one line on the containment self-test (faias_harness/selftest): the result,
time and commit of the newest self-test log (--selftest-log, or the folders of FAIAS_HARNESS_RUNS, or runs/), and
whether its code manifest matches this checkout. For TP-AGT-012 and TP-DAT-009, --start refuses without a verified,
passing log of this code. For other procedures the line is information only.

With --settings <settings.yaml> of `kind: external` (version 0.5), it checks an external run instead: TP-SEC-001
against a deployed staging agent, through the channel adapter that the profile names (docs/ARCHITECTURE.md,
section 13). It prints the adapter, the endpoint host, what the channel lets the harness observe, the declared
model and version, the calls against the call budget, and whether the key variable is present (never its value).
--probes makes one channel probe (one benign message). --start makes the probe, refuses if it gets no usable
reply, and starts the run only after the owner types `yes`.

Exit codes: 0 table printed, or a run finished; 2 start refused or profile or settings invalid; 3 start
conditions met, but a profile alone starts no run; 4 the owner did not type `yes`; 5 the run stopped at the cap
or on an error.
"""
import argparse
import os
import sys
import time
from pathlib import Path

import yaml

from .profile import REPO, ProfileError, load_profile
from .settings import SEC006, SettingsError, load_settings

NEEDS_FILE = REPO / "records" / "capability_needs.yaml"
RECORDS_DIR = REPO / "records"


def load_needs(path=NEEDS_FILE):
    """Return {procedure: [needed capabilities]}. Only the capabilities that both passes marked as needed count."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return {tp: list(entry["needed"]) for tp, entry in data["needs"].items()}


def open_entries(path=NEEDS_FILE):
    """Return {procedure: {pass_1: [...], pass_2: [...]}} for the procedures where the two passes differ."""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
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
        rec = yaml.safe_load(p.read_text(encoding="utf-8"))
        keys += [t["firm_value_key"] for t in rec["thresholds"] if t.get("firm_value_key")]
    return keys


def declared_firm_values(profile, keys):
    """Number of keys with a non-null value in the profile's firm-values file, or None if there is no file."""
    if profile.firm_values is None or not profile.firm_values.is_file():
        return None
    data = yaml.safe_load(profile.firm_values.read_text(encoding="utf-8")) or {}
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
    problems = attestation_problems(profile) + registry.problems(profile)
    if registry.is_external(profile.adapter):
        env = registry.key_env(profile)
        if env and not os.environ.get(env):
            problems.append(f"the environment variable {env} is not set")
    if settings.call_budget < 2:
        problems.append("the call budget allows fewer than two calls (the channel probe and one item)")
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
    """With tools on (since 0.6): the refund gate needs its declared values, and TP-SEC-006 with tools on is not
    built yet. With tools off: none."""
    if not getattr(settings, "tools_enabled", False):
        return []
    from .engine import spike
    spike._srd1()
    from targets.srd1.app import gate
    problems = [f"{p} (targets/srd1/risk_appetite.yaml): SRD-1 does not enable its tool"
                for p in gate.declared_problems()]
    if settings.procedure == SEC006:
        problems.append("TP-SEC-006 with tools on is not built yet: its engine does not start the tool emulator")
    return problems


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
    else:
        print(f"M4 bar: {m4}; reason: {settings.m4_reason or 'not set'}")
    pin, pout = settings.price_in_per_m, settings.price_out_per_m
    print(f"Prices (USD per million tokens): in {pin if pin is not None else 'not set'}, "
          f"out {pout if pout is not None else 'not set'}")
    cap = f"USD {settings.cap_usd}" if settings.cap_usd is not None else "not set"
    if estimate_usd is None:
        print(f"Worst-case estimate: not available (prices not set); cap: {cap}")
    else:
        note = "  WARNING: the estimate is above the cap; the run may stop at the cap" \
            if settings.cap_usd is not None and estimate_usd > settings.cap_usd else ""
        print(f"Worst-case estimate for the whole run: USD {estimate_usd:.4f}; cap: {cap}{note}")
    print(f"Key variable: {settings.api_key_env} is {'present' if settings.key_present() else 'NOT present'}")
    if getattr(settings, "tools_enabled", False):
        print(f"Tools: on (fee_refund through the MCP tool emulator; its key is generated in the run and carried in "
              f"{settings.tool_key_env})")
        for p in tools_problems(settings):
            print(f"  - {p}")
    print("Judge-family check: not applicable (the engine has no model judge)")


def external_settings_report(profile, settings, est):
    """Print the settings block of an external run. It never prints the value of a key."""
    from .adapters import registry
    from .adapters.base import host_entry
    print(f"Engine run: TP-SEC-001 against an external target, item set {settings.item_set}")
    if registry.is_external(profile.adapter):
        entry = registry.ADAPTERS[profile.adapter]
        print(f"Target: adapter {profile.adapter}: {entry.channel}; endpoint host {host_entry(profile.endpoint) if profile.endpoint else 'not set'}")
        print(f"The harness observes: {entry.observes}")
    print(f"Mode: {settings.mode}")
    print(f"Settings hash: {settings.digest()}")
    print(f"Declared model: {settings.declared_model_id or 'not declared'}; declared target version: "
          f"{settings.declared_target_version or 'not declared'} (declared by the operator, not observed)")
    print(f"Runs per item: {settings.runs_per_item} (FAIAS minimum 3)")
    m4 = "not set" if settings.m4_value is None else settings.m4_value
    print(f"M4 bar: {m4}; reason: {settings.m4_reason or 'not set'}")
    print(f"Calls in the run: {est['calls']} (plus 1 channel probe; retries are extra)")
    mc = f", max_calls {settings.max_calls}" if settings.max_calls is not None else ""
    print(f"Budget: cap USD {settings.cap_usd}; cost per call USD {settings.cost_per_call_usd} (the operator's "
          f"estimate){mc}; call budget {settings.call_budget}")
    note = "  WARNING: the calls are above the call budget; the run will stop at the cap" \
        if est["calls"] + 1 > settings.call_budget else ""
    print(f"Estimated cost for the whole run: USD {est['cost_usd']:.4f}{note}")
    print(f"Pacing: at least {settings.min_interval_ms} ms between calls; reply timeout {settings.reply_timeout_s} s; "
          f"stop after {settings.max_consecutive_errors} channel errors in a row")
    env = registry.key_env(profile) if registry.is_external(profile.adapter) else None
    if env:
        print(f"Key variable: {env} is {'present' if os.environ.get(env) else 'NOT present'}")
    else:
        print("Key variable: none needed by this adapter configuration")
    print("Judge-family check: not applicable (the engine has no model judge)")
    print("Not observed through the channel: a routing lock or a pinned model; the model, host, tokens and cost "
          "unless the response names them")


def print_external_probe(probe):
    print("Channel probe (one call):")
    if probe.get("ok"):
        print(f"  reply received from {probe.get('channel_host')} in {probe.get('latency_ms')} ms"
              + (f"; model named: {probe['served_model']}" if probe.get("served_model") else ""))
        print(f"  reply start: {probe.get('reply_start')!r}")
    else:
        print(f"  no usable reply (error: {probe.get('error') or 'empty reply'}"
              + (f", HTTP {probe['status']}" if probe.get("status") else "") + ")"
              + (f": {probe['detail']}" if probe.get("detail") else ""))


def _default_external_executor(**kw):
    from .engine import external
    return external.execute(**kw)


def run_external(a, profile, settings, *, input_fn, executor, inspect_check):
    """The preflight of an external run: report, probe, ask for `yes`, run. Returns the exit code."""
    from .adapters import registry
    from .adapters.base import CallBudget
    from .engine import external
    reasons = registry.problems(profile)
    est = external.estimate(settings, profile=profile)
    print()
    external_settings_report(profile, settings, est)
    if reasons:
        print()
        print("PROFILE CANNOT DRIVE AN EXTERNAL RUN. No run starts:")
        for p in reasons:
            print(f"  - {p}")
        return 2
    if not a.start and not a.probes:
        print("Channel probe: not run (use --probes, or --start)")
        return 0
    print()
    problems = external_start_problems(profile, settings)
    if a.start:
        from .engine import runlog
        problems += runlog.key_problems(a.sign_key) if a.sign_key else ["no signing key given (--sign-key)"]
        missing = inspect_check()
        if missing:
            problems.append(missing)
        if a.positive_control:
            problems.append("--positive-control does not apply to an external run: the harness cannot weaken a "
                            "deployed agent; run a second staging agent with no rules, with its own profile")
        if settings.m4_value is None or not settings.m4_reason:
            problems.append("reviewer_values.M4 needs a value and a reason before a run (verify refuses a log without them)")
    # The probe of an external run reaches the firm's agent, so it needs the isolation attestation too.
    if problems:
        print("START REFUSED. No run starts. Fix these points first:" if a.start else "PROBES REFUSED. Fix these points first:")
        for p in problems:
            print(f"  - {p}")
        return 2
    budget = CallBudget(settings.cap_usd, settings.cost_per_call_usd, settings.max_calls)
    egress = external.make_egress(profile)
    try:
        probe = external.channel_probe(external.make_target(profile, settings, egress, budget))
    finally:
        egress.close()
    print_external_probe(probe)
    if not a.start:
        return 0
    if not probe.get("ok"):
        print()
        print("START REFUSED: the channel probe got no usable reply. Fix the channel first.")
        return 2
    print()
    print("Start conditions are met.")
    print(f"Item set {settings.item_set}. This run sends {est['calls']} calls ({settings.runs_per_item} runs per item) "
          f"through the {profile.adapter} channel to {profile.endpoint}. Call budget {settings.call_budget} "
          f"({budget.calls} used by the probe); estimated cost USD {est['cost_usd']:.4f}; cap USD {settings.cap_usd}. "
          f"The result is labelled '{external.sec001.LABELS[settings.item_set]}'.")
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
    if kw["settings"].procedure == SEC006:
        from .engine import run_sec006
        return run_sec006.execute(**kw)
    from .engine import spike
    return spike.execute(**kw)


def sec006_baseline(settings, variant="standard"):
    """The check of the M4 baseline log of a TP-SEC-006 run: (run label, lines to print, problems). A log that cannot
    be read, a public key file inside the repository, and a signature that does not verify are problems. In evidence
    mode, a log whose model ID, variant, system prompt hash or routing locks differ from the run is a problem too:
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
        settings.model_id, variant, run_sec006.prompt_sha256(variant), settings.routing()))
    match = not base["differs_from_run"]
    label = sec006_log.run_label(match, base)
    if base.get("item_set") != sec006_log.FULL_SET:
        lines.append(f"  The baseline is not on the full TP-SEC-001 set. The run label adds '{sec006_log.ON_SPIKE}'.")
    if match:
        lines.append("M4 baseline match: yes (the same model ID, variant, system prompt hash and routing locks as the "
                     "run)")
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
    where a key is named. In evidence mode, the baseline log must be of the same model, variant, system prompt and
    routing locks as the run, and in evidence mode itself (sec006_baseline)."""
    problems = [f"reviewer_values.{m} needs a value and a reason before a run (verify refuses a log without them)"
                for m, v, r in (("M3", settings.m3_value, settings.m3_reason), ("M4", settings.m4_value, settings.m4_reason))
                if v is None or not r]
    return problems + sec006_baseline(settings, variant)[2]


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
                         "harness must find failing")
    ap.add_argument("--selftest-log", help="with --settings: a containment self-test log, or a folder that holds one "
                                           "(default: the folders of FAIAS_HARNESS_RUNS, or runs/)")
    a = ap.parse_args(argv)
    try:
        profile = load_profile(a.profile)
    except ProfileError as e:
        print("PROFILE INVALID:")
        for p in e.problems:
            print(f"  - {p}")
        return 2
    settings = None
    if (a.probes or a.sign_key or a.run_dir or a.positive_control) and not a.settings:
        print("--probes, --sign-key, --run-dir and --positive-control need --settings")
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
    variant = "positive_control" if a.positive_control else "standard"
    sec006_label, base_problems, isolation = None, [], None
    if settings is not None:
        print()
        from . import selftest
        from .engine import run_sec006, spike
        est = run_sec006.estimate(settings) if settings.procedure == SEC006 else spike.estimate(settings)
        settings_report(settings, est)
        isolation = selftest.current(selftest_log=a.selftest_log)
        print(selftest.preflight_line(isolation))
        if settings.procedure == SEC006:
            sec006_label, lines, base_problems = sec006_baseline(settings, variant)
            for line in lines:
                print(line)
    if not a.start and not a.probes:
        if settings is not None:
            if base_problems and settings.mode == "evidence":
                print()
                print("M4 BASELINE REFUSED. No run starts. Fix these points first:")
                for p in base_problems:
                    print(f"  - {p}")
                return 2
            print("Probes: not run (use --probes, or --start)")
        return 0
    print()
    problems = start_problems(profile, settings)
    if settings is not None and a.start:
        from .engine import runlog
        problems += runlog.key_problems(a.sign_key) if a.sign_key else ["no signing key given (--sign-key)"]
        missing = inspect_check()
        if missing:
            problems.append(missing)
        if settings.procedure == SEC006:
            problems += sec006_start_problems(settings, variant)
        from . import selftest
        problems += selftest.gate_problems(settings.procedure, isolation)     # TP-AGT-012 and TP-DAT-009 only
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
        calls, label = f"at most {run_sec006.planned_calls(settings)}", sec006_label
    else:
        calls, label = len(spike.run_items(settings)) * settings.runs_per_item, spike.sec001.LABELS[settings.item_set]
    print(f"Item set {settings.item_set}. This run sends {calls} model calls ({settings.runs_per_item} runs per item) to {settings.model_id} on {', '.join(settings.only) or 'any host'}. "
          f"Worst-case cost USD {est:.4f}; cap USD {settings.cap_usd}. The result is labelled '{label}'.")
    if a.positive_control:
        print("Variant: positive control (the weakened system prompt, no rules). The harness must find successes.")
    answer = input_fn("Type yes to start the run (anything else cancels): ").strip()
    if answer != "yes":
        print("Run not started: `yes` was not typed.")
        return 4
    confirmation = {"typed": "yes", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    run_dir = Path(a.run_dir) if a.run_dir else REPO / "runs" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    result = executor(settings=settings, run_dir=run_dir, sign_key=a.sign_key, confirmation=confirmation,
                      probes=probes, prior_spent_usd=gateway.cap.spent_usd, variant=variant)
    s = result.summary
    print(f"Run {result.status}. Calls {s.get('model_calls')}, cost USD {s.get('cost_usd')}. Label: {s.get('label')}.")
    print(f"Log: {result.log_path}\nSignature: {result.sig_path}")
    return 0 if result.status == "complete" else 5


if __name__ == "__main__":
    sys.exit(main())
