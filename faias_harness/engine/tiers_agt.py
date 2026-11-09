"""The Tier S engine of bench mode for three agent item sets: TP-AGT-001, TP-AGT-002 and TP-AGT-010.

Tier S is the model inside SRD-1 minimal with its tools through the harness's MCP tool emulator. This engine runs the
draft item sets of the benchmark (research/BENCH_2026-11/items_draft/TP-AGT-00x, task W7), each with its own synthetic
store. It runs in bench mode only (faias_harness/bench.py). The run log has its own schema, faias-tiers-agt-log/1
(tiers_agt_log.py). The scorer is tiers_agt_score.py.

The steps, in order:
  1. The cell settings (cell_settings). A bench cell of tier S whose procedure is one of PROCEDURES takes the shared
     keys of an SRD-1 settings file (checked by settings.validate), with tools_enabled true and tool_key_env, and six
     keys of its own: `items_file` and `items_sha256` (the item file, outside the repository, and its SHA-256),
     `store_file` and `store_sha256` (the item set's synthetic store, outside the repository, and its SHA-256),
     `store_replies` (full, the default, or hash_only) and `splits` (private, the default, dev, or both). The runs per
     item come from PLAYBOOK_AGT.md line 45: at least 5 (RUNS_MIN); a cell with fewer than 10 (RUNS_PREFERRED, "for
     consequential-action scenarios"; also the n>=10 of line 18 of each procedure's playbook) runs, and its label
     says so. A fee refund is a consequential action.
  2. The record. The procedure ID and the star label come from records/<procedure>.yaml (tierm.record_problems). A
     procedure with no record is refused before any file or call. TP-AGT-001 has no record today (its classification
     row has a human step), so its cells are refused until the record builder makes one.
  3. The item file and the store. Each is refused when it is inside the repository, absent or of another SHA-256. The
     item file must be one UTF-8 JSON object with no repeated key, name the procedure and the dataset GUID, and pin
     the store by its SHA-256 (tools.store_sha256). The store is read with the emulator's store loader and never
     written; the shipped stores of targets/srd1/data/ are not used and not changed (AGENTS.md rule 16: the emulator
     returns synthetic results only). Each item is checked against the store (spec_problems): its customer, accounts,
     fees and documents exist there. Its scoring inputs (the spec) go to the header manifest, so the log can be scored
     again without the item file. No message quotes item text.
  4. The self-test gate of the tools-on engines (isolation_problems): the rule of selftest.gate_problems, as for
     TP-AGT-012.
  5. The build. TP-AGT-002: SRD-1 minimal with tools on and retrieval off, the tools file targets/srd1/tools.yaml
     (fee_refund). TP-AGT-001 and TP-AGT-010: retrieval_mode tool with refund_tool (build.py), the tools file
     targets/srd1/tools_tier_s.yaml (fee_refund and document_search). C-07 reads the store as its data file and its
     document file; the emulator reads it as its store (toolrun.ToolRun, store=). The refund gate C-09 is the declared
     one (targets/srd1/risk_appetite.yaml). The system prompt is the standard prompt; the engine reads it through
     config_read and checks its SHA-256 and its source, and checks the served tool names.
  6. Each item runs k times. Each run opens a session for the item's customer, sends the turns in order, reads the
     session's trace and the new ledger lines, and closes the session. The run record holds each turn (its request
     and reply, or their SHA-256 with hash_only), the `tool_call` lines of the trace with their turn, the ledger
     lines, and the facts of the scorer. Free text in tool arguments (a query, a reason) is kept with full and kept as
     a SHA-256 with hash_only; identifiers and amounts are kept in both. Each text is redacted with sec006.redact.
     A cap reached, an error or, in evidence mode, a host other than the locked one stops the run.
  7. The summary: the measures that the trace and the ledger give (tiers_agt_score.summarise), the measures not
     computed with the reason, the other scores, the reconciliation of trace and ledger, whether the store file is
     unchanged, the cost, the trace and ledger chains, the egress summary. Then the signature.

Every row is model propensity, not control (benchreport.PROPENSITY). Tests use local stand-ins only. No live run has
used this engine.
"""
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .. import selftest
from .. import settings as settings_mod
from ..adapters.srd1_http import Srd1ChatTarget
from ..egress import EgressClient, EgressLog
from ..emulators import handlers, mcp_tools
from ..errors import CapReached
from . import jsonkeys, runlog, sec006, spike, tierm, toolrun
from .tiers_agt_score import AGT001, AGT002, AGT010, REFUND, SEARCH

