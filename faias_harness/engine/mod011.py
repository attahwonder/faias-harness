"""TP-MOD-011 (benchmark contamination), Component 1: the allow-list of benchmarks, the item file, the probe template,
a deterministic match scorer, the measure M1, and the check of the comparator file.

This module does not import `inspect_ai`. It makes no network call and no model call.

The allow-list. ALLOW_LIST is a fixed map of the benchmarks that a run may probe. A settings file cannot add an entry.
Each entry names its source, its split, its kind, its licence and licence file, its item file and its digest file. The
digest file is in sha256sum form. It holds the SHA-256 of the source files as fetched (not shipped) and of the shipped
licence file and item file. The entry pins the digest file in turn: digest_prefix is the first DIGEST_PREFIX_HEX
characters of its SHA-256, so a change to the digest file is a change to this module. benchmark_problems refuses, each
with its reason: an ID that is not in the map; an absent or changed licence file; an absent digest file, or one whose
SHA-256 does not start with digest_prefix; an absent item file, or one whose SHA-256 differs from the digest file; an
item file in which a JSON object holds a key more than once (jsonkeys.unique_keys); and an item file that names another
split.

The item file (items/TP-MOD-011.json). tools/mod011_items.py makes it from a copy of the source and a seed. Each item
is a question of the test split, cut at a sentence end into a prefix and a remainder. A sentence ends at ".", "?" or
"!" followed by a space (SENTENCE_END). The remainder holds MIN_REMAINDER_WORDS words or more (a rule of this harness).
Each item records its source line index, the question, the cut, the prefix, the remainder and two match features from
the answer field: the count of calculator annotations (steps) and the final answer. The answer text is not shipped.

The probe. PROBE_TEMPLATE names the data set and its split, gives the first piece and asks for the second piece as the
data set holds it. It follows the guided instruction of Golchin and Surdeanu (ICLR 2024, arXiv 2308.08493v3) in its
parts; the words are this harness's. Both arms get the same template with the same data set name
(PLAYBOOK_MOD_011.md line 31). TEMPLATE_SHA256 is its SHA-256.

The scorer (score_completion), for each reply. It uses no model judge. The words of a text are its NFKC form in lower
case, with each run of other characters as one space (sec006._words, so a letter outside ASCII is dropped). An echoed
"First piece" label, an echoed prefix and a "Second piece" label are removed from the start of the reply first.
  exact        the reply starts with the words of the remainder
  near_exact   not exact, and the ROUGE-L F1 over words of the remainder and the first n words of the reply is at or
               above tau, where n is the word count of the remainder. The reviewer sets tau. With no tau, no reply is
               near-exact
  unrelated    any other reply with text
  no_result    an empty reply, an error or a block. It is counted apart and left out of the rates
Each score also holds the ROUGE-L value and the longest run of remainder words in the reply. It is an uncalibrated
match rule: the cited method labels a near-exact match by human judgement (section 4).

M1 (compute_m1). For each arm, the counts of the four classes, and over items the exact rate and the exact-or-near-exact
rate, each with a Wilson 95% interval. An item takes its highest class over its k attempts. The per-attempt rates
stand beside it. The primary form is the exact-only differential (the benchmark rate minus the comparator rate), with a
one-sided Fisher exact p and materially_higher against the reviewer's bar. The secondary form is the same for exact or
near-exact. Beside them, the comparator arm's two rates are a reading, not a measure (COMPARATOR_READING). The harness
decides no Pass, Fail or Partial.

The comparator file. It is private: it stays outside the repository. Each comparator has the keys of COMPARATOR_KEYS:
its ID, its benchmark, the benchmark item that it is matched with (pair), the question, the cut, the prefix, the
remainder, the creation date (YYYY-MM-DD), the writer's model family, the steps and the answer. check_comparators gives
one problem for each fault. tools/mod011_overlap.py makes the overlap result that it reads. match_features gives the
match features of each arm and each pair; the harness refuses no pair on them.

The instrument check (a rule of this harness). INSTRUMENT_FILE holds ten short passages of public-domain text: the
opening of ten works. The maintainer made it offline from the openings of the works, with a tool that is not in this
repository. Each passage is cut by the rule of the items (a sentence end, a remainder of MIN_REMAINDER_WORDS words or
more). The run probes each one with PROBE_TEMPLATE before the arms, with the data set name INSTRUMENT_DATASET and the
title of the work as the split, and scores the reply with score_completion. A probe and a scorer that work give a high
exact rate on such well-known text; a low rate points to a refusal or a broken probe. The result is a reading in the
header, not part of M1. instrument_problems refuses an absent file, a digest file whose SHA-256 does not start with
INSTRUMENT_DIGEST_PREFIX, a SHA-256 that differs from INSTRUMENT_DIGEST_FILE, a JSON object with a repeated key, and a
passage of the wrong form.
"""
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import jsonkeys, sec001, sec006

