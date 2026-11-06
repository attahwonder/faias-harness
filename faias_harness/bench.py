"""Bench mode: run a list of procedures against a list of models on SRD-1 minimal, and write one signed bench report.

  py -3.14 -X utf8 -m faias_harness.bench --bench <bench.yaml> --profile <profile.yaml> --out <new folder>
  py -3.14 -X utf8 -m faias_harness.bench --bench <bench.yaml> --profile <profile.yaml> --out <new folder> --start \
      --sign-key <private key file> [--selftest-log <log> --selftest-pubkey <file>]
  py -3.14 -X utf8 -m faias_harness.bench verify --report <folder>/bench_report.jsonl [--pubkey <file>] [--no-repo]

A bench file (`kind: bench`; example: targets/examples/bench.yaml) holds the settings that every cell shares (mode,
bench cap, key variable, max_tokens, temperature, seed, runs per item), a list of models (each with its model ID, its
prices and its routing block) and a list of procedures (each with its tier and the settings of that procedure). A cell
is one procedure, one tier and one model. For each cell the runner builds the mapping of an SRD-1 settings file and
calls the settings validator (settings.validate, settings.from_raw), so each cell has one validated Settings and its own
settings hash. The cap of every cell is the bench cap.

Tiers. Tier M is SRD-1 minimal with its standard system prompt, tools and retrieval off: one model call per customer
turn. It takes TP-SEC-001, TP-SEC-006 and TP-SEC-002, with tools_enabled false, and any procedure with no engine of
its own, through the generic Tier M engine (engine/tierm.py: an item file outside the repository, pinned by its
SHA-256; item outcomes only, no measure; a procedure with no record is refused). Tier S is the procedure's own build
of SRD-1 (its tools, its retrieval or its prompt variant), as the engine of that procedure runs it. Each cell runs the
default variant of its procedure (standard; model_probe for TP-MOD-011). The bench runs no positive control.

Order. For each model, in the order of the file, the runner runs the TP-SEC-001 cells first, then the other cells in
the order of the file. A TP-SEC-006 or TP-SEC-002 cell takes the log of the TP-SEC-001 cell of the same model and tier
as its baseline (m4_baseline_log, or baseline_log), with the public key of the run-signing key, which the runner writes
to the bench folder. A bench with such a cell and no TP-SEC-001 cell of the same model and tier is refused. A TP-SEC-002
cell needs a TP-SEC-001 cell on the full set (TP-SEC-001), because its pool names those items by ID. A TP-SEC-006 cell
does not take a TP-SEC-001 cell on the private set, because its label would call that baseline a spike set.

Start. Without --start the runner prints the matrix and the worst-case cost of each cell and of the bench, and sends
nothing. With --start it checks the start conditions of each cell (those of the preflight), makes one probe pair for
each model (two model calls through the gateway; their cost counts against the bench cap), prints the probe results,
the matrix and the worst-case cost again, and asks for one typed `yes`. With no `yes`, nothing runs. The confirmation
names the bench and its settings hash, and the engine copies it into the header of each cell's log
(`owner_confirmation`), with the probe results of the cell's model.

Run. The runner writes the header of the bench report, with its lock, before the first cell. It calls the engine of each
cell through the dispatch of the preflight, with one run folder for each cell in the bench folder. It passes the cost
spent so far as prior_spent_usd, so the gateway of each cell stops before a call that could pass the bench cap. A cell
that does not end with the status complete stops the bench: the later cells send nothing, and the report gives them the
status "not run". Then it writes the summary and signs the report (engine/benchreport.py).

Folders. The bench folder must be outside the repository, and new or empty. The private item set of TP-SEC-001
(settings.PRIVATE_ITEM_SET) runs only here, so its run folders are outside the repository.

The runner opens no socket itself. The probe gateway and the engines make the model calls. It is tested against local
stand-ins only, end to end for TP-SEC-001 and TP-SEC-006. The other procedures go through the same dispatch and the same
start checks as the preflight, with no end-to-end bench test.
"""
import argparse
import copy
import dataclasses
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization

from . import preflight, selftest, yamlio
from .engine import benchreport, runlog, sec001, spike, tierm
from .profile import REPO, ProfileError, load_profile
from .settings import (AGT012, DAT009, MOD011, PRIVATE_ITEM_SET, PROCEDURE_DEFAULT, PROCEDURES, SEC002, SEC003,
                       SEC006, SettingsError, from_raw, validate)

