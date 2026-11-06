"""The generic Tier M engine of bench mode: any item file of the bench's draft form, on SRD-1 minimal, chat only.

Tier M is SRD-1 minimal with its standard system prompt, tools off and retrieval off: one model call for each customer
turn. This engine runs a procedure that has no engine of its own (a procedure not in settings.PROCEDURES). It runs in
bench mode only (faias_harness/bench.py). The run log has its own schema, faias-tierm-log/1 (tierm_log.py).

The steps, in order:
  1. The cell settings (cell_settings). A bench cell of tier M whose procedure has no engine of its own takes the
     shared keys of an SRD-1 settings file (checked by settings.validate) and four keys of its own: `items_file` (the
     path of the item file, outside the repository), `items_sha256` (its SHA-256), `store_replies` (full, the default,
     or hash_only) and `splits` (the splits to run: private, the default, dev, or both). It takes no item set, no
     reviewer value and no key of another procedure. Tools stay off. In evidence mode the settings.validate line about
     the M4 value of TP-SEC-001 is left out (M4_LINE): a cell of this engine has no M4. Every other line stays.
  2. The record (record_problems). The procedure ID and the star label come from records/<procedure>.yaml, which
     tools/build_records.py builds from FAIAS. A procedure with no record is refused before any file or call. The
     engine never computes a star label and never makes up a record.
  3. The item file (item_problems), the TP-SEC-001 private-set rule: named, outside the repository, present, of the
     SHA-256 of the settings, one UTF-8 JSON object with no repeated key (jsonkeys.unique_keys), whose `procedure` is
     the procedure of the cell and which holds a `dataset_guid`. Each item has a unique `id`, a `split` (dev or
     private) and exactly one of `text` (one customer turn) or `turns` (a list of customer turns, sent in order in one
     conversation). An item may hold `variants`: each with its own unique `id` and its own `text` or `turns`, in the
     split of its item. Each text and variant text is a unit. No message quotes item text. Each unit carries the
     scoring fields of its item or variant that the measure scorer of the procedure reads (`meta`;
     tierm_measures.FIELDS); a procedure with no scorer has none.
  4. The checks that the file declares (declared_checks). Three forms: `deterministic` (a mapping of a check name to a
     regular expression and its flags, the form of the TP-CON-003 draft); `decision_values` with an `answer_suffix`
     that asks for a line "DECISION: <value>" (the form of the TP-MOD-014 draft: the extractor of its rubric, section 2,
     kind decision_line); and `checks` (a list of checks of the kinds regex, decision_line and first_word, the form of
     this engine). An item whose `scoring.deterministic` list names checks runs those only. The checks run on each
     reply, after the redaction of the staged placeholders. They give item outcomes. They do not give a measure.
  5. SRD-1 minimal with the standard prompt, tools off, retrieval off. The engine reads the prompt through config_read
     and checks its SHA-256. The header (tierm_log.build_header) and its lock go to disk before the first call.
  6. Each unit runs k times (runs_per_item). Each run opens a new session, sends each turn in order and closes the
     session. One `run` record holds every turn: its request (or its SHA-256), its reply (or its SHA-256 with
     store_replies hash_only), its checks and the facts of its model call. The record also holds `request` and `reply`
     for a reader of one exchange (the judge pass of the bench): for one turn, the customer text and the reply; for
     more turns, the conversation before the last reply as a transcript, and the last reply. With hash_only, both are
     SHA-256 only. A cap reached, an error or, in evidence mode, a host other than the locked one stops the run.
  7. The summary: the item outcomes (tierm_log.outcomes), "item outcomes, measure not computed", the cost, the trace
     and the egress summary. Then the signature.

The engine computes no measure of any procedure. For five procedures, the bench report computes the measures from the
signed log afterwards (tierm_measures.py). Tests use local stand-ins only. No live run has used this engine.
"""
import json
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import settings as settings_mod
from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..errors import CapReached
from . import jsonkeys, runlog, sec006, spike, tierm_measures

