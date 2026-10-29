"""Testability report: which of the procedures with a record the harness can run against a target, from its profile.

  py -3.14 -X utf8 -m faias_harness.testability --profile <profile.yaml> --out <folder> [--sign-key <private key file>]
  py -3.14 -X utf8 -m faias_harness.testability verify --report <folder>/testability.json --pubkey <public key file>

The command writes two files to the folder: testability.json and testability.md. It reads the profile, the capability
needs and the records. It makes no network call and no model call, and it needs no key. It tests nothing. The result
of each procedure comes from the preflight (`judge` and `load_needs` in preflight.py), so the report and the preflight
give the same counts.

The capability needs are a draft (records/capability_needs.yaml; docs/ARCHITECTURE.md, section 9). No person has
checked them. The first line of testability.md and the field `needs_draft` of testability.json say so.

With --sign-key, the command signs testability.json with the run-signing key, as runlog.py signs a run log: an
Ed25519 signature over the exact bytes, in testability.json.sig. The key file must be outside the repository.
`verify` checks the signature with the public key that --pubkey names. It then checks that each result follows from
the needs and the capabilities in the file. The signature does not cover testability.md.

The command refuses an --out folder inside records/ or faias_harness/, and a folder that holds a report file already.
Exit codes: 0 report written, or verify passed; 1 verify failed; 2 refused.
"""
import argparse
import base64
import json
import re
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from . import __version__, yamlio
from .capabilities import CAPABILITIES
from .engine import runlog
from .preflight import NEEDS_FILE, RECORDS_DIR, judge, load_needs, open_entries
from .profile import REPO, ProfileError, load_profile

SCHEMA = "faias-harness-testability/1"
JSON_NAME, MD_NAME = "testability.json", "testability.md"
SIG_NAME = JSON_NAME + ".sig"
BARRED_OUT = ("records", "faias_harness")
DRAFT_NOTE = "The capability needs are a draft. No person has checked them."
RESULT_RULES = {
    "full": "every need is in the profile",
    "partly": "any other case",
    "none": "the procedure needs conversation and the profile lacks it, or every need is absent from the profile",
}
NEEDS_STATUS = {
    "agreed": "Two model passes agree. Nobody has checked the entry at each line.",
    "settled": "The two passes differed. A third reading, also a model, settled the entry at the lines.",
    "open": "The two passes differ, and nothing is decided. The needs list holds only what both passes need.",
}


def not_shown(procedures):
    return [
        "It does not test anything. It reads the profile, the capability needs and the records, and no system.",
        "A full result means only that the profile declares every capability that the draft needs list. It does "
        "not mean that an adapter implements them, or that a test passes.",
        "The operator declares the capabilities in the profile. The harness has not observed them.",
        f"It does not cover the FAIAS procedures outside the {procedures} records in records/.",
    ]


def _sha(path):
    return runlog.sha256_hex(Path(path).read_bytes())


def faias_version(records_dir=RECORDS_DIR):
    """The FAIAS version that the attribution in records/README.md names, or None."""
    m = re.search(r"\bversion (\d+\.\d+\.\d+)\b", (Path(records_dir) / "README.md").read_text(encoding="utf-8"))
    return m.group(1) if m else None


def levers(needs, have):
    """For each capability not in `have`: the procedures that move to full if the profile adds it alone."""
    out = []
    for cap in CAPABILITIES:
        if cap in have:
            continue
        moved = [tp for tp in sorted(needs)
                 if judge(needs[tp], have)[0] != "full" and judge(needs[tp], have | {cap})[0] == "full"]
        out.append({"capability": cap, "to_full": len(moved), "procedures": moved})
    return sorted(out, key=lambda r: -r["to_full"])     # a stable sort: a tie keeps the order of CAPABILITIES


def build(profile, *, signed=False):
    """The report as a dict. The results come from `judge` and `load_needs`; the record fields are copied."""
    raw = yamlio.load(NEEDS_FILE.read_text(encoding="utf-8"))
    needs = load_needs(NEEDS_FILE)
    opened = open_entries(NEEDS_FILE)
    have = set(profile.capabilities)
    rows, record_files = [], []
    counts = {"full": 0, "partly": 0, "none": 0}
    for tp in sorted(needs):
        path = RECORDS_DIR / f"{tp}.yaml"
        rec = yamlio.load(path.read_text(encoding="utf-8"))
        entry = raw["needs"][tp]
        result, missing = judge(needs[tp], have)
        counts[result] += 1
        rows.append({"tp": rec["tp"], "irq": rec["irq"], "title": rec["title"], "star": rec["star"],
                     "method_class": rec["method_class"], "result": result, "needed": needs[tp],
                     "missing": missing, "lines": list(entry["lines"]),
                     "needs_status": "open" if tp in opened else "settled" if entry.get("settled") else "agreed"})
        record_files.append({"file": f"records/{path.name}", "sha256": _sha(path)})
    return {
        "schema": SCHEMA,
        "needs_draft": {"draft": True, "note": DRAFT_NOTE, "status_line": raw["status"]},
        "profile": {"name": profile.name, "status": profile.status, "file_name": profile.path.name,
                    "sha256": _sha(profile.path), "capabilities": list(profile.capabilities)},
        "inputs": {"capability_needs": {"file": "records/capability_needs.yaml", "sha256": _sha(NEEDS_FILE)},
                   "records": record_files},
        "harness_version": __version__,
        "faias_version": faias_version(),
        "faias_version_source": "records/README.md (the attribution of the records)",
        "time_utc": runlog.iso(),
        "signature": {"alg": "Ed25519", "file": SIG_NAME, "covers": JSON_NAME} if signed else None,
        "summary": {"procedures": len(rows), **counts},
        "result_rules": RESULT_RULES,
        "needs_status_meaning": NEEDS_STATUS,
        "star_note": "the star and the method class are copied from the record; the harness never computes them",
        "not_shown": not_shown(len(rows)),
        "levers": levers(needs, have),
        "rows": rows,
    }