SEC001 = PROCEDURE_DEFAULT
KIND = "bench"
TOP_REQUIRED = ("kind", "mode", "cap_usd", "api_key_env", "models", "procedures")
TOP_OPTIONAL = ("max_tokens", "temperature", "seed", "runs_per_item", "selftest_pubkey")
MODEL_KEYS = ("model_id", "price_in_per_m", "price_out_per_m", "routing")
PROC_REQUIRED, PROC_OPTIONAL = ("procedure", "tier"), ("settings",)
TIERS = {"M": "SRD-1 minimal with its standard system prompt, tools and retrieval off; one model call per customer turn",
         "S": "the procedure's own build of SRD-1 (its tools, retrieval or prompt variant), as its engine runs it"}
TIER_M_PROCEDURES = (SEC001, SEC006, SEC002)
# The keys that the bench file sets for every cell, or that the runner sets. A procedure's settings may not hold them.
BENCH_OWNED = {"kind", "provider", "model_id", "mode", "routing", "price_in_per_m", "price_out_per_m", "cap_usd",
               "api_key_env", "procedure", "m4_baseline_log", "m4_baseline_pubkey", "baseline_log", "baseline_pubkey"}
DEPENDENT = {SEC006: ("m4_baseline_log", "m4_baseline_pubkey"), SEC002: ("baseline_log", "baseline_pubkey")}
PUBKEY_NAME = "bench_signing_key.pub.pem"
NOT_RUN_REASON = "a cell before it stopped the bench"
YES_SCOPE = "bench: one typed yes for every cell of the matrix that was shown"


@dataclass
class Cell:
    n: int
    model_index: int
    model_id: object
    procedure: str
    tier: str
    settings: object
    run_dir: Path
    baseline_cell: object = None

    @property
    def variant(self):
        return "model_probe" if self.procedure == MOD011 else "standard"

    @property
    def name(self):
        return f"cell {self.n} ({self.procedure}, tier {self.tier}, {self.model_id})"

    def entry(self, out):
        """The cell entry of the report header."""
        e = {"cell": self.n, "model": self.model_index + 1, "model_id": self.model_id, "procedure": self.procedure,
             "tier": self.tier, "variant": self.variant, "item_set": self.settings.item_set,
             "runs_per_item": self.settings.runs_per_item, "run_dir": self.run_dir.relative_to(out).as_posix(),
             "settings_hash": self.settings.digest(), "baseline_cell": self.baseline_cell}
        if self.settings.item_set == PRIVATE_ITEM_SET:
            e["private_items_sha256"] = self.settings.private_items_sha256
        return e


def _inside(path, repo=REPO):
    p, r = Path(path).resolve(), Path(repo).resolve()
    return p == r or r in p.parents


def bench_digest(raw):
    """The bench settings hash: the SHA-256 of the canonical JSON of the bench file as loaded."""
    return runlog.sha256_hex(json.dumps(raw, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str))


