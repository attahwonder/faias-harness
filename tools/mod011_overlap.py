"""Check each comparator of TP-MOD-011 for a run of words in common with GSM8K question text, offline.

  py -3.14 -X utf8 tools/mod011_overlap.py --comparators <file> --gsm8k <train.jsonl> <test.jsonl> --out <new file>

The comparator file is private, so keep it and the result outside the repository. The GSM8K files are the files that
the operator names: train.jsonl and test.jsonl of the GSM8K repository. The tool reads the field `question` of each of
their lines. It never reads the answer text. For each comparator it finds the longest run of words of its question
that is also a run of words of one GSM8K question (the words of mod011.words; a run never spans two questions). It
writes the result file: the SHA-256 of the comparator file and of each GSM8K file, the limit, and for each comparator
its ID, the length of its longest run and where that run is (file and line index, from 0). The result holds no text.
mod011.check_comparators reads it. A run of OVERLAP_LIMIT (8) words or more is a problem there.

The tool reads no network and calls no model. It refuses an --out path that exists. Exit 0 when each run is under the
limit, 1 when a run is at or above it, 2 when it refuses.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from faias_harness.engine import mod011, sec006  # noqa: E402

VERSION = "1"
_GAP = "\0"         # between two questions; no word of a comparator is this token, so no run spans it


def question_stream(files):
    """(the words of all questions, one gap token after each; the (file, line index) of each word). `files` is a list
    of (name, raw bytes). Only the field `question` of each line is read."""
    stream, owner = [], []
    for name, raw in files:
        for n, line in enumerate(raw.decode("utf-8").splitlines()):
            if line.strip():
                w = mod011.words(json.loads(line).get("question") or "")
                stream += w + [_GAP]
                owner += [(name, n)] * (len(w) + 1)
    return stream, owner


def overlap(comparators, files):
    """One row for each comparator: its ID, its longest run in common with the question text of `files`, and where."""
    stream, owner = question_stream(files)
    pos = sec006._word_index(stream)
    rows = []
    for c in comparators:
        w = mod011.words(c.get("question") or "")
        best, end = sec006._longest_run(w, pos)
        row = {"id": c.get("id"), "longest_run": best, "at": None}
        if best:
            run = w[end - best + 1:end + 1]
            p = next(p for p in pos[run[0]] if stream[p:p + best] == run)
            row["at"] = {"file": owner[p][0], "line_index": owner[p][1]}
        rows.append(row)
    return rows


def result(comparator_raw, comparator_name, files):
    """The result file, as a dict."""
    data = json.loads(comparator_raw.decode("utf-8"))
    rows = overlap(data.get("comparators") or [], files)
    return {"tool": "tools/mod011_overlap.py", "version": VERSION, "procedure": mod011.TP,
            "reads": "the field question of each GSM8K line; the answer text is not read",
            "limit": mod011.OVERLAP_LIMIT,
            "comparator_file": {"name": comparator_name, "sha256": hashlib.sha256(comparator_raw).hexdigest()},
            "gsm8k_files": [{"name": name, "sha256": hashlib.sha256(raw).hexdigest(),
                             "questions": sum(1 for line in raw.decode("utf-8").splitlines() if line.strip())}
                            for name, raw in files],
            "results": rows,
            "at_or_above_limit": [r["id"] for r in rows if r["longest_run"] >= mod011.OVERLAP_LIMIT]}


def main(argv=None):
    ap = argparse.ArgumentParser(description="Check the comparators of TP-MOD-011 against GSM8K question text.")
    ap.add_argument("--comparators", required=True, help="the private comparator file (JSON)")
    ap.add_argument("--gsm8k", nargs="+", required=True, help="the GSM8K files to read: train.jsonl and test.jsonl")
    ap.add_argument("--out", required=True, help="a new file for the result (JSON)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    if out.exists():
        print(f"refused: {out} exists")
        return 2
    comp = Path(a.comparators)
    res = result(comp.read_bytes(), comp.name, [(Path(p).name, Path(p).read_bytes()) for p in a.gsm8k])
    out.write_bytes((json.dumps(res, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(f"wrote {out}: {len(res['results'])} comparators; {len(res['at_or_above_limit'])} at or above the limit of "
          f"{mod011.OVERLAP_LIMIT} words: {', '.join(map(str, res['at_or_above_limit'])) or 'none'}")
    return 1 if res["at_or_above_limit"] else 0


if __name__ == "__main__":
    sys.exit(main())
