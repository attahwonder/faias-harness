"""Test check_records.py: it passes on a clean copy of the records and fails on each seeded fault.

  py -3.14 tools/test_check_records.py --faias <path to the FAIAS tree>
Works on copies in a temporary folder. The real records are not touched.
"""
import argparse
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import check_records  # noqa: E402
from faias_harness import yamlio  # noqa: E402  (check_records puts the repository on the path)

RECORDS = HERE.parent / "records"


def seeded(tmp, name, tp, change):
    d = tmp / name
    shutil.copytree(RECORDS, d)
    p = d / f"{tp}.yaml"
    r = yamlio.load(p.read_text(encoding="utf-8"))
    change(r)
    p.write_text(yaml.safe_dump(r, sort_keys=False, allow_unicode=True, width=10000), encoding="utf-8")
    return d


def wrong_star(r):
    r["star"] = "★3" if r["star"] != "★3" else "★2"


def number_moved(r):
    t = next(t for t in r["thresholds"] if "95%" in t["text"])
    t["text"] = t["text"].replace("95%", "90%")


def no_basis(r):
    del r["thresholds"][0]["basis"]


def firm_without_key(r):
    t = next(t for t in r["thresholds"] if t["basis"] == "firm")
    del t["firm_value_key"]


def unknown_capability(r):
    r["needs"]["TP-SEC-001"]["needed"].append("telepathy")


def line_outside_file(r):
    r["needs"]["TP-SEC-001"]["lines"][0] = "V3_Testing_Playbook/PLAYBOOK_SEC_001.md:9999"


def settled_outside_passes(r):
    e = next(e for e in r["needs"].values() if "settled" in e)
    both = e["settled"]["pass_1"] + e["settled"]["pass_2"]
    e["needed"].append(next(c for c in check_records.CAPABILITIES if c not in both))


def open_and_settled(r):
    e = next(e for e in r["needs"].values() if "settled" in e)
    e["open"] = {"pass_1": e["settled"]["pass_1"], "pass_2": e["settled"]["pass_2"]}


CASES = [("wrong star", "TP-SEC-001", wrong_star, "does not equal the Confidence line"),
         ("number not at its line", "TP-SEC-003", number_moved, "number '90%' not at"),
         ("missing basis", "TP-AGT-004", no_basis, "basis None is not one of"),
         ("firm basis without a firm value key", "TP-AGT-002", firm_without_key, "firm basis without firm_value_key"),
         ("unknown capability in the needs file", "capability_needs", unknown_capability, "unknown capability 'telepathy'"),
         ("needs line outside its file", "capability_needs", line_outside_file, "line outside the file"),
         ("settled needs outside both passes", "capability_needs", settled_outside_passes, "not drawn from the two passes"),
         ("open and settled in one entry", "capability_needs", open_and_settled, "both open and settled")]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--faias", required=True)
    faias = Path(ap.parse_args().faias)
    failures = []
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        clean = tmp / "clean"
        shutil.copytree(RECORDS, clean)
        errors, _ = check_records.check(clean, faias)
        if errors:
            failures.append(f"clean copy: {len(errors)} error(s), first: {errors[0]}")
        for name, tp, change, expect in CASES:
            errors, _ = check_records.check(seeded(tmp, name.replace(" ", "_"), tp, change), faias)
            if not any(expect in e and e.startswith(tp) for e in errors):
                failures.append(f"{name}: expected an error with {expect!r} for {tp}; got {errors[:3]}")
            else:
                print(f"caught: {name} ({tp})")
    if failures:
        print("\n".join(failures))
        return 1
    print(f"test_check_records: PASS (clean copy passes; {len(CASES)} seeded faults caught)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
