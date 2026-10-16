"""Preflight: compare a target profile with the capability needs of each procedure. No network. No run.

  py -3.14 -m faias_harness.preflight --profile <profile.yaml> [--start]

It prints, for each procedure with a record, the capabilities it needs, the capabilities the profile lacks, and
a result: full, partly or none. Full: every need is present. None: the procedure needs `conversation` and the
profile lacks it, or every need is missing. Partly: anything else.
The capability needs are a DRAFT (records/capability_needs.yaml): the needs that two independent model passes
agree on, or that a third reading settled where they differed. A procedure with an open entry is not settled.

With --start, it refuses unless the isolation attestation file exists and is not empty, and the settings
provider, model_id and cap_usd are set. Version 0.2 has no run engine. Even when every condition is met,
it starts nothing.

Exit codes: 0 table printed; 2 start refused or profile invalid; 3 start conditions met, but v0.2 has no run engine.
"""
import argparse
import sys
from pathlib import Path

import yaml

from .profile import REPO, ProfileError, load_profile

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


def start_problems(profile):
    """The reasons why a run may not start. An empty list means every start condition is met."""
    problems = []
    att = profile.attestation_file
    if att is None:
        problems.append("the profile names no isolation attestation file (attestation_file)")
    elif not att.is_file():
        problems.append(f"the isolation attestation file does not exist: {att}")
    elif not att.read_text(encoding="utf-8", errors="replace").strip():
        problems.append(f"the isolation attestation file is empty: {att}")
    st = profile.settings
    for k in ("provider", "model_id"):
        if not st.get(k):
            problems.append(f"settings.{k} is not set")
    cap = st.get("cap_usd")
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


def main(argv=None):
    ap = argparse.ArgumentParser(prog="faias_harness.preflight", description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True, help="path to a target profile YAML")
    ap.add_argument("--start", action="store_true", help="check the start conditions; v0.2 never starts a run")
    a = ap.parse_args(argv)
    try:
        profile = load_profile(a.profile)
    except ProfileError as e:
        print("PROFILE INVALID:")
        for p in e.problems:
            print(f"  - {p}")
        return 2
    report(profile, load_needs())
    if not a.start:
        return 0
    print()
    problems = start_problems(profile)
    if problems:
        print("START REFUSED. No run starts. Fix these points first:")
        for p in problems:
            print(f"  - {p}")
        return 2
    print("Start conditions are met.")
    print("No run starts: no run engine in v0.2.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
