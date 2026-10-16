"""Check the procedure records against the FAIAS tree.

  py -3.14 tools/check_records.py --faias <path to the FAIAS tree> [--records <dir>]

Checks: the set (one record per procedure in records/classification.tsv, no other); schema and allowed values;
the star equals the procedure's Confidence line; every number in a copied text appears at its cited line, and the
text's words come from that line; every threshold has an allowed basis, and each `firm` threshold has its
firm_value_key; capability_needs.yaml is marked draft, names exactly the 45 procedures, uses only the seven
capabilities, keeps `needed` equal to the capabilities that both passes need where an `open` entry shows the two
sets, keeps a `settled` entry within the two sets (all that both need, and only what one needs) with a note, and
every file:line it cites exists in the FAIAS tree, in the file of its own procedure.
Written apart from build_records.py, so that one bug cannot pass both.
Exits non-zero on any failure. Tested by test_check_records.py.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from faias_harness.capabilities import CAPABILITIES  # noqa: E402

TOP = ["tp", "title", "irq", "star", "method_class", "source", "class", "human_step", "lever",
       "design", "measures", "thresholds", "interpretation", "evidence_outputs"]
BASES = {"cited", "firm", "reviewer", "not stated"}
DESIGN_BASES = {"reviewer-set design minimum", "cited", "not stated"}
OUTCOMES = {"Pass", "Fail", "Partial", "other"}
DIGITS = re.compile(r"(?<![A-Za-z.:\-_/])\d+(?:[.,]\d+)?%?")
WORD = re.compile(r"[A-Za-z]{4,}")


def check_item(tp, where, item, lines, errors, rel):
    m = re.fullmatch(re.escape(rel) + r":(\d+)", str(item.get("src", "")))
    if not m:
        errors.append(f"{tp} {where}: src {item.get('src')!r} is not {rel}:<line>")
        return
    n = int(m.group(1))
    if not 1 <= n <= len(lines):
        errors.append(f"{tp} {where}: line {n} out of range")
        return
    raw = lines[n - 1]
    text = str(item.get("text", ""))
    if not text:
        errors.append(f"{tp} {where}: empty text")
    for tok in DIGITS.findall(text):
        if tok not in raw:
            errors.append(f"{tp} {where}: number {tok!r} not at {item['src']}")
    words = WORD.findall(text)
    found = sum(w in raw for w in words)
    if words and found < 0.9 * len(words):
        errors.append(f"{tp} {where}: text does not come from {item['src']} ({found}/{len(words)} words)")


def cap_list(tp, where, value, errors):
    """A list of capability names from the needs file. Return it as a list, or [] after an error."""
    if not isinstance(value, list):
        errors.append(f"capability_needs.yaml {tp}: {where} is not a list")
        return []
    bad = [c for c in value if c not in CAPABILITIES]
    for c in bad:
        errors.append(f"capability_needs.yaml {tp}: unknown capability {c!r} in {where}")
    if len(set(value)) != len(value):
        errors.append(f"capability_needs.yaml {tp}: {where} names a capability twice")
    return [] if bad else value


def check_needs(records_dir, want, faias, errors):
    path = records_dir / "capability_needs.yaml"
    if not path.exists():
        errors.append("capability_needs.yaml: missing")
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not str(data.get("status", "")).startswith("draft:"):
        errors.append("capability_needs.yaml: status does not start with 'draft:'")
        return
    needs = data.get("needs")
    if not isinstance(needs, dict):
        errors.append("capability_needs.yaml: no `needs` map")
        return
    if set(needs) != set(want):
        errors.append("capability_needs.yaml: procedures differ from classification.tsv "
                      f"(missing {sorted(set(want) - set(needs))}, extra {sorted(set(needs) - set(want))})")
    for tp, entry in sorted(needs.items()):
        if not isinstance(entry, dict) or not {"needed", "lines"} <= set(entry) <= {"needed", "lines", "open", "settled"}:
            errors.append(f"capability_needs.yaml {tp}: fields are not needed, lines and an optional open or settled")
            continue
        needed = cap_list(tp, "needed", entry["needed"], errors)
        if "open" in entry and "settled" in entry:
            errors.append(f"capability_needs.yaml {tp}: both open and settled")
        for key in ("open", "settled"):
            op = entry.get(key)
            if op is None:
                continue
            if (not isinstance(op, dict) or not {"pass_1", "pass_2"} <= set(op) <= {"pass_1", "pass_2", "note"}
                    or (key == "settled" and "note" not in op)):
                errors.append(f"capability_needs.yaml {tp}: {key} does not hold pass_1, pass_2 and "
                              + ("a note" if key == "settled" else "an optional note"))
                continue
            if "note" in op and not (isinstance(op["note"], str) and op["note"].strip()):
                errors.append(f"capability_needs.yaml {tp}: {key}.note is empty")
            p1 = cap_list(tp, f"{key}.pass_1", op["pass_1"], errors)
            p2 = cap_list(tp, f"{key}.pass_2", op["pass_2"], errors)
            if set(p1) == set(p2):
                errors.append(f"capability_needs.yaml {tp}: {key} entry, but the two passes do not differ")
            if key == "open" and set(needed) != set(p1) & set(p2):
                errors.append(f"capability_needs.yaml {tp}: needed is not what both passes need")
            if key == "settled" and not set(p1) & set(p2) <= set(needed) <= set(p1) | set(p2):
                errors.append(f"capability_needs.yaml {tp}: settled needed is not drawn from the two passes "
                              "(it keeps all that both need and adds only what one needs)")
        refs = entry["lines"]
        if not isinstance(refs, list) or (needed and not refs):
            errors.append(f"capability_needs.yaml {tp}: lines is not a list, or is empty while needed is not")
            continue
        own = "V3_Testing_Playbook/PLAYBOOK_" + tp[3:].replace("-", "_") + ".md"
        for ref in refs:
            m = re.fullmatch(r"(V3_Testing_Playbook/PLAYBOOK_[A-Z]{3}_\d{3}\.md):(\d+)", str(ref))
            if not m:
                errors.append(f"capability_needs.yaml {tp}: line reference {ref!r} is not <file>:<line>")
            elif m.group(1) != own:
                errors.append(f"capability_needs.yaml {tp}: {ref} is not in the file of the procedure")
            elif not (faias / m.group(1)).is_file():
                errors.append(f"capability_needs.yaml {tp}: {ref}: the file does not exist in the FAIAS tree")
            else:
                n = len((faias / m.group(1)).read_text(encoding="utf-8").splitlines())
                if not 1 <= int(m.group(2)) <= n:
                    errors.append(f"capability_needs.yaml {tp}: {ref}: line outside the file ({n} lines)")


def check(records_dir, faias):
    errors = []
    with open(records_dir / "classification.tsv", encoding="utf-8", newline="") as fh:
        want = {r["tp"]: r for r in csv.DictReader(fh, delimiter="\t") if r["class"] == "H" and r["human_step"] == "N"}
    have = {p.stem: p for p in records_dir.glob("TP-*.yaml")}
    for tp in sorted(set(want) - set(have)):
        errors.append(f"{tp}: no record")
    for tp in sorted(set(have) - set(want)):
        errors.append(f"{tp}: record for a procedure outside the set")
    counts = {"records": 0, "thresholds": 0, "firm": 0}
    for tp in sorted(set(want) & set(have)):
        r = yaml.safe_load(have[tp].read_text(encoding="utf-8"))
        counts["records"] += 1
        missing = [k for k in TOP if k not in r]
        if missing:
            errors.append(f"{tp}: missing fields {missing}")
            continue
        rel = r["source"]
        if rel != "V3_Testing_Playbook/PLAYBOOK_" + tp[3:].replace("-", "_") + ".md":
            errors.append(f"{tp}: source {rel!r}")
            continue
        lines = (faias / rel).read_text(encoding="utf-8").splitlines()
        text = "\n".join(lines)
        if r["tp"] != tp or not lines[0].startswith(f"# {tp} — ") or r["title"] not in lines[0]:
            errors.append(f"{tp}: tp or title differs from line 1")
        if f"**IRQ:** {r['irq']}" not in text:
            errors.append(f"{tp}: IRQ {r['irq']} not on the IRQ line")
        conf = [l for l in lines if re.match(r"^\*\*(?:★ )?Confidence", l)]
        if len(conf) != 1 or not re.match(r"^★[1-5]$", str(r["star"])) or r["star"] not in conf[0].split("Method")[0]:
            errors.append(f"{tp}: star {r['star']!r} does not equal the Confidence line")
        for k in ("class", "human_step", "lever"):
            if str(r[k]) != want[tp][k]:
                errors.append(f"{tp}: {k} {r[k]!r} differs from classification.tsv")
        for i, d in enumerate(r["design"]):
            check_item(tp, f"design[{i}]", d, lines, errors, rel)
            if d.get("basis") not in DESIGN_BASES:
                errors.append(f"{tp} design[{i}]: basis {d.get('basis')!r}")
        ids = []
        for i, m in enumerate(r["measures"]):
            check_item(tp, f"measures[{i}]", m, lines, errors, rel)
            ids.append(m.get("id"))
        if not ids or ids != [f"M{n}" for n in range(1, len(ids) + 1)]:
            errors.append(f"{tp}: measure ids {ids}")
        if [t.get("measure") for t in r["thresholds"]] != ids:
            errors.append(f"{tp}: thresholds do not match the measures one to one")
        for t in r["thresholds"]:
            counts["thresholds"] += 1
            where = f"threshold {t.get('measure')}"
            check_item(tp, where, t, lines, errors, rel)
            if t.get("basis") not in BASES:
                errors.append(f"{tp} {where}: basis {t.get('basis')!r} is not one of {sorted(BASES)}")
            bs = str(t.get("basis_source", ""))
            if bs != "the measure's own text":
                src_line = lines[int(str(t.get("src", ":0")).rsplit(":", 1)[1]) - 1] if t.get("src") else ""
                if not bs or sum(w in src_line for w in WORD.findall(bs)) < 0.9 * len(WORD.findall(bs)):
                    errors.append(f"{tp} {where}: basis_source does not come from its line")
            if t.get("basis") == "firm":
                counts["firm"] += 1
                key = f"firm.{tp}.{t.get('measure')}"
                if t.get("firm_value_key") != key:
                    errors.append(f"{tp} {where}: firm basis without firm_value_key {key}")
            elif "firm_value_key" in t:
                errors.append(f"{tp} {where}: firm_value_key on a basis other than firm")
            if "override" in t and not (t["override"].get("reason") and re.fullmatch(
                    r"V3_Testing_Playbook/PLAYBOOK_[A-Z]{3}_\d{3}\.md:\d+", str(t["override"].get("read", "")))):
                errors.append(f"{tp} {where}: override without a reason or a line read")
        outs = set()
        for i, it in enumerate(r["interpretation"]):
            check_item(tp, f"interpretation[{i}]", it, lines, errors, rel)
            outs.add(it.get("outcome"))
            if it.get("outcome") not in OUTCOMES:
                errors.append(f"{tp} interpretation[{i}]: outcome {it.get('outcome')!r}")
        if not {"Pass", "Fail"} <= outs:
            errors.append(f"{tp}: interpretation lacks a Pass or a Fail item")
        if not r["evidence_outputs"]:
            errors.append(f"{tp}: no evidence outputs")
        for i, e in enumerate(r["evidence_outputs"]):
            check_item(tp, f"evidence_outputs[{i}]", e, lines, errors, rel)
            lv = str(e.get("level", ""))
            if not re.fullmatch(r"L[1-5](?:\s*[–-]\s*L?[1-5])?(?: / L[1-5](?:\s*[–-]\s*L?[1-5])?)*", lv):
                errors.append(f"{tp} evidence_outputs[{i}]: level {lv!r}")
            elif any(x not in lines[int(e["src"].rsplit(":", 1)[1]) - 1] for x in re.findall(r"L[1-5]", lv)):
                errors.append(f"{tp} evidence_outputs[{i}]: level {lv!r} not at its line")
    check_needs(records_dir, want, faias, errors)
    return errors, counts


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--faias", required=True, help="path to the FAIAS tree (the folder with V3_Testing_Playbook)")
    ap.add_argument("--records", default=str(REPO / "records"))
    a = ap.parse_args(argv)
    errors, c = check(Path(a.records), Path(a.faias))
    print(f"{c['records']} records, {c['thresholds']} thresholds ({c['firm']} with a firm value key)")
    if errors:
        print("\n".join(errors))
        return 1
    print("check_records: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