REPO = Path(__file__).resolve().parents[2]
KIND = "tiers_agt"
ITEM_SET = "tiers_agt_private"
VARIANT = "standard"
PROCEDURES = (AGT001, AGT002, AGT010)
RUNS_MIN, RUNS_PREFERRED = 5, 10
RUNS_SOURCE = ("PLAYBOOK_AGT.md:45: n>=5, preferably n>=10 for consequential-action scenarios (reviewer-set design "
               "minima); a fee refund is a consequential action (targets/srd1/app/gate.py, IRREVERSIBLE_ACTIONS)")
SPLITS = tierm.SPLITS
STORE_MODES = tierm.STORE_MODES
CELL_KEYS = ("items_file", "items_sha256", "store_file", "store_sha256", "store_replies", "splits")
SHARED_KEYS = tierm.SHARED_KEYS
SRD1 = REPO / "targets" / "srd1"
TOOLS_FILES = {AGT001: SRD1 / "tools_tier_s.yaml", AGT002: SRD1 / "tools.yaml", AGT010: SRD1 / "tools_tier_s.yaml"}
SEARCH_SETS = (AGT001, AGT010)
ID_ARGS = ("account_ref", "fee_id", "amount_gbp", "on_behalf_of")
GATE_RULE_OF = "TP-AGT-012"     # the self-test rule of the tools-on engines (selftest.GATED)
PER_ROUND_CALLS = 3             # the estimate's assumption: tool calls in one round
BUILDS = {
    AGT001: "SRD-1 minimal, standard prompt, retrieval_mode tool with refund_tool: fee_refund and document_search "
            "through the MCP tool emulator, the refund gate C-09 in the orchestrator, C-07 on each call",
    AGT002: "SRD-1 minimal, standard prompt, tools on, retrieval off: fee_refund through the MCP tool emulator, the "
            "refund gate C-09 in the orchestrator, C-07 on each call",
    AGT010: "SRD-1 minimal, standard prompt, retrieval_mode tool with refund_tool: fee_refund and document_search "
            "through the MCP tool emulator, the refund gate C-09 in the orchestrator, C-07 on each call",
}


# ---------- settings ----------

@dataclass(frozen=True)
class TierSSettings:
    """The settings of one cell: the shared SRD-1 settings (`base`) and the keys of this engine. Every other attribute
    is read from `base`."""
    base: object
    procedure: str
    items_file: str
    items_sha256: str
    store_file: str
    store_sha256: str
    store_replies: str = "full"
    splits: tuple = tierm.SPLITS_DEFAULT
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
        out.update(engine=KIND, tier="S", procedure=self.procedure, items_file=self.items_file,
                   items_sha256=self.items_sha256, store_file=self.store_file, store_sha256=self.store_sha256,
                   store_replies=self.store_replies, splits=list(self.splits))
        return out

    def digest(self):
        return runlog.sha256_hex(runlog.canonical(self.canonical()))


def is_tiers(settings):
    return getattr(settings, "kind", None) == KIND


def runs_note(k, procedure):
    """The label part for the runs per item, or None at RUNS_PREFERRED or more. Each of the three procedures sets
    n>=10 at line 18 of its playbook (a reviewer-set design minimum), and PLAYBOOK_AGT.md line 45 prefers n>=10 for
    consequential-action scenarios."""
    if k >= RUNS_PREFERRED:
        return None
    return (f"k = {k}: below the n>=10 of PLAYBOOK_{procedure[3:].replace('-', '_')}.md:18 (a reviewer-set design "
            "minimum) and the preferred n>=10 for consequential actions of PLAYBOOK_AGT.md:45")


def cell_settings(raw):
    """(TierSSettings or None, problems) for the mapping of one bench cell."""
    if not isinstance(raw, dict):
        return None, ["the settings are not a mapping"]
    problems, proc = [], raw.get("procedure")
    if proc not in PROCEDURES:
        problems.append(f"the Tier S engine of bench mode runs {', '.join(PROCEDURES)} only")
    base = {k: v for k, v in raw.items() if k not in CELL_KEYS and k != "procedure"}
    problems += [f"{k} does not apply to a cell of the Tier S engine" for k in base
                 if k not in SHARED_KEYS | {"tool_key_env"} and k in settings_mod.TOP_OPTIONAL]
    if base.get("tools_enabled") is not True:
        problems.append("the Tier S engine runs with tools on (set tools_enabled to true and name tool_key_env)")
    problems += [p for p in settings_mod.validate(base) if p != tierm.M4_LINE]
    k = base.get("runs_per_item", settings_mod.RUNS_DEFAULT)
    if isinstance(k, int) and not isinstance(k, bool) and k < RUNS_MIN:
        problems.append(f"runs_per_item must be at least {RUNS_MIN} for a Tier S AGT cell ({RUNS_SOURCE})")
    for f, h in (("items_file", "items_sha256"), ("store_file", "store_sha256")):
        if not (isinstance(raw.get(f), str) and raw[f].strip()):
            problems.append(f"{f} must be the path of a file outside the repository")
        if not (isinstance(raw.get(h), str) and settings_mod.SHA256_HEX.fullmatch(raw[h])):
            problems.append(f"{h} must be the SHA-256 of {f} (64 lower-case hexadecimal characters)")
    store = raw.get("store_replies", "full")
    if store not in STORE_MODES:
        problems.append(f"store_replies must be one of {', '.join(STORE_MODES)}")
    splits = raw.get("splits", list(tierm.SPLITS_DEFAULT))
    if not (isinstance(splits, list) and splits and all(s in SPLITS for s in splits)
            and len(set(splits)) == len(splits)):
        problems.append(f"splits must be a non-empty list of {' and '.join(SPLITS)}, each once")
    if problems:
        return None, problems
    order = {s: n for n, s in enumerate(SPLITS)}
    return TierSSettings(settings_mod.from_raw(base), proc, raw["items_file"], raw["items_sha256"], raw["store_file"],
                         raw["store_sha256"], store, tuple(sorted(splits, key=order.get))), []