REPO = Path(__file__).resolve().parents[2]
TP = "TP-MOD-011"
FAIAS_VERSION = sec001.FAIAS_VERSION
ITEM_FILE = "items/TP-MOD-011.json"
MIN_ITEMS = 50              # PLAYBOOK_MOD_011.md line 18: "n≥50 benchmark items and n≥50 matched novel comparators"
MIN_REMAINDER_WORDS = 15    # a rule of this harness
OVERLAP_LIMIT = 8           # a rule of this harness: a comparator shares no run of 8 words with GSM8K question text
DIGEST_PREFIX_HEX = 16      # the pin of a digest file: the first 16 hex characters of its SHA-256 (the sweep bars 40)
# A new entry is a code change, with its own licence fetch and the owner's approval.
ALLOW_LIST = {
    "gsm8k": {"name": "GSM8K", "kind": "capability", "split": "test",
              "source": ("https://raw.githubusercontent.com/openai/grade-school-math/master/grade_school_math/data/"
                         "test.jsonl"),
              "source_file": "test.jsonl", "overlap_files": ("train.jsonl", "test.jsonl"),
              "licence": "MIT", "licence_file": "third_party/gsm8k/LICENSE", "item_file": ITEM_FILE,
              "digest_file": "third_party/gsm8k/gsm8k.sha256", "digest_prefix": "b6cc025d3ee522b9"},
}
SENTENCE_END = re.compile(r"[.?!](?= )")
NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
CALCULATOR = re.compile(r"<<[^<>]*>>")
CLASSES = ("exact", "near_exact", "unrelated", "no_result")
RANK = {"no_result": 0, "unrelated": 1, "near_exact": 2, "exact": 3}
PROBE_TEMPLATE = ("Here is the first piece of an item from the {split} split of the {dataset} data set. Complete the "
                  "item: write its second piece in the words that the data set holds.\n\nFirst piece: {first_piece}\n"
                  "Second piece:")
TEMPLATE_SHA256 = hashlib.sha256(PROBE_TEMPLATE.encode("utf-8")).hexdigest()
FIRST_LABEL, SECOND_LABEL = "First piece", "Second piece"
COMPARATOR_READING = "comparator arm at or near chance (a reading, not a measure)"
COMPARATOR_KEYS = ("id", "benchmark", "pair", "question", "cut", "prefix", "remainder", "created", "writer_family",
                   "steps", "answer")
FEATURES = ("prefix_words", "remainder_words", "sentences", "numbers", "steps", "answer_digits")
_DIGEST_LINE = re.compile(r"([0-9a-f]{64})  (\S+)")


# ---------- words and cuts ----------

def words(text):
    """The words of a text: NFKC, lower case, letters and digits (sec006._words)."""
    return sec006._words(unicodedata.normalize("NFKC", text or ""))


def sentence_ends(question):
    """The cuts of a question: the index just after each ".", "?" or "!" that a space follows."""
    return [m.end() for m in SENTENCE_END.finditer(question)]


def split_at(question, cut):
    """(prefix, remainder) of a question cut at `cut`."""
    return question[:cut], question[cut:].strip()


def qualifying_cuts(question):
    """The sentence ends that leave a remainder of MIN_REMAINDER_WORDS words or more."""
    return [c for c in sentence_ends(question) if len(words(question[c:])) >= MIN_REMAINDER_WORDS]


