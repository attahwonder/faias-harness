"""Make the item file of TP-MOD-011 (items/TP-MOD-011.json) from a copy of the GSM8K test split, offline.

  py -3.14 -X utf8 tools/mod011_items.py --source <test.jsonl> --out <new file> [--seed N]

The source is the file test.jsonl of the GSM8K repository (the allow-list entry gsm8k of faias_harness/engine/mod011.py
names its URL). The tool refuses a source whose SHA-256 is not the one in the digest file of the entry
(third_party/gsm8k/gsm8k.sha256). It reads no network and calls no model. It refuses an --out path that exists.

The selection. An item qualifies when its question has a sentence end that leaves a remainder of 15 words or more
(mod011.qualifying_cuts). A random generator with the seed picks COUNT of the qualifying items, which are then taken in
the order of the source. For each one, in that order, the generator picks one of its qualifying cuts. Each item records
its source line index (from 0), the question, the cut, the prefix and the remainder, and two numbers from the answer
field: the count of calculator annotations (steps) and the final answer. The answer text is not written. The file
records the seed and the version of this tool, so the same source and seed give the same bytes.

The tool prints the SHA-256 of the file that it wrote. The digest file of the entry pins that value; a new file needs a
new digest line, which is a change for review.
"""
import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from faias_harness.engine import mod011  # noqa: E402
from faias_harness.text import escape_streams  # noqa: E402

VERSION = "1"
SEED = 20261103
COUNT = mod011.MIN_ITEMS
BENCHMARK = "gsm8k"


def source_rows(raw):
    """(line index, row) for each line of the source that holds a JSON object, with its index in the file from 0."""
    return [(n, json.loads(line)) for n, line in enumerate(raw.decode("utf-8").splitlines()) if line.strip()]


def select(rows, seed=SEED, count=COUNT):
    """The items for `rows` ((line index, row) pairs) and `seed`, in the order of the source."""
    pool = [(n, r, mod011.qualifying_cuts(r["question"])) for n, r in rows]
    pool = [p for p in pool if p[2]]
    rng = random.Random(seed)
    out = []
    for n, row, cuts in (pool[k] for k in sorted(rng.sample(range(len(pool)), count))):
        cut = rng.choice(cuts)
        prefix, remainder = mod011.split_at(row["question"], cut)
        steps, answer = mod011.answer_features(row["answer"])
        out.append({"id": f"{BENCHMARK}-test-{n:04d}", "source_index": n, "question": row["question"], "cut": cut,
                    "prefix": prefix, "remainder": remainder, "steps": steps, "answer": answer})
    return out, len(pool)


def build(rows, seed=SEED, count=COUNT):
    """The item file, as a dict."""
    e = mod011.ALLOW_LIST[BENCHMARK]
    items, qualifying = select(rows, seed, count)
    return {"procedure": mod011.TP, "benchmark": BENCHMARK, "name": e["name"], "split": e["split"],
            "source": e["source"], "source_digest_file": e["digest_file"],
            "licence": f"{e['licence']}; the licence text is in {e['licence_file']}",
            "generated_by": {"tool": "tools/mod011_items.py", "version": VERSION, "seed": seed, "count": count,
                             "source_rows": len(rows), "qualifying_rows": qualifying,
                             "rule": f"a cut at a sentence end ('.', '?' or '!' followed by a space) that leaves a "
                                     f"remainder of {mod011.MIN_REMAINDER_WORDS} words or more; the answer text is "
                                     "not written"},
            "items": items}


def dump(data):
    """The bytes of the item file: JSON with an indent of one space, UTF-8, one final newline."""
    return (json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def main(argv=None):
    escape_streams()
    ap = argparse.ArgumentParser(description="Make the item file of TP-MOD-011 from a copy of the GSM8K test split.")
    ap.add_argument("--source", required=True, help="the file test.jsonl of the GSM8K repository")
    ap.add_argument("--out", required=True, help="a new file")
    ap.add_argument("--seed", type=int, default=SEED)
    a = ap.parse_args(argv)
    raw, out = Path(a.source).read_bytes(), Path(a.out)
    e = mod011.ALLOW_LIST[BENCHMARK]
    want = (mod011.read_digests(REPO / e["digest_file"]) or {}).get(e["source_file"])
    if hashlib.sha256(raw).hexdigest() != want:
        print(f"refused: the source does not have the SHA-256 of {e['source_file']} in {e['digest_file']}")
        return 2
    if out.exists():
        print(f"refused: {out} exists")
        return 2
    data = dump(build(source_rows(raw), a.seed))
    out.write_bytes(data)
    print(f"wrote {out}: {COUNT} items, seed {a.seed}, SHA-256 {hashlib.sha256(data).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