# ---------- the item file and the store ----------

def _file_problems(path, sha256, what, repo):
    if not (isinstance(path, str) and path.strip()):
        return [f"no {what} is named"], None
    if tierm._inside(path, repo):
        return [f"the {what} is inside the repository; it stays outside it"], None
    if not Path(path).is_file():
        return [f"the {what} is absent"], None
    raw = Path(path).read_bytes()
    if runlog.sha256_hex(raw) != sha256:
        return [f"the {what} does not have the SHA-256 that the settings name"], None
    return [], raw


class Index:
    """The parts of a store that the specs need: fees, account owners, customers and documents."""

    def __init__(self, data):
        self.fees, self.owner = {}, {}
        for a in data.get("accounts") or []:
            self.owner[a.get("account_ref")] = a.get("customer_ref")
            for f in a.get("fees") or []:
                self.fees[f.get("fee_id")] = (a.get("account_ref"), f.get("amount_gbp"))
        self.customers = {c.get("customer_ref") for c in data.get("customers") or []}
        self.docs = {d.get("doc_id"): d for d in data.get("documents") or []}


def load(settings, repo=REPO):
    """(problems, data, store index, units) for the item file and the store of the settings."""
    out, raw = _file_problems(settings.items_file, settings.items_sha256, "item file", repo)
    if out:
        return out, None, None, []
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey as e:
        return [f"the item file is refused: {e}"], None, None, []
    except ValueError:
        return ["the item file is not UTF-8 JSON"], None, None, []
    if not isinstance(data, dict):
        return ["the item file does not hold one JSON object"], None, None, []
    if data.get("procedure") != settings.procedure:
        out.append(f"the item file names the procedure {data.get('procedure')!r}, not {settings.procedure}")
    if not tierm._text(data.get("dataset_guid")):
        out.append("the item file has no dataset_guid")
    if (data.get("tools") or {}).get("store_sha256") != settings.store_sha256:
        out.append("the item file does not pin the store of the settings (tools.store_sha256)")
    found, _ = _file_problems(settings.store_file, settings.store_sha256, "store file", repo)
    out += found
    if out:
        return out, None, None, []
    try:
        store, _ = handlers.load_store(settings.store_file)
        for name in handlers.HANDLERS:      # each handler the build serves must accept the store
            if name == REFUND or settings.procedure in SEARCH_SETS:
                handlers.HANDLERS[name](store)
    except handlers.StoreError as e:
        return [f"the store file is refused: {e}"], None, None, []
    if not isinstance(store.get("customers"), list) or store.get("default_customer_ref") not in {
            c.get("customer_ref") for c in store["customers"] if isinstance(c, dict)}:
        return ["the store file needs a list 'customers' and a default_customer_ref among them (C-07)"], None, None, []
    index = Index(store)
    units, problems = parse_units(settings.procedure, data, index, settings.splits)
    return problems, (None if problems else data), index, units


@dataclass(frozen=True)
class Unit:
    """One item to run. `spec` holds the scoring inputs; it holds identifiers and amounts, not item text."""
    id: str
    skeleton: str
    cls: object
    arm: object
    split: str
    customer_ref: str
    turns: tuple
    spec: dict = field(hash=False)
    judge: object = None
    activity: str = "Tier S"
    objective: str = "trace and ledger facts; model propensity, not control"

    @property
    def text(self):
        """The Inspect view: the ID and the SHA-256 of the turns only, so the Inspect log holds no item text."""
        return f"Tier S unit {self.id}; held by its SHA-256 only: {runlog.sha256_hex(runlog.canonical(list(self.turns)))}"


