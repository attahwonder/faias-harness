"""Preflight: compare a target profile with the capability needs of each procedure. No network. No run.

  py -3.14 -m faias_harness.preflight --profile <profile.yaml> [--start]

It prints, for each procedure with a record, the capabilities it needs, the capabilities the profile lacks, and
a result: full, partly or none. Full: every need is present. None: the procedure needs `conversation` and the
profile lacks it, or every need is missing. Partly: anything else.
The capability needs are a DRAFT (records/capability_needs.yaml): the needs that two independent model passes
agree on, or that a third reading settled where they differed. A procedure with an open entry is not settled.

With --start, it refuses unless the isolation attestation file exists and is not empty, and the settings
provider, model_id and cap_usd are set. With a profile alone, it starts nothing, even when every condition is met.

With --settings <settings.yaml> (version 0.3), it also checks the settings file of the engine spike
(TP-SEC-001 on SRD-1 minimal) and prints the provider, model, host, precision, routing locks, the worst-case
estimate against the cap, and whether the key variable is present (never its value). An evidence-mode settings
file that lacks any of the five routing locks is refused. With --start it checks the signing key, makes two probe
calls (a tool-call probe and a reasoning-text probe; the judge-family check is not applicable because the spike
has no model judge), shows the result, and starts the run only after the owner types `yes`. With --probes it makes
the two probe calls and starts nothing.

Exit codes: 0 table printed, or a run finished; 2 start refused or profile or settings invalid; 3 start
conditions met, but a profile alone starts no run; 4 the owner did not type `yes`; 5 the run stopped at the cap
or on an error.
"""
import argparse
import sys
import time
from pathlib import Path

import yaml

from .profile import REPO, ProfileError, load_profile
from .settings import SettingsError, load_settings

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


def start_problems(profile, settings=None):
    """The reasons why a run may not start. An empty list means every start condition is met.

    With `settings` (the engine settings file), provider, model_id and cap_usd come from it, and the prices, the
    key variable and the signing key are checked too. Without it, they come from the profile (version 0.2 rules).
    """
    problems = []
    att = profile.attestation_file
    if att is None:
        problems.append("the profile names no isolation attestation file (attestation_file)")
    elif not att.is_file():
        problems.append(f"the isolation attestation file does not exist: {att}")
    elif not att.read_text(encoding="utf-8", errors="replace").strip():
        problems.append(f"the isolation attestation file is empty: {att}")
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
    if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
        problems.append("settings.cap_usd is not set to a number above 0")
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
    print("Engine spike: TP-SEC-001 on SRD-1 minimal")
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
    print("Judge-family check: not applicable (the spike has no model judge)")


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
    from .engine import spike
    return spike.execute(**kw)


def _inspect_missing():
    from .engine import spike
    return None if spike.inspect_available() else "the run engine inspect_ai is not installed"


def main(argv=None, *, input_fn=input, gateway_factory=_default_gateway, executor=_default_executor,
         inspect_check=_inspect_missing):
    ap = argparse.ArgumentParser(prog="faias_harness.preflight", description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True, help="path to a target profile YAML")
    ap.add_argument("--settings", help="path to the settings YAML of the engine spike")
    ap.add_argument("--start", action="store_true",
                    help="check the start conditions; with --settings, probe, ask for `yes` and run")
    ap.add_argument("--probes", action="store_true", help="with --settings: make the two probe calls and start nothing")
    ap.add_argument("--sign-key", help="with --settings and --start: the run-signing private key (outside the repository)")
    ap.add_argument("--run-dir", help="with --settings and --start: where the run files go (default runs/<time>)")
    ap.add_argument("--positive-control", action="store_true",
                    help="with --settings and --start: run SRD-1 with the weakened system prompt, which the "
                         "harness must find failing")
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
    est = None
    if settings is not None:
        print()
        from .engine import spike
        est = spike.estimate(settings)
        settings_report(settings, est)
    if not a.start and not a.probes:
        if settings is not None:
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
    if a.probes and not a.start:
        problems = [p for p in problems if "attestation" not in p and "signing" not in p]
    if problems:
        print("START REFUSED. No run starts. Fix these points first:" if a.start else "PROBES REFUSED. Fix these points first:")
        for p in problems:
            print(f"  - {p}")
        return 2
    if settings is None:
        print("Start conditions are met.")
        print("No run starts: a profile alone names no run. Give --settings to run the engine spike.")
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
    calls = len(spike.run_items(settings)) * settings.runs_per_item
    print(f"Item set {settings.item_set}. This run sends {calls} model calls ({settings.runs_per_item} runs per item) to {settings.model_id} on {', '.join(settings.only) or 'any host'}. "
          f"Worst-case cost USD {est:.4f}; cap USD {settings.cap_usd}. The result is labelled '{spike.sec001.LABELS[settings.item_set]}'.")
    if a.positive_control:
        print("Variant: positive control (the weakened system prompt, no rules). The harness must find successes.")
    answer = input_fn("Type yes to start the run (anything else cancels): ").strip()
    if answer != "yes":
        print("Run not started: `yes` was not typed.")
        return 4
    confirmation = {"typed": "yes", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    run_dir = Path(a.run_dir) if a.run_dir else REPO / "runs" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    result = executor(settings=settings, run_dir=run_dir, sign_key=a.sign_key, confirmation=confirmation,
                      probes=probes, prior_spent_usd=gateway.cap.spent_usd,
                      variant="positive_control" if a.positive_control else "standard")
    s = result.summary
    print(f"Run {result.status}. Calls {s.get('model_calls')}, cost USD {s.get('cost_usd')}. Label: {s.get('label')}.")
    print(f"Log: {result.log_path}\nSignature: {result.sig_path}")
    return 0 if result.status == "complete" else 5


if __name__ == "__main__":
    sys.exit(main())
