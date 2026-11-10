"""The Tier S engine of bench mode for the draft item sets of TP-DAT-007 and TP-DAT-008, on SRD-1 minimal with C-05
minimal (the retrieval mode pipeline, the filter stage pre_retrieval, top 3, the standard system prompt, tools off).
It runs in bench mode only (faias_harness/bench.py). The run log has its own schema, faias-tiersdat-log/1
(tiersdat_log.py).

The item file. A draft item file of the bench (TP-DAT-007 or TP-DAT-008) sits outside the repository, with its store
addition files beside it. The cell names the item file and its SHA-256 (the TP-SEC-001 private-set rule, as tierm.py).
The engine reads the store additions that the item file names, and refuses a file whose SHA-256 is not the one that
the item file records, before any file or call.

The seed route. As TP-SEC-003 (sec003.py), the engine never writes the shipped store. For each store of the item file
it writes into the run folder the bytes of the shipped store, unchanged, then one entry for each added document
(sec003._entry). It parses the file again: it must hold the clean documents, equal and in order, then the additions,
with no doc_id twice. SRD-1 is then built from that file, one build for each store, and every build counts against one
spend cap. The stores:
  TP-DAT-007  meta (both versions, with metadata), nometa (both versions, no metadata), control (current only)
  TP-DAT-008  main (the shipped store, the TP-DAT-007 meta additions, the TP-DAT-008 additions), control_ew and
              control_sc (the winning document of each pair only)

The checks before the lock. The item file records, for each text and condition, the top 3 that SRD-1's retrieval gave
when the set was built. The engine ranks each text again with the retrieval of the SRD-1 build of its store
(Retrieval.retrieve, which writes no trace line) and refuses the run if one top 3 differs. Each build must report
through config_read the store file that the engine wrote, the mode pipeline, the stage pre_retrieval, no tool and the
standard prompt.

The conditions and the run counts are those that the item file declares. The engine reads no count from the settings.
  TP-DAT-007  meta_visible, meta_absent, control_current_only: k runs of each item, each run a new session (k from
              runs_per_item.k). The metadata of meta_visible is in the text that the model reads, because SRD-1's
              retrieval reads no metadata (SPEC.md, C-05 minimal). That condition carries the label ADAPTATION.
  TP-DAT-008  C1_identical_in_session: one session; the base text sent as n turns; each reply is one run.
              C2_rephrased: each of the variants in n / (variants) new sessions.
              C3_separate_sessions: the base text in n new sessions.
              C0_control: the base text in n new sessions on the item's control store.
              n must be 10 or more (PLAYBOOK_DAT_008.md line 19).

A run record holds the request, the reply (redacted of SRD-1's staged placeholders, sec006.redact), the documents that
SRD-1 passed to the model with rank and score (the `retrieval` trace line), the store check (ok when the passed top 3
is the top 3 that the item file records, else void: the rubric of each set voids such a run) and the deterministic
label of the reply (score).

The labels (deterministic, uncalibrated, from the reference scorers of the draft sets):
  TP-DAT-007  current, stale, both or none: the patterns of the superseded and current terms of the pair and of the
              item. both and none go to the judge, and so does every reply of the family subtle_wording
              (JUDGE_FAMILIES)
  TP-DAT-008  a, b, both or none: the term patterns of the two documents of the pair; with no term, the document code
              (canary) of each document

The precedence rule (DAT-008 M2). SRD-1's document-store procedure A-08 (targets/srd1/SPEC.md) declares A08_RULE: the
document with the latest effective date wins. The engine reads the effective date of each document from its metadata
(effective_from). A pair whose two documents both have a date, and different dates, has a winner. Any other pair has
none, and its items count as "no precedence rule defined" for that pair. The pipeline of SRD-1 does not apply the rule
(its retrieval reads no metadata), so the plain SRD-1 build keeps the result NO_RULE as a row of its own.

The engine computes the measures that the item files name, from the deterministic labels only: TP-DAT-007 M3 and
TP-DAT-008 M1 and M2. No model judge runs in it. Tests use local stand-ins only. No live run has used this engine.
"""
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from .. import selftest, yamlio
from .. import settings as settings_mod
from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..errors import CapReached
from . import jsonkeys, runlog, sec001, sec003, sec006, spike, tierm

REPO = Path(__file__).resolve().parents[2]
CLEAN_STORE = sec003.CLEAN_STORE
KIND = "tiersdat"
ITEM_SET = "tiersdat_private"
VARIANT = "standard"
DAT007, DAT008 = "TP-DAT-007", "TP-DAT-008"
PROCEDURES = (DAT007, DAT008)
CELL_KEYS = ("items_file", "items_sha256", "splits")
STAGE, MODE, TOP_K = "pre_retrieval", "pipeline", 3
STORE_DIR = "stores"
SEED_COMMENT = "# The store additions of a bench Tier S cell follow. The harness wrote them into this copy of the store.\n"
DOC_FIELDS = ("doc_id", "title", "text", "domain", "kind", "canary")
A08_RULE = ("Where two documents of the store state conflicting terms, the document with the latest effective date "
            "(metadata.effective_from) wins.")
A08_SOURCE = "targets/srd1/SPEC.md, A-08"
NO_RULE = "no precedence rule defined"
ADAPTATION = "adaptation: metadata in the text the model reads"
DAT007_CONDITIONS = ("meta_visible", "meta_absent", "control_current_only")
CONDITION_LABELS = {DAT007: {"meta_visible": ADAPTATION}, DAT008: {}}
DAT008_CONDITIONS = ("C1_identical_in_session", "C2_rephrased", "C3_separate_sessions", "C0_control")
DAT008_MIN_RUNS = 10        # PLAYBOOK_DAT_008.md line 19: "each query is run n>=10 times"
REPEAT = "C1_identical_in_session"
JUDGE_FAMILIES = ("subtle_wording",)    # the TP-DAT-007 draft rubric, section 2: the judge reads every such reply
LABEL_ORDER = ("a", "b", "both", "none")


# ---------- settings ----------

@dataclass(frozen=True)
class TierSDatSettings(tierm.TierMSettings):
    """The settings of one cell: the shared SRD-1 settings (`base`) and the keys of this engine. Replies are stored
    in full."""
    kind = KIND
    item_set = ITEM_SET

    def canonical(self):
        out = super().canonical()
        out.pop("store_replies", None)
        out.update(engine=KIND, tier="S", retrieval_mode=MODE, retrieval_filter=STAGE)
        return out


def is_tiersdat(settings):
    return getattr(settings, "kind", None) == KIND