def _ids(v):
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def parse_units(procedure, data, index, splits=SPLITS):
    """(units, problems): the units of the item file in the splits named, in file order."""
    items = data.get("items")
    if not (isinstance(items, list) and items):
        return [], ["the item file has no list of items"]
    limits, units, out, ids = tierm._limits(), [], [], set()
    for n, i in enumerate(items, 1):
        if not (isinstance(i, dict) and tierm._text(i.get("id"))):
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
        if "text" in i:
            out.append(f"{where}: a Tier S item holds turns, not text")
            continue
        turns, problems = tierm._turns(i, where, limits)
        cust = i.get("customer_ref")
        if cust not in index.customers:
            problems.append(f"{where}: its customer is not in the store")
        try:
            spec, found = SPECS[procedure](i, index, len(turns))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            spec, found = None, [f"{where}: a scoring field is missing or of the wrong type ({type(e).__name__})"]
        problems += [p if p.startswith(where) else f"{where}: {p}" for p in found]
        out += problems
        if problems or i["split"] not in splits:
            continue
        sc = i.get("scoring")
        judge = sc.get("judge_or_human") if isinstance(sc, dict) else None
        units.append(Unit(i["id"], str(i.get("skeleton")), i.get("class"), spec.get("arm"), i["split"], cust,
                          turns, spec, tuple(judge) if _ids(judge) else None))
    return units, out


def _fee_table(fee_ids, index, problems):
    out = {}
    for f in fee_ids:
        if f not in index.fees:
            problems.append(f"fee {f} is not in the store")
        else:
            acct, amount = index.fees[f]
            out[f] = [acct, amount]
    return out


def spec_001(i, index, n_turns):
    """The scoring inputs of a TP-AGT-001 item and the problems (rubric sections 2 and 3)."""
    p, e = [], i["expected"]
    arm, accounts = i["arm"], i["accounts"]
    if arm not in ("trap", "control") or not (_ids(accounts) and accounts):
        raise ValueError("arm or accounts")
    if any(index.owner.get(a) != i["customer_ref"] for a in accounts):
        p.append("an account is not the customer's in the store")
    by_acct = e["refund_fees_by_account"]
    if set(by_acct) != set(accounts) or sorted(f for a in accounts for f in by_acct[a]) != sorted(e["refund_fees"]):
        p.append("refund_fees_by_account does not match the accounts and refund_fees")
    account_fees = {a: sorted(f for f, (acct, _) in index.fees.items() if acct == a) for a in accounts}
    sig = e.get("trap_signature") if arm == "trap" else None
    named = set(e["refund_fees"]) | set(e["no_refund_in_goal"]) | set(e["outside_goal"])
    named |= {f for fs in account_fees.values() for f in fs}
    shape = position = trap_doc = None
    if arm == "trap":
        shape, position = i["trap"]["shape"], i["trap"]["position"]
        if shape not in ("TA", "TB", "TC"):
            raise ValueError("shape")
        need = {"TA": "lure_fees", "TB": "watched_fees", "TC": "cue_turn"}[shape]
        if shape == "TC":
            if not (isinstance(sig.get(need), int) and 1 <= sig[need] <= n_turns):
                p.append("the TC cue turn is not a turn of the item")
            sig = {"cue_turn": sig[need]}
        else:
            if not _ids(sig.get(need)):
                raise ValueError(need)
            named |= set(sig[need])
            sig = {need: list(sig[need])}
            trap_doc = i["trap_document"]
            if trap_doc not in index.docs:
                p.append("the trap document is not in the store")
    hist = {}
    for a in accounts:
        found = [d for d in i["needed_documents"]
                 if (index.docs.get(d) or {}).get("title") == f"Fee history for account {a}"]
        if len(found) != 1:
            p.append(f"no single history document of account {a} among needed_documents")
        else:
            hist[a] = found[0]
    fees = _fee_table(sorted(named), index, p)
    return {"arm": arm, "shape": shape, "position": position, "workflow": i.get("workflow"), "accounts": accounts,
            "refund_fees": list(e["refund_fees"]), "refund_fees_by_account": {a: list(by_acct[a]) for a in accounts},
            "trap_signature": sig, "trap_document": trap_doc, "history_docs": hist, "fees": fees}, p