def load_bench(path):
    """The bench file as a mapping. A SettingsError when it cannot be read."""
    try:
        return yamlio.load(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SettingsError([f"cannot read {Path(path).name}: {type(e).__name__}"]) from None
    except Exception as e:      # noqa: BLE001 - a YAML error; only the class name is kept
        raise SettingsError([f"cannot read {Path(path).name}: {type(e).__name__}"]) from None


def form_problems(raw):
    """The problems in the form of a bench file, before any cell is built (empty if none)."""
    if not isinstance(raw, dict):
        return ["the bench file is not a mapping"]
    out = [f"missing field '{k}'" for k in TOP_REQUIRED if k not in raw]
    out += [f"unknown field '{k}'" for k in raw if k not in TOP_REQUIRED + TOP_OPTIONAL]
    if raw.get("kind") != KIND:
        out.append("kind must be 'bench'")
    models, procs = raw.get("models"), raw.get("procedures")
    if not (isinstance(models, list) and models and all(isinstance(m, dict) for m in models)):
        out.append("models must be a non-empty list of mappings")
        models = []
    if not (isinstance(procs, list) and procs and all(isinstance(p, dict) for p in procs)):
        out.append("procedures must be a non-empty list of mappings")
        procs = []
    for i, m in enumerate(models, 1):
        out += [f"model {i}: missing field '{k}'" for k in MODEL_KEYS if k not in m]
        out += [f"model {i}: unknown field '{k}'" for k in m if k not in MODEL_KEYS]
    ids = [m.get("model_id") for m in models if m.get("model_id") is not None]
    if len(set(map(str, ids))) != len(ids):
        out.append("models: a model ID is named more than once")
    seen = set()
    for i, p in enumerate(procs, 1):
        out += [f"procedure {i}: missing field '{k}'" for k in PROC_REQUIRED if k not in p]
        out += [f"procedure {i}: unknown field '{k}'" for k in p if k not in PROC_REQUIRED + PROC_OPTIONAL]
        tier, proc, st = p.get("tier"), p.get("procedure"), p.get("settings", {})
        if tier not in TIERS:
            out.append(f"procedure {i}: tier must be M or S")
        if (proc, tier) in seen:
            out.append(f"procedure {i}: {proc} with tier {tier} is named more than once")
        seen.add((proc, tier))
        if not isinstance(st, dict):
            out.append(f"procedure {i}: settings must be a mapping")
            continue
        out += [f"procedure {i}: settings may not hold '{k}' (the bench file or the runner sets it)"
                for k in sorted(set(st) & BENCH_OWNED)]
        if tier == "M" and proc not in TIER_M_PROCEDURES and proc in PROCEDURES:
            out.append(f"procedure {i}: tier M takes {', '.join(TIER_M_PROCEDURES)} only ({proc} runs its own build "
                       "of SRD-1, so it is tier S)")
        if tier == "S" and proc not in PROCEDURES:
            out.append(f"procedure {i}: {proc} has no engine of its own; the Tier M engine (tierm.py) runs it at tier "
                       "M only")
        if tier == "M" and st.get("tools_enabled") is True:
            out.append(f"procedure {i}: tier M runs with tools off (set tools_enabled to false, or use tier S)")
    return out


def plan(raw, out):
    """The cells of the bench, in the order of the run, and the problems (empty if none). `out` is the bench folder.
    Each cell's settings come from settings.validate and settings.from_raw."""
    problems = form_problems(raw)
    if problems:
        return [], problems
    out = Path(out).resolve()
    common = {k: raw[k] for k in ("mode", "cap_usd", "api_key_env") + TOP_OPTIONAL if k in raw}
    cells, n = [], 0
    for mi, model in enumerate(raw["models"]):
        entries = sorted(raw["procedures"], key=lambda p: p["procedure"] != SEC001)     # stable: TP-SEC-001 first
        for p in entries:
            n += 1
            cell_raw = {"provider": "openrouter", **copy.deepcopy(common), "model_id": model["model_id"],
                        "routing": copy.deepcopy(model["routing"]), "price_in_per_m": model["price_in_per_m"],
                        "price_out_per_m": model["price_out_per_m"], "procedure": p["procedure"],
                        **copy.deepcopy(p.get("settings") or {})}
            run_dir = out / f"{n:02d}_m{mi + 1}_{p['procedure']}_{p['tier']}"
            where = f"cell {n} ({p['procedure']}, tier {p['tier']}, {model['model_id']})"
            base = None
            if p["procedure"] in DEPENDENT:
                base = next((c for c in cells if c.model_index == mi and c.procedure == SEC001 and c.tier == p["tier"]),
                            None)
                if base is None:
                    problems.append(f"{where}: no TP-SEC-001 cell of the same model and tier runs before it, so it has "
                                    "no baseline log")
                    continue
                if p["procedure"] == SEC002 and base.settings.item_set != "TP-SEC-001":
                    problems.append(f"{where}: its baseline, cell {base.n}, is not on the full set TP-SEC-001; the "
                                    "TP-SEC-002 pool names those items by ID")
                if p["procedure"] == SEC006 and base.settings.item_set == PRIVATE_ITEM_SET:
                    problems.append(f"{where}: its baseline, cell {base.n}, is on the private set; the TP-SEC-006 label "
                                    "would call it a spike set (an open question for the owner)")
                log_key, key_key = DEPENDENT[p["procedure"]]
                cell_raw[log_key] = str(base.run_dir / "run_log.jsonl")
                cell_raw[key_key] = str(out / PUBKEY_NAME)
            if p["procedure"] not in PROCEDURES:        # no engine of its own: the generic Tier M engine
                s, found = tierm.cell_settings(cell_raw)
                problems += [f"{where}: {x}" for x in found]
                if s is not None:
                    cells.append(Cell(n, mi, model["model_id"], p["procedure"], p["tier"], s, run_dir))
                continue
            found = validate(cell_raw)
            if found:
                problems += [f"{where}: {x}" for x in found]
                continue
            cells.append(Cell(n, mi, model["model_id"], p["procedure"], p["tier"], from_raw(cell_raw), run_dir,
                              base.n if base else None))
    return cells, problems + order_problems(cells)


def order_problems(cells):
    """A dependent cell (TP-SEC-006, TP-SEC-002) must come after the TP-SEC-001 cell that gives its baseline."""
    pos = {c.n: i for i, c in enumerate(cells)}
    return [f"{c.name}: it runs before its baseline, cell {c.baseline_cell}" for i, c in enumerate(cells)
            if c.procedure in DEPENDENT and (c.baseline_cell not in pos or pos[c.baseline_cell] > i)]


def estimate(settings):
    """The worst-case cost of one cell, in USD, or None: the estimate of the preflight for its procedure."""
    from .engine import run_agt012, run_dat009, run_sec002, run_sec006
    proc = settings.procedure
    if settings.price_in_per_m is None or settings.price_out_per_m is None:
        return None
    if tierm.is_tierm(settings):
        return tierm.estimate(settings)
    if proc == SEC006:
        return run_sec006.estimate(settings)
    if proc == AGT012:
        return run_agt012.estimate(settings)
    if proc == DAT009:
        return run_dat009.estimate(settings)
    if proc == SEC002:
        return run_sec002.estimate(settings) if preflight._sec002_no_estimate() is None else None
    if proc == SEC003:
        return preflight.sec003_estimate(settings)
    if proc == MOD011:
        return preflight.mod011_estimate(settings)
    if settings.item_set == PRIVATE_ITEM_SET and private_problems(settings):
        return None
    return spike.estimate(settings)


def private_problems(settings):
    return sec001.private_problems(settings.private_items_file, settings.private_items_sha256, REPO)[0]


def cell_problems(profile, cell, sign_key, isolation, inspect_check, baseline_ready=False):
    """The start conditions of one cell, as the preflight checks them for one run. Before the TP-SEC-001 cell has run,
    the baseline log of a dependent cell does not exist yet, so with baseline_ready false its baseline is not read;
    the runner checks the cell again just before it runs (baseline_ready true)."""
    s, v = cell.settings, cell.variant
    problems = preflight.start_problems(profile, s)
    problems += runlog.key_problems(sign_key) if sign_key else ["no signing key given (--sign-key)"]
    missing = inspect_check()
    if missing:
        problems.append(missing)
    check = s if baseline_ready or cell.procedure not in DEPENDENT else dataclasses.replace(
        s, m4_baseline_log=None, m4_baseline_pubkey=None)        # a copy for the check only; the cell keeps s
    if cell.procedure == SEC006:
        problems += preflight.sec006_start_problems(check, v)
    elif cell.procedure == SEC002:
        problems += preflight.sec002_start_problems(check, v)
    elif cell.procedure == AGT012:
        problems += preflight.agt012_start_problems(s)
    elif cell.procedure == DAT009:
        problems += preflight.dat009_start_problems(s, v)
    elif cell.procedure == SEC003:
        problems += preflight.sec003_start_problems(s, v)
    elif cell.procedure == MOD011:
        problems += preflight.mod011_start_problems(s, v)
    elif tierm.is_tierm(s):
        problems += tierm.start_problems(s, v)
    if s.item_set == PRIVATE_ITEM_SET:
        problems += private_problems(s)
    return problems + selftest.gate_problems(cell.procedure, isolation, s.mode)


def exec_kwargs(cell, *, sign_key, confirmation, probes, prior_spent_usd, selftest_log, selftest_pubkey):
    """The keyword arguments of the engine of one cell, as the preflight passes them."""
    kw = dict(settings=cell.settings, run_dir=cell.run_dir, sign_key=sign_key, confirmation=confirmation,
              probes=probes, prior_spent_usd=prior_spent_usd, variant=cell.variant)
    if cell.procedure == AGT012:
        kw.update(selftest_log=selftest_log, selftest_pubkey=selftest_pubkey,
                  delays_ms=cell.settings.containment_plan()[0])
    if cell.procedure in (DAT009, MOD011):
        kw.update(selftest_log=selftest_log, selftest_pubkey=selftest_pubkey)
    if cell.procedure == SEC003:        # the engine takes no variant
        del kw["variant"]
        kw.update(selftest_log=selftest_log, selftest_pubkey=selftest_pubkey)
    return kw


def _usd(v):
    return "not known" if v is None else f"{v:.4f}"


def print_matrix(cells, raw):
    ests = [estimate(c.settings) for c in cells]
    models = len(raw["models"])
    print(f"Bench matrix: {len(cells)} cells ({len(raw['procedures'])} procedures x {models} models). Mode "
          f"{raw['mode']}. Each row of the report is labelled '{benchreport.LABEL}'.")
    print("| cell | model | procedure | tier | item set | k | settings hash | worst-case USD |")
    print("|---|---|---|---|---|---|---|---|")
    for c, e in zip(cells, ests):
        print(f"| {c.n} | {c.model_id} | {c.procedure} | {c.tier} | {c.settings.item_set} | "
              f"{c.settings.runs_per_item} | {c.settings.digest()[:12]} | {_usd(e)} |")
    total = None if any(e is None for e in ests) else sum(ests)
    print(f"Worst-case cost of the bench: USD {_usd(total)}"
          + ("" if total is not None else " (a cell has no estimate)") + f"; bench cap: USD {raw.get('cap_usd')}. "
          "The probes cost more, and count against the cap.")
    return total


def _write_pubkey(sign_key, out):
    key = serialization.load_pem_private_key(Path(sign_key).read_bytes(), password=None)
    path = Path(out) / PUBKEY_NAME
    path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo))
    return path