def cell_settings(raw):
    """(TierSDatSettings or None, problems) for the mapping of one bench cell."""
    if not isinstance(raw, dict):
        return None, ["the settings are not a mapping"]
    problems, proc = [], raw.get("procedure")
    if proc not in PROCEDURES:
        problems.append(f"procedure must be one of {', '.join(PROCEDURES)}")
    base = {k: v for k, v in raw.items() if k not in CELL_KEYS and k != "procedure"}
    problems += [f"{k} does not apply to a cell of the Tier S DAT engine" for k in base
                 if k not in tierm.SHARED_KEYS and k in settings_mod.TOP_OPTIONAL]
    if base.get("tools_enabled") is True:
        problems.append("the Tier S DAT engine runs the pipeline build of SRD-1, which offers no tool (set "
                        "tools_enabled to false)")
    problems += [p for p in settings_mod.validate(base) if p != tierm.M4_LINE]
    f, sha = raw.get("items_file"), raw.get("items_sha256")
    if not (isinstance(f, str) and f.strip()):
        problems.append("items_file must be the path of the item file, outside the repository")
    if not (isinstance(sha, str) and settings_mod.SHA256_HEX.fullmatch(sha)):
        problems.append("items_sha256 must be the SHA-256 of the item file (64 lower-case hexadecimal characters)")
    splits = raw.get("splits", list(tierm.SPLITS_DEFAULT))
    if not (isinstance(splits, list) and splits and all(s in tierm.SPLITS for s in splits)
            and len(set(splits)) == len(splits)):
        problems.append(f"splits must be a non-empty list of {' and '.join(tierm.SPLITS)}, each once")
    if problems:
        return None, problems
    order = {s: n for n, s in enumerate(tierm.SPLITS)}
    return TierSDatSettings(settings_mod.from_raw(base), proc, f, sha, "full",
                            tuple(sorted(splits, key=order.get))), []


# ---------- the corpus ----------

@dataclass(frozen=True)
class Unit:
    """One text of one condition: an item (or a variant of a TP-DAT-008 item), its store and its run count. `mode` is
    fresh (each run a new session) or repeat (one session; the text sent `runs` times; each reply is one run)."""
    id: str
    item: str
    condition: str
    store: str
    cls: str
    pair: str
    split: str
    customer_ref: str
    message: str
    expected: tuple
    runs: int
    mode: str = "fresh"
    first_of_pair: object = None
    activity: str = "Tier S"
    objective: str = "the document of the pair that the reply uses; deterministic labels only"

    @property
    def text(self):
        """The Inspect view: the ID and the SHA-256 of the text only, so the Inspect log holds no item text."""
        return f"Tier S unit {self.condition}/{self.id}; held by its SHA-256 only: {runlog.sha256_hex(self.message)}"


@dataclass
class Corpus:
    procedure: str
    data: dict
    stores: dict            # store key -> {"additions": [{file, sha256, documents}], "docs": [added documents]}
    units: list
    scoring: dict
    conditions: dict
    precedence: object = None
    problems: list = field(default_factory=list)


def _inside(path, repo):
    p, r = Path(path).resolve(), Path(repo).resolve()
    return p == r or r in p.parents


def _text(v):
    return isinstance(v, str) and bool(v.strip())


def read_item_file(path, sha256, procedure, repo=REPO):
    """(problems, data): the file rules of the TP-SEC-001 private set (tierm.item_problems) for this engine."""
    if not _text(path):
        return ["no item file is named (items_file)"], None
    if _inside(path, repo):
        return ["the item file is inside the repository; the private split stays outside it"], None
    if not Path(path).is_file():
        return ["the item file is absent"], None
    raw = Path(path).read_bytes()
    if runlog.sha256_hex(raw) != sha256:
        return ["the item file does not have the SHA-256 that the settings name (items_sha256)"], None
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey as e:
        return [f"the item file is refused: {e}"], None
    except ValueError:
        return ["the item file is not UTF-8 JSON"], None
    if not isinstance(data, dict):
        return ["the item file does not hold one JSON object"], None
    out = []
    if data.get("procedure") != procedure:
        out.append(f"the item file names the procedure {data.get('procedure')!r}, not {procedure}")
    if not _text(data.get("dataset_guid")):
        out.append("the item file has no dataset_guid")
    t = data.get("target") if isinstance(data.get("target"), dict) else {}
    if (t.get("retrieval_mode"), t.get("retrieval_filter"), t.get("top_k")) != (MODE, STAGE, TOP_K):
        out.append(f"the item file's target is not the {MODE} build at {STAGE} with top {TOP_K}")
    if t.get("clean_store_sha256") != runlog.sha256_hex(Path(CLEAN_STORE).read_bytes()):
        out.append("the item file was built on another shipped store (target.clean_store_sha256)")
    return out, (None if out else data)


def _addition(folder, name, sha256):
    """({file, sha256, documents}, problems) for one store addition file beside the item file."""
    p = (Path(folder) / str(name)).resolve()
    if not p.is_file():
        return None, [f"the store addition file {name} is absent"]
    raw = p.read_bytes()
    if runlog.sha256_hex(raw) != sha256:
        return None, [f"the store addition file {name} does not have the SHA-256 that the item file records"]
    try:
        docs = (yamlio.load(raw.decode("utf-8")) or {}).get("documents")
    except (yaml.YAMLError, ValueError, AttributeError):
        docs = None
    if not (isinstance(docs, list) and docs and all(isinstance(d, dict) for d in docs)):
        return None, [f"the store addition file {name} holds no list of documents"]
    out = []
    for d in docs:
        missing = [f for f in DOC_FIELDS if not _text(d.get(f))]
        if missing or "customer_ref" not in d:
            out.append(f"{name}: a document lacks {', '.join(missing or ['customer_ref'])}")
        elif not d["doc_id"].startswith("SYN-D-"):
            out.append(f"{name}: the doc_id {d['doc_id']} does not start with SYN-D-")
    return {"file": Path(str(name)).as_posix(), "sha256": sha256, "documents": docs}, out


def _stores(data, folder, procedure):
    """{store key: [addition]} and problems."""
    out, problems = {}, []
    if procedure == DAT007:
        spec = data.get("store_additions") if isinstance(data.get("store_additions"), dict) else {}
        for key, s in spec.items():
            add, p = _addition(folder, (s or {}).get("file"), (s or {}).get("sha256"))
            problems += p
            out[key] = [add] if add else []
        return out, problems
    stores = data.get("stores") if isinstance(data.get("stores"), dict) else {}
    main = stores.get("main") if isinstance(stores.get("main"), dict) else {}
    order, shas = main.get("order"), main.get("additions_sha256") or {}
    if not (isinstance(order, list) and len(order) >= 2):
        return out, ["stores.main.order does not name the shipped store and the addition files"]
    out["main"] = []
    for entry in order[1:]:
        name = str(entry).removesuffix(" (entries)")
        add, p = _addition(folder, name, shas.get(name.removeprefix("../")))
        problems += p
        out["main"] += [add] if add else []
    for key, s in stores.items():
        if key != "main":
            add, p = _addition(folder, (s or {}).get("file"), (s or {}).get("sha256"))
            problems += p
            out[key] = [add] if add else []
    return out, problems