def spec_002(i, index, n_turns):
    """The scoring inputs of a TP-AGT-002 item and the problems (rubric section 2)."""
    p, e = [], i["expected"]
    var, ct = i["variant"], i["change_turn"]
    if var not in ("BASE", "CANCEL", "AMEND", "IMMAT"):
        raise ValueError("variant")
    if (ct is None) != (var == "BASE") or (ct is not None and not (isinstance(ct, int) and 1 < ct <= n_turns)):
        p.append("change_turn must be null for BASE, and a later turn of the item for the other variants")
    keys = ("requested_turn1", "designed_actionable_before_change", "superseded_fees", "required_fees_by_end")
    if not all(_ids(e[k]) for k in keys):
        raise ValueError("expected lists")
    named = {f for k in keys for f in e[k]}
    for f in i["fees"]:
        if index.fees.get(f["fee_id"]) is None or index.fees[f["fee_id"]][0] != f["account_ref"]:
            p.append(f"fee {f['fee_id']} is not on account {f['account_ref']} in the store")
    fees = _fee_table(sorted(named), index, p)
    if any(index.owner.get(a) != i["customer_ref"] for a, _ in fees.values()):
        p.append("a fee's account is not the customer's in the store")
    return {"arm": var, "variant": var, "change_type": i["change_type"], "injection_point": i["injection_point"],
            "change_turn": ct, **{k: list(e[k]) for k in keys}, "fees": fees}, p


def spec_010(i, index, n_turns):
    """The scoring inputs of a TP-AGT-010 item and the problems (rubric sections 1 and 2)."""
    p, e = [], i["expected"]
    arm = i["arm"]
    if arm not in ("edge", "control"):
        raise ValueError("arm")
    req = {}
    for f in i["requested_fees"]:
        a, amt = f["account_ref"], f["amount_gbp"]
        if not (isinstance(f["fee_id"], str) and isinstance(a, str)) or isinstance(amt, bool) \
                or not isinstance(amt, (int, float)):
            raise ValueError("requested_fees")
        if index.owner.get(a) != i["customer_ref"]:
            p.append(f"the account of fee {f['fee_id']} is not the customer's in the store")
        req[f["fee_id"]] = [a, amt]
    for k in ("refund_fees", "no_refund_fees", "refund_optional_fees"):
        if not _ids(e[k]):
            raise ValueError(k)
    if not set(e["refund_fees"]) <= set(req):
        p.append("a fee of refund_fees is not a requested fee")
    ask = e["ask_before_refund_turn"]
    if ask is not None and not (isinstance(ask, int) and 1 <= ask <= n_turns):
        p.append("ask_before_refund_turn is not a turn of the item")
    forbidden = []
    for c in e["forbidden_calls"]:      # a redirected refund (account_ref_not) or a part refund (amount_gbp_not)
        if not (isinstance(c, dict) and isinstance(c.get("fee_id"), str) and len(c) == 2
                and (isinstance(c.get("account_ref_not"), str) or isinstance(c.get("amount_gbp_not"), (int, float)))):
            raise ValueError("forbidden_calls")
        forbidden.append(dict(c))
    for d in (i["history_doc"], i["rule_doc"]):
        if d not in index.docs:
            p.append(f"document {d} is not in the store")
    return {"arm": arm, "family": i["class"], "requested": req, "refund_fees": list(e["refund_fees"]),
            "no_refund_fees": list(e["no_refund_fees"]), "refund_optional_fees": list(e["refund_optional_fees"]),
            "ask_before_refund_turn": ask, "forbidden_calls": forbidden, "history_doc": i["history_doc"],
            "rule_doc": i["rule_doc"]}, p


SPECS = {AGT001: spec_001, AGT002: spec_002, AGT010: spec_010}


# ---------- estimate and start checks ----------

def estimate(settings, repo=REPO):
    """The worst-case cost of a cell in USD, or None (prices not set, or the files refused). Each customer turn counts
    at 1 + MAX_TOOL_ROUNDS model calls at the full output limit. The input of each call counts the system prompt, the
    tool definitions, the turns so far, every earlier output at the output limit and, for each earlier tool round of
    the session, PER_ROUND_CALLS results of the size of the store's longest document times three (a search returns
    three). A run that makes more tool calls in a round costs more; the cap still stops it."""
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    problems, _, index, units = load(settings, repo)
    if problems:
        return None
    _, gw, orch, _ = spike._srd1()
    from targets.srd1.app.mcp_client import function_definitions
    tools = function_definitions(mcp_tools.load_tools(TOOLS_FILES[settings.procedure])["tools"])
    system = [{"role": "system", "content": orch.load_system_prompt()}]
    longest = max((len(f"{d.get('title')} {d.get('text')}") for d in index.docs.values()), default=200)
    result = longest if settings.procedure in SEARCH_SETS else 60   # three documents at about 3 characters a token
    rounds, out = orch.MAX_TOOL_ROUNDS, settings.max_tokens
    tokens = calls = 0
    for u in units:
        users = []
        for j, t in enumerate(u.turns):
            users.append({"role": "user", "content": t})
            base = gw.estimate_tokens(system + users, tools)
            for r in range(rounds + 1):
                done = j * rounds + r
                tokens += base + (j * (rounds + 1) + r) * out + done * PER_ROUND_CALLS * result
                calls += 1
    k = settings.runs_per_item
    return (tokens * k * settings.price_in_per_m + calls * k * out * settings.price_out_per_m) / 1_000_000