def answer_features(answer_text):
    """(steps, answer) from a GSM8K answer field: the count of calculator annotations, and the number after ####."""
    final = answer_text.rsplit("####", 1)[-1].strip().replace(",", "")
    try:
        value = int(final)
    except ValueError:
        value = float(final)
    return len(CALCULATOR.findall(answer_text)), value


def _cut_problems(eid, question, cut, prefix, remainder):
    """The problems of one cut (empty if none): the prefix and the remainder are the question cut at `cut`, the cut is
    at a sentence end, and the remainder is long enough."""
    if split_at(question, cut) != (prefix, remainder):
        return [f"{eid}: the prefix and the remainder are not the question cut at {cut}"]
    out = []
    if cut not in sentence_ends(question):
        out.append(f"{eid}: the cut {cut} is not at a sentence end")
    n = len(words(remainder))
    if n < MIN_REMAINDER_WORDS:
        out.append(f"{eid}: the remainder has {n} words; the minimum is {MIN_REMAINDER_WORDS}")
    return out


# ---------- the allow-list and the item file ----------

def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def pin_matches(path, prefix):
    """True when the SHA-256 of the file at `path` starts with the pinned `prefix` of DIGEST_PREFIX_HEX characters."""
    return len(prefix) == DIGEST_PREFIX_HEX and _sha256(Path(path).read_bytes()).startswith(prefix)


def read_digests(path):
    """{file name: SHA-256} from a digest file in sha256sum form. None if the file is absent. A line in another form
    is left out."""
    path = Path(path)
    if not path.is_file():
        return None
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _DIGEST_LINE.fullmatch(line)
        if m:
            out[m.group(2)] = m.group(1)
    return out


def entry(benchmark, repo=REPO):
    """The allow-list entry of a benchmark, with the digests of its digest file (source_sha256, overlap_sha256,
    licence_sha256 and item_sha256), for the header of a run. None if the ID is not in the map."""
    e = ALLOW_LIST.get(benchmark)
    if e is None:
        return None
    d = read_digests(Path(repo) / e["digest_file"]) or {}
    return {**e, "overlap_files": list(e["overlap_files"]), "source_sha256": d.get(e["source_file"]),
            "overlap_sha256": {f: d.get(f) for f in e["overlap_files"]},
            "licence_sha256": d.get(Path(e["licence_file"]).name), "item_sha256": d.get(Path(e["item_file"]).name)}


def benchmark_problems(benchmark, repo=REPO):
    """Why a run may not probe `benchmark` (empty if it may): the refusals of the allow-list, then the problems of the
    item file (check_item_set)."""
    e = ALLOW_LIST.get(benchmark)
    if e is None:
        return [f"the benchmark {benchmark!r} is not on the allow-list of {TP} ({', '.join(ALLOW_LIST)}); a new entry "
                "is a code change, with its own licence fetch"]
    repo = Path(repo)
    digests = read_digests(repo / e["digest_file"])
    if digests is None:
        return [f"{benchmark}: the digest file {e['digest_file']} is absent, so no file of the entry can be checked"]
    if not pin_matches(repo / e["digest_file"], e["digest_prefix"]):
        return [f"{benchmark}: the digest file {e['digest_file']} does not have the SHA-256 that mod011.py pins "
                f"({e['digest_prefix']}...); a changed digest file is a code change"]
    problems = []
    licence = repo / e["licence_file"]
    if not licence.is_file():
        problems.append(f"{benchmark}: the licence file {e['licence_file']} is absent; the {e['licence']} licence asks "
                        "that its text goes with each copy of the items")
    elif _sha256(licence.read_bytes()) != digests.get(licence.name):
        problems.append(f"{benchmark}: the licence file {e['licence_file']} does not have the SHA-256 of the digest "
                        "file")
    path = repo / e["item_file"]
    if not path.is_file():
        return problems + [f"{benchmark}: the item file {e['item_file']} is absent"]
    raw = path.read_bytes()
    if _sha256(raw) != digests.get(path.name):
        return problems + [f"{benchmark}: the item file {e['item_file']} does not have the SHA-256 of the digest "
                           f"file {e['digest_file']}"]
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey as err:     # in the form of the TP-SEC-002 refusal (preflight._sec002_item_file)
        return problems + [f"{benchmark}: the item file {e['item_file']} is refused: {err}"]
    return problems + check_item_set(data, benchmark)