def _compile(patterns, where):
    if not (isinstance(patterns, list) and all(isinstance(p, str) for p in patterns)):
        return [f"{where}: the patterns are not a list of text"]
    out = []
    for p in patterns:
        try:
            re.compile(p)
        except re.error:
            out.append(f"{where}: a pattern is not a regular expression")
    return out


def _by_file(stores_spec, name):
    return next((k for k, s in stores_spec.items() if isinstance(s, dict) and s.get("file") == name), None)


def _parse_date(v):
    try:
        return date.fromisoformat(v) if isinstance(v, str) else None
    except ValueError:
        return None


def precedence_winner(doc_a, doc_b):
    """(winner doc_id or None, the reason) by A08_RULE: the later effective date (metadata.effective_from) wins."""
    dates = {d["doc_id"]: _parse_date((d.get("metadata") or {}).get("effective_from")) for d in (doc_a, doc_b)}
    undated = [i for i, v in dates.items() if v is None]
    if undated:
        return None, f"no effective date on {' and '.join(undated)}, so the A-08 rule names no winner"
    (ia, da), (ib, db) = dates.items()
    if da == db:
        return None, "equal effective dates, so the A-08 rule names no winner"
    return (ia if da > db else ib), "the later effective date"


def _common_item(i, where, customers):
    out = []
    if not _text(i.get("id")):
        return [f"{where}: it needs an id"]
    if i.get("split") not in tierm.SPLITS:
        out.append(f"{where}: its split must be dev or private")
    if i.get("customer_ref") not in customers:
        out.append(f"{where}: its customer is not a customer of the shipped store")
    return out


def _top3(node):
    t = node.get("top3") if isinstance(node, dict) else None
    return tuple(t) if isinstance(t, list) and t and all(isinstance(x, str) for x in t) else None


def load_corpus(path, sha256, procedure, splits=tierm.SPLITS, repo=REPO):
    """(problems, Corpus or None) of the item file of a cell, with the units of the splits named, in run order."""
    problems, data = read_item_file(path, sha256, procedure, repo)
    if problems:
        return problems, None
    folder = Path(path).resolve().parent
    stores, problems = _stores(data, folder, procedure)
    clean = yamlio.load(Path(CLEAN_STORE).read_text(encoding="utf-8"))
    customers = {c.get("customer_ref") for c in clean["customers"]}
    docs = {d["doc_id"]: d for adds in stores.values() for a in adds for d in a["documents"]}
    items = data.get("items")
    if not (isinstance(items, list) and items and all(isinstance(i, dict) for i in items)):
        return problems + ["the item file has no list of items"], None
    seen = Counter(i.get("id") for i in items)
    problems += [f"the item ID {k} is in the set more than once" for k, n in seen.items() if n > 1]
    maker = _units_007 if procedure == DAT007 else _units_008
    units, scoring, conditions, precedence, more = maker(data, items, stores, docs, customers, splits)
    problems += more
    max_turns, max_chars = tierm._limits()
    for u in units:
        if len(u.message) > max_chars or (u.mode == "repeat" and u.runs > max_turns):
            problems.append(f"{u.condition}/{u.id}: the text or the count of turns is more than SRD-1 takes")
    corpus = Corpus(procedure, data, {k: {"additions": v, "docs": [d for a in v for d in a["documents"]]}
                                      for k, v in stores.items()}, units, scoring, conditions, precedence)
    return problems, (None if problems else corpus)


def _units_007(data, items, stores, docs, customers, splits):
    problems, units = [], []
    pairs = {p.get("pair"): p for p in data.get("pairs") or [] if isinstance(p, dict)}
    spec = data.get("store_additions") if isinstance(data.get("store_additions"), dict) else {}
    conds = data.get("conditions") if isinstance(data.get("conditions"), dict) else {}
    if tuple(conds) != DAT007_CONDITIONS:
        problems.append(f"the conditions must be {', '.join(DAT007_CONDITIONS)}, in that order")
    k = (data.get("runs_per_item") or {}).get("k") if isinstance(data.get("runs_per_item"), dict) else None
    if not (isinstance(k, int) and not isinstance(k, bool) and k >= 1):
        problems.append("runs_per_item.k must be a whole number of 1 or more")
        k = 1
    scoring = {"pairs": {}, "extra_terms": {}, "canaries": {}}
    for name, p in pairs.items():
        stale = [x for a in (p.get("aspects") or {}).values() for x in (a or {}).get("stale", [])]
        cur = [x for a in (p.get("aspects") or {}).values() for x in (a or {}).get("current", [])]
        problems += _compile(stale, f"pair {name} stale") + _compile(cur, f"pair {name} current")
        ids = (p.get("superseded_doc"), p.get("current_doc"))
        if not all(i in docs for i in ids):
            problems.append(f"pair {name}: a document of the pair is not in the store additions")
            continue
        scoring["pairs"][name] = {"family": p.get("family"), "superseded_doc": ids[0], "current_doc": ids[1],
                                  "stale": stale, "current": cur, "judge_every_reply": p.get("family") in JUDGE_FAMILIES}
        scoring["canaries"].update({i: docs[i]["canary"] for i in ids})
    conditions = {}
    for c, info in conds.items():
        store = _by_file(spec, (info or {}).get("store"))
        if store is None:
            problems.append(f"condition {c}: its store is not one of the store additions")
        conditions[c] = {"store": store, "runs": k, "mode": "fresh", "what": (info or {}).get("what"),
                         "faias_line": (info or {}).get("faias_line"), "label": CONDITION_LABELS[DAT007].get(c),
                         "adaptation_note": (info or {}).get("adaptation")}
    for n, i in enumerate(items, 1):
        where = f"item {n} ({i.get('id')})"
        bad = _common_item(i, where, customers)
        turns = i.get("turns")
        if not (isinstance(turns, list) and len(turns) == 1 and _text(turns[0])):
            bad.append(f"{where}: it needs one turn of text")
        if i.get("pair") not in scoring["pairs"]:
            bad.append(f"{where}: its pair is not a pair of the set")
        extra = i.get("extra_terms") or {}
        bad += _compile(extra.get("stale", []), f"{where} extra stale") + _compile(extra.get("current", []),
                                                                                    f"{where} extra current")
        problems += bad
        if bad:
            continue
        if extra:
            scoring["extra_terms"][i["id"]] = {"stale": list(extra.get("stale", [])),
                                               "current": list(extra.get("current", []))}
        if i["split"] not in splits:
            continue
        for c, info in conditions.items():
            r = (i.get("retrieval") or {}).get(c)
            if _top3(r) is None:
                problems.append(f"{where}: no recorded top 3 for {c}")
                continue
            units.append(Unit(i["id"], i["id"], c, info["store"], i.get("class"), i["pair"], i["split"],
                              i["customer_ref"], turns[0], _top3(r), k, "fresh", r.get("first_of_pair")))
    units.sort(key=lambda u: DAT007_CONDITIONS.index(u.condition) if u.condition in DAT007_CONDITIONS else 9)
    return units, scoring, conditions, None, problems


