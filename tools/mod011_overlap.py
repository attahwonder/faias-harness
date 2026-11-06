"""Check each comparator of TP-MOD-011 for a run of words in common with GSM8K question text, offline.

  py -3.14 -X utf8 tools/mod011_overlap.py --comparators <file> --gsm8k <train.jsonl> <test.jsonl> --out <new file>

The comparator file is private, so keep it and the result outside the repository. The GSM8K files are the files that
the operator names: train.jsonl and test.jsonl of the GSM8K repository. The tool reads the field `question` of each of
their lines. It never reads the answer text. For each comparator it finds the longest run of words of its question
that is also a run of words of one GSM8K question (the words of mod011.words; a run never spans two questions). It
writes the result file: the SHA-256 of the comparator file and of each GSM8K file, the limit, and for each comparator
its ID, the length of its longest run and where that run is (file and line index, from 0). The result holds no text.
mod011.check_comparators reads it. A run of OVERLAP_LIMIT (8) words or more is a problem there.

The tool reads no network and calls no model. It refuses an --out path that exists. It refuses a comparator file or a
GSM8K file that is not found, or that the system does not let it read (read_file). It refuses a comparator file in
which a JSON object holds a key more than once, at any depth, with the key, as the engine does (jsonkeys.unique_keys):
plain json.loads keeps only the last value. It also refuses a comparator file that is not UTF-8 JSON, one that does
not hold one JSON object, and one whose field comparators is not a list of objects (read_comparators), in the words of
the refusals of TP-SEC-002. It refuses a GSM8K file that is not UTF-8, and a GSM8K line that is not JSON, that does
not hold one JSON object, or whose field question is not a text, with the file and the line index (question_stream).
It does not read such a question as absent, because an absent question would hide an overlap. A comparator whose
question is not a text is read as absent: its longest run is 0, and mod011.check_comparators still refuses it
(overlap). A refused file gives no result. Exit 0 when each run is under the limit, 1 when a run is at or above it, 2
when it refuses.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from faias_harness.engine import jsonkeys, mod011, sec002, sec006  # noqa: E402

VERSION = "1"
_GAP = "\0"         # between two questions; no word of a comparator is this token, so no run spans it
# The refusal of a field comparators that is not a list of objects, in the form of the TP-SEC-002 line of the preflight
# for the field families ("the field families of the item file is not a list of texts").
NOT_A_LIST = "the field comparators is not a list of objects"


def read_file(path, what):
    """The bytes of a file that the operator names. Raises ValueError "the <what> <path> is not found" for a path that
    does not exist, in the words of the preflight for an absent item file. Raises ValueError "the <what> <path> cannot
    be read (<reason>)" for a path that the system does not let the tool read, for example a folder."""
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        raise ValueError(f"the {what} {path} is not found") from None
    except OSError as e:
        raise ValueError(f"the {what} {path} cannot be read ({e.strerror or type(e).__name__})") from None


def question_stream(files):
    """(the words of all questions, one gap token after each; the (file, line index) of each word). `files` is a list
    of (name, raw bytes). Only the field `question` of each line is read. A blank line is skipped. Raises ValueError,
    with the file and the line index, in the words of the refusals of TP-SEC-002: for a file that is not UTF-8, and
    for a line that is not JSON, that does not hold one JSON object, or whose field question is not a text. Such a
    question is not read as absent, because an absent question would hide an overlap."""
    stream, owner = [], []
    for name, raw in files:
        try:
            text = raw.decode("utf-8")
        except ValueError as e:         # UnicodeDecodeError is a ValueError
            raise ValueError(f"the GSM8K file {name}: the file is not UTF-8 JSON ({e})") from None
        for n, line in enumerate(text.splitlines()):
            if not line.strip():
                continue
            where = f"the GSM8K file {name}, line index {n}"
            try:
                data = json.loads(line)
            except ValueError as e:     # json.JSONDecodeError is a ValueError
                raise ValueError(f"{where}: the line is not UTF-8 JSON ({e})") from None
            if not isinstance(data, dict):
                raise ValueError(f"{where}: the line does not hold one JSON object")
            if not isinstance(data.get("question"), str):
                raise ValueError(f"{where}: the field question is not a text")
            w = mod011.words(data["question"])
            stream += w + [_GAP]
            owner += [(name, n)] * (len(w) + 1)
    return stream, owner


def overlap(comparators, files):
    """One row for each comparator: its ID, its longest run in common with the question text of `files`, and where.
    A question of a comparator that is not a text is read as absent, by the rule of the TP-SEC-002 item check: it
    gives no words, so its longest run is 0. mod011.check_comparators still refuses such a comparator
    (mod011._comparator_form)."""
    stream, owner = question_stream(files)
    pos = sec006._word_index(stream)
    rows = []
    for c in comparators:
        q = c.get("question")
        w = mod011.words(q if isinstance(q, str) else "")
        best, end = sec006._longest_run(w, pos)
        row = {"id": c.get("id"), "longest_run": best, "at": None}
        if best:
            run = w[end - best + 1:end + 1]
            p = next(p for p in pos[run[0]] if stream[p:p + best] == run)
            row["at"] = {"file": owner[p][0], "line_index": owner[p][1]}
        rows.append(row)
    return rows


def read_comparators(comparator_raw):
    """The list comparators of a comparator file, from its bytes. Raises jsonkeys.RepeatedKey (a ValueError) for a JSON
    object that holds a key more than once. Raises ValueError, in the words of TP-SEC-002, for a file that is not UTF-8
    JSON (preflight._sec002_item_file), for a file that does not hold one JSON object (sec002.NOT_ONE_OBJECT), and for a
    field comparators that is not a list of objects (NOT_A_LIST)."""
    try:
        data = json.loads(comparator_raw.decode("utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey:
        raise
    except ValueError as e:         # json.JSONDecodeError and UnicodeDecodeError are ValueError
        raise ValueError(f"the file is not UTF-8 JSON ({e})") from None
    if not isinstance(data, dict):
        raise ValueError(sec002.NOT_ONE_OBJECT)
    comps = data.get("comparators")
    if not (isinstance(comps, list) and all(isinstance(c, dict) for c in comps)):
        raise ValueError(NOT_A_LIST)
    return comps


def result(comparator_raw, comparator_name, files):
    """The result file, as a dict. Raises ValueError for a comparator file that read_comparators refuses, and for a
    GSM8K file that question_stream refuses."""
    rows = overlap(read_comparators(comparator_raw), files)
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
    try:
        raw = read_file(comp, "comparator file")
        try:
            read_comparators(raw)
        except ValueError as e:     # each refusal of read_comparators; jsonkeys.RepeatedKey is a ValueError
            raise ValueError(f"the comparator file {comp}: {e}") from None
        res = result(raw, comp.name, [(Path(p).name, read_file(p, "GSM8K file")) for p in a.gsm8k])
    except ValueError as e:         # the refusals of read_file, read_comparators and question_stream
        print(f"refused: {e}")
        return 2
    out.write_bytes((json.dumps(res, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    print(f"wrote {out}: {len(res['results'])} comparators; {len(res['at_or_above_limit'])} at or above the limit of "
          f"{mod011.OVERLAP_LIMIT} words: {', '.join(map(str, res['at_or_above_limit'])) or 'none'}")
    return 1 if res["at_or_above_limit"] else 0


if __name__ == "__main__":
    sys.exit(main())
