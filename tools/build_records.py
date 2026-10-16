"""Build one generic procedure record per automated procedure.

  py -3.14 tools/build_records.py --faias <path to the FAIAS tree>

Reads the procedures from <faias>/V3_Testing_Playbook (read only), records/classification.tsv and
records/OVERRIDES.yaml. Writes records/TP-*.yaml. The schema is records/SCHEMA.md.
It does not write records/capability_needs.yaml: that file is an input that a person keeps.
A second run gives byte-identical output. The tool prints the items left for an override.
"""
import argparse
import csv
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
RECORDS = REPO / "records"

SECTIONS = ("test design", "observations & measures", "interpretation", "evidence outputs")
NUM = re.compile(r"(?<![A-Za-z.:\-_/])\d+(?:[.,]\d+)?%?")
YEAR = re.compile(r"^(199\d|20[0-3]\d)$")
FIRM = re.compile(r"firm'?s own|firm-set|firm-defined|firm-approved|approved (?:value|tolerance|threshold)|risk appetite"
                  r"|firm'?s (?:documented|stated|declared|defined|chosen) (?:tolerance|threshold|value)", re.I)
REVIEWER = re.compile(r"reviewer judgement|no established (?:acceptance )?threshold|no faias(?:-standard)? (?:acceptance )?"
                      r"(?:threshold|tolerance)|\bno threshold|no numeric threshold|no fixed threshold", re.I)
CITED = re.compile(r"canvas|owasp|nist|\biso\b|mitre|pass indicator|fail indicator|§", re.I)
LEVEL = re.compile(r"L[1-5](?:\s*[–-]\s*L?[1-5])?")
OUTCOME = re.compile(r"^\s*(?:[-*]\s+)?\**(Pass|Fail|Partial)\b")

LABEL = re.compile(r"\b(?:Component|Limb|Layer|Track|Tier|Step|Phase|Stage|Part|Section|Table|Annex|Appendix|Incident"
                   r"|field|fields|layers|tiers|limbs|components)s?\s+\d+(?:\s*(?:–|-|and|to)\s*\d+)?|\b[A-Z]{1,3}\d+\b|§\s*\d+|\(\d+\)")


def clean(s):
    """Source line to record text: no table pipes at the ends, no Markdown emphasis or code marks, spaces collapsed."""
    s = s.strip()
    if s.startswith("|"):
        s = " ".join(c.strip() for c in s.strip("|").split(" | "))
    s = re.sub(r"^\s*(?:[-*]|\d+\.)\s+", "", s)
    s = s.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def numbers(s):
    """Numbers that are counts or values: years and label numbers (Component 2, Limb 1, X4, §11) do not count."""
    return [n for n in NUM.findall(LABEL.sub(" ", s)) if not YEAR.match(n)]


def cells(line):
    return [c.strip() for c in line.strip().strip("|").split(" | ")]


def is_rule(line):
    return bool(re.match(r"^\|[\s|:\-]+\|$", line.strip()))


def split_sections(lines):
    out, cur = {}, None
    for i, l in enumerate(lines, 1):
        m = re.match(r"^#{2,4}\s+(?:\d+\.\s+)?(.*?)\s*$", l)
        if m:
            h = m.group(1).lower()
            cur = next((s for s in SECTIONS if h.startswith(s)), None)
            if cur:
                out[cur] = []
            continue
        if cur and l.strip():
            out[cur].append((i, l))
    return out


def classify(text):
    hits = {k for k, rx in (("firm", FIRM), ("reviewer", REVIEWER), ("cited", CITED)) if rx.search(text)}
    if "firm" in hits and "reviewer" in hits:
        return "residue", hits
    for k in ("firm", "reviewer", "cited"):
        if k in hits:
            return k, hits
    return "not stated", hits