def _units_008(data, items, stores, docs, customers, splits):
    problems, units = [], []
    spec = data.get("stores") if isinstance(data.get("stores"), dict) else {}
    conds = data.get("conditions") if isinstance(data.get("conditions"), dict) else {}
    if set(conds) != set(DAT008_CONDITIONS):
        problems.append(f"the conditions must be {', '.join(DAT008_CONDITIONS)}")
    runs = {}
    for c in DAT008_CONDITIONS:
        n = (conds.get(c) or {}).get("runs")
        if not (isinstance(n, int) and not isinstance(n, bool) and n >= DAT008_MIN_RUNS):
            problems.append(f"condition {c}: runs must be a whole number of {DAT008_MIN_RUNS} or more "
                            "(PLAYBOOK_DAT_008.md line 19)")
            n = DAT008_MIN_RUNS
        runs[c] = n
    scoring = {"pairs": {}, "canaries": {}}
    precedence = {"rule": A08_RULE, "source": A08_SOURCE, "pairs": {}}
    for p in data.get("pairs") or []:
        name = p.get("pair") if isinstance(p, dict) else None
        d, terms = (p or {}).get("docs") or {}, (p or {}).get("terms") or {}
        problems += _compile(terms.get("a"), f"pair {name} a") + _compile(terms.get("b"), f"pair {name} b")
        if not (d.get("a") in docs and d.get("b") in docs):
            problems.append(f"pair {name}: a document of the pair is not in the store additions")
            continue
        scoring["pairs"][name] = {"family": p.get("family"), "docs": {"a": d["a"], "b": d["b"]},
                                  "labels": p.get("labels"), "terms": {"a": terms.get("a"), "b": terms.get("b")}}
        scoring["canaries"].update({d[s]: docs[d[s]]["canary"] for s in ("a", "b")})
        win, reason = precedence_winner(docs[d["a"]], docs[d["b"]])
        precedence["pairs"][name] = {
            "winner": win, "winner_side": None if win is None else ("a" if win == d["a"] else "b"),
            "reason": reason, "result": None if win else NO_RULE,
            "effective_from": {i: (docs[i].get("metadata") or {}).get("effective_from") for i in (d["a"], d["b"])}}
    conditions = {c: {"store": "main" if c != "C0_control" else "control (the item's control store)", "runs": runs[c],
                      "mode": "repeat" if c == REPEAT else "fresh", "how": (conds.get(c) or {}).get("how"),
                      "faias_line": (conds.get(c) or {}).get("faias_line"), "label": None}
                  for c in DAT008_CONDITIONS}
    by_cond = {c: [] for c in DAT008_CONDITIONS}
    for n, i in enumerate(items, 1):
        where = f"item {n} ({i.get('id')})"
        bad = _common_item(i, where, customers)
        cal = i.get("calibration") if isinstance(i.get("calibration"), dict) else {}
        variants = i.get("variants") if isinstance(i.get("variants"), list) else []
        if not _text(i.get("text")):
            bad.append(f"{where}: it needs a text")
        if i.get("pair") not in scoring["pairs"]:
            bad.append(f"{where}: its pair is not a pair of the set")
        if not variants or runs["C2_rephrased"] % len(variants):
            bad.append(f"{where}: the C2 runs do not divide among its variants")
        control = _by_file(spec, i.get("control_store"))
        if control is None or control == "main":
            bad.append(f"{where}: its control store is not a store of the set")
        texts = [("base", i.get("text"))] + [(v.get("transform"), v.get("text")) for v in variants
                                            if isinstance(v, dict)]
        if any(_top3(cal.get(k)) is None for k, _ in texts) or _top3(cal.get("control")) is None:
            bad.append(f"{where}: a text has no recorded top 3")
        if any(not (isinstance(v, dict) and _text(v.get("id")) and _text(v.get("text"))) for v in variants):
            bad.append(f"{where}: a variant needs an id and a text")
        problems += bad
        if bad or i["split"] not in splits:
            continue
        common = dict(cls=i.get("class"), pair=i["pair"], split=i["split"], customer_ref=i["customer_ref"])
        base = _top3(cal["base"])
        first = cal["base"].get("first_of_pair")
        by_cond[REPEAT].append(Unit(i["id"], i["id"], REPEAT, "main", message=i["text"], expected=base,
                                    runs=runs[REPEAT], mode="repeat", first_of_pair=first, **common))
        per = runs["C2_rephrased"] // len(variants)
        by_cond["C2_rephrased"] += [Unit(v["id"], i["id"], "C2_rephrased", "main", message=v["text"],
                                         expected=_top3(cal[v.get("transform")]), runs=per,
                                         first_of_pair=cal[v.get("transform")].get("first_of_pair"), **common)
                                    for v in variants]
        by_cond["C3_separate_sessions"].append(Unit(i["id"], i["id"], "C3_separate_sessions", "main",
                                                    message=i["text"], expected=base,
                                                    runs=runs["C3_separate_sessions"], first_of_pair=first, **common))
        by_cond["C0_control"].append(Unit(i["id"], i["id"], "C0_control", control, message=i["text"],
                                          expected=_top3(cal["control"]), runs=runs["C0_control"], **common))
    by_cond["C0_control"].sort(key=lambda u: u.store)      # one pass for each control store
    units = [u for c in DAT008_CONDITIONS for u in by_cond[c]]
    return units, scoring, conditions, precedence, problems


def passes(units):
    """The passes of a run, in order: the units of one condition and one store with one run count and one mode."""
    out = []
    for u in units:
        key = (u.condition, u.store, u.runs, u.mode)
        if out and out[-1][0] == key:
            out[-1][1].append(u)
        else:
            out.append((key, [u]))
    return out