def check_item_set(data, benchmark):
    """The problems of an item file of one benchmark (empty if none): the procedure, the benchmark, the split of the
    entry, the count, unique IDs, and the form and cut of each item."""
    e = ALLOW_LIST[benchmark]
    problems = []
    if data.get("procedure") != TP:
        problems.append(f"the item file does not name the procedure {TP}")
    if data.get("benchmark") != benchmark:
        problems.append(f"the item file does not name the benchmark {benchmark}")
    if data.get("split") != e["split"]:
        problems.append(f"the item file names the split {data.get('split')!r}; the allow-list allows the split "
                        f"{e['split']} of {benchmark} only")
    items = data.get("items") if isinstance(data.get("items"), list) else []
    if len(items) < MIN_ITEMS:
        problems.append(f"the item file holds {len(items)} items; the minimum is {MIN_ITEMS} (PLAYBOOK_MOD_011.md "
                        "line 18)")
    ids = Counter(i.get("id") for i in items if isinstance(i, dict))
    problems += [f"the item ID {k} is in the file more than once" for k, n in ids.items() if n > 1]
    for i in items:
        if not (isinstance(i, dict) and isinstance(i.get("id"), str) and isinstance(i.get("question"), str)
                and _whole(i.get("source_index")) and _whole(i.get("cut")) and isinstance(i.get("prefix"), str)
                and isinstance(i.get("remainder"), str) and _whole(i.get("steps")) and _number(i.get("answer"))):
            problems.append(f"{i.get('id') if isinstance(i, dict) else i!r}: an item key is missing or has the wrong "
                            "form")
            continue
        problems += _cut_problems(i["id"], i["question"], i["cut"], i["prefix"], i["remainder"])
    return problems


def _whole(v):
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


@dataclass(frozen=True)
class Item:
    id: str
    benchmark: str
    source_index: int           # the line of the item in the source file, from 0
    question: str
    cut: int
    prefix: str
    remainder: str
    steps: int
    answer: object


def items_from_set(data):
    return tuple(Item(i["id"], data["benchmark"], i["source_index"], i["question"], i["cut"], i["prefix"],
                      i["remainder"], i["steps"], i["answer"]) for i in data["items"])


def load_items(benchmark="gsm8k", repo=REPO):
    """The items of a benchmark of the allow-list. Raises ValueError with each problem of benchmark_problems."""
    problems = benchmark_problems(benchmark, repo)
    if problems:
        raise ValueError("; ".join(problems))
    e = ALLOW_LIST[benchmark]
    return items_from_set(json.loads((Path(repo) / e["item_file"]).read_text(encoding="utf-8"),
                                     object_pairs_hook=jsonkeys.unique_keys))


def probe_text(benchmark, prefix):
    """The probe message for a first piece. The two arms get the same text with the same data set name."""
    e = ALLOW_LIST[benchmark]
    return PROBE_TEMPLATE.format(split=e["split"], dataset=e["name"], first_piece=prefix)


# ---------- scorer ----------

def _drop_head(w, head):
    return w[len(head):] if head and w[:len(head)] == head else w


def lcs_length(a, b):
    """The length of the longest common subsequence of two word lists."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for x in a:
        cur = [0]
        for j, y in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if x == y else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def rouge_l_f1(ref, cand):
    """ROUGE-L F1 over words of a reference and a candidate (word lists)."""
    n = lcs_length(ref, cand)
    if n == 0:
        return 0.0
    p, r = n / len(cand), n / len(ref)
    return 2 * p * r / (p + r)


def score_completion(reply, prefix, remainder, tau, *, failed=False):
    """The match class of one reply, with the ROUGE-L value and the longest run of remainder words in the reply.
    `failed` is true for an error or a provider block. `tau` is the reviewer's bar of a near-exact match, or None."""
    if failed or reply is None or not reply.strip():
        return {"class": "no_result", "rouge_l": None, "longest_run": None}
    w = _drop_head(words(reply), words(FIRST_LABEL))
    w = _drop_head(w, words(prefix))
    w = _drop_head(w, words(SECOND_LABEL))
    rem = words(remainder)
    rouge = rouge_l_f1(rem, w[:len(rem)])
    run = sec006._longest_run(rem, sec006._word_index(w))[0]
    if w[:len(rem)] == rem:
        cls = "exact"
    elif tau is not None and rouge >= tau:
        cls = "near_exact"
    else:
        cls = "unrelated"
    return {"class": cls, "rouge_l": rouge, "longest_run": run}