REPO = Path(__file__).resolve().parents[2]
KIND = "tierm"
ITEM_SET = "tierm_private"
VARIANT = "standard"
TP_FORM = re.compile(r"TP-[A-Z]{3}-\d{3}")
SPLITS = ("dev", "private")
SPLITS_DEFAULT = ("private",)
STORE_MODES = ("full", "hash_only")
CELL_KEYS = ("items_file", "items_sha256", "store_replies", "splits")
# The keys of an SRD-1 settings file that a cell of this engine shares with every engine.
SHARED_KEYS = set(settings_mod.TOP_REQUIRED) | {"kind", "max_tokens", "temperature", "seed", "runs_per_item",
                                                "tools_enabled", "selftest_pubkey"}
# The one line of settings.validate that names the M4 value of TP-SEC-001 in evidence mode. A cell of this engine has
# no M4, so the line is left out. If its words change, the line is not left out and the cell is refused (the safe side).
M4_LINE = "evidence mode: reviewer_values.M4 needs a value and a reason"
CHECK_NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
CHECK_KINDS = ("regex", "decision_line", "first_word")
FLAGS = {None: 0, "": 0, "ignore case": re.IGNORECASE}
NONE = "NONE"
NO_REPLY = "the run has no reply"


# ---------- settings ----------

@dataclass(frozen=True)
class TierMSettings:
    """The settings of one cell: the shared SRD-1 settings (`base`, a settings.Settings) and the keys of this engine.
    Every other attribute is read from `base`. `kind` and `item_set` are class attributes, as in settings.Settings."""
    base: object
    procedure: str
    items_file: str
    items_sha256: str
    store_replies: str = "full"
    splits: tuple = SPLITS_DEFAULT
    kind = KIND
    item_set = ITEM_SET

    def __getattr__(self, name):
        if name.startswith("__") or name == "base":
            raise AttributeError(name)
        return getattr(self.base, name)

    def canonical(self):
        out = self.base.canonical()
        out.pop("item_set", None)
        out.pop("reviewer_values", None)
        out.update(engine=KIND, tier="M", procedure=self.procedure, items_file=self.items_file,
                   items_sha256=self.items_sha256, store_replies=self.store_replies, splits=list(self.splits))
        return out

    def digest(self):
        return runlog.sha256_hex(runlog.canonical(self.canonical()))


def is_tierm(settings):
    return getattr(settings, "kind", None) == KIND


def cell_settings(raw):
    """(TierMSettings or None, problems) for the mapping of one bench cell."""
    if not isinstance(raw, dict):
        return None, ["the settings are not a mapping"]
    problems, proc = [], raw.get("procedure")
    if not (isinstance(proc, str) and TP_FORM.fullmatch(proc)):
        problems.append("procedure must be a FAIAS procedure ID of the form TP-XXX-NNN")
    elif proc in settings_mod.PROCEDURES:
        problems.append(f"{proc} has an engine of its own; the Tier M engine runs procedures that have none")
    base = {k: v for k, v in raw.items() if k not in CELL_KEYS and k != "procedure"}
    problems += [f"{k} does not apply to a cell of the Tier M engine" for k in base
                 if k not in SHARED_KEYS and k in settings_mod.TOP_OPTIONAL]
    if base.get("tools_enabled") is True:
        problems.append("the Tier M engine runs with tools off (set tools_enabled to false)")
    problems += [p for p in settings_mod.validate(base) if p != M4_LINE]
    f, sha = raw.get("items_file"), raw.get("items_sha256")
    if not (isinstance(f, str) and f.strip()):
        problems.append("items_file must be the path of the item file, outside the repository")
    if not (isinstance(sha, str) and settings_mod.SHA256_HEX.fullmatch(sha)):
        problems.append("items_sha256 must be the SHA-256 of the item file (64 lower-case hexadecimal characters)")
    store = raw.get("store_replies", "full")
    if store not in STORE_MODES:
        problems.append(f"store_replies must be one of {', '.join(STORE_MODES)}")
    splits = raw.get("splits", list(SPLITS_DEFAULT))
    if not (isinstance(splits, list) and splits and all(s in SPLITS for s in splits)
            and len(set(splits)) == len(splits)):
        problems.append(f"splits must be a non-empty list of {' and '.join(SPLITS)}, each once")
    if problems:
        return None, problems
    order = {s: n for n, s in enumerate(SPLITS)}
    return TierMSettings(settings_mod.from_raw(base), proc, f, sha, store,
                         tuple(sorted(splits, key=order.get))), []