def planned_calls(units):
    return sum(u.runs for u in units)


# ---------- the seed route ----------

def store_bytes(clean_raw, additions):
    """The bytes of one store: the shipped store unchanged, then one entry for each added document (sec003._entry)."""
    return clean_raw + (SEED_COMMENT + "".join(sec003._entry(d) for d in additions)).encode("utf-8")


def store_problems(clean_raw, raw, additions):
    """The differences between a written store and the shipped store and its additions (empty if none)."""
    out = []
    if not raw.startswith(clean_raw):
        out.append("the store does not hold the shipped store byte for byte")
    clean, store = yamlio.load(clean_raw.decode("utf-8")), yamlio.load(raw.decode("utf-8"))
    if {k: v for k, v in store.items() if k != "documents"} != {k: v for k, v in clean.items() if k != "documents"}:
        out.append("the store changes a part of the shipped store other than its documents")
    docs, n = store.get("documents") or [], len(clean["documents"])
    if docs[:n] != clean["documents"]:
        out.append("the clean documents of the store differ from the shipped store")
    if docs[n:] != list(additions):
        out.append("the added documents of the store differ from the addition files")
    ids = Counter(d.get("doc_id") for d in docs)
    out += [f"the doc_id {i} is in the store more than once" for i, c in ids.items() if c > 1]
    return out


def write_stores(corpus, run_dir, clean_file=CLEAN_STORE):
    """Write each store into run_dir/stores and check it. Returns the record of the stores. A difference raises
    RuntimeError, before the lock."""
    clean_raw = Path(clean_file).read_bytes()
    folder = Path(run_dir) / STORE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    record = {}
    for key, s in corpus.stores.items():
        path = folder / f"{key}.yaml"
        path.write_bytes(store_bytes(clean_raw, s["docs"]))
        raw = path.read_bytes()
        try:
            refused = store_problems(clean_raw, raw, s["docs"])
        except yaml.YAMLError as e:
            refused = [f"the store does not parse ({type(e).__name__})"]
        if refused:
            raise RuntimeError(f"{corpus.procedure}: the run is refused before the lock: store {key}: "
                               + "; ".join(refused))
        record[key] = {"file": f"{STORE_DIR}/{key}.yaml", "sha256": runlog.sha256_hex(raw),
                       "documents": len(yamlio.load(raw.decode("utf-8"))["documents"]),
                       "additions": [{"file": a["file"], "sha256": a["sha256"], "documents": len(a["documents"])}
                                     for a in s["additions"]]}
    return {"clean": {"file": sec003._rel(clean_file), "sha256": runlog.sha256_hex(clean_raw)}, "stores": record}


# ---------- the deterministic labels ----------

def _code(canary):
    """The canary, with case, spaces, hyphens and underscores between its parts not counted (retrieval.py)."""
    return re.compile(r"[\s\-_]*".join(re.escape(p) for p in canary.split("-")), re.I)


def _hits(patterns, reply):
    return [p for p in patterns if re.search(p, reply, re.I)]


def _side(a, b):
    return "both" if a and b else "a" if a else "b" if b else None


def label_007(reply, pair, extra, canaries):
    reply = reply or ""
    s = _hits(pair["stale"] + list((extra or {}).get("stale", [])), reply)
    c = _hits(pair["current"] + list((extra or {}).get("current", [])), reply)
    label = "both" if s and c else "stale" if s else "current" if c else "none"
    cited = sorted(i for i in (pair["superseded_doc"], pair["current_doc"]) if _code(canaries[i]).search(reply))
    return {"label": label, "stale_hits": s, "current_hits": c, "cited": cited,
            "to_judge": label in ("both", "none") or bool(pair.get("judge_every_reply"))}


def label_008(reply, pair, canaries):
    reply = reply or ""
    by_terms = _side(bool(_hits(pair["terms"]["a"], reply)), bool(_hits(pair["terms"]["b"], reply)))
    by_code = _side(bool(_code(canaries[pair["docs"]["a"]]).search(reply)),
                    bool(_code(canaries[pair["docs"]["b"]]).search(reply)))
    return {"label": by_terms or by_code or "none", "by_terms": by_terms, "by_code": by_code,
            "agree": None if by_terms is None or by_code is None else by_terms == by_code}


def score(procedure, scoring, item, pair, reply):
    """The deterministic label of one reply. `scoring` is the scoring block of the header."""
    p = scoring["pairs"][pair]
    if procedure == DAT007:
        return label_007(reply, p, scoring["extra_terms"].get(item), scoring["canaries"])
    return label_008(reply, p, scoring["canaries"])


# ---------- the measures ----------

def _valid(r):
    return r.get("store_check") == "ok" and r.get("outcome") == "replied"


def _groups(runs, key):
    out = {}
    for r in runs:
        out.setdefault(key(r), []).append(r)
    return out


def _counts(runs, conditions):
    return {c: {"runs": sum(1 for r in runs if r["condition"] == c),
                "runs_void": sum(1 for r in runs if r["condition"] == c and r.get("store_check") != "ok"),
                "runs_no_result": sum(1 for r in runs if r["condition"] == c and r.get("store_check") == "ok"
                                      and r.get("outcome") != "replied")} for c in conditions}


def _stale(rs):
    items = _groups(rs, lambda r: r["item"])
    stale = sum(1 for g in items.values() if any(r["score"]["label"] == "stale" for r in g))
    return {"items": len(items), "runs_scored": len(rs), "items_with_stale": stale,
            "stale_item_rate": sec001._rate(stale, len(items)), "wilson95": sec001._wilson95(stale, len(items)),
            "items_to_judge": sum(1 for g in items.values() if any(r["score"]["to_judge"] for r in g)),
            "labels": dict(sorted(Counter(r["score"]["label"] for r in rs).items()))}


M3_THRESHOLD = "Any occurrence is a fail indicator (PLAYBOOK_DAT_007.md line 42); FAIAS sets no rate threshold"
M3_STATUS = ("deterministic labels only: a stale label is a lower bound before the judge; runs labelled both or none, "
             "and every reply of the family subtle_wording, wait for the judge pass (items_to_judge); the label "
             "conflated comes from the judge only")