def build(play, tp, cls, overrides, rel):
    f = play / ("PLAYBOOK_" + tp[3:].replace("-", "_") + ".md")
    lines = f.read_text(encoding="utf-8").splitlines()
    text = "\n".join(lines)
    m = re.match(r"^#\s+(TP-[A-Z]+-\d{3})\s+—\s+(.*)", lines[0])
    irq = re.search(r"\*\*IRQ:\*\*\s+(IRQ-[A-Z]+-\d{3})", text)
    conf = re.search(r"^\*\*(?:★ )?Confidence[^\n]*?(★\d)", text, flags=re.M)
    mc = re.search(r"Method class(?:ification)?:\**\s*\**([^.—(*]+)", text)
    if not (m and m.group(1) == tp and irq and conf):
        sys.exit(f"{tp}: title, IRQ or Confidence line not found")
    src = lambda i: f"{rel}:{i}"
    sec = split_sections(lines)
    if set(sec) != set(SECTIONS):
        sys.exit(f"{tp}: sections missing: {set(SECTIONS) - set(sec)}")

    design = []
    for i, l in sec["test design"]:
        if is_rule(l) or not numbers(clean(l)):
            continue
        t = clean(l)
        basis = ("reviewer-set design minimum" if re.search(r"reviewer-set design minimum", t, re.I)
                 else "cited" if CITED.search(t) else "not stated")
        design.append({"text": t, "src": src(i), "basis": basis})

    measures, thresholds, residue = [], [], []
    body = [(i, l) for i, l in sec["observations & measures"]]
    table = [(i, l) for i, l in body if l.lstrip().startswith("|") and not is_rule(l)]
    if table:
        head = [c.lower() for c in cells(table[0][1])]
        if head[:3] != ["measure", "threshold", "basis"]:
            sys.exit(f"{tp}: measures table header {head}")
        items = [(i, cells(l)) for i, l in table[1:]]
        for n, (i, c) in enumerate(items, 1):
            mid = f"M{n}"
            measures.append({"id": mid, "text": clean(c[0]), "src": src(i)})
            basis, hits = classify(c[1] + " " + c[2])
            thresholds.append({"measure": mid, "text": clean(c[1]), "basis": basis,
                               "basis_source": clean(c[2]), "src": src(i)})
    else:
        # a bullet in italics is a note on the section, not a measure (PLAYBOOK_SEC_014.md, the last bullet)
        bullets = [(i, l) for i, l in body if re.match(r"^\s*[-*] ", l) and not re.match(r"^\s*[-*] \*(?!\*)", l)]
        for n, (i, l) in enumerate(bullets, 1):
            mid = f"M{n}"
            t = clean(l)
            measures.append({"id": mid, "text": t, "src": src(i)})
            basis, hits = classify(t)
            thresholds.append({"measure": mid, "text": t, "basis": basis, "basis_source": "the measure's own text",
                               "src": src(i)})
    if not measures:
        sys.exit(f"{tp}: no measures")
    unknown = set(overrides.get(tp, {})) - {t["measure"] for t in thresholds}
    if unknown:
        sys.exit(f"{tp}: OVERRIDES.yaml names measures that do not exist: {sorted(unknown)}")
    for th in thresholds:
        o = overrides.get(tp, {}).get(th["measure"])
        if o:
            th["basis"] = o["basis"]
            th["override"] = {"reason": o["reason"], "read": o["read"]}
        if th["basis"] == "residue":
            residue.append((tp, th["measure"], th["src"], th["text"][:160]))
        if th["basis"] == "firm":
            th["firm_value_key"] = f"firm.{tp}.{th['measure']}"

    interp = []
    for i, l in sec["interpretation"]:
        if is_rule(l):
            continue
        om = OUTCOME.match(l)
        interp.append({"outcome": om.group(1) if om else "other", "text": clean(l), "src": src(i)})

    evidence = []
    rows = [(i, l) for i, l in sec["evidence outputs"] if not is_rule(l)]
    if rows and rows[0][1].lstrip().startswith("|"):
        head = [c.lower() for c in cells(rows[0][1])]
        if "evidence level" not in head:
            sys.exit(f"{tp}: evidence table header {head}")
        for i, l in rows[1:]:
            if not l.lstrip().startswith("|"):
                continue
            c = cells(l)
            evidence.append({"text": clean(c[0]), "level": " / ".join(LEVEL.findall(clean(c[-1]))), "src": src(i)})
    else:
        for i, l in rows:
            if not re.match(r"^\s*[-*] ", l):
                continue
            t = clean(l)
            evidence.append({"text": t, "level": " / ".join(LEVEL.findall(t)), "src": src(i)})

    rec = {
        "tp": tp, "title": m.group(2).strip(), "irq": irq.group(1), "star": conf.group(1),
        "method_class": mc.group(1).strip() if mc else "-",
        "source": rel,
        "class": cls["class"], "human_step": cls["human_step"], "lever": cls["lever"],
        "design": design, "measures": measures, "thresholds": thresholds,
        "interpretation": interp, "evidence_outputs": evidence,
    }
    return rec, residue


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--faias", required=True, help="path to the FAIAS tree (the folder with V3_Testing_Playbook)")
    a = ap.parse_args()
    play = Path(a.faias) / "V3_Testing_Playbook"
    if not play.is_dir():
        sys.exit(f"no V3_Testing_Playbook folder under {a.faias}")
    with open(RECORDS / "classification.tsv", encoding="utf-8", newline="") as fh:
        cls = {r["tp"]: r for r in csv.DictReader(fh, delimiter="\t") if r["class"] == "H" and r["human_step"] == "N"}
    ov_path = RECORDS / "OVERRIDES.yaml"
    overrides = (yaml.safe_load(ov_path.read_text(encoding="utf-8")) or {}) if ov_path.exists() else {}
    residue_all, keys = [], []
    for tp in sorted(cls):
        rel = "V3_Testing_Playbook/PLAYBOOK_" + tp[3:].replace("-", "_") + ".md"
        rec, residue = build(play, tp, cls[tp], overrides, rel)
        residue_all += residue
        keys += [th["firm_value_key"] for th in rec["thresholds"] if th.get("firm_value_key")]
        out = yaml.safe_dump(rec, sort_keys=False, allow_unicode=True, width=10000)
        (RECORDS / f"{tp}.yaml").write_text("# Generated by tools/build_records.py; do not edit. Schema: SCHEMA.md\n" + out,
                                            encoding="utf-8", newline="\n")
    print(f"{len(cls)} records; {len(keys)} firm-value keys; {len(residue_all)} item(s) need an override")
    for r in residue_all:
        print("  ", *r)


if __name__ == "__main__":
    main()