# ---------- record ----------

def record_path(tp, repo=REPO):
    return Path(repo) / "records" / f"{tp}.yaml"


def record_problems(tp, repo=REPO):
    """(problems, record). The record of the procedure must exist. Its ID must be the procedure and its star label
    must be set."""
    from .. import yamlio
    if not (isinstance(tp, str) and TP_FORM.fullmatch(tp)):
        return ["the procedure is not a FAIAS procedure ID"], None
    path = record_path(tp, repo)
    if not path.is_file():
        return [f"{tp} has no record in records/ (tools/build_records.py makes the records from FAIAS); the engine "
                "copies the procedure ID and the star label from a record and never makes one up, so the cell is "
                "refused"], None
    rec = yamlio.load(path.read_text(encoding="utf-8"))
    if not isinstance(rec, dict) or rec.get("tp") != tp or not rec.get("star"):
        return [f"the record of {tp} does not name the procedure or has no star label"], None
    return [], rec


# ---------- item file ----------

def _inside(path, repo):
    p, r = Path(path).resolve(), Path(repo).resolve()
    return p == r or r in p.parents


def _text(v):
    return isinstance(v, str) and bool(v.strip())


def item_problems(path, sha256, procedure, repo=REPO):
    """(problems, data) for the item file of a cell: the file rules of step 3. `data` is None with a problem."""
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
    _, problems = parse_units(data)
    _, check_problems = declared_checks(data)
    out += problems + check_problems
    return out, (None if out else data)


@dataclass(frozen=True)
class Unit:
    """One text to run: an item, or one variant of an item. `checks` names the declared checks that its replies get."""
    id: str
    item: str
    variant_of: object
    cls: object
    split: str
    turns: tuple
    checks: tuple = ()
    judge: object = None
    meta: dict = field(default_factory=dict, hash=False, compare=False)    # tierm_measures.unit_meta
    activity: str = "Tier M"
    objective: str = "item outcomes only; no measure is computed"

    @property
    def text(self):
        """The Inspect view (spike._run_inspect reads `text`): the ID and the SHA-256 of the turns only, so the
        Inspect log holds no item text."""
        return f"Tier M unit {self.id}; held by its SHA-256 only: {runlog.sha256_hex(runlog.canonical(list(self.turns)))}"


def _turns(node, where, limits):
    """(turns, problems) of an item or a variant: exactly one of `text` and `turns`."""
    max_turns, max_chars = limits
    has_text, has_turns = "text" in node, "turns" in node
    if has_text == has_turns:
        return (), [f"{where}: it needs exactly one of text and turns"]
    turns = [node["text"]] if has_text else node["turns"]
    if not (isinstance(turns, list) and turns and all(_text(t) for t in turns)):
        return (), [f"{where}: its text or turns must be non-empty text"]
    out = []
    if len(turns) > max_turns:
        out.append(f"{where}: it has {len(turns)} turns; SRD-1 takes {max_turns} at most")
    if any(len(t) > max_chars for t in turns):
        out.append(f"{where}: a turn is longer than {max_chars} characters, the most that SRD-1 takes")
    return tuple(turns), out


def _limits():
    _, _, orch, _ = spike._srd1()
    return orch.MAX_TURNS, orch.MAX_MESSAGE_CHARS


def _check_names(item):
    """The check names that an item's `scoring.deterministic` list gives (the first word of each entry), or None."""
    sc = item.get("scoring")
    det = sc.get("deterministic") if isinstance(sc, dict) else None
    if det is None:
        return None
    if not (isinstance(det, list) and all(_text(x) for x in det)):
        return ["?"]
    return [re.split(r"[\s(]", x.strip(), maxsplit=1)[0] for x in det]


def _judge_criteria(item):
    sc = item.get("scoring")
    j = sc.get("judge") if isinstance(sc, dict) else None
    return tuple(j) if isinstance(j, list) and all(isinstance(x, str) for x in j) else None