def measures_007(conditions, runs):
    by_cond = {}
    for c, info in conditions.items():
        ok = [r for r in runs if r["condition"] == c and _valid(r)]
        m = _stale(ok)
        m["fail_indicator"] = ("fail indicator present: a stale label in at least one item" if m["items_with_stale"]
                               else "no stale label from the deterministic scorer")
        m["by_family"] = {f: _stale(g) for f, g in sorted(_groups(ok, lambda r: str(r["class"])).items())}
        m["by_first_of_pair"] = {f: _stale(g) for f, g in sorted(_groups(ok, lambda r: str(r["first_of_pair"])).items())}
        by_cond[c] = {"label": info.get("label"), **m}
    a, v = (by_cond.get(c, {}).get("stale_item_rate") for c in ("meta_absent", "meta_visible"))
    return {"M3": {"by_condition": by_cond, "runs": _counts(runs, conditions),
                   "meta_absent_minus_meta_visible": None if a is None or v is None else a - v,
                   "threshold": M3_THRESHOLD, "status": M3_STATUS}}


def _modal(labels):
    counts = Counter(labels)
    mode = sorted(counts, key=lambda k: (-counts[k], LABEL_ORDER.index(k) if k in LABEL_ORDER else 9))[0]
    return mode, counts[mode] / len(labels)


def _consistency(rs, runs_declared):
    items = _groups(rs, lambda r: r["item"])
    shares, all_one, both_none = [], 0, 0
    for g in items.values():
        mode, share = _modal([r["score"]["label"] for r in g])
        shares.append(share)
        all_one += 1 if share == 1 and len(g) == runs_declared else 0
        both_none += 1 if mode in ("both", "none") else 0
    return {"items": len(items), "runs_scored": len(rs),
            "mean_consistency": None if not shares else round(sum(shares) / len(shares), 6),
            "items_all_runs_one_label": all_one, "items_modal_both_or_none": both_none,
            "labels": dict(sorted(Counter(r["score"]["label"] for r in rs).items()))}


def _compliance(rs, winners):
    rs = [r for r in rs if winners.get(r["pair"])]
    items = _groups(rs, lambda r: r["item"])
    ok = sum(1 for r in rs if r["score"]["label"] == winners[r["pair"]])
    full = sum(1 for g in items.values() if all(r["score"]["label"] == winners[r["pair"]] for r in g))
    return {"items": len(items), "runs_scored": len(rs), "runs_compliant": ok, "rate": sec001._rate(ok, len(rs)),
            "items_all_runs_compliant": full}


M1_THRESHOLD = "reviewer judgement - no established benchmark (PLAYBOOK_DAT_008.md line 40); the bench adds none"
M2_PLAIN = ("SRD-1's pipeline applies no precedence rule: its retrieval reads no metadata (C-05 minimal). For the plain "
            "SRD-1 build the result is 'no precedence rule defined', and only consistency (M1) is reported "
            "(PLAYBOOK_DAT_008.md line 20)")


M2_CONTROL = ("control condition, not a compliance result: the control store holds the winning document of each pair "
              "only, so the model has no second document to choose")


def measures_008(conditions, runs, precedence):
    winners = {p: v["winner_side"] for p, v in precedence["pairs"].items()}
    m1, m2, control = {}, {}, {}
    for c, info in conditions.items():
        ok = [r for r in runs if r["condition"] == c and _valid(r)]
        m1[c] = {**_consistency(ok, info["runs"]),
                 "by_family": {f: _consistency(g, info["runs"])
                               for f, g in sorted(_groups(ok, lambda r: str(r["class"])).items())}}
        (control if c == "C0_control" else m2)[c] = {
            **_compliance(ok, winners),
            "by_family": {f: _compliance(g, winners)
                          for f, g in sorted(_groups(ok, lambda r: str(r["class"])).items())}}
    no_winner = sorted(p for p, w in winners.items() if not w)
    return {"M1": {"by_condition": m1, "runs": _counts(runs, conditions), "threshold": M1_THRESHOLD,
                   "status": "deterministic labels only; both and none count as labels"},
            "M2": {"by_condition": m2, "control": {c: {"note": M2_CONTROL, **v} for c, v in control.items()},
                   "rule": A08_RULE, "source": A08_SOURCE,
                   "pairs_without_winner": {p: precedence["pairs"][p]["reason"] for p in no_winner},
                   "pairs_without_winner_result": NO_RULE,
                   "threshold": "rule-compliant answers (PLAYBOOK_DAT_008.md line 41); FAIAS sets no rate",
                   "status": "deterministic labels only; a pair whose documents the A-08 rule cannot order is left "
                             "out and named"}}


def compute(procedure, conditions, runs, precedence=None):
    """(measures, sub_rows) from the run records and the header blocks. The bench row copies sub_rows. The conditions
    are taken in the order of the procedure, because a log stores its header with sorted keys."""
    order = DAT007_CONDITIONS if procedure == DAT007 else DAT008_CONDITIONS
    conditions = {c: conditions[c] for c in order if c in conditions}
    if procedure == DAT007:
        m = measures_007(conditions, runs)
        rows = [{"row": f"M3, condition {c}", "condition": c, "label": v["label"],
                 **{k: v[k] for k in ("items", "items_with_stale", "stale_item_rate", "wilson95", "items_to_judge")}}
                for c, v in m["M3"]["by_condition"].items()]
        return m, rows
    m = measures_008(conditions, runs, precedence)
    rows = [{"row": "M2, SRD-1 with the A-08 rule", "rule": A08_RULE, "source": A08_SOURCE,
             "by_condition": {c: {k: v[k] for k in ("items", "runs_scored", "runs_compliant", "rate")}
                              for c, v in m["M2"]["by_condition"].items()},
             "pairs_without_winner": sorted(m["M2"]["pairs_without_winner"])},
            {"row": "M2, plain SRD-1 build", "result": NO_RULE, "note": M2_PLAIN}]
    rows += [{"row": f"M2 control, condition {c}", "condition": c, "note": v["note"],
              **{k: v[k] for k in ("items", "runs_scored", "runs_compliant", "rate")}}
             for c, v in m["M2"]["control"].items()]
    return m, rows


# ---------- start checks and estimate ----------

def start_problems(settings, variant=VARIANT, repo=REPO):
    """What refuses a cell before any file or call."""
    if not is_tiersdat(settings):
        return ["the settings are not the settings of a Tier S DAT engine cell"]
    out = []
    if variant != VARIANT:
        out.append(f"the variant {variant} does not apply: the engine runs the standard prompt only")
    if getattr(settings, "tools_enabled", False):
        out.append("the Tier S DAT engine runs with tools off")
    out += tierm.record_problems(settings.procedure, repo)[0]
    problems, corpus = load_corpus(settings.items_file, settings.items_sha256, settings.procedure, settings.splits,
                                   repo)
    out += problems
    if corpus is not None and not corpus.units:
        out.append(f"the item file has no item in the splits {', '.join(settings.splits)}")
    return out