# ---------- M1 ----------

def fisher_one_sided(a, n1, c, n2):
    """The one-sided Fisher exact p that the first rate (a of n1) is higher than the second (c of n2): P(X >= a) for
    the hypergeometric count of the first group, with the margins fixed. None when a group is empty."""
    if not n1 or not n2:
        return None
    k, n = a + c, n1 + n2
    top = sum(math.comb(k, x) * math.comb(n - k, n1 - x) for x in range(a, min(k, n1) + 1))
    return top / math.comb(n, n1)


def _arm(records, runs_per_item):
    """The summary of one arm. Only items with all k attempts count; an item with no attempt with a result is left out
    of the rates, as in TP-SEC-001."""
    by_item = {}
    for r in records:
        by_item.setdefault(r["item"], {})[r["attempt"]] = r["class"]
    complete = {i: list(a.values()) for i, a in by_item.items() if len(a) == runs_per_item}
    rated = {i: max(cs, key=RANK.get) for i, cs in complete.items() if any(c != "no_result" for c in cs)}
    attempts = [c for i in rated for c in complete[i] if c != "no_result"]
    n, best = len(rated), Counter(rated.values())
    exact, either = best["exact"], best["exact"] + best["near_exact"]
    a_exact = sum(c == "exact" for c in attempts)
    a_either = sum(c in ("exact", "near_exact") for c in attempts)
    return {"attempts": len(records), "counts": {c: sum(r["class"] == c for r in records) for c in CLASSES},
            "items": n, "item_counts": {c: best[c] for c in CLASSES if c != "no_result"},
            "exact_items": exact, "exact_rate": sec001._rate(exact, n), "exact_wilson95": sec001._wilson95(exact, n),
            "exact_or_near_exact_items": either, "exact_or_near_exact_rate": sec001._rate(either, n),
            "exact_or_near_exact_wilson95": sec001._wilson95(either, n),
            "attempt_exact_rate": sec001._rate(a_exact, len(attempts)),
            "attempt_exact_or_near_exact_rate": sec001._rate(a_either, len(attempts)),
            "items_without_result": len(complete) - n, "items_incomplete": len(by_item) - len(complete)}


def _differential(b, c, key, bar):
    rb, rc = b[f"{key}_rate"], c[f"{key}_rate"]
    diff = None if (rb is None or rc is None) else rb - rc
    return {"benchmark_rate": rb, "comparator_rate": rc, "differential": diff,
            "fisher_p_one_sided": fisher_one_sided(b[f"{key}_items"], b["items"], c[f"{key}_items"], c["items"]),
            "bar": bar, "materially_higher": None if (diff is None or bar is None) else diff > bar}


def compute_m1(records, *, runs_per_item, bar, tau):
    """M1 for one benchmark. `records`: dicts with arm (benchmark or comparator), item, attempt and class. `bar` is the
    reviewer's bar of a material differential, or None; `tau` the reviewer's bar of a near-exact match, or None."""
    arms = {a: _arm([r for r in records if r["arm"] == a], runs_per_item) for a in ("benchmark", "comparator")}
    b, c = arms["benchmark"], arms["comparator"]
    secondary = {"measure": "exact or near-exact", "tau": tau, **_differential(b, c, "exact_or_near_exact", bar)}
    if tau is None:
        secondary.update({k: None for k in ("benchmark_rate", "comparator_rate", "differential", "fisher_p_one_sided",
                                            "materially_higher")}, reason="no tau is set, so no reply is near-exact")
    return {"benchmark": b, "comparator": c,
            "primary": {"measure": "exact only", **_differential(b, c, "exact", bar)},
            "secondary": secondary,
            "comparator_reading": {"reading": COMPARATOR_READING, "exact_rate": c["exact_rate"],
                                   "exact_wilson95": c["exact_wilson95"],
                                   "exact_or_near_exact_rate": c["exact_or_near_exact_rate"],
                                   "exact_or_near_exact_wilson95": c["exact_or_near_exact_wilson95"]}}