def parse_units(data, splits=SPLITS):
    """(units, problems): the units of the item file in the splits named, in file order (an item, then its
    variants)."""
    items = data.get("items")
    if not (isinstance(items, list) and items):
        return [], ["the item file has no list of items"]
    names = [c["name"] for c in declared_checks(data)[0]]
    limits, units, out, ids = _limits(), [], [], set()
    for n, i in enumerate(items, 1):
        if not (isinstance(i, dict) and _text(i.get("id"))):
            out.append(f"item {n}: it needs an id")
            continue
        where = f"item {n} ({i['id']})"
        if i["id"] in ids:
            out.append(f"{where}: the id is used more than once")
            continue
        ids.add(i["id"])
        if i.get("split") not in SPLITS:
            out.append(f"{where}: its split must be one of {', '.join(SPLITS)}")
            continue
        turns, problems = _turns(i, where, limits)
        out += problems
        wanted = _check_names(i)
        if wanted is not None and [w for w in wanted if w not in names]:
            out.append(f"{where}: its scoring names a check that the file does not declare")
        checks = tuple(names if wanted is None else [c for c in names if c in wanted])
        cls = i.get("class") if isinstance(i.get("class"), str) else None
        proc = data.get("procedure")
        group = [Unit(i["id"], i["id"], None, cls, i["split"], turns, checks, _judge_criteria(i),
                      tierm_measures.unit_meta(proc, i))]
        variants = i.get("variants", [])
        if not isinstance(variants, list):
            out.append(f"{where}: its variants are not a list")
            variants = []
        for m, v in enumerate(variants, 1):
            vw = f"{where}, variant {m}"
            if not (isinstance(v, dict) and _text(v.get("id"))):
                out.append(f"{vw}: it needs an id")
                continue
            if v["id"] in ids:
                out.append(f"{vw} ({v['id']}): the id is used more than once")
                continue
            ids.add(v["id"])
            if "split" in v and v["split"] != i["split"]:
                out.append(f"{vw} ({v['id']}): its split differs from the split of its item")
            vturns, problems = _turns(v, f"{vw} ({v['id']})", limits)
            out += problems
            group.append(Unit(v["id"], i["id"], i["id"], cls, i["split"], vturns, checks, _judge_criteria(i),
                              tierm_measures.unit_meta(proc, v, variant=True)))
        if i["split"] in splits:
            units += group
    return units, out


def _flags(spec, where):
    f = spec.get("flags")
    if not (f is None or (isinstance(f, str) and f in FLAGS)):
        return None, [f"{where}: flags must be 'ignore case' or empty"]
    return f, []


def declared_checks(data):
    """(checks, problems): the checks that the item file declares, each a dict with name, kind, source and the fields
    of its kind (pattern and flags; or label and values)."""
    checks, out = [], []
    det = data.get("deterministic")
    if det is not None:
        if not isinstance(det, dict):
            out.append("deterministic must be a mapping of check names to patterns")
            det = {}
        for name, spec in det.items():
            spec = spec if isinstance(spec, dict) else {}
            checks.append({"name": name, "kind": "regex", "pattern": spec.get("pattern"), "flags": spec.get("flags"),
                           "source": "deterministic"})
    if "decision_values" in data:
        suffix = data.get("answer_suffix")
        if not (_text(suffix) and "DECISION:" in suffix):
            out.append("decision_values needs an answer_suffix that asks for a line 'DECISION: <value>'")
        checks.append({"name": "decision", "kind": "decision_line", "label": "DECISION",
                       "values": data.get("decision_values"), "source": "decision_values"})
    explicit = data.get("checks")
    if explicit is not None:
        if not isinstance(explicit, list):
            out.append("checks must be a list")
            explicit = []
        for c in explicit:
            c = c if isinstance(c, dict) else {}
            checks.append({**{k: c.get(k) for k in ("name", "kind")},
                           **({"pattern": c.get("pattern"), "flags": c.get("flags")} if c.get("kind") == "regex" else
                              {"label": c.get("label", "DECISION"), "values": c.get("values")}
                              if c.get("kind") == "decision_line" else {"values": c.get("values")}),
                           "source": "checks"})
    out += check_problems(checks)
    return (checks if not out else []), out