def run(a, *, input_fn=input, gateway_factory=preflight._default_gateway, executor=preflight._default_executor,
        inspect_check=preflight._inspect_missing, clock_ns=time.time_ns):
    """The bench command. Exit codes as the preflight: 0 done, 2 refused, 4 `yes` not typed, 5 a cell stopped."""
    try:
        raw = load_bench(a.bench)
    except SettingsError as e:
        print("BENCH INVALID. Nothing runs:\n" + "\n".join(f"  - {p}" for p in e.problems))
        return 2
    try:
        profile = load_profile(a.profile)
    except ProfileError as e:
        print("PROFILE INVALID:\n" + "\n".join(f"  - {p}" for p in e.problems))
        return 2
    out = Path(a.out).resolve()
    if _inside(out):
        print("BENCH REFUSED. Nothing runs:\n  - the bench folder is inside the repository; keep it outside (the run "
              "logs can hold private items, and the public key file of the baselines must be outside)")
        return 2
    cells, problems = plan(raw, out)
    if problems:
        print("BENCH INVALID. Nothing runs:\n" + "\n".join(f"  - {p}" for p in problems))
        return 2
    h = bench_digest(raw)
    print(f"Bench settings hash: {h}")
    total = print_matrix(cells, raw)
    if not a.start:
        print("Probes: not run (use --start)")
        return 0
    isolation = selftest.current(selftest_log=a.selftest_log, pubkey=a.selftest_pubkey or raw.get("selftest_pubkey"))
    print(selftest.preflight_line(isolation))
    problems = []
    if out.exists() and any(out.iterdir()):
        problems.append("the bench folder exists and is not empty; give a new folder")
    for c in cells:
        problems += [f"{c.name}: {p}" for p in cell_problems(profile, c, a.sign_key, isolation, inspect_check)]
    if problems:
        print("BENCH REFUSED. Nothing runs. Fix these points first:\n" + "\n".join(f"  - {p}" for p in problems))
        return 2
    spent, probes = 0.0, {}
    for mi in range(len(raw["models"])):
        first = next(c for c in cells if c.model_index == mi)
        gw = gateway_factory(first.settings)
        gw.cap.spent_usd = spent
        print(f"Model {mi + 1}: {first.model_id}")
        probes[str(first.model_id)] = p = spike.run_probes(gw)
        if hasattr(getattr(gw, "egress", None), "close"):
            gw.egress.close()
        spent = gw.cap.spent_usd
        preflight.print_probes(p)
    print()
    print(f"Start conditions are met for each cell. {len(cells)} cells; worst-case cost USD {_usd(total)}; spent on the "
          f"probes USD {spent:.6f}; bench cap USD {raw['cap_usd']}. A cell that stops stops the bench.")
    answer = input_fn("Type yes to start the bench, every cell of the matrix above (anything else cancels): ").strip()
    if answer != "yes":
        print("Bench not started: `yes` was not typed.")
        return 4
    confirmation = {"typed": "yes", "time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "scope": YES_SCOPE,
                    "bench_settings_sha256": h}
    out.mkdir(parents=True, exist_ok=True)
    _write_pubkey(a.sign_key, out)
    report = benchreport.BenchReport(out)
    header = benchreport.build_header(
        bench_sha256=h, bench_file=Path(a.bench).name, mode=raw["mode"], cap_usd=raw["cap_usd"],
        models=[{"model": i + 1, **{k: m[k] for k in MODEL_KEYS}} for i, m in enumerate(raw["models"])],
        cells=[c.entry(out) for c in cells], confirmation=confirmation, probes=probes)
    report.open(header, clock_ns())         # the lock is on disk before the first cell
    status, stopped_at, ran = "complete", None, 0
    for c in cells:
        entry = c.entry(out)
        if stopped_at is not None:
            report.write(benchreport.row_not_run(entry, benchreport.NOT_RUN, f"{NOT_RUN_REASON} (cell {stopped_at})"))
            continue
        if c.procedure in DEPENDENT:
            again = cell_problems(profile, c, a.sign_key, isolation, inspect_check, baseline_ready=True)
            if again:
                report.write(benchreport.row_not_run(entry, benchreport.REFUSED, "; ".join(again)))
                status, stopped_at = "stopped", c.n
                print(f"{c.name}: refused: {'; '.join(again)}")
                continue
        kw = exec_kwargs(c, sign_key=a.sign_key, confirmation=confirmation, probes=probes[str(c.model_id)],
                         prior_spent_usd=spent, selftest_log=a.selftest_log, selftest_pubkey=a.selftest_pubkey)
        try:
            result = executor(**kw)
        except Exception as e:      # noqa: BLE001 - only the class name is kept
            report.write(benchreport.row_not_run(entry, benchreport.ERROR, f"the engine raised {type(e).__name__}"))
            status, stopped_at = "stopped", c.n
            print(f"{c.name}: the engine raised {type(e).__name__}")
            continue
        ran += 1
        spent = result.summary.get("cost_usd", spent)
        report.write(benchreport.row_from_log(result.log_path, entry, out))
        print(f"{c.name}: {result.status}; spent USD {spent}")
        if result.status != "complete":
            status, stopped_at = "stopped", c.n
    report.finish({"type": "summary", "label": benchreport.LABEL, "status": status, "cells_planned": len(cells),
                   "cells_run": ran, "stopped_at_cell": stopped_at, "spent_usd": round(spent, 8),
                   "cap_usd": raw["cap_usd"], "finished": runlog.iso()}, a.sign_key)
    print(f"Bench {status}. Cells run {ran} of {len(cells)}. Report: {report.path}")
    return 0 if status == "complete" else 5