# ---------- comparators ----------

def _date(v):
    """A date from a date or a YYYY-MM-DD text, or None."""
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(v) if isinstance(v, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) else None
    except ValueError:
        return None


def _family(name):
    return str(name).strip().casefold()


def _comparator_form(c):
    """The keys of a comparator that are missing or have the wrong form, in the order of COMPARATOR_KEYS."""
    text = ("id", "benchmark", "pair", "question", "prefix", "remainder", "writer_family")
    bad = [k for k in text if not (isinstance(c.get(k), str) and c[k].strip())]
    bad += [k for k in ("cut", "steps") if not _whole(c.get(k))]
    bad += ["created"] if _date(c.get("created")) is None else []
    bad += ["answer"] if not _number(c.get("answer")) else []
    return [k for k in COMPARATOR_KEYS if k in bad]


def check_comparators(data, items, *, file_sha256, overlap, training_cutoff=None, target_family=None, evidence=False,
                      sme_record_sha256=None, repo=REPO):
    """The problems of a comparator file (empty if none), one for each fault. `data` is the file as loaded,
    `file_sha256` its SHA-256 and `items` the benchmark items. `overlap` is the result of tools/mod011_overlap.py as
    loaded, or None. `training_cutoff` and `target_family` are as the operator declares them, or None; with None, the
    date or the family is not compared. In evidence mode the SME record hash must be set."""
    if not (isinstance(data, dict) and isinstance(data.get("comparators"), list)):
        return ["the comparator file is not a JSON object with a list comparators"]
    cutoff = _date(training_cutoff)
    pairs = {i.id: i.benchmark for i in items}
    problems, valid, counts = [], [], Counter()
    for c in data["comparators"]:
        cid = c.get("id") if isinstance(c, dict) else None
        cid = cid if isinstance(cid, str) and cid.strip() else repr(cid)
        if isinstance(c, dict) and isinstance(c.get("benchmark"), str) and c["benchmark"] in ALLOW_LIST:
            counts[c["benchmark"]] += 1         # a comparator with a fault still counts, so one fault is one problem
        bad = _comparator_form(c) if isinstance(c, dict) else list(COMPARATOR_KEYS)
        if bad:
            problems.append(f"{cid}: the key(s) {', '.join(bad)} are missing or have the wrong form")
            continue
        if c["benchmark"] not in ALLOW_LIST:
            problems.append(f"{cid}: the benchmark {c['benchmark']!r} is not on the allow-list of {TP}")
            continue
        valid.append(c)
        if pairs.get(c["pair"]) != c["benchmark"]:
            problems.append(f"{cid}: the pair {c['pair']} is not an item of {c['benchmark']}")
        if cutoff is not None and _date(c["created"]) <= cutoff:
            problems.append(f"{cid}: the creation date {c['created']} is on or before the declared training cut-off "
                            f"{cutoff.isoformat()}")
        problems += _cut_problems(cid, c["question"], c["cut"], c["prefix"], c["remainder"])
        if target_family is not None and _family(c["writer_family"]) == _family(target_family):
            problems.append(f"{cid}: the writer family is the declared family of the target")
    for k, n in Counter(c["id"] for c in valid).items():
        if n > 1:
            problems.append(f"the comparator ID {k} is in the file more than once")
    for k, n in Counter(c["pair"] for c in valid).items():
        if n > 1:
            problems.append(f"the item {k} is the pair of more than one comparator")
    for b in sorted(set(counts) | {i.benchmark for i in items}):   # a benchmark of the run with no comparator has 0
        if counts[b] < MIN_ITEMS:
            problems.append(f"the benchmark {b} has {counts[b]} comparators; the minimum is {MIN_ITEMS} "
                            "(PLAYBOOK_MOD_011.md line 18)")
    problems += _overlap_problems(valid, overlap, file_sha256, sorted(counts), repo)
    if evidence and not sme_record_sha256:
        problems.append("evidence mode: the SHA-256 of the SME record (the check of each pair) is not set")
    return problems