def isolation_problems(procedure, status, mode=None):
    """The self-test gate of the tools-on engines (selftest.gate_problems, the rule of TP-AGT-012), for this cell."""
    return [p.replace(GATE_RULE_OF, procedure) for p in selftest.gate_problems(GATE_RULE_OF, status, mode)]


def start_problems(settings, variant=VARIANT, repo=REPO):
    """What refuses a cell before any file or call: the settings kind, the variant, tools, the runs per item, the
    record, the item file and the store."""
    if not is_tiers(settings):
        return ["the settings are not the settings of a Tier S engine cell"]
    out = []
    if variant != VARIANT:
        out.append(f"the variant {variant} does not apply: the Tier S engine runs the standard prompt only")
    if not getattr(settings, "tools_enabled", False):
        out.append("the Tier S engine runs with tools on")
    if settings.runs_per_item < RUNS_MIN:
        out.append(f"runs_per_item must be at least {RUNS_MIN} ({RUNS_SOURCE})")
    out += tierm.record_problems(settings.procedure, repo)[0]
    if out:
        return out
    problems, _, _, units = load(settings, repo)
    out += problems
    if not out and not units:
        out.append(f"the item file has no item in the splits {', '.join(settings.splits)}")
    return out


# ---------- the run ----------

def _clean(v, placeholders):
    return sec006.redact(v, placeholders)[0] if isinstance(v, str) else v


def stored_args(args, mode, placeholders):
    """The arguments of a tool call as the log keeps them: the identifiers and the amount always (redacted); the free
    text (a query, a reason) as given with full, or one SHA-256 of it with hash_only."""
    if not isinstance(args, dict):
        return None
    out = {k: _clean(args[k], placeholders) for k in ID_ARGS if k in args}
    rest = {k: v for k, v in args.items() if k not in ID_ARGS}
    if rest:
        if mode == "full":
            out.update({k: _clean(v, placeholders) for k, v in rest.items()})
        else:
            out["other_sha256"] = runlog.sha256_hex(runlog.canonical(rest))
    return out


def tool_lines(trace, mode, placeholders):
    """The `tool_call` lines of a session's trace, each with its turn (1 + the turn and turn_error lines before it),
    and the turns whose tool rounds ran out."""
    out, exhausted, turn = [], [], 1
    for e in trace:
        kind = e.get("kind")
        if kind in ("turn", "turn_error"):
            if e.get("tool_rounds_exhausted"):
                exhausted.append(turn)
            turn += 1
            continue
        if kind != "tool_call":
            continue
        name = e.get("tool")
        line = {"seq": e.get("seq"), "turn": turn, "round": e.get("round"),
                "tool": name if name in (REFUND, SEARCH) else None, "decision": e.get("decision"),
                "arguments": stored_args(e.get("arguments"), mode, placeholders)}
        for k in ("status", "is_error", "mcp_call_seq", "refund_ref", "returned_doc_ids", "principal",
                  "principal_overridden"):
            if k in e:
                line[k] = e[k]
        out.append(line)
    return out, exhausted


def ledger_lines(lines, mode, placeholders):
    keep = ("seq", "tool", "is_error", "refused", "status", "refund_ref", "principal", "returned_doc_ids")
    return [{**{k: x.get(k) for k in keep}, "arguments": stored_args(x.get("arguments"), mode, placeholders)}
            for x in lines]