def _cell(text):
    return str(text).replace("|", "\\|")


def _codes(items):
    return ", ".join(f"`{i}`" for i in items) or "-"


def markdown(rep, json_sha256):
    """The Markdown copy of the report. Its first line says that the needs are a draft."""
    p, s, n = rep["profile"], rep["summary"], rep["inputs"]["capability_needs"]
    sig = rep["signature"]
    out = [
        f"> **Draft capability needs.** No person has checked the capability needs in this report. The status line "
        f"of `records/capability_needs.yaml` is \"{rep['needs_draft']['status_line']}\".",
        "",
        f"# Testability report: {p['name']}",
        "",
        f"This report shows which of the {s['procedures']} procedures with a record the harness can run against the "
        "target. It uses the target profile alone. It tests nothing. The results come from the preflight of the "
        "harness, on the draft capability needs.",
        "",
        "## Header",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Profile | {_cell(p['name'])} |",
        f"| Profile status | {_cell(p['status'] or 'not stated')} |",
        f"| Profile file | `{_cell(p['file_name'])}` |",
        f"| Profile SHA-256 | `{p['sha256']}` |",
        f"| Capabilities in the profile | {_codes(p['capabilities'])} |",
        f"| Capability needs | `{n['file']}`, SHA-256 `{n['sha256']}` |",
        f"| Records | {len(rep['inputs']['records'])} files; the SHA-256 of each is in the section Inputs |",
        f"| Harness version | {rep['harness_version']} |",
        f"| FAIAS version that the records name | {rep['faias_version'] or 'not found'} |",
        f"| Time (UTC) | {rep['time_utc']} |",
        f"| Signature | {'Ed25519, detached, in `' + sig['file'] + '`; it covers `' + sig['covers'] + '` only' if sig else 'unsigned'} |",
        f"| `{JSON_NAME}` SHA-256 | `{json_sha256}` |",
        "",
        "This Markdown file is not signed. It shows the content of the JSON file.",
        "",
        "## Summary",
        "",
        "| Result | Procedures | Rule |",
        "|---|---|---|",
    ]
    out += [f"| `{r}` | {s[r]} | {RESULT_RULES[r]} |" for r in ("full", "partly", "none")]
    out += ["", "## What this report does not show", ""]
    out += [f"- {line}" for line in rep["not_shown"]]
    out += ["", "## Capability levers", "",
            "The table lists each capability that the profile does not declare. For each, it counts the procedures "
            "that move to `full` if the profile adds that capability alone. The other capabilities stay as they are.",
            "", "| Capability | Procedures that move to `full` | Procedures |", "|---|---|---|"]
    out += [f"| `{lv['capability']}` | {lv['to_full']} | {', '.join(lv['procedures']) or '-'} |"
            for lv in rep["levers"]]
    if not rep["levers"]:
        out += ["| - | 0 | the profile declares all seven capabilities |"]
    out += ["", "## Procedures", "",
            "The ★ and the method class are copied from the record. The harness never computes them.", "",
            "| Needs status | Meaning |", "|---|---|"]
    out += [f"| `{k}` | {v} |" for k, v in NEEDS_STATUS.items()]
    out += ["", "| TP | IRQ | Title | ★ | Method class | Result | Needs | Absent | Needs status | FAIAS lines |",
            "|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        out.append(f"| {r['tp']} | {r['irq']} | {_cell(r['title'])} | {r['star']} | {_cell(r['method_class'])} | "
                   f"`{r['result']}` | {_codes(r['needed'])} | {_codes(r['missing'])} | `{r['needs_status']}` | "
                   f"{_cell(', '.join(r['lines']))} |")
    out += ["", "## Inputs", "", "| File | SHA-256 |", "|---|---|", f"| `{n['file']}` | `{n['sha256']}` |"]
    out += [f"| `{f['file']}` | `{f['sha256']}` |" for f in rep["inputs"]["records"]]
    return "\n".join(out) + "\n"