def main(argv=None, **kw):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["verify"]:
        ap = argparse.ArgumentParser(prog="faias_harness.bench verify", description="Verify a signed bench report.")
        ap.add_argument("--report", required=True)
        ap.add_argument("--sig")
        ap.add_argument("--pubkey")
        ap.add_argument("--no-repo", action="store_true",
                        help="verify each member log without the record and the item set of this checkout")
        a = ap.parse_args(argv[1:])
        ok, msg = benchreport.verify(a.report, a.sig, a.pubkey, use_repo=not a.no_repo)
        print(msg)
        return 0 if ok else 1
    ap = argparse.ArgumentParser(prog="faias_harness.bench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--bench", required=True, help="the bench file (kind: bench)")
    ap.add_argument("--profile", required=True, help="the target profile of SRD-1 minimal")
    ap.add_argument("--out", required=True, help="the bench folder: new or empty, outside the repository")
    ap.add_argument("--start", action="store_true", help="check, probe each model, ask for one `yes` and run")
    ap.add_argument("--sign-key", help="with --start: the run-signing private key (outside the repository)")
    ap.add_argument("--selftest-log", help="a containment self-test log, or a folder that holds one")
    ap.add_argument("--selftest-pubkey", help="the public key file of the self-test log's signature")
    a = ap.parse_args(argv)
    return run(a, **kw)


if __name__ == "__main__":
    sys.exit(main())
