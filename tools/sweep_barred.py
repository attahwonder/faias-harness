"""Sweep the repository for barred terms and for key-like strings.

  py -3.14 tools/sweep_barred.py --faias <path to the FAIAS tree> [--root <dir>]

Barred terms are stored reversed, so that this file does not contain them. The match ignores case.
Text that a record quotes from FAIAS is never changed. A hit for the name of one AI vendor in a quoted field of
records/TP-*.yaml is allowed if the cited FAIAS source line contains the same name. The sweep reports it as
"allowed: FAIAS citation". Every other hit fails. Exit 0 only if there are no failing hits.
"""
import argparse
import re
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]

_REVERSED = [
    "nevroN", "azriM", "demmahoM", "imiK", "keeSpeeD", "oMiM", "veJ", "efaSepyT", "rrevif", "tidder",
    "SNOITISOP", "REKCART", "FFODNAH", "GOL_KROW", "GOLKCAB", "dm.EDUALC", "eduaLC", "ciporhtnA",
    "rotanidrooc", "TRA_ROIRP", "WEIVER_SDROCER", "_TNEDNEPEDNI", "\\lanretni", "/lanretni", "nebrednowhatta",
    "imoaiX", "secapskroWtiduA",
]
BARRED = [t[::-1] for t in _REVERSED]
BARRED_RX = re.compile("|".join(re.escape(t) for t in BARRED) + r"|§1[89]\d|§2\d\d", re.I)
ALLOWED_IF_CITED = "ciporhtnA"[::-1].lower()

KEY_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"Bearer\s+[A-Za-z0-9._\-]{20,}"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"'][^\"'\s]{12,}[\"']"),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
]
SKIP_DIRS = {".git", "__pycache__", "runs", ".pytest_cache"}


def files(root):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not (set(p.relative_to(root).parts) & SKIP_DIRS):
            yield p


def leaves(node, trail=()):
    """Yield (item, field, string) for every string field of the items in a record."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                yield node, k, v
            else:
                yield from leaves(v, trail + (k,))
    elif isinstance(node, list):
        for v in node:
            yield from leaves(v, trail)


def sweep_record(path, faias, fails, allowed):
    """A record file: scan comment lines raw, and every string field apart, so a quoted FAIAS line can be allowed."""
    text = path.read_text(encoding="utf-8")
    for n, line in enumerate(text.splitlines(), 1):
        if line.lstrip().startswith("#") and BARRED_RX.search(line):
            fails.append(f"{path}:{n}: {BARRED_RX.search(line).group(0)!r}")
    rec = yaml.safe_load(text)
    for item, field, s in leaves(rec):
        hits = {m.group(0).lower() for m in BARRED_RX.finditer(s)}
        if not hits:
            continue
        src = str(item.get("src", ""))
        ok = False
        if hits == {ALLOWED_IF_CITED} and field in ("text", "basis_source") and faias is not None:
            m = re.fullmatch(r"(.+\.md):(\d+)", src)
            if m and (faias / m.group(1)).is_file():
                lines = (faias / m.group(1)).read_text(encoding="utf-8").splitlines()
                ok = 1 <= int(m.group(2)) <= len(lines) and ALLOWED_IF_CITED in lines[int(m.group(2)) - 1].lower()
        if ok:
            allowed.append(f"allowed: FAIAS citation  {path.name} {field} at {src}")
        else:
            fails.append(f"{path}: field {field!r}: {sorted(hits)} (src {src or 'none'})")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--faias", default=None, help="path to the FAIAS tree, needed to allow cited text in records")
    ap.add_argument("--root", default=str(REPO))
    a = ap.parse_args(argv)
    root = Path(a.root)
    faias = Path(a.faias) if a.faias else None
    fails, allowed, n = [], [], 0
    for p in files(root):
        n += 1
        rel = p.relative_to(root).as_posix()
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            fails.append(f"{rel}: not UTF-8 text")
            continue
        if re.fullmatch(r"records/TP-[A-Z]+-\d{3}\.yaml", rel):
            sweep_record(p, faias, fails, allowed)
        else:
            for ln, line in enumerate(text.splitlines(), 1):
                m = BARRED_RX.search(line)
                if m:
                    fails.append(f"{rel}:{ln}: barred term {m.group(0)!r}")
        for ln, line in enumerate(text.splitlines(), 1):
            for rx in KEY_PATTERNS:
                if rx.search(line):
                    fails.append(f"{rel}:{ln}: key-like string")
    for line in allowed:
        print(line)
    for line in fails:
        print("FAIL", line)
    print(f"sweep_barred: {n} files, {len(fails)} hit(s), {len(allowed)} allowed (FAIAS citation)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