def _overlap_problems(valid, overlap, file_sha256, benchmarks, repo):
    """The problems of the overlap result. A field of the wrong type is read as absent, by the rule of the TP-SEC-002
    item check (sec002.check_item_set), so the problem of an absent field names it: a comparator_file that is not an
    object, gsm8k_files or results that are not a list, and an entry of them that is not an object or whose sha256 or
    id is not a text."""
    if not isinstance(overlap, dict):
        return ["no overlap result (tools/mod011_overlap.py) for the comparator file"]
    out = []
    cfile = overlap.get("comparator_file")
    if (cfile.get("sha256") if isinstance(cfile, dict) else None) != file_sha256:
        out.append("the overlap result is for another comparator file (its SHA-256 differs)")
    files = overlap.get("gsm8k_files")
    read = {f["sha256"] for f in (files if isinstance(files, list) else [])
            if isinstance(f, dict) and isinstance(f.get("sha256"), str)}
    for b in benchmarks:
        want = entry(b, repo)["overlap_sha256"]
        if any(v is None or v not in read for v in want.values()):
            out.append(f"the overlap result does not read the files {', '.join(want)} of {b} that the digest file "
                       "names")
    results = overlap.get("results")
    rows = {r["id"]: r for r in (results if isinstance(results, list) else [])
            if isinstance(r, dict) and isinstance(r.get("id"), str)}
    for c in valid:
        row = rows.get(c["id"])
        if row is None or not _whole(row.get("longest_run")):
            out.append(f"{c['id']}: the overlap result has no row for it")
        elif row["longest_run"] >= OVERLAP_LIMIT:
            out.append(f"{c['id']}: a run of {row['longest_run']} words in common with GSM8K question text; the "
                       f"limit is under {OVERLAP_LIMIT}")
    return out


def features(question, prefix, remainder, steps, answer):
    """The match features of one benchmark item or comparator."""
    return {"prefix_words": len(words(prefix)), "remainder_words": len(words(remainder)),
            "sentences": len(sentence_ends(question)) + 1,
            "numbers": len(NUMBER.findall(unicodedata.normalize("NFKC", question))), "steps": steps,
            "answer_digits": len(str(int(abs(answer))))}


def _stats(rows):
    return {f: {"mean": round(sum(r[f] for r in rows) / len(rows), 2), "min": min(r[f] for r in rows),
                "max": max(r[f] for r in rows)} if rows else None for f in FEATURES}


def match_features(items, comparators):
    """The match features: the mean and range of each feature for each arm, the difference of each feature in each
    pair (a comparator and its item), and the largest difference in a pair. Read after check_comparators."""
    item_f = {i.id: features(i.question, i.prefix, i.remainder, i.steps, i.answer) for i in items}
    comp_f = {c["id"]: features(c["question"], c["prefix"], c["remainder"], c["steps"], c["answer"])
              for c in comparators}
    pairs = [{"comparator": c["id"], "item": c["pair"],
              "difference": {f: abs(comp_f[c["id"]][f] - item_f[c["pair"]][f]) for f in FEATURES}}
             for c in comparators if c["pair"] in item_f]
    return {"features": list(FEATURES), "benchmark": _stats(list(item_f.values())),
            "comparator": _stats(list(comp_f.values())), "pairs": pairs,
            "largest_pair_difference": {f: max((p["difference"][f] for p in pairs), default=None) for f in FEATURES}}


# ---------- the instrument check ----------

INSTRUMENT_FILE = "items/TP-MOD-011-instrument.json"
INSTRUMENT_DIGEST_FILE = "third_party/public_domain/instrument.sha256"
INSTRUMENT_DIGEST_PREFIX = "0963b471b84b26db"  # the pin of the digest file (DIGEST_PREFIX_HEX characters)
INSTRUMENT_DATASET = "public-domain works"  # the data set name of the probe; the split is the title of the work
INSTRUMENT_COUNT = 10
INSTRUMENT_READING = "instrument check, outside M1 (a reading, not a measure)"


@dataclass(frozen=True)
class Passage:
    id: str
    title: str
    author: str
    year: int
    passage: str
    cut: int
    prefix: str
    remainder: str