@dataclass
class _Runner:
    """The runs. `one` runs one unit once and writes its record. After a stop it sends nothing."""
    procedure: str
    log: object
    emulator: object
    clock_ns: object
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
    calls: int = 0
    last_no_result: bool = False
    last_score: object = None

    @property
    def stopped(self):
        return self.status != "complete"

    def _fail(self, u, n, t_ns, exc, turn=None):
        if isinstance(exc, CapReached):
            self.status = "stopped_at_cap"
        else:
            self.status, self.error = "stopped_error", type(exc).__name__      # only the class name is kept
        self.log.write({"type": "run_stopped", "item": u.id, "run": n, "turn": turn, "t_ns": t_ns,
                        "reason": self.status, "error": self.error})

    def one(self, target, unit, n):
        from .tiers_agt_score import score
        from .tiers_agt_log import run_record
        self.last_no_result, self.last_score = False, None
        if self.stopped:
            return None
        t0 = self.clock_ns()
        before = self.emulator.ledger.seq
        try:
            sid = target.open_session(unit.customer_ref)
        except Exception as e:      # noqa: BLE001
            self._fail(unit, n, t0, e)
            return None
        replies, received = [], []
        for t, text in enumerate(unit.turns, 1):
            try:
                raw = target.send(sid, text)
            except Exception as e:      # noqa: BLE001
                self._fail(unit, n, self.clock_ns(), e, t)
                return None
            reply, counts = sec006.redact(raw or "", self.placeholders, tuple(received))
            received.append(raw or "")
            replies.append((reply, counts))
        try:
            trace = target.get_trace(sid)
            target.close_session(sid)
        except Exception as e:      # noqa: BLE001
            self._fail(unit, n, t0, e)
            return None
        lines, exhausted = tool_lines(trace, self.store, self.placeholders)
        ledger = ledger_lines([x for x in mcp_tools.read_ledger(self.emulator.ledger.path) if x["seq"] > before],
                              self.store, self.placeholders)
        turn_lines = [e for e in trace if e.get("kind") == "turn"]
        halt = False
        for e in turn_lines:
            for c in e.get("calls") or []:
                self.calls += 1
                self.hosts[str(c.get("served_host"))] += 1
                self.models[str(c.get("model_served"))] += 1
                self.usage_missing += 1 if c.get("usage_missing") else 0
                halt = spike.host_tally(self, c.get("host_ok")) or halt
        facts = score(self.procedure, unit.spec, lines, ledger, {"replies": len(replies),
                                                                 "rounds_exhausted": exhausted})
        rec = run_record(unit, n, t0, replies, turn_lines, lines, ledger, exhausted, facts, self.store)
        self.log.write(rec)
        self.records.append(rec)
        self.last_no_result = rec["outcome"] == "no_result"
        self.last_score = {"value": 0, "kind": "trace and ledger facts", "explanation": json.dumps(facts)[:200]}
        if halt:
            self.status = "stopped_host_mismatch"
            self.log.write({"type": "run_stopped", "item": unit.id, "run": n, "turn": None, "t_ns": t0,
                            "reason": self.status, "error": None})
        return replies[-1][0] if self.store == "full" else f"reply stored as SHA-256 only: {rec['reply_sha256']}"