def check_problems(checks):
    """The problems of a list of declared checks (empty if none). tierm_log.check reads the list in a header too."""
    out, seen = [], set()
    for n, c in enumerate(checks, 1):
        name = c.get("name")
        where = f"check {n} ({name})" if isinstance(name, str) else f"check {n}"
        if not (isinstance(name, str) and CHECK_NAME.fullmatch(name)):
            out.append(f"{where}: the name must be a-z, 0-9 and _")
        elif name in seen:
            out.append(f"{where}: the name is used more than once")
        seen.add(name)
        kind = c.get("kind")
        if kind not in CHECK_KINDS:
            out.append(f"{where}: the kind must be one of {', '.join(CHECK_KINDS)}")
        elif kind == "regex":
            out += _flags(c, where)[1]
            try:
                if not _text(c.get("pattern")):
                    raise re.error("empty")
                re.compile(c["pattern"])
            except re.error:
                out.append(f"{where}: the pattern is not a regular expression")
        else:
            v = c.get("values")
            if not (isinstance(v, list) and v and all(isinstance(x, str) and re.fullmatch(r"[A-Z][A-Z_]*", x)
                                                      for x in v) and len(set(v)) == len(v) and NONE not in v):
                out.append(f"{where}: values must be a list of upper-case words, each once, not {NONE}")
            if kind == "decision_line" and not (isinstance(c.get("label"), str)
                                                and re.fullmatch(r"[A-Z][A-Z_]*", c["label"])):
                out.append(f"{where}: the label must be an upper-case word")
    return out


# ---------- the checks (the deterministic scorer of the item outcomes) ----------

_STRIP = re.compile(r"[*_`]")


def _clean_lines(reply):
    """The lines of a reply with *, _ and ` removed and a leading > removed, stripped, with no empty line (the
    TP-MOD-014 rubric, section 2, step 1)."""
    out = []
    for line in (reply or "").splitlines():
        line = _STRIP.sub("", line).strip()
        line = line[1:].strip() if line.startswith(">") else line
        if line:
            out.append(line)
    return out


def decision_line(reply, label, values):
    """{value, route}: the extractor of the TP-MOD-014 rubric, section 2. The last cleaned line must be exactly
    '<label>: <value>' (case ignored, an optional full stop): route primary. Else every '<label>: <value>' in the
    cleaned reply; one value only: route fallback. Else NONE, route none."""
    alt = "|".join(re.escape(v) for v in values)
    lines = _clean_lines(reply)
    if lines:
        m = re.fullmatch(rf"{re.escape(label)}\s*:\s*({alt})\s*\.?", lines[-1], re.IGNORECASE)
        if m:
            return {"value": m.group(1).upper(), "route": "primary"}
    found = {m.upper() for m in re.findall(rf"\b{re.escape(label)}\s*:\s*({alt})\b", "\n".join(lines), re.IGNORECASE)}
    if len(found) == 1:
        return {"value": found.pop(), "route": "fallback"}
    return {"value": NONE, "route": "none"}


def first_word(reply, values):
    """{value, route}: the extractor of the TP-CON-001 rubric, section 3, steps 1 to 5. The first word of the first
    line that is not empty, after the removal of leading space, * _ # > ` and quotation marks: route first_word. Else
    exactly one of the values as a whole word in that line: route first_line. Else NONE, route none."""
    lines = [re.sub(r"^[\s*_#>`\"'“”‘’]+", "", x) for x in (reply or "").splitlines()]
    first = next((x for x in lines if x.strip()), "")
    word = (first.split() or [""])[0].upper().rstrip(".,:;!-")
    if word in values:
        return {"value": word, "route": "first_word"}
    found = {v for v in values if re.search(rf"\b{re.escape(v)}\b", first, re.IGNORECASE)}
    if len(found) == 1:
        return {"value": found.pop(), "route": "first_line"}
    return {"value": NONE, "route": "none"}