def check_instrument(data):
    """The problems of an instrument file (empty if none): the procedure, the data set name, the count, unique IDs,
    and the form and cut of each passage."""
    problems = []
    if data.get("procedure") != TP:
        problems.append(f"the instrument file does not name the procedure {TP}")
    if data.get("dataset") != INSTRUMENT_DATASET:
        problems.append(f"the instrument file does not name the data set {INSTRUMENT_DATASET}")
    rows = data.get("passages") if isinstance(data.get("passages"), list) else []
    if len(rows) != INSTRUMENT_COUNT:
        problems.append(f"the instrument file holds {len(rows)} passages; the check uses {INSTRUMENT_COUNT}")
    ids = Counter(p.get("id") for p in rows if isinstance(p, dict))
    problems += [f"the passage ID {k} is in the file more than once" for k, n in ids.items() if n > 1]
    for p in rows:
        if not (isinstance(p, dict) and all(isinstance(p.get(k), str) and p[k].strip()
                                            for k in ("id", "title", "author", "passage", "prefix", "remainder"))
                and all(_whole(p.get(k)) for k in ("year", "cut"))):
            problems.append(f"{p.get('id') if isinstance(p, dict) else p!r}: a passage key is missing or has the "
                            "wrong form")
            continue
        problems += _cut_problems(p["id"], p["passage"], p["cut"], p["prefix"], p["remainder"])
    return problems


def instrument_problems(repo=REPO):
    """Why the run may not use the instrument file (empty if it may): an absent digest file or instrument file, a
    digest file whose SHA-256 does not start with INSTRUMENT_DIGEST_PREFIX, an instrument file whose SHA-256 differs
    from the digest file, and the problems of check_instrument."""
    repo = Path(repo)
    digests = read_digests(repo / INSTRUMENT_DIGEST_FILE)
    if digests is None:
        return [f"the instrument digest file {INSTRUMENT_DIGEST_FILE} is absent"]
    if not pin_matches(repo / INSTRUMENT_DIGEST_FILE, INSTRUMENT_DIGEST_PREFIX):
        return [f"the instrument digest file {INSTRUMENT_DIGEST_FILE} does not have the SHA-256 that mod011.py pins "
                f"({INSTRUMENT_DIGEST_PREFIX}...); a changed digest file is a code change"]
    path = repo / INSTRUMENT_FILE
    if not path.is_file():
        return [f"the instrument file {INSTRUMENT_FILE} is absent"]
    raw = path.read_bytes()
    if _sha256(raw) != digests.get(path.name):
        return [f"the instrument file {INSTRUMENT_FILE} does not have the SHA-256 of {INSTRUMENT_DIGEST_FILE}"]
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey as err:     # in the form of the TP-SEC-002 refusal (preflight._sec002_item_file)
        return [f"the instrument file {INSTRUMENT_FILE} is refused: {err}"]
    return check_instrument(data)


def load_instrument(repo=REPO):
    """The passages of the instrument file. Raises ValueError with each problem of instrument_problems."""
    problems = instrument_problems(repo)
    if problems:
        raise ValueError("; ".join(problems))
    data = json.loads((Path(repo) / INSTRUMENT_FILE).read_text(encoding="utf-8"),
                      object_pairs_hook=jsonkeys.unique_keys)
    return tuple(Passage(p["id"], p["title"], p["author"], p["year"], p["passage"], p["cut"], p["prefix"],
                         p["remainder"]) for p in data["passages"])


def instrument_probe_text(passage):
    """The probe message for a passage: PROBE_TEMPLATE with the data set INSTRUMENT_DATASET and the title as the
    split."""
    return PROBE_TEMPLATE.format(split=passage.title, dataset=INSTRUMENT_DATASET, first_piece=passage.prefix)


def instrument_summary(classes):
    """The result of the instrument check from the class of each passage: the counts of the four classes, and the
    exact rate over the passages with a result, with a Wilson 95% interval. It is a reading, not a measure."""
    n = sum(c != "no_result" for c in classes)
    exact = sum(c == "exact" for c in classes)
    return {"reading": INSTRUMENT_READING, "passages": len(classes),
            "counts": {c: sum(x == c for x in classes) for c in CLASSES}, "with_result": n,
            "exact_rate": sec001._rate(exact, n), "exact_wilson95": sec001._wilson95(exact, n)}
