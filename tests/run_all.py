"""Run every test of the harness from one command.

  py -3.14 -X utf8 tests/run_all.py --faias <path to the FAIAS v1.3.4 tree>

Steps:
  1. rebuild the records twice: the output is byte-identical, and equal to the files in the repository
  2. check_records passes
  3. the seeded-fault test passes (8 faults caught: 4 in records, 4 in the capability needs)
  4. preflight: the SRD-1 profile gives 45 full; the example profiles give stable, correct counts;
     --start refuses without an attestation file and without settings; a profile alone starts nothing;
     with a settings file: the shipped explore files (TP-SEC-001, TP-SEC-006 and TP-AGT-012) are accepted, an evidence file that lacks any of the five
     routing locks is refused, and --start refuses without the key variable; TP-AGT-012 --start refuses without a
     containment self-test that passes; with the external settings file
     (version 0.5): each external example profile is accepted, an evidence file without the declared model and
     version is refused, and --start refuses without the attestation and the key variable
  5. the firm-values file of SRD-1 lists exactly the keys of the records; each declared value is a number with a
     basis, and firm.TP-AGT-012.M3 equals the time-to-halt of risk_appetite.yaml
  6. the sweep for barred terms and key-like strings finds 0 hits
  7. the unit tests of the engine (settings, SRD-1 minimal, gateway, scorers of TP-SEC-001 and TP-SEC-006, run log,
     signature, end to end against a local stand-in model host; no outside network). They also cover TP-AGT-012,
     TP-DAT-009, the retrieval of SRD-1 and the local tool emulator. Each failed test is named on
     a "failed:" line. The "[test note]" lines of
     the tests follow the count: for example, how many signed live logs the live-log tests read (FAIAS_HARNESS_RUNS).
     The two example logs in examples/logs/ are read without that variable.
  8. the containment self-test (python -m faias_harness.selftest) into a temporary folder: result PASS, 0 guard trips;
     the step prints the probe count

Step 4 also checks the shipped TP-DAT-009 file. The preflight accepts it, and its item set check finds 0 problems.
Its --start refuses without a self-test that passes. It also refuses the display filter in the standard variant.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"}
PY = [sys.executable, "-X", "utf8"]
RESULT = re.compile(r"Result for (\d+) procedures: full (\d+), partly (\d+), none (\d+)")
failures = []


def run(args):
    p = subprocess.run(PY + args, cwd=REPO, env=ENV, capture_output=True, text=True, encoding="utf-8")
    return p.returncode, p.stdout + p.stderr


def preflight(profile, *extra):
    return run(["-m", "faias_harness.preflight", "--profile", str(profile), *extra])


def expect(ok, message):
    if not ok:
        failures.append(message)
        print("  FAIL:", message)


def digest():
    h = hashlib.sha256()
    for p in sorted((REPO / "records").glob("*")):
        h.update(p.name.encode() + b"\0" + p.read_bytes())
    return h.hexdigest()


def expected_counts(capabilities):
    needs = yaml.safe_load((REPO / "records" / "capability_needs.yaml").read_text(encoding="utf-8"))["needs"]
    c = {"full": 0, "partly": 0, "none": 0}
    for n in (e["needed"] for e in needs.values()):
        missing = [x for x in n if x not in capabilities]
        if not missing:
            c["full"] += 1
        elif "conversation" in missing or len(missing) == len(n):
            c["none"] += 1
        else:
            c["partly"] += 1
    return len(needs), c["full"], c["partly"], c["none"]


def write_profile(path, attestation, **settings):
    s = {"provider": None, "model_id": None, "price_in_per_m": None, "price_out_per_m": None, "cap_usd": None}
    s.update(settings)
    doc = {"name": "temporary test profile", "adapter": "generic-http", "endpoint": "https://staging.example.test/v1",
           "trace_source": None, "capabilities": ["conversation"], "precondition_map": None, "firm_values": None,
           "attestation_file": str(attestation) if attestation else None, "settings": s}
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path


LOCKS = ("only", "allow_fallbacks", "zdr", "data_collection", "quantizations")


def settings_checks(tmp, att):
    """The preflight with a settings file, run as a command. No model call, no key."""
    base = write_profile(tmp / "engine_profile.yaml", att)
    rc, out = preflight(base, "--settings", "targets/srd1/settings.yaml")
    expect(rc == 0 and "Mode: explore" in out and "Judge-family check: not applicable" in out,
           f"the shipped explore settings were not accepted: exit {rc}, {out[-300:]}")
    evidence = {"provider": "openrouter", "model_id": "example-vendor/example-model", "mode": "evidence",
                "routing": {"only": ["examplehost"], "allow_fallbacks": False, "zdr": True,
                            "data_collection": "deny", "quantizations": ["fp8"]},
                "price_in_per_m": 1.0, "price_out_per_m": 2.0, "cap_usd": 1.0, "api_key_env": "OPENROUTER_API_KEY",
                "temperature": 0.7, "seed": 1000,
                "reviewer_values": {"M4": {"value": 0.5, "reason": "test value"}}}
    good = tmp / "evidence.yaml"
    good.write_text(yaml.safe_dump(evidence), encoding="utf-8")
    rc, out = preflight(base, "--settings", good)
    expect(rc == 0 and "Host (routing.only): examplehost" in out and "Precision (routing.quantizations): fp8" in out
           and "Worst-case estimate for the whole run: USD" in out and "cap: USD 1.0" in out,
           f"the evidence settings were not shown: exit {rc}, {out[-300:]}")
    for lock in LOCKS:
        bad = {**evidence, "routing": {k: v for k, v in evidence["routing"].items() if k != lock}}
        f = tmp / f"missing_{lock}.yaml"
        f.write_text(yaml.safe_dump(bad), encoding="utf-8")
        rc, out = preflight(base, "--settings", f, "--start")
        expect(rc == 2 and "SETTINGS INVALID" in out and f"routing.{lock}" in out, f"missing lock {lock} was not refused")
    env = {k: v for k, v in ENV.items() if k != "OPENROUTER_API_KEY"}
    p = subprocess.run(PY + ["-m", "faias_harness.preflight", "--profile", str(base), "--settings", str(good), "--start"],
                       cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", input="")
    expect(p.returncode == 2 and "OPENROUTER_API_KEY is not set" in p.stdout and "START REFUSED" in p.stdout,
           f"--start without the key variable: exit {p.returncode}, {p.stdout[-300:]}")
    rc, out = preflight(base, "--settings", "targets/srd1/settings_sec006.yaml")
    expect(rc == 0 and "Engine run: TP-SEC-006 on SRD-1 minimal" in out and "M3 bar" in out,
           f"the shipped TP-SEC-006 settings were not accepted: exit {rc}, {out[-300:]}")
    # For TP-AGT-012, the preflight accepts the shipped explore settings. Then --start refuses without a self-test
    # that passes. FAIAS_HARNESS_RUNS is emptied, so no self-test log is found. The item file ships, so no refusal
    # names it.
    rc, out = preflight(base, "--settings", "targets/srd1/settings_agt012.yaml")
    expect(rc == 0 and "Engine run: TP-AGT-012 on SRD-1 minimal" in out and "Containment: delays" in out,
           f"the shipped TP-AGT-012 settings were not accepted: exit {rc}, {out[-300:]}")
    env_no_runs = {**ENV, "FAIAS_HARNESS_RUNS": ""}
    p = subprocess.run(PY + ["-m", "faias_harness.preflight", "--profile", str(base),
                             "--settings", "targets/srd1/settings_agt012.yaml", "--start"],
                       cwd=REPO, env=env_no_runs, capture_output=True, text=True, encoding="utf-8", input="")
    expect(p.returncode == 2 and "START REFUSED" in p.stdout and "isolation self-test log" in p.stdout
           and "items/TP-AGT-012.json" not in p.stdout,
           f"TP-AGT-012 --start without a self-test was not refused: exit {p.returncode}, {p.stdout[-400:]}")
    print("   settings: explore files accepted (TP-SEC-001, TP-SEC-006, TP-AGT-012); 5 locks missing in evidence mode "
          "refused; --start refuses without the key variable; TP-AGT-012 --start refuses without a self-test that passes")
    dat009_checks(tmp, base, env_no_runs)


def dat009_checks(tmp, base, env_no_runs):
    """The checks of the shipped TP-DAT-009 settings. The preflight accepts them, and they name the stage and the
    arms. The item set check finds 0 problems. Then --start refuses without a self-test that passes. It also
    refuses the display filter in the standard variant. No model call, no key."""
    shipped = "targets/srd1/settings_dat009.yaml"
    rc, out = preflight(base, "--settings", shipped)
    expect(rc == 0 and "Engine run: TP-DAT-009 on SRD-1 minimal" in out and "Retrieval: filter stage pre_retrieval" in out
           and "arms pipeline, tool" in out, f"the shipped TP-DAT-009 settings were not accepted: exit {rc}, {out[-300:]}")
    rc, out = run(["-c", "from faias_harness.engine import dat009; d = dat009.load_data(); "
                         "print('problems:', len(dat009.check_items(dat009.load_set(), d[0], d[1])))"])
    expect(rc == 0 and "problems: 0" in out, f"the TP-DAT-009 item set check: exit {rc}, {out[-300:]}")
    display = tmp / "dat009_display.yaml"
    display.write_text(yaml.safe_dump({**yaml.safe_load((REPO / shipped).read_text(encoding="utf-8")),
                                       "retrieval_filter": "post_generation"}), encoding="utf-8")
    for f, want in ((shipped, "isolation self-test log"), (display, "the display filter is the positive control")):
        p = subprocess.run(PY + ["-m", "faias_harness.preflight", "--profile", str(base), "--settings", str(f), "--start"],
                           cwd=REPO, env=env_no_runs, capture_output=True, text=True, encoding="utf-8", input="")
        expect(p.returncode == 2 and "START REFUSED" in p.stdout and want in p.stdout,
               f"TP-DAT-009 --start with {Path(f).name} was not refused for '{want}': exit {p.returncode}, "
               f"{p.stdout[-400:]}")
    print("   TP-DAT-009: explore file accepted; item set check 0 problems; --start refuses without a passing self-test "
          "and refuses the display filter in the standard variant")


def firm_value_problems(firm_file=REPO / "targets" / "srd1" / "firm_values.yaml",
                        risk_file=REPO / "targets" / "srd1" / "risk_appetite.yaml", records=REPO / "records"):
    """Step 5. It returns the problems, the keys of the records and the declared keys. The file lists exactly the
    firm_value_key set of the records. A declared value is a number with a basis in `bases`. A basis names a declared
    value only. The time-to-halt has two homes, firm.TP-AGT-012.M3 and time_to_halt_s in the risk-appetite file. The
    two must agree."""
    fv_file = yaml.safe_load(Path(firm_file).read_text(encoding="utf-8"))
    fv, bases = fv_file["values"], fv_file.get("bases") or {}
    keys = set()
    for p in Path(records).glob("TP-*.yaml"):
        keys |= {t["firm_value_key"] for t in yaml.safe_load(p.read_text(encoding="utf-8"))["thresholds"] if t.get("firm_value_key")}
    declared = {k for k, v in fv.items() if v is not None}
    problems = []
    if set(fv) != keys:
        problems.append("the firm-values file does not list exactly the firm_value_key set of the records")
    if not all(isinstance(fv[k], (int, float)) and not isinstance(fv[k], bool) and str(bases.get(k) or "").strip()
               for k in declared):
        problems.append("a firm value is set with no basis, or is not a number")
    if not set(bases) <= declared:
        problems.append("a basis names a firm value that is not declared")
    ra = yaml.safe_load(Path(risk_file).read_text(encoding="utf-8"))["fee_refund"]
    if fv.get("firm.TP-AGT-012.M3") != ra.get("time_to_halt_s"):
        problems.append("firm.TP-AGT-012.M3 differs from time_to_halt_s in risk_appetite.yaml")
    return problems, keys, declared


EXTERNAL_EXAMPLES = (("copilot_studio", "directline"), ("responses_agent", "openai-responses"), ("a2a_agent", "a2a"),
                     ("json_app", "json-http"))


def external_checks(tmp):
    """The preflight with the external settings file, run as a command. No channel call, no key."""
    shipped = "targets/examples/external_settings.yaml"
    for name, adapter in EXTERNAL_EXAMPLES:
        profile = f"targets/examples/{name}.yaml"
        rc, out = preflight(profile)
        m = RESULT.search(out)
        expect(rc == 0 and m and tuple(map(int, m.groups())) == expected_counts({"conversation"}),
               f"{name}: counts {m and m.group(0)} differ from {expected_counts({'conversation'})}")
        rc, out = preflight(profile, "--settings", shipped)
        expect(rc == 0 and "against an external target" in out and f"adapter {adapter}" in out
               and "Channel probe: not run" in out, f"{name}: the external settings were not accepted: exit {rc}, {out[-300:]}")
    raw = yaml.safe_load((REPO / shipped).read_text(encoding="utf-8"))
    ev = tmp / "external_evidence.yaml"
    ev.write_text(yaml.safe_dump({**raw, "mode": "evidence"}), encoding="utf-8")
    rc, out = preflight("targets/examples/responses_agent.yaml", "--settings", ev)
    expect(rc == 2 and "SETTINGS INVALID" in out and "declared.model_id" in out and "declared.target_version" in out,
           f"external evidence settings without the declared model and version were not refused: exit {rc}")
    env = {k: v for k, v in ENV.items() if k != "EXAMPLE_AGENT_TOKEN"}
    p = subprocess.run(PY + ["-m", "faias_harness.preflight", "--profile", "targets/examples/responses_agent.yaml",
                             "--settings", shipped, "--start"],
                       cwd=REPO, env=env, capture_output=True, text=True, encoding="utf-8", input="")
    expect(p.returncode == 2 and "START REFUSED" in p.stdout and "attestation" in p.stdout
           and "EXAMPLE_AGENT_TOKEN is not set" in p.stdout,
           f"external --start without the attestation and the key variable: exit {p.returncode}, {p.stdout[-300:]}")
    print(f"   external: {len(EXTERNAL_EXAMPLES)} example profiles accepted; evidence without the declared model and "
          "version refused; --start refuses without the attestation and the key variable")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--faias", required=True, help="path to the FAIAS v1.3.4 tree")
    faias = str(Path(ap.parse_args().faias))

    print("1. rebuild idempotence")
    before = digest()
    rc1, out1 = run(["tools/build_records.py", "--faias", faias])
    d1 = digest()
    rc2, out2 = run(["tools/build_records.py", "--faias", faias])
    d2 = digest()
    expect(rc1 == 0 and rc2 == 0, f"build_records exit codes {rc1}, {rc2}: {out1[-200:]} {out2[-200:]}")
    expect(d1 == d2, "the second build differs from the first")
    expect(before == d1, "the records in the repository differ from a fresh build")
    print("  ", out2.strip().splitlines()[0] if out2.strip() else "no output")

    print("2. check_records")
    rc, out = run(["tools/check_records.py", "--faias", faias])
    expect(rc == 0 and "check_records: PASS" in out, f"check_records failed: {out[-400:]}")
    print("  ", out.strip().splitlines()[-1])

    print("3. seeded faults")
    rc, out = run(["tools/test_check_records.py", "--faias", faias])
    expect(rc == 0 and "8 seeded faults caught" in out, f"test_check_records failed: {out[-400:]}")
    print("  ", out.strip().splitlines()[-1])

    print("4. preflight")
    rc, out = preflight("targets/srd1/profile.yaml")
    m = RESULT.search(out)
    expect(rc == 0 and m and tuple(map(int, m.groups())) == (45, 45, 0, 0), f"SRD-1 profile: expected 45 full; got {m and m.group(0)}")
    print("   SRD-1:", m.group(0) if m else out[-200:])
    for name, caps in (("chat_only", ["conversation"]), ("chat_and_traces", ["conversation", "traces"])):
        profile = f"targets/examples/{name}.yaml"
        rc, out = preflight(profile)
        rc_b, out_b = preflight(profile)
        m = RESULT.search(out)
        expect(rc == 0 and out == out_b, f"{name}: the output is not stable")
        expect(m and tuple(map(int, m.groups())) == expected_counts(set(caps)), f"{name}: counts {m and m.group(0)} differ from {expected_counts(set(caps))}")
        print(f"   {name}:", m.group(0) if m else out[-200:])
    rc, out = preflight("targets/examples/chat_only.yaml", "--start")
    expect(rc == 2 and "START REFUSED" in out and "attestation" in out, "--start did not refuse without an attestation file")
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        att = tmp / "attestation.txt"
        att.write_text("This target is a test system with synthetic data only.\n", encoding="utf-8")
        empty = tmp / "empty.txt"
        empty.write_text("  \n", encoding="utf-8")
        good = dict(provider="example-provider", model_id="example-model", cap_usd=5.0)
        rc, out = preflight(write_profile(tmp / "ok.yaml", att, **good), "--start")
        expect(rc == 3 and "a profile alone names no run" in out, f"start with every condition met: exit {rc}, {out[-200:]}")
        rc, out = preflight(write_profile(tmp / "empty.yaml", empty, **good), "--start")
        expect(rc == 2 and "empty" in out, "--start did not refuse an empty attestation file")
        rc, out = preflight(write_profile(tmp / "nocap.yaml", att, provider="example-provider", model_id="example-model"), "--start")
        expect(rc == 2 and "cap_usd" in out, "--start did not refuse without cap_usd")
        rc, out = preflight(write_profile(tmp / "nomodel.yaml", att, provider="example-provider", cap_usd=5.0), "--start")
        expect(rc == 2 and "model_id" in out, "--start did not refuse without model_id")
        bad = tmp / "bad.yaml"
        bad.write_text(yaml.safe_dump({"name": "x", "adapter": "y", "endpoint": None, "trace_source": None,
                                       "capabilities": ["telepathy"], "precondition_map": None, "firm_values": None,
                                       "attestation_file": None,
                                       "settings": {"provider": None, "model_id": None, "price_in_per_m": None,
                                                    "price_out_per_m": None, "cap_usd": None, "api_key": "x"}}),
                       encoding="utf-8")
        rc, out = preflight(bad)
        expect(rc == 2 and "unknown capability" in out and "never goes in a profile" in out, "an invalid profile was not refused")
        print("   --start: refuses without an attestation, with an empty one, without cap_usd, without model_id; exit 3 when all met")
        settings_checks(tmp, att)
        external_checks(tmp)

    print("5. firm values")
    problems, keys, declared = firm_value_problems()
    for p in problems:
        expect(False, p)
    print(f"   {len(keys)} keys, {len(declared)} declared with a basis ({', '.join(sorted(declared)) or 'none'}), "
          f"{len(keys) - len(declared)} null")

    print("6. sweep")
    rc, out = run(["tools/sweep_barred.py", "--faias", faias])
    expect(rc == 0, f"sweep found hits: {out[-600:]}")
    print("  ", out.strip().splitlines()[-1])

    print("7. unit tests")
    rc, out = run(["-m", "unittest", "discover", "-s", "tests", "-t", ".", "-p", "test_*.py"])
    m = re.search(r"Ran (\d+) tests? in", out)
    expect(rc == 0 and m and re.search(r"^OK\s*$", out, re.M), f"unit tests failed: {out[-1500:]}")
    print("  ", f"{m.group(1)} tests, OK" if m and rc == 0 else out.strip().splitlines()[-1])
    for line in re.findall(r"^(?:FAIL|ERROR): [^\n]*", out, re.M):    # name each failed test
        print("   failed:", line)
    for line in re.findall(r"\[test note\] [^\n]*", out):     # a test that read no live log says so here
        print("  ", line)

    print("8. containment self-test")
    with tempfile.TemporaryDirectory() as t:
        rc, out = run(["-m", "faias_harness.selftest", "--out", str(Path(t) / "selftest")])
    m = re.search(r"paths probed: (\d+), refused: (\d+), guard trips: (\d+), static scan: (\d+) unexpected, "
                  r"result: (\w+)", out)
    expect(rc == 0 and m and m.group(5) == "PASS" and m.group(3) == "0" and m.group(4) == "0",
           f"the containment self-test did not pass: {out[-800:]}")
    print("  ", f"{m.group(1)} probes: " + m.group(0) if m else out.strip().splitlines()[-1:])

    if failures:
        print(f"run_all: FAIL ({len(failures)} failure(s))")
        return 1
    print("run_all: PASS (8 steps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
