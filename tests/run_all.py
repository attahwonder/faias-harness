"""Run every test of the harness from one command.

  py -3.14 -X utf8 tests/run_all.py --faias <path to the FAIAS v1.3.3 tree>

Steps:
  1. rebuild the records twice: the output is byte-identical, and equal to the files in the repository
  2. check_records passes
  3. the seeded-fault test passes (6 faults caught: 4 in records, 2 in the capability needs)
  4. preflight: the SRD-1 profile gives 45 full; the example profiles give stable, correct counts;
     --start refuses without an attestation file and without settings; v0.1 starts nothing
  5. the firm-values file of SRD-1 lists exactly the keys of the records
  6. the sweep for barred terms and key-like strings finds 0 hits
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--faias", required=True, help="path to the FAIAS v1.3.3 tree")
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
    expect(rc == 0 and "6 seeded faults caught" in out, f"test_check_records failed: {out[-400:]}")
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
        expect(rc == 3 and "no run engine in v0.1" in out, f"start with every condition met: exit {rc}, {out[-200:]}")
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

    print("5. firm values")
    fv = yaml.safe_load((REPO / "targets" / "srd1" / "firm_values.yaml").read_text(encoding="utf-8"))["values"]
    keys = set()
    for p in (REPO / "records").glob("TP-*.yaml"):
        keys |= {t["firm_value_key"] for t in yaml.safe_load(p.read_text(encoding="utf-8"))["thresholds"] if t.get("firm_value_key")}
    expect(set(fv) == keys, "the firm-values file does not list exactly the firm_value_key set of the records")
    expect(all(v is None for v in fv.values()), "a firm value is set before any run")
    print(f"   {len(keys)} keys, all null")

    print("6. sweep")
    rc, out = run(["tools/sweep_barred.py", "--faias", faias])
    expect(rc == 0, f"sweep found hits: {out[-600:]}")
    print("  ", out.strip().splitlines()[-1])

    if failures:
        print(f"run_all: FAIL ({len(failures)} failure(s))")
        return 1
    print("run_all: PASS (6 steps)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