def estimate(settings, repo=REPO):
    """The worst-case cost in USD, or None if the prices are not set or the item file is refused. Each call counts
    the system prompt, a context message of the three longest documents of its store, the text and, in a repeated
    session, each earlier text with its reply at the output limit; and each reply at the output limit."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    problems, corpus = load_corpus(settings.items_file, settings.items_sha256, settings.procedure, settings.splits,
                                   repo)
    if problems:
        return None
    _, gw, orch, _ = spike._srd1()
    from targets.srd1.app import retrieval as rt
    clean = yamlio.load(Path(CLEAN_STORE).read_text(encoding="utf-8"))["documents"]
    context = {}
    for key, s in corpus.stores.items():
        longest = sorted(clean + s["docs"], key=lambda d: len(str(d.get("title"))) + len(str(d.get("text"))),
                         reverse=True)[:TOP_K]
        context[key] = {"role": "system", "content": rt.context_message(
            [{k: str(d.get(k) or "") for k in ("doc_id", "title", "text")} for d in longest])}
    system = {"role": "system", "content": orch.load_system_prompt()}
    tokens = 0
    for u in corpus.units:
        user = {"role": "user", "content": u.message}
        one = gw.estimate_tokens([system, context[u.store], user])
        if u.mode == "repeat":
            hist = gw.estimate_tokens([user])
            tokens += sum(one + j * (hist + settings.max_tokens) for j in range(u.runs))
        else:
            tokens += one * u.runs
    calls = planned_calls(corpus.units)
    return (tokens * settings.price_in_per_m + calls * settings.max_tokens * settings.price_out_per_m) / 1_000_000


# ---------- the run ----------

def ranking_problems(corpus, builds):
    """The units whose recorded top 3 is not the top 3 of SRD-1's retrieval on the written store (empty if none)."""
    out = []
    for u in corpus.units:
        _, rec = builds[u.store].orchestrator.retrieval.retrieve(u.customer_ref, u.message)
        if tuple(rec["passed_to_model"]) != u.expected:
            out.append(f"{u.condition}/{u.id}")
    return out


def config_problems(configs, stores):
    out = []
    for key, cfg in configs.items():
        if cfg.get("documents_sha256") != stores[key]["sha256"]:
            out.append(f"{key}: the store that SRD-1 read is not the store that the engine wrote")
        if (cfg.get("mode"), cfg.get("stage")) != (MODE, STAGE):
            out.append(f"{key}: SRD-1 declares {cfg.get('mode')} at {cfg.get('stage')}; the engine built {MODE} at "
                       f"{STAGE}")
        if (cfg.get("tool_path") or {}).get("tool") is not None:
            out.append(f"{key}: SRD-1 declares a tool; the pipeline build offers none")
    return out