def run_check(check, reply):
    kind = check["kind"]
    if kind == "regex":
        n = len(re.findall(check["pattern"], reply or "", FLAGS[check.get("flags")]))
        return {"matched": n > 0, "count": n}
    if kind == "decision_line":
        return decision_line(reply, check["label"], check["values"])
    return first_word(reply, check["values"])


def run_checks(checks, names, reply):
    """The results of the checks named in `names`, on one reply, by name."""
    by_name = {c["name"]: c for c in checks}
    return {n: run_check(by_name[n], reply) for n in names}


# ---------- estimate ----------

def load_units(settings, repo=REPO):
    """(units, checks, data) of the item file of the settings, in the splits of the settings. A ValueError names the
    problems."""
    problems, data = item_problems(settings.items_file, settings.items_sha256, settings.procedure, repo)
    if problems:
        raise ValueError("; ".join(problems))
    units, _ = parse_units(data, settings.splits)
    return units, declared_checks(data)[0], data


def planned_calls(settings, units):
    return sum(len(u.turns) for u in units) * settings.runs_per_item


def estimate(settings, repo=REPO):
    """The worst-case cost of a cell in USD, or None if the prices are not set or the item file is refused. Each call
    counts at the full output limit, and each earlier reply of a conversation counts at the output limit too."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    try:
        units, _, _ = load_units(settings, repo)
    except ValueError:
        return None
    _, gw, orch, _ = spike._srd1()
    system = [{"role": "system", "content": orch.load_system_prompt()}]
    tokens = 0
    for u in units:
        users = []
        for j, t in enumerate(u.turns):
            users.append({"role": "user", "content": t})
            tokens += gw.estimate_tokens(system + users) + j * settings.max_tokens
    k, calls = settings.runs_per_item, planned_calls(settings, units)
    return (tokens * k * settings.price_in_per_m + calls * settings.max_tokens * settings.price_out_per_m) / 1_000_000


# ---------- start checks ----------

def start_problems(settings, variant=VARIANT, repo=REPO):
    """What refuses a cell before any file or call: the settings kind, the variant, tools, the record and the item
    file."""
    if not is_tierm(settings):
        return ["the settings are not the settings of a Tier M engine cell"]
    out = []
    if variant != VARIANT:
        out.append(f"the variant {variant} does not apply: the Tier M engine runs the standard prompt only")
    if getattr(settings, "tools_enabled", False):
        out.append("the Tier M engine runs with tools off")
    out += record_problems(settings.procedure, repo)[0]
    out += item_problems(settings.items_file, settings.items_sha256, settings.procedure, repo)[0]
    if not out and not load_units(settings, repo)[0]:
        out.append(f"the item file has no item in the splits {', '.join(settings.splits)}")
    return out


# ---------- the run ----------

def transcript(turns, replies):
    """The judge view of a conversation: for one turn, the customer text; for more, the customer turns and the earlier
    replies as a transcript, up to the last customer turn."""
    if len(turns) == 1:
        return turns[0]
    lines = []
    for t, text in enumerate(turns, 1):
        lines.append(f"Customer (turn {t}): {text}")
        if t < len(turns):
            lines.append(f"Assistant (turn {t}): {replies[t - 1]}")
    return "\n".join(lines)


def _call_facts(tr):
    return {"finish_reason": tr.get("finish_reason"), "blocked": tr.get("blocked"), "served_host": tr["served_host"],
            "served_model": tr["model_served"], "host_ok": tr["host_ok"], "model_requested": tr["model_requested"],
            "prompt_tokens": tr["prompt_tokens"], "completion_tokens": tr["completion_tokens"],
            "usage_missing": tr["usage_missing"], "cost_usd": tr["cost_usd"], "latency_ms": tr["latency_ms"],
            "seed": tr.get("seed"), "retries": tr.get("retries", 0), "retry_reasons": tr.get("retry_reasons", []),
            "trace_seq": tr["seq"]}


def turn_outcome(reply, tr):
    if tr.get("blocked") or tr.get("finish_reason") == "error" or not (reply or "").strip():
        return "no_result"
    return "replied"


@dataclass
class _Runner:
    """The runs. `one` runs one unit once and writes its record. Both engines call it. After a stop it sends nothing."""
    log: object
    clock_ns: object
    checks: list
    store: str
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
    retries: int = 0
    last_no_result: bool = False
    last_score: object = None

    @property
    def stopped(self):
        return self.status != "complete"

    def _stop(self, u, n, t_ns, turn=None, **extra):
        self.log.write({"type": "run_stopped", "item": u.id, "run": n, "turn": turn, "t_ns": t_ns,
                        "reason": self.status, "error": self.error, **extra})

    def _fail(self, u, n, t_ns, exc, turn=None):
        if isinstance(exc, CapReached):
            self.status = "stopped_at_cap"
        else:
            self.status, self.error = "stopped_error", type(exc).__name__      # only the class name is kept
        self._stop(u, n, t_ns, turn)

    def one(self, target, unit, n):
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        t0 = self.clock_ns()
        try:
            sid = target.open_session()
        except Exception as e:      # noqa: BLE001
            self._fail(unit, n, t0, e)
            return None
        turns, received, replies, halt = [], [], [], False
        full = self.store == "full"
        for t, text in enumerate(unit.turns, 1):
            t_ns = self.clock_ns()
            try:
                raw = target.send(sid, text)
                tr = [e for e in target.get_trace(sid) if e["kind"] == "turn"][-1]
            except Exception as e:      # noqa: BLE001
                self._fail(unit, n, t_ns, e, t)
                return None
            reply, counts = sec006.redact(raw or "", self.placeholders, tuple(received))
            received.append(raw or "")
            replies.append(reply)
            self.hosts[str(tr["served_host"])] += 1
            self.models[str(tr["model_served"])] += 1
            halt = spike.host_tally(self, tr["host_ok"]) or halt
            self.usage_missing += 1 if tr["usage_missing"] else 0
            self.retries += tr.get("retries", 0)
            entry = {"turn": t, "t_ns": t_ns}
            entry.update({"request": text} if full else {"request_sha256": runlog.sha256_hex(text)})
            entry.update({"reply": reply} if full else {"reply_sha256": runlog.sha256_hex(reply)})
            entry.update(placeholders=counts, checks=run_checks(self.checks, unit.checks, reply),
                         outcome=turn_outcome(reply, tr), **_call_facts(tr))
            turns.append(entry)
        try:
            target.close_session(sid)
        except Exception as e:      # noqa: BLE001
            self._fail(unit, n, t0, e)
            return None
        view = transcript(unit.turns, replies)
        outcome = ("no_result" if all(x["outcome"] == "no_result" for x in turns) else
                   "replied" if all(x["outcome"] == "replied" for x in turns) else "partial")
        rec = {"type": "run", "item": unit.id, "base_item": unit.item, "variant_of": unit.variant_of,
               "class": unit.cls, "split": unit.split, "run": n, "t_ns": t0, "time": runlog.iso(t0), "turns": turns}
        rec.update({"request": view, "reply": replies[-1]} if full else
                   {"request_sha256": runlog.sha256_hex(view), "reply_sha256": runlog.sha256_hex(replies[-1])})
        rec.update(score={"turn": len(turns), "checks": turns[-1]["checks"]}, outcome=outcome,
                   judge={"status": "not judged in this log",
                          "criteria": list(unit.judge) if unit.judge is not None else None})
        self.log.write(rec)
        self.records.append(rec)
        self.last_no_result = outcome == "no_result"
        self.last_score = {"value": 0, "kind": "item outcome", "explanation": "item outcomes, measure not computed"}
        if halt:
            self.status = "stopped_host_mismatch"
            self._stop(unit, n, t0, served_host=turns[-1]["served_host"])
        return replies[-1] if full else f"reply stored as SHA-256 only: {rec['reply_sha256']}"


def execute(settings, *, run_dir, sign_key, confirmation, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant=VARIANT, engine="inspect"):
    """Run one Tier M cell on SRD-1 minimal. Returns a spike.RunResult. `base_url` and `env` are for tests."""
    from . import tierm_log
    build, gw, orch_mod, trace_mod = spike._srd1()
    refused = start_problems(settings, variant, repo)
    if refused:
        raise ValueError(f"{getattr(settings, 'procedure', '?')}: the Tier M cell is refused: " + "; ".join(refused))
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    record = record_problems(settings.procedure, repo)[1]
    units, checks, data = load_units(settings, repo)
    prompt_file = orch_mod.PROMPT_VARIANTS[VARIANT]
    system_prompt = orch_mod.load_system_prompt(prompt_file)
    base_url = base_url or gw.OPENROUTER_BASE
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    (run_dir / spike.TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / spike.TARGET_DIR / "README.txt").write_text(tierm_log.TARGET_README, encoding="utf-8")
    srd1 = build.build_srd1(settings, egress, run_dir / spike.TARGET_DIR / "srd1_trace.jsonl", base_url=base_url,
                            env=env, system_prompt=system_prompt, prompt_source=prompt_file.name)
    egress.add_allow(f"127.0.0.1:{srd1.server.port}")
    srd1.gateway.cap.spent_usd = prior_spent_usd
    target = Srd1ChatTarget(srd1.server.url, egress, timeout=gw.max_call_seconds(egress.timeout) + spike.LOCAL_MARGIN_S)
    log_path, log, summary, engine_facts = run_dir / "run_log.jsonl", None, None, None
    srd1.server.start()
    try:
        config = target.read_config("prompt")       # through the interface
        if (runlog.sha256_hex(config.get("prompt") or "") != config.get("prompt_sha256")
                or config.get("prompt_source") != prompt_file.name):
            raise RuntimeError("SRD-1 states a prompt that is not the standard prompt, or whose SHA-256 differs; the "
                               "cell is refused before the lock")
        header = tierm_log.build_header(
            settings, record=record, data=data, units=units, checks=checks, allow_hosts=egress.allow_text,
            system_prompt_sha256=config["prompt_sha256"], confirmation=confirmation, probes=probes, repo=repo,
            engine=engine)
        tierm_log.lock_header(header, clock_ns())
        log = runlog.RunLog(log_path)
        log.write(header)                   # the lock is on disk before the first call
        runlog.wait_past_lock(header, clock_ns)
        runner = _Runner(log, clock_ns, checks, settings.store_replies,
                         tuple(sec006.find_placeholders([system_prompt])),
                         halt_on_mismatch=settings.mode == "evidence")
        k = settings.runs_per_item
        engine_facts = (spike._run_inspect(target, runner, run_dir, k, units, model_name="tierm") if engine == "inspect"
                        else spike._run_direct(target, runner, k, units))
    except BaseException:
        if log is not None:
            log.close()
        srd1.server.stop()
        egress.close()
        raise
    srd1.server.stop()
    try:
        chain_ok, chain_problems = trace_mod.verify_chain(srd1.trace.path, expected_head=srd1.trace.head)
        egress.close()
        cap = srd1.gateway.cap
        summary = {
            "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
            "runs_recorded": len(runner.records), "measures": {}, "measures_complete": False,
            "measure_status": tierm_log.MEASURE_STATUS, "item_outcomes": tierm_log.outcomes(runner.records),
            "cost_usd": round(cap.spent_usd, 8), "model_calls": cap.calls, "cap_usd": settings.cap_usd,
            "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
            "host_mismatches": runner.mismatches, "host_not_reported": runner.host_not_reported,
            "usage_missing_calls": runner.usage_missing, "retries": runner.retries,
            "calls_over_max_tokens": cap.calls_over_max_tokens,
            "trace": {"file": f"{spike.TARGET_DIR}/{srd1.trace.path.name}",
                      "sha256": runlog.sha256_hex(srd1.trace.path.read_bytes()), "entries": len(srd1.trace.entries()),
                      "head": srd1.trace.head, "chain_ok": chain_ok, "problems": chain_problems},
            "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                       "summary": elog.summary()},
            "run_engine": engine_facts,
            "finished": runlog.iso(),
        }
        log.write(summary)
    finally:
        log.close()
    sig = runlog.sign_log(log_path, sign_key)
    return spike.RunResult(runner.status, run_dir, log_path, sig, summary)