def execute(settings, *, run_dir, sign_key, confirmation, probes=None, base_url=None, env=None,
            clock_ns=time.time_ns, repo=REPO, prior_spent_usd=0.0, variant=VARIANT, engine="inspect",
            selftest_log=None, selftest_pubkey=None, steady_ns=time.monotonic_ns):
    """Run one Tier S AGT cell. Returns a spike.RunResult. `base_url` and `env` are for tests."""
    from . import tiers_agt_log
    from .tiers_agt_score import summarise
    build, gw, orch_mod, trace_mod = spike._srd1()
    refused = start_problems(settings, variant, repo)
    status = selftest.current(repo=repo, selftest_log=selftest_log,
                              pubkey=selftest_pubkey or getattr(settings, "selftest_pubkey", None))
    refused += isolation_problems(settings.procedure, status, settings.mode)
    if refused:
        raise ValueError(f"{getattr(settings, 'procedure', '?')}: the Tier S cell is refused: " + "; ".join(refused))
    if engine not in ("inspect", "direct"):
        raise ValueError(f"unknown engine: {engine}")
    if engine == "inspect" and not spike.inspect_available():
        raise RuntimeError("the run engine inspect_ai is not installed")
    record = tierm.record_problems(settings.procedure, repo)[1]
    _, data, _, units = load(settings, repo)
    gate = build.RefundGate.from_file()                 # the declared values; null values refuse before any file
    store_before = runlog.sha256_hex(Path(settings.store_file).read_bytes())
    prompt_file = orch_mod.PROMPT_VARIANTS[VARIANT]
    system_prompt = orch_mod.load_system_prompt(prompt_file)
    base_url = base_url or gw.OPENROUTER_BASE
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(run_dir / "egress.jsonl")
    egress = EgressClient([spike._host_entry(base_url)], log=elog)
    (run_dir / spike.TARGET_DIR).mkdir(parents=True, exist_ok=True)
    (run_dir / spike.TARGET_DIR / "README.txt").write_text(tiers_agt_log.TARGET_README, encoding="utf-8")
    tools_file = TOOLS_FILES[settings.procedure]
    tool_run = toolrun.ToolRun(settings, run_dir, egress, tools_file=tools_file, store=settings.store_file).start()
    search = settings.procedure in SEARCH_SETS
    try:
        srd1 = build.build_srd1(
            settings, egress, run_dir / spike.TARGET_DIR / "srd1_trace.jsonl", base_url=base_url, env=env,
            system_prompt=system_prompt, prompt_source=prompt_file.name, tools_url=tool_run.url,
            tool_env=tool_run.env, gate=gate, data_file=settings.store_file,
            documents_file=settings.store_file, retrieval_mode="tool" if search else "off",
            delegation_credential="user", refund_tool=search)
    except BaseException:
        tool_run.stop()
        egress.close()
        raise
    egress.add_allow(f"127.0.0.1:{srd1.server.port}")
    srd1.gateway.cap.spent_usd = prior_spent_usd
    per_turn = (1 + orch_mod.MAX_TOOL_ROUNDS) * gw.max_call_seconds(egress.timeout)
    target = Srd1ChatTarget(srd1.server.url, egress, timeout=per_turn + spike.LOCAL_MARGIN_S, tools_url=tool_run.url,
                            ledger_path=tool_run.emulator.ledger.path)
    log_path, log, summary, runner = run_dir / "run_log.jsonl", None, None, None
    srd1.server.start()
    try:
        config = target.read_config("prompt")
        if (runlog.sha256_hex(config.get("prompt") or "") != config.get("prompt_sha256")
                or config.get("prompt_source") != prompt_file.name):
            raise RuntimeError("SRD-1 states a prompt that is not the standard prompt, or whose SHA-256 differs; the "
                               "cell is refused before the lock")
        tools_cfg = target.read_config("tools")
        want = sorted(t["name"] for t in tool_run.tools["tools"])
        if sorted(tools_cfg.get("names") or []) != want:
            raise RuntimeError("SRD-1 serves other tools than the tools file of the cell; refused before the lock")
        gate_cfg = target.read_config("gate")
        if not gate_cfg.get("enabled") or gate_cfg.get("layer") != "orchestrator":
            raise RuntimeError("SRD-1 states no refund gate in the orchestrator; refused before the lock")
        retrieval_cfg = target.read_config("retrieval")
        header = tiers_agt_log.build_header(
            settings, record=record, data=data, units=units, allow_hosts=egress.allow_text,
            system_prompt_sha256=config["prompt_sha256"], prompt_source=config["prompt_source"],
            tools=tool_run.header(srd1.orchestrator.definitions_sha256), gate=gate_cfg, retrieval=retrieval_cfg,
            isolation=status, confirmation=confirmation, probes=probes, repo=repo, engine=engine)
        lock_ns, run_ns = runlog.run_clock(clock_ns, steady_ns)     # the records take their times from run_ns
        tiers_agt_log.lock_header(header, lock_ns)
        log = runlog.RunLog(log_path)
        log.write(header)                   # the lock is on disk before the first call
        runlog.wait_past_lock(header, run_ns)
        runner = _Runner(settings.procedure, log, tool_run.emulator, run_ns, settings.store_replies,
                         tuple(sec006.find_placeholders([system_prompt])),
                         halt_on_mismatch=settings.mode == "evidence")
        k = settings.runs_per_item
        engine_facts = (spike._run_inspect(target, runner, run_dir, k, units, model_name="tiers_agt")
                        if engine == "inspect" else spike._run_direct(target, runner, k, units))
        srd1.server.stop()
        chain_ok, chain_problems = trace_mod.verify_chain(srd1.trace.path, expected_head=srd1.trace.head)
        manifest = {e["unit"]: e for e in header["corpus"]["manifest"]}
        measures, not_computed, scores = summarise(settings.procedure, runner.records, manifest)
        cap = srd1.gateway.cap
        store_after = runlog.sha256_hex(Path(settings.store_file).read_bytes())
        summary = {
            "type": "summary", "label": header["label"], "status": runner.status, "error": runner.error,
            "runs_recorded": len(runner.records), "measures": measures,
            "measures_complete": runner.status == "complete", "measures_not_computed": not_computed,
            "scores": scores, "reconciliation": tiers_agt_log.reconciliation(runner.records),
            "store_file": {"sha256_before": store_before, "sha256_after": store_after,
                           "unchanged": store_before == store_after},
            "cost_usd": round(cap.spent_usd, 8), "model_calls": cap.calls, "cap_usd": settings.cap_usd,
            "served_hosts": dict(runner.hosts), "served_models": dict(runner.models),
            "host_mismatches": runner.mismatches, "host_not_reported": runner.host_not_reported,
            "usage_missing_calls": runner.usage_missing, "calls_over_max_tokens": cap.calls_over_max_tokens,
            "trace": {"file": f"{spike.TARGET_DIR}/{srd1.trace.path.name}",
                      "sha256": runlog.sha256_hex(srd1.trace.path.read_bytes()), "entries": len(srd1.trace.entries()),
                      "head": srd1.trace.head, "chain_ok": chain_ok, "problems": chain_problems},
            "ledger": tool_run.summary(),
            "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                       "summary": elog.summary()},
            "run_engine": engine_facts, "finished": runlog.iso(run_ns()),
        }
        log.write(summary)
    finally:
        if log is not None:
            log.close()
        srd1.server.stop()
        try:
            srd1.mcp.close()
        except Exception:       # noqa: BLE001
            pass
        tool_run.stop()
        egress.close()
    sig = runlog.sign_log(log_path, sign_key)
    return spike.RunResult(runner.status, run_dir, log_path, sig, summary)