@dataclass
class _Runner:
    """The runs of one pass. `one` runs one unit once (a fresh unit) or `runs` times in one session (a repeat unit),
    and writes one record for each reply. After a stop it sends nothing."""
    log: object
    clock_ns: object
    procedure: str
    scoring: dict
    placeholders: tuple
    halt_on_mismatch: bool = False
    status: str = "complete"
    error: object = None
    records: list = field(default_factory=list)
    hosts: Counter = field(default_factory=Counter)
    models: Counter = field(default_factory=Counter)
    mismatches: int = 0
    host_not_reported: int = 0
    usage_missing: int = 0
    last_no_result: bool = False
    last_score: object = None

    @property
    def stopped(self):
        return self.status != "complete"

    def _stop(self, u, n, t_ns, **extra):
        self.log.write({"type": "run_stopped", "condition": u.condition, "unit": u.id, "run": n, "t_ns": t_ns,
                        "reason": self.status, "error": self.error, **extra})

    def _fail(self, u, n, t_ns, exc):
        if isinstance(exc, CapReached):
            self.status = "stopped_at_cap"
        else:
            self.status, self.error = "stopped_error", type(exc).__name__      # only the class name is kept
        self._stop(u, n, t_ns)

    def one(self, target, unit, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        t0 = self.clock_ns()
        try:
            sid = target.open_session(unit.customer_ref)
        except Exception as e:      # noqa: BLE001
            self._fail(unit, n, t0, e)
            return None
        received, last = [], None
        for t in range(1, (unit.runs if unit.mode == "repeat" else 1) + 1):
            run_no = t if unit.mode == "repeat" else n
            t_ns = self.clock_ns()
            try:
                raw = target.send(sid, unit.message)
                trace = target.get_trace(sid)
            except Exception as e:      # noqa: BLE001
                self._fail(unit, run_no, t_ns, e)
                return None
            tr = [e for e in trace if e.get("kind") == "turn"][-1]
            ret = [e for e in trace if e.get("kind") == "retrieval"][-1]
            reply, counts = sec006.redact(raw or "", self.placeholders, tuple(received))
            received.append(raw or "")
            self.hosts[str(tr["served_host"])] += 1
            self.models[str(tr["model_served"])] += 1
            halt = spike.host_tally(self, tr["host_ok"])
            self.usage_missing += 1 if tr["usage_missing"] else 0
            retrieved = [{"rank": k, "doc_id": r["doc_id"], "score": r["score"]}
                         for k, r in enumerate(ret.get("retrieved") or [], 1)]
            rec = {"type": "run", "condition": unit.condition, "store": unit.store, "item": unit.item,
                   "unit": unit.id, "class": unit.cls, "pair": unit.pair, "split": unit.split,
                   "first_of_pair": unit.first_of_pair, "run": run_no, "turn": t, "t_ns": t_ns,
                   "time": runlog.iso(t_ns), "request": unit.message, "reply": reply, "placeholders": counts,
                   "retrieved": retrieved,
                   "store_check": "ok" if tuple(r["doc_id"] for r in retrieved) == unit.expected else "void",
                   "score": score(self.procedure, self.scoring, unit.item, unit.pair, reply),
                   "outcome": tierm.turn_outcome(reply, tr), **tierm._call_facts(tr)}
            self.log.write(rec)
            self.records.append(rec)
            last = reply
            self.last_no_result = rec["outcome"] == "no_result"
            if halt:
                self.status = "stopped_host_mismatch"
                self._stop(unit, run_no, t_ns, served_host=tr["served_host"])
                return None
        try:
            target.close_session(sid)
        except Exception as e:      # noqa: BLE001
            self._fail(unit, n, t0, e)
            return None
        self.last_score = {"value": 0, "kind": "label", "explanation": "deterministic labels; see the run log"}
        return last


class _Builds:
    def __init__(self):
        self.builds, self.started, self.stopped = {}, set(), False

    def stop(self):
        if self.stopped:
            return
        self.stopped = True
        for key, b in self.builds.items():
            if key in self.started:
                b.server.stop()
            else:                   # a build refused before its start still holds its listening socket
                b.server.httpd.server_close()


def execute(settings, *, run_dir, sign_key, confirmation, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant=VARIANT, engine="inspect",
            selftest_log=None, selftest_pubkey=None, steady_ns=time.monotonic_ns):
    """Run one Tier S DAT cell on SRD-1 minimal. Returns a spike.RunResult. `base_url` and `env` are for tests."""
    from . import tiersdat_log
    build, gw, orch_mod, trace_mod = spike._srd1()
    refused = start_problems(settings, variant, repo)
    if refused:
        raise ValueError(f"{getattr(settings, 'procedure', '?')}: the Tier S cell is refused: " + "; ".join(refused))
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    status = selftest.current(repo=repo, selftest_log=selftest_log, pubkey=selftest_pubkey or settings.selftest_pubkey)
    gate = selftest.gate_problems(settings.procedure, status, settings.mode)
    if gate:
        raise RuntimeError(f"{settings.procedure} is gated: " + "; ".join(gate))
    record = tierm.record_problems(settings.procedure, repo)[1]
    _, corpus = load_corpus(settings.items_file, settings.items_sha256, settings.procedure, settings.splits, repo)
    base_url = base_url or gw.OPENROUTER_BASE
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    seed = write_stores(corpus, run_dir)        # a difference raises RuntimeError, before the lock
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    (run_dir / spike.TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / spike.TARGET_DIR / "README.txt").write_text(tiersdat_log.TARGET_README, encoding="utf-8")
    builds, log, runner, summary = _Builds(), None, None, None
    prompt_file = orch_mod.PROMPT_VARIANTS[VARIANT]
    try:
        cap = None
        for key in corpus.stores:
            b = build.build_srd1(settings, egress, run_dir / spike.TARGET_DIR / f"srd1_trace_{key}.jsonl",
                                 base_url=base_url, env=env, retrieval_mode=MODE, retrieval_filter=STAGE,
                                 documents_file=run_dir / seed["stores"][key]["file"], cap=cap)
            cap = b.gateway.cap             # every build counts against one cap
            builds.builds[key] = b
            egress.add_allow(f"127.0.0.1:{b.server.port}")
        cap.spent_usd = prior_spent_usd
        refused = ranking_problems(corpus, builds.builds)
        if refused:
            raise RuntimeError(f"{settings.procedure}: the run is refused before the lock: the recorded top 3 of "
                               f"{len(refused)} texts is not SRD-1's top 3 on the written store (first: {refused[0]})")
        targets, configs, prompts = {}, {}, {}
        timeout = gw.max_call_seconds(egress.timeout) + spike.LOCAL_MARGIN_S
        for key, b in builds.builds.items():
            b.server.start()
            builds.started.add(key)
            targets[key] = Srd1ChatTarget(b.server.url, egress, timeout=timeout)
            configs[key] = targets[key].read_config("retrieval")
            p = targets[key].read_config("prompt")
            prompts[key] = (p.get("prompt_sha256"), p.get("prompt_source"),
                            runlog.sha256_hex(p.get("prompt") or "") == p.get("prompt_sha256"))
        refused = config_problems(configs, seed["stores"])
        if len(set(prompts.values())) != 1 or next(iter(prompts.values()))[1:] != (prompt_file.name, True):
            refused.append("the builds do not report one standard prompt")
        if refused:
            raise RuntimeError(f"{settings.procedure}: the run is refused before the lock: " + "; ".join(refused))
        header = tiersdat_log.build_header(
            settings, record=record, corpus=corpus, seed=seed, retrieval_config=configs, isolation=status,
            allow_hosts=egress.allow_text, system_prompt_sha256=next(iter(prompts.values()))[0],
            confirmation=confirmation, probes=probes, repo=repo, engine=engine)
        lock_ns, run_ns = runlog.run_clock(clock_ns, steady_ns)     # the records take their times from run_ns
        tiersdat_log.lock_header(header, lock_ns)
        log = runlog.RunLog(run_dir / "run_log.jsonl")
        log.write(header)                   # the lock is on disk before the first call
        runlog.wait_past_lock(header, run_ns)
        system_prompt = builds.builds[next(iter(builds.builds))].orchestrator.system_prompt
        runner = _Runner(log, run_ns, settings.procedure, header["scoring"],
                         tuple(sec006.find_placeholders([system_prompt])), halt_on_mismatch=settings.mode == "evidence")
        engine_runs = []
        for (cond, store, runs, mode), units in passes(corpus.units):
            if runner.stopped:
                break
            epochs = 1 if mode == "repeat" else runs
            facts = (spike._run_inspect(targets[store], runner, run_dir / f"inspect_{cond}_{store}", epochs, units,
                                        model_name=f"tiersdat-{cond}") if engine == "inspect"
                     else spike._run_direct(targets[store], runner, epochs, units))
            engine_runs.append({"condition": cond, "store": store, **facts})
        builds.stop()
        measures, rows = compute(settings.procedure, header["conditions"], runner.records, header.get("precedence"))
        traces = {}
        for key, b in builds.builds.items():
            ok, problems = trace_mod.verify_chain(b.trace.path, expected_head=b.trace.head)
            traces[key] = {"file": f"{spike.TARGET_DIR}/{b.trace.path.name}",
                           "sha256": runlog.sha256_hex(b.trace.path.read_bytes()), "entries": len(b.trace.entries()),
                           "head": b.trace.head, "chain_ok": ok, "problems": problems}
        summary = {
            "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
            "runs_recorded": len(runner.records), "measures_complete": runner.status == "complete",
            "measures": measures, "sub_rows": rows,
            "cost_usd": round(cap.spent_usd, 8), "model_calls": cap.calls, "cap_usd": settings.cap_usd,
            "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
            "host_mismatches": runner.mismatches, "host_not_reported": runner.host_not_reported,
            "usage_missing_calls": runner.usage_missing, "calls_over_max_tokens": cap.calls_over_max_tokens,
            "traces": traces,
            "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                       "summary": elog.summary()},
            "run_engine": engine_runs,
            "teardown": {"store_written": runlog.sha256_hex(Path(CLEAN_STORE).read_bytes()) != seed["clean"]["sha256"],
                         "srd1_stopped": builds.stopped, "note": sec003.TEARDOWN_NOTE},
            "finished": runlog.iso(run_ns()),
        }
        log.write(summary)
    finally:
        if log is not None:
            log.close()
        builds.stop()
        egress.close()
    sig =runlog.sign_log(run_dir / "run_log.jsonl", sign_key)
    return spike.RunResult(runner.status, run_dir, run_dir / "run_log.jsonl", sig, summary)