def write(profile, out, sign_key=None):
    """Write testability.json, its signature (with a key) and testability.md to `out`. Return the report."""
    rep = build(profile, signed=bool(sign_key))
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    data = (runlog.canonical(rep) + "\n").encode("utf-8")
    (out / JSON_NAME).write_bytes(data)
    if sign_key:
        runlog.sign_log(out / JSON_NAME, sign_key)
    (out / MD_NAME).write_bytes(markdown(rep, runlog.sha256_hex(data)).encode("utf-8"))
    return rep


def check_report(rep):
    """The problems in the form of a report (empty if none): each result, the summary and the levers must follow
    from the needs and the capabilities that the report itself lists."""
    if rep.get("schema") != SCHEMA:
        return ["unknown schema"]
    problems = []
    if not (rep.get("needs_draft") or {}).get("status_line"):
        problems.append("the status line of the draft needs is absent")
    have = set((rep.get("profile") or {}).get("capabilities") or [])
    rows = rep.get("rows") or []
    counts = {"full": 0, "partly": 0, "none": 0}
    for r in rows:
        result, missing = judge(r.get("needed") or [], have)
        counts[result] += 1
        if r.get("result") != result or r.get("missing") != missing:
            problems.append(f"{r.get('tp')}: the result does not follow from its needs and the profile capabilities")
    if rep.get("summary") != {"procedures": len(rows), **counts}:
        problems.append("the summary does not equal the counts of the rows")
    if rep.get("levers") != levers({r.get("tp"): r.get("needed") or [] for r in rows}, have):
        problems.append("the lever table does not follow from the rows")
    return problems


def verify(json_path, pubkey_path, sig_path=None):
    """Return (ok, message). The signature is over the exact bytes of testability.json."""
    json_path = Path(json_path)
    sig_path = Path(sig_path) if sig_path else json_path.with_name(json_path.name + ".sig")
    try:
        s = json.loads(sig_path.read_bytes().decode("utf-8"))
        data = json_path.read_bytes()
        pub = serialization.load_pem_public_key(Path(pubkey_path).read_bytes())
        pub.verify(base64.b64decode(s["signature_b64"]), data)
    except InvalidSignature:
        return False, "FAIL: the signature does not match the report"
    except Exception as e:      # noqa: BLE001
        return False, f"FAIL: cannot verify ({type(e).__name__})"
    try:
        problems = check_report(json.loads(data.decode("utf-8")))
    except ValueError as e:
        problems = [f"cannot read the report ({type(e).__name__})"]
    if problems:
        return False, "FAIL: the signature is valid but the report is malformed: " + "; ".join(problems)
    return True, "PASS: signature valid (pinned public key); each result follows from the needs and the capabilities"


def _inside(path, folder):
    path, folder = Path(path).resolve(), Path(folder).resolve()
    return path == folder or folder in path.parents


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:1] == ["verify"]:
        ap = argparse.ArgumentParser(prog="faias_harness.testability verify",
                                     description="Verify the signature and the form of a testability report.")
        ap.add_argument("--report", required=True, help="the testability.json file")
        ap.add_argument("--pubkey", required=True, help="the public key file of the signing key")
        ap.add_argument("--sig", help="the signature file (default: the report path with .sig added)")
        a = ap.parse_args(argv[1:])
        ok, msg = verify(a.report, a.pubkey, a.sig)
        print(msg)
        return 0 if ok else 1
    ap = argparse.ArgumentParser(prog="faias_harness.testability", description=__doc__.split("\n\n")[0],
                                 epilog="To verify a signed report: python -m faias_harness.testability verify "
                                        "--report <testability.json> --pubkey <public key file>")
    ap.add_argument("--profile", required=True, help="path to a target profile YAML")
    ap.add_argument("--out", required=True, help="the folder for the report (not inside records/ or faias_harness/)")
    ap.add_argument("--sign-key", help="the run-signing private key (outside the repository)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    for name in BARRED_OUT:
        if _inside(out, REPO / name):
            print(f"REFUSED: the --out folder is inside {name}/. Choose a folder outside it.")
            return 2
    held = [n for n in (JSON_NAME, MD_NAME, SIG_NAME) if (out / n).exists()]
    if held:
        print(f"REFUSED: the folder holds a report file already ({', '.join(held)}). Choose a new folder.")
        return 2
    if a.sign_key:
        problems = runlog.key_problems(a.sign_key)
        if problems:
            print("REFUSED: " + "; ".join(problems))
            return 2
    try:
        profile = load_profile(a.profile)
    except ProfileError as e:
        print("PROFILE INVALID:")
        for p in e.problems:
            print(f"  - {p}")
        return 2
    rep = write(profile, out, a.sign_key)
    s = rep["summary"]
    print(f"Result for {s['procedures']} procedures: full {s['full']}, partly {s['partly']}, none {s['none']}")
    print(DRAFT_NOTE)
    print(f"Report: {out / JSON_NAME}\nReport: {out / MD_NAME}")
    print(f"Signature: {out / SIG_NAME}" if a.sign_key else "Signature: none (unsigned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
