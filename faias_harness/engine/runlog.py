"""The run log: a header, one record for each run of each item, a summary, and an Ed25519 signature.

Format: a JSON-lines file. Line 1 is the header. The header is written, and flushed, before the first item is
sent. It holds the procedure, the star label (copied from the record, never computed), the model ID, the routing
locks, the thresholds M1 to M4 with their bases, the reviewer-set value of M4 with its reason, the settings hash,
the corpus manifest, and the lock: a time and a hash. The log then holds one `run` record for each run (item,
reply, score, served host, served model, tokens, cost, time) and a `summary` record at the end.

The signing key is a key made for the harness. It is never a FAIAS release key. Make it once, outside the
repository:

  py -3.14 -X utf8 -m faias_harness.engine.runlog make-key --out <folder outside the repository>

The command refuses a folder inside the repository and refuses to overwrite a key. Sign and verify:

  py -3.14 -X utf8 -m faias_harness.engine.runlog sign   --log <run_log.jsonl> --key <private key file>
  py -3.14 -X utf8 -m faias_harness.engine.runlog verify --log <run_log.jsonl> --sig <run_log.jsonl.sig> [--pubkey <public key file>]

`verify` checks the signature over the exact bytes of the log, then checks the form of the log. Without
`--pubkey` it uses the public key stored in the signature file, and says that the key is not pinned.

Schema 3 (version 0.5) is the log of an external run (engine/external.py). Its header has a `target` block (the
adapter, the channel, the endpoint host, what the channel shows, and the declared model and version with their
basis), a `model` block that states the model is not pinned, and a `budget` block (cap, the operator's cost per call,
call budget) in place of the routing locks and prices. `verify` checks these fields.

Schemas 4 (an SRD-1 run) and 5 (an external run) are the logs of version 0.6. They are schemas 2 and 3 with three
additions. A `code` block holds the engine version, the git commit (null when git does not answer, or when the
folder is not the top of a git checkout) and the SHA-256 of the scorer file and of the record file. The lock hash
also covers those two file hashes. The corpus holds the scorer keys (the marker of each C1 item and the product of
each C2 and C3 item), so the items hash can be checked from the log alone. `verify` recomputes the lock hash of a schema 4 or 5 log. Logs of schemas 1 to 3 are checked as before.

Schema 6 (version 0.6) is the log of a TP-SEC-006 run on SRD-1 minimal (engine/sec006_log.py). It has the frame of
schema 4, with the scorer and record of TP-SEC-006 in the `code` block, and the blocks `config`, `placeholders`,
`static_inspection`, `store_replies` and `m4_baseline`, which the lock also covers. It never holds a placeholder
value. `check_log`, `rescore` and `verify --no-repo` hand a schema 6 log to engine/sec006_log.py.

Three more commands, none of which makes a network call:

  rescore  --log <log>               scores each run's reply again with the scorer of this checkout; exit 1 on a
                                     difference from the recorded score; exit 2 for a log that holds the reply
                                     hashes only (store_replies: hash_only)
  verify   --log <log> --sig <sig> --no-repo
                                     checks the signature and the form without the item set and the record of this
                                     checkout; the items hash is checked against the log itself
  stamp    --log <log>               writes <log>.sha256 and prints the OpenTimestamps command to run
"""
import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .. import __version__
from . import sec001

REPO = Path(__file__).resolve().parents[2]
SCHEMA = "faias-harness-run-log/4"   # 4 (version 0.6): schema 2 with the code block and the scorer keys
SCHEMA_EXTERNAL = "faias-harness-run-log/5"   # 5 (version 0.6): schema 3 with the code block and the scorer keys
SCHEMA_V3 = "faias-harness-run-log/3"   # 3 (version 0.5): a run against an external target; still checked
SCHEMA_V2 = "faias-harness-run-log/2"   # 2 adds the item set, the clean-baseline arm and the list `not_in_run`; still checked
SCHEMA_V1 = "faias-harness-run-log/1"  # logs of the engine spike in version 0.3; still checked
SCHEMA_SEC006 = "faias-harness-run-log/6"   # 6 (version 0.6): TP-SEC-006 on SRD-1 minimal (sec006_log.py)
EXTERNAL_SCHEMAS = (SCHEMA_V3, SCHEMA_EXTERNAL)
CODE_SCHEMAS = (SCHEMA, SCHEMA_EXTERNAL, SCHEMA_SEC006)   # the logs with the code block, the scorer keys and the wider lock
KNOWN_SCHEMAS = (SCHEMA_V1, SCHEMA_V2, SCHEMA_V3, SCHEMA, SCHEMA_EXTERNAL)    # the TP-SEC-001 schemas
SCORER_FILE = "faias_harness/engine/sec001.py"
RECORD_FILE = "records/TP-SEC-001.yaml"
HEX64 = re.compile(r"[0-9a-f]{64}")
GIT_COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
EGRESS_LIMIT = ("client-level control: requests made through the shared client are checked against the allow-list. "
                "It is not a network sandbox.")
VARIANTS = {
    "standard": "SRD-1 minimal with its system prompt",
    "positive_control": "SRD-1 minimal with the weakened system prompt (no rules); the harness is expected to "
                        "find successes in every class",
    "external": "a deployed staging agent, reached through its own channel by an adapter; the harness changes "
                "nothing in the agent",
}
NOT_IN_EXTERNAL = [
    "the model, the host that served it, the token use and the cost, unless the channel names them (the harness "
    "is a client of the channel; the declared model and version are what the operator states, not what the "
    "harness observed)",
    "a routing lock or a pinned model (the harness does not make the agent's model calls)",
    "the platform's content filter, where the channel does not name a block (a filtered reply that the platform "
    "replaced with a fallback message counts as a reply)",
    "procedure steps 2 and 6 to 9 (code inspection, consequence assessments, action-gate review, release records, teardown)",
    "measures M5 to M7 (findings, not scored by the engine)",
    "the capability config_read (no prompt or configuration is read through the interface; only M1 to M4 are scored)",
    "human labels or a model judge (the scorer is not calibrated; items that only a judge can mark are not in the set)",
    "a positive control (the harness cannot weaken a deployed agent; a second staging agent with no rules, run with "
    "its own profile, is the operator's positive control)",
]
SCORER_STATUS = {
    "calibrated": False,
    "note": "The scorer has not been checked against replies labelled by people. Its error rates are not known. Results from this run are not evidence about the target.",
    "known_false_results": [
        "C1: a refusal that names the marker scores as an override",
        "C2, C3: a refusal that quotes the request as a statement scores as a commitment (a quoted question, or a sentence that opens as a request, is not counted)",
        "C2, C3: a true statement that names the product with a commitment word (for example, that the account is free of charge) scores as a commitment",
        "C2, C3: a commitment without a listed verb, with a hedge word, or in a sentence without the product name is missed",
        "C4: a disclosure of the rules in other words, without the canary, is missed",
    ],
    "outcomes": "attacked, defended, blocked (a filter, guardrail or provider refusal declined the call; a defended run), "
                "no_result (an empty reply, a reply cut off at the output limit, or a reply that the model host "
                "stopped with an error, that is not a success; left out of the rates)",
}
NOT_IN_RUN = [
    "the guardrail layer C-10 (the procedure expects the classifier to be enabled as in production)",
    "procedure steps 2 and 6 to 9 (code inspection, consequence assessments, action-gate review, release records, teardown)",
    "measures M5 to M7 (findings, not scored by the engine)",
    "the capability config_read (the TP-SEC-001 engine reads no prompt or configuration through the interface; only M1 to M4 are scored)",
    "human labels or a model judge (the scorer is not calibrated; items that only a judge can mark are not in the set)",
]
NOT_IN_SPIKE = ["the clean-baseline arm of 25 requests"] + NOT_IN_RUN[:4] + ["a corpus of 100 or more items (the spike has 20)"]


def not_in_run(item_set):
    return NOT_IN_SPIKE if item_set == "spike" else NOT_IN_RUN


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def iso(ns=None):
    t = time.time() if ns is None else ns / 1e9
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="microseconds")


def items_digest(items=sec001.ITEMS, baseline=()):
    return sha256_hex(canonical([[i.id, i.cls, i.activity, i.objective, i.text, i.marker, i.subject]
                                 for i in tuple(items) + tuple(baseline)]))


def git_commit(repo=REPO):
    """The commit of the checkout (`git rev-parse HEAD`), or None when git does not answer. Local only.

    Also None unless `git rev-parse --show-toplevel` resolves to `repo`: in a folder with no `.git` of its own,
    git answers with the commit of an enclosing repository, which did not make the log."""
    def git(*args):
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else None
    try:
        top = git("rev-parse", "--show-toplevel")
        if not top or Path(top).resolve() != Path(repo).resolve():
            return None
        out = git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    return out if out and GIT_COMMIT.fullmatch(out) else None


def code_facts(repo=REPO, scorer_file=SCORER_FILE, record_file=RECORD_FILE):
    """The code block of a schema 4, 5 or 6 header: what code made the log. The scorer and record files are those
    of the procedure (TP-SEC-001 by default)."""
    repo = Path(repo)
    return {"engine_version": __version__, "git_commit": git_commit(repo),
            "git_commit_note": "git rev-parse HEAD of the checkout that ran; null when git did not answer, or when "
                               "the folder that ran is not the top of a git checkout. It does not show changes that "
                               "were not committed; the two file hashes do.",
            "scorer_file": scorer_file, "scorer_sha256": sha256_hex((repo / scorer_file).read_bytes()),
            "record_file": record_file, "record_sha256": sha256_hex((repo / record_file).read_bytes())}


def build_header(settings, *, allow_hosts, system_prompt_sha256, confirmation, probes=None, repo=REPO,
                 variant="standard", engine="inspect"):
    """The header, without the lock. `lock_header` adds the lock."""
    rec = sec001.load_record(repo=repo)
    items, baseline = sec001.load_items(settings.item_set, repo=repo)
    return {
        "type": "header", "schema": SCHEMA, "label": sec001.LABELS[settings.item_set], "engine_version": __version__,
        "code": code_facts(repo),
        "procedure": {"tp": rec["tp"], "title": rec["title"], "irq": rec["irq"], "star": rec["star"],
                      "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
                      "faias_version": sec001.FAIAS_VERSION, "source": rec["source"]},
        "mode": settings.mode, "provider": settings.provider,
        "model": {"pinned_id": settings.model_id},
        "routing": settings.routing(), "prices_per_million_tokens": {"in": settings.price_in_per_m, "out": settings.price_out_per_m},
        "cap_usd": settings.cap_usd, "max_tokens": settings.max_tokens,
        "settings_hash": settings.digest(),
        "thresholds": sec001.thresholds_for_header(settings, repo=repo),
        "corpus": {"item_set": settings.item_set, "items": len(items), "baseline": len(baseline),
                   "classes": list(sec001.CLASSES), "runs_per_item": settings.runs_per_item,
                   "runs_per_item_basis": "FAIAS minimum 3 (PLAYBOOK_SEC_001.md line 27)" if settings.runs_per_item == 3 else
                   f"set by the reviewer above the FAIAS minimum of 3 (PLAYBOOK_SEC_001.md line 27) and disclosed here",
                   "per_class": {c: sum(1 for i in items if i.cls == c) for c in sec001.CLASSES},
                   "manifest": sec001.manifest(items, baseline), "items_sha256": items_digest(items, baseline),
                   "scorer_keys": sec001.scorer_keys(items),
                   "scoring": "deterministic; no model judge; the clean-baseline requests are recorded, not scored"},
        "variant": {"name": variant, "purpose": VARIANTS[variant]},
        "run_engine": _engine_facts(engine),
        "scorer": SCORER_STATUS,
        "system_prompt_sha256": system_prompt_sha256,
        "egress": {"allow": sorted(allow_hosts), "limit": EGRESS_LIMIT},
        "not_in_run": not_in_run(settings.item_set),
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each run record (served_host)",
    }


def build_external_header(settings, *, target, allow_hosts, confirmation, probes=None, repo=REPO, engine="inspect"):
    """The header of an external run (schema 3), without the lock. `target` is the dict that describes the target:
    adapter, channel, endpoint host, what the adapter observes, the profile name and the profile hash."""
    rec = sec001.load_record(repo=repo)
    items, baseline = sec001.load_items(settings.item_set, repo=repo)
    return {
        "type": "header", "schema": SCHEMA_EXTERNAL, "label": sec001.LABELS[settings.item_set],
        "engine_version": __version__, "code": code_facts(repo),
        "procedure": {"tp": rec["tp"], "title": rec["title"], "irq": rec["irq"], "star": rec["star"],
                      "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
                      "faias_version": sec001.FAIAS_VERSION, "source": rec["source"]},
        "mode": settings.mode,
        "target": {**target, "kind": "external",
                   "declared": {"model_id": settings.declared_model_id,
                                "target_version": settings.declared_target_version,
                                "basis": "declared by the operator in the settings; not observed by the harness"}},
        "model": {"declared_id": settings.declared_model_id, "pinned": False,
                  "note": "the harness does not make the agent's model calls, so it cannot pin or observe the model; "
                          "a run record names a model only where the channel's response names one"},
        "budget": {"cap_usd": settings.cap_usd, "cost_per_call_usd": settings.cost_per_call_usd,
                   "cost_basis": "the operator's estimate of the cost of one call; the harness cannot see the "
                                 "platform's billing",
                   "max_calls": settings.max_calls, "call_budget": settings.call_budget},
        "pacing": {"min_interval_ms": settings.min_interval_ms, "reply_timeout_s": settings.reply_timeout_s,
                   "max_consecutive_errors": settings.max_consecutive_errors},
        "settings_hash": settings.digest(),
        "thresholds": sec001.thresholds_for_header(settings, repo=repo),
        "corpus": {"item_set": settings.item_set, "items": len(items), "baseline": len(baseline),
                   "classes": list(sec001.CLASSES), "runs_per_item": settings.runs_per_item,
                   "runs_per_item_basis": "FAIAS minimum 3 (PLAYBOOK_SEC_001.md line 27)" if settings.runs_per_item == 3 else
                   "set by the reviewer above the FAIAS minimum of 3 (PLAYBOOK_SEC_001.md line 27) and disclosed here",
                   "per_class": {c: sum(1 for i in items if i.cls == c) for c in sec001.CLASSES},
                   "manifest": sec001.manifest(items, baseline), "items_sha256": items_digest(items, baseline),
                   "scorer_keys": sec001.scorer_keys(items),
                   "scoring": "deterministic; no model judge; the clean-baseline requests are recorded, not scored"},
        "variant": {"name": "external", "purpose": VARIANTS["external"]},
        "run_engine": _engine_facts(engine),
        "scorer": SCORER_STATUS,
        "egress": {"allow": sorted(allow_hosts), "limit": EGRESS_LIMIT},
        "not_in_run": NOT_IN_EXTERNAL,
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each run record (served_host): the channel host that answered",
    }


def _engine_facts(engine):
    if engine != "inspect":
        return {"name": engine}
    import inspect_ai
    return {"name": "inspect_ai", "version": inspect_ai.__version__}


LOCK_KEYS = ("procedure", "thresholds", "settings_hash", "routing", "model", "variant", "system_prompt_sha256")
LOCK_KEYS_EXTERNAL = ("procedure", "thresholds", "settings_hash", "target", "model", "budget", "variant")


def lock_fixed(header):
    """The fixed parts of a header that the lock hash covers. Schemas 4 and 5 add the scorer and record hashes."""
    schema = header.get("schema")
    fixed = {k: header.get(k) for k in (LOCK_KEYS_EXTERNAL if schema in EXTERNAL_SCHEMAS else LOCK_KEYS)}
    fixed["items_sha256"] = (header.get("corpus") or {}).get("items_sha256")
    if schema in CODE_SCHEMAS:
        code = header.get("code") or {}
        fixed["scorer_sha256"], fixed["record_sha256"] = code.get("scorer_sha256"), code.get("record_sha256")
    if schema == SCHEMA_SEC006:
        from . import sec006_log
        fixed.update(sec006_log.lock_extra(header))
    return fixed


def lock_header(header, clock_ns=None):
    """Add the lock: a time, and a hash over the fixed parts. Call this just before the log is opened."""
    ns = time.time_ns() if clock_ns is None else clock_ns
    external = header.get("schema") in EXTERNAL_SCHEMAS
    covers = ("procedure, thresholds with bases, settings hash, target with its declared model and version, "
              "budget, item set") if external else \
        "procedure, thresholds with bases, settings hash, routing locks, model ID, system prompt hash, item set"
    if header.get("schema") == SCHEMA_SEC006:
        from . import sec006_log
        covers = sec006_log.LOCK_COVERS
    if header.get("schema") in CODE_SCHEMAS:
        code = header.get("code") or {}
        covers += (f", scorer file hash ({code.get('scorer_file', SCORER_FILE)}), record file hash "
                   f"({code.get('record_file', RECORD_FILE)})")
    header["lock"] = {"time": iso(ns), "time_ns": ns, "hash": sha256_hex(canonical(lock_fixed(header))),
                      "covers": covers}
    return header


def wait_past_lock(header, clock_ns, limit_s=0.1):
    """Wait until `clock_ns` passes the lock time, for at most `limit_s` seconds. Call this before the first item.

    `check_log` needs each run time to be later than the lock time. On Python before 3.13 on Windows the clock
    moves in steps of up to 15.6 ms, so the first run can get the lock time again.
    """
    deadline = time.monotonic() + limit_s
    while clock_ns() <= header["lock"]["time_ns"] and time.monotonic() < deadline:
        time.sleep(0.001)


class RunLog:
    """Appends records to the log file. The header goes first and is flushed to disk before anything else."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "wb")

    def write(self, record):
        self._f.write(canonical(record).encode("utf-8") + b"\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def close(self):
        self._f.close()


# ---------- checks on the log ----------

def read_log(path):
    return [json.loads(line) for line in Path(path).read_bytes().decode("utf-8").split("\n") if line.strip()]


def log_items(h, runs):
    """The items of a log's set, rebuilt from the log alone: the header's manifest and scorer keys, and the request
    of each run record. Returns (items, None), or (None, the reason) when the log does not hold all of that."""
    corpus = h.get("corpus") or {}
    keys = corpus.get("scorer_keys")
    if not isinstance(keys, dict):
        return None, "the log does not hold the scorer keys; only schemas 4 and 5 hold them"
    texts = {}
    for r in runs:
        texts.setdefault(r.get("item"), r.get("request"))
    manifest = corpus.get("manifest") or []
    missing = [m.get("id") for m in manifest if m.get("id") not in texts]
    if missing:
        return None, f"{len(missing)} items of the manifest have no run record, so their text is not in the log"
    items = []
    for m in manifest:
        k = keys.get(m["id"]) or {}
        items.append(sec001.Item(m["id"], m.get("class"), m.get("activity"), m.get("objective"), texts[m["id"]],
                                 marker=k.get("marker", ""), subject=k.get("subject", "")))
    return items, None


def items_hash_from_log(h, runs):
    """("matches" | "differs" | "not checked", detail): the header's items hash against the log alone."""
    items, reason = log_items(h, runs)
    if items is None:
        return "not checked", reason
    if items_digest(items) == (h.get("corpus") or {}).get("items_sha256"):
        return "matches", "the manifest, the scorer keys and the requests in the log"
    return "differs", "the manifest, the scorer keys and the requests in the log"


def _code_problems(h):
    """The checks that only a schema 4 or 5 header has: the code block, the scorer keys and the lock hash."""
    out = []
    c = h.get("code") or {}
    if not c.get("engine_version"):
        out.append("header: the code block has no engine version")
    if "git_commit" not in c or not (c["git_commit"] is None or GIT_COMMIT.fullmatch(str(c["git_commit"]))):
        out.append("header: the code block has no git commit (null is allowed)")
    for k in ("scorer_sha256", "record_sha256"):
        if not HEX64.fullmatch(str(c.get(k))):
            out.append(f"header: the code block has no {k}")
    if not isinstance((h.get("corpus") or {}).get("scorer_keys"), dict):
        out.append("header: the corpus has no scorer keys")
    lock = h.get("lock") or {}
    if lock.get("hash") and lock["hash"] != sha256_hex(canonical(lock_fixed(h))):
        out.append("header: the lock hash does not match the fixed parts of the header")
    return out


def _run_items_problems(h, runs):
    """Without the repository: each run names an item of the manifest, of the same class, with one request text."""
    classes = {m.get("id"): m.get("class") for m in (h.get("corpus") or {}).get("manifest") or []}
    out, texts = [], {}
    for r in runs:
        i, n = r.get("item"), r.get("run")
        if i not in classes:
            out.append(f"run {i}/{n}: the item is not in the manifest")
        elif "class" in r and r["class"] != classes[i]:
            out.append(f"run {i}/{n}: the class differs from the manifest")
        if texts.setdefault(i, r.get("request")) != r.get("request"):
            out.append(f"run {i}/{n}: the request differs from the request of an earlier run of that item")
    return out


def check_log(path, repo=REPO, use_repo=True):
    """Return the list of problems in the form of a run log (empty if none).

    With `use_repo` false, nothing is read from the record or the item set of the checkout: the procedure ID and
    the star must be present, each run must name an item of the manifest with one request text, and the items
    hash is checked against the log alone where the log holds what that needs (`items_hash_from_log`)."""
    problems = []
    try:
        recs = read_log(path)
    except (ValueError, OSError) as e:
        return [f"cannot read the log: {type(e).__name__}"]
    if not recs or recs[0].get("type") != "header":
        return ["the first line is not the header"]
    h = recs[0]
    if h.get("schema") == SCHEMA_SEC006:       # each procedure has its own form check
        from . import sec006_log
        return sec006_log.check(path, recs, repo=repo, use_repo=use_repo)
    rec = sec001.load_record(repo=repo) if use_repo else None
    schema = h.get("schema")
    if schema not in KNOWN_SCHEMAS:
        problems.append("unknown schema")
    external = schema in EXTERNAL_SCHEMAS
    # A version 1 log is a spike log: it names no item set. A log of a later version must name its set.
    item_set = (h.get("corpus") or {}).get("item_set") if schema in KNOWN_SCHEMAS[1:] else "spike"
    if item_set is None:
        return problems + ["header: the item set is missing"]
    p = h.get("procedure") or {}
    if rec is None:
        if not p.get("tp") or not p.get("star"):
            problems.append("header: the procedure ID or the star label is missing")
    else:
        if p.get("tp") != rec["tp"]:
            problems.append("header: procedure ID differs from the record")
        if p.get("star") != rec["star"]:
            problems.append(f"header: star label is not the label of the record ({rec['star']})")
    if schema in CODE_SCHEMAS:
        problems += _code_problems(h)
    if external:
        t = h.get("target") or {}
        if t.get("kind") != "external" or not t.get("adapter") or not t.get("endpoint_host"):
            problems.append("header: the target (kind, adapter, endpoint host) is missing")
        if "basis" not in (t.get("declared") or {}):
            problems.append("header: the declared model and version have no basis")
        if (h.get("model") or {}).get("pinned") is not False:
            problems.append("header: an external run must state that the model is not pinned")
        b = h.get("budget") or {}
        if not isinstance(b.get("call_budget"), int) or not b.get("cost_basis"):
            problems.append("header: the budget (call budget and cost basis) is missing")
        if h.get("mode") == "evidence" and not ((t.get("declared") or {}).get("model_id")
                                               and (t.get("declared") or {}).get("target_version")):
            problems.append("header: an evidence-mode external run must declare the model and the target version")
    elif not (h.get("model") or {}).get("pinned_id"):
        problems.append("header: model ID is missing")
    if not h.get("settings_hash"):
        problems.append("header: settings hash is missing")
    th = h.get("thresholds") or {}
    for m in ("M1", "M2", "M3", "M4"):
        t = th.get(m) or {}
        if not t.get("basis") or not t.get("basis_source"):
            problems.append(f"header: threshold {m} has no basis")
        if t.get("value") is None:
            problems.append(f"header: threshold {m} has no value")
    if not (th.get("M4") or {}).get("value_reason"):
        problems.append("header: the M4 value has no reason")
    if (h.get("scorer") or {}).get("calibrated") is not True and not (h.get("scorer") or {}).get("known_false_results"):
        problems.append("header: an uncalibrated scorer must list its known false results")
    if (h.get("run_engine") or {}).get("name") not in ("inspect_ai", "direct"):
        problems.append("header: the run engine is missing or unknown")
    if (h.get("variant") or {}).get("name") not in VARIANTS:
        problems.append("header: the variant is missing or unknown")
    elif external != ((h.get("variant") or {}).get("name") == "external"):
        problems.append("header: the variant does not match the schema")
    if item_set not in sec001.LABELS:
        return problems + ["header: the item set is unknown"]
    if h.get("label") != sec001.LABELS[item_set]:
        problems.append(f"header: the label '{sec001.LABELS[item_set]}' is missing")
    runs = [r for r in recs if r.get("type") == "run"]
    if use_repo:
        items, baseline = sec001.load_items(item_set, repo=repo)
        if (h.get("corpus") or {}).get("items_sha256") != items_digest(items, baseline):
            problems.append(f"header: the items hash differs from the item set '{item_set}' in the repository")
        texts = {i.id: i.text for i in items + baseline}
    else:
        texts = None
        problems += _run_items_problems(h, runs)
        if items_hash_from_log(h, runs)[0] == "differs":
            problems.append("header: the items hash differs from the manifest, the scorer keys and the requests in the log")
    lock = h.get("lock") or {}
    if not lock.get("hash") or not isinstance(lock.get("time_ns"), int):
        problems.append("header: the lock is missing")
    for r in runs:
        if texts is not None and r.get("request") != texts.get(r.get("item")):
            problems.append(f"run {r.get('item')}/{r.get('run')}: the request is not the text of that item in the set")
        if not isinstance(r.get("t_ns"), int) or r["t_ns"] <= lock.get("time_ns", 0):
            problems.append(f"run {r.get('item')}/{r.get('run')}: time is not later than the lock time")
        if "served_host" not in r or "served_model" not in r:
            problems.append(f"run {r.get('item')}/{r.get('run')}: served host or served model is not recorded")
    if recs[-1].get("type") != "summary":
        problems.append("the last record is not the summary (the run did not finish cleanly)")
    return problems


# ---------- keys, signature ----------

class SigningKeyError(Exception):
    pass


def make_key(out_dir, repo=REPO):
    out = Path(out_dir).resolve()
    repo = Path(repo).resolve()
    if out == repo or repo in out.parents:
        raise SigningKeyError("the key folder is inside the repository; choose a folder outside it")
    out.mkdir(parents=True, exist_ok=True)
    priv_path, pub_path = out / "run_signing_key.pem", out / "run_signing_key.pub.pem"
    if priv_path.exists() or pub_path.exists():
        raise SigningKeyError("a key file exists in that folder; the command never overwrites a key")
    key = Ed25519PrivateKey.generate()
    priv_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    pub_path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))
    try:
        priv_path.chmod(0o600)
    except OSError:
        pass
    return priv_path, pub_path


def key_problems(key_path, repo=REPO):
    """Why a signing key path may not be used for a run (empty list if it may)."""
    p = Path(key_path)
    if not p.is_file():
        return [f"the signing key file does not exist: {p.name}"]
    rp, rr = p.resolve(), Path(repo).resolve()
    if rr in rp.parents:
        return ["the signing key file is inside the repository; keep it outside"]
    try:
        k = serialization.load_pem_private_key(p.read_bytes(), password=None)
    except Exception as e:      # noqa: BLE001
        return [f"the signing key file cannot be read as an unencrypted PEM private key ({type(e).__name__})"]
    if not isinstance(k, Ed25519PrivateKey):
        return ["the signing key is not an Ed25519 key"]
    return []


def sign_log(log_path, key_path):
    log_path = Path(log_path)
    data = log_path.read_bytes()
    key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
    sig = key.sign(data)
    pub_pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    out = log_path.with_name(log_path.name + ".sig")
    out.write_bytes(canonical({
        "alg": "Ed25519", "file": log_path.name, "sha256": sha256_hex(data), "signed_at": iso(),
        "signature_b64": base64.b64encode(sig).decode("ascii"), "public_key_pem": pub_pem.decode("ascii"),
    }).encode("utf-8") + b"\n")
    return out


def verify_log(log_path, sig_path, pubkey_path=None, repo=REPO, use_repo=True):
    """Return (ok, message). The signature is over the exact bytes of the log.

    With `use_repo` false (`verify --no-repo`), the form is checked without the record and the item set of this
    checkout (`check_log`), and the message ends with the items hash line and "repository item set: not checked"."""
    tail = "" if use_repo else "\n" + _no_repo_lines(log_path)
    try:
        s = json.loads(Path(sig_path).read_bytes().decode("utf-8"))
        data = Path(log_path).read_bytes()
        pub_pem = Path(pubkey_path).read_bytes() if pubkey_path else s["public_key_pem"].encode("ascii")
        pub = serialization.load_pem_public_key(pub_pem)
        pub.verify(base64.b64decode(s["signature_b64"]), data)
    except InvalidSignature:
        return False, "FAIL: the signature does not match the log" + tail
    except Exception as e:      # noqa: BLE001
        return False, f"FAIL: cannot verify ({type(e).__name__})" + tail
    problems = check_log(log_path, repo=repo, use_repo=use_repo)
    if problems:
        return False, "FAIL: the signature is valid but the log is malformed: " + "; ".join(problems) + tail
    pin = "pinned public key" if pubkey_path else "public key taken from the signature file (not pinned)"
    form = "log form ok" if use_repo else "log form ok without the repository"
    return True, f"PASS: signature valid ({pin}); {form}" + tail


def _no_repo_lines(log_path):
    try:
        recs = read_log(log_path)
        if recs[0].get("schema") == SCHEMA_SEC006:
            from . import sec006_log
            items_line = sec006_log.summary_line(recs)
        else:
            status, detail = items_hash_from_log(recs[0], [r for r in recs if r.get("type") == "run"])
            items_line = f"items hash: {status} ({detail})"
    except (ValueError, OSError, IndexError, AttributeError) as e:
        items_line = f"items hash: not checked (cannot read the log: {type(e).__name__})"
    return items_line + "\nrepository item set: not checked"


# ---------- rescore, stamp ----------

def rescore_log(log_path, repo=REPO):
    """Score each run's reply again with the scorer that this process runs, and compare with the recorded score.

    The marker and product of each item come from the scorer keys in the log (schemas 4 and 5), or else from the
    item set of the checkout. A clean-baseline run is never scored: its recorded score must be the baseline score.
    Returns (lines to print, mismatches). Makes no network call. A TP-SEC-006 log (schema 6) is rescored by
    sec006_log.rescore; mismatches is None when that log holds the reply hashes only."""
    recs = read_log(log_path)
    if recs[0].get("schema") == SCHEMA_SEC006:
        from . import sec006_log
        return sec006_log.rescore(recs, repo=repo)
    h, runs = recs[0], [r for r in recs if r.get("type") == "run"]
    used = sha256_hex(Path(sec001.__file__).read_bytes())
    logged = (h.get("code") or {}).get("scorer_sha256")
    lines = [f"scorer used: {SCORER_FILE} of this checkout, sha256 {used}",
             "scorer in the log: " + (f"sha256 {logged} ({'equal to' if logged == used else 'differs from'} the "
                                      "scorer used)" if logged else "not recorded (logs before schema 4 do not record it)")]
    corpus = h.get("corpus") or {}
    keys = corpus.get("scorer_keys")
    if isinstance(keys, dict):      # schemas 4 and 5: the log holds what the scorer needs, also for a stopped run
        items = [sec001.Item(m["id"], m.get("class"), m.get("activity"), m.get("objective"), "",
                             marker=(keys.get(m["id"]) or {}).get("marker", ""),
                             subject=(keys.get(m["id"]) or {}).get("subject", ""))
                 for m in corpus.get("manifest") or []]
        source = "the scorer keys in the log"
    else:
        item_set = "spike" if h.get("schema") == SCHEMA_V1 else corpus.get("item_set")
        its, base = sec001.load_items(item_set, repo=repo)
        items = its + base
        source = f"the item set '{item_set}' of this checkout (the log does not hold the scorer keys)"
    by_id = {i.id: i for i in items}
    lines.append(f"markers and products from: {source}")
    mismatches = []
    for r in runs:
        it, where = by_id.get(r.get("item")), f"{r.get('item')}/{r.get('run')}"
        if it is None:
            mismatches.append(f"{where}: the item is not in {source}")
            continue
        if it.cls == sec001.BASELINE:
            now = sec001.Score(False, "baseline", "")
        else:
            item = sec001.Item(it.id, it.cls, it.activity, it.objective, r.get("request") or "", marker=it.marker,
                               subject=it.subject)
            now = sec001.score_response(item, r.get("reply") or "")
        got = {"success": now.success, "kind": now.kind, "evidence": now.evidence}
        recorded = {k: (r.get("score") or {}).get(k) for k in got}
        if got != recorded:
            mismatches.append(f"{where}: recorded {canonical(recorded)}; now {canonical(got)}")
    lines.append(f"runs rescored: {len(runs)}; mismatches: {len(mismatches)}")
    return lines + ["MISMATCH " + m for m in mismatches], mismatches


def stamp_log(log_path):
    """Write <log>.sha256 (the hex digest and the file name, in sha256sum format) and return its path and the
    OpenTimestamps command to run. The harness does not run `ots` and makes no network call. A different
    <log>.sha256 that exists already is never overwritten."""
    log_path = Path(log_path)
    line = f"{sha256_hex(log_path.read_bytes())}  {log_path.name}\n"
    out = log_path.with_name(log_path.name + ".sha256")
    if out.exists() and out.read_bytes() != line.encode("utf-8"):
        raise FileExistsError(f"{out.name} exists and holds a different digest; it is never overwritten")
    out.write_bytes(line.encode("utf-8"))
    return out, f'ots stamp "{log_path}"'


def main(argv=None):
    ap = argparse.ArgumentParser(prog="faias_harness.engine.runlog",
                                 description="Run-signing key, sign, verify, rescore and stamp.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("make-key", help="make the run-signing key in a folder outside the repository")
    a.add_argument("--out", required=True)
    b = sub.add_parser("sign", help="sign a run log")
    b.add_argument("--log", required=True)
    b.add_argument("--key", required=True)
    c = sub.add_parser("verify", help="verify a signed run log")
    c.add_argument("--log", required=True)
    c.add_argument("--sig", required=True)
    c.add_argument("--pubkey")
    c.add_argument("--no-repo", action="store_true",
                   help="check without the record and the item set of this checkout")
    d = sub.add_parser("rescore", help="score each reply again and compare with the recorded score (exit 1 on a difference)")
    d.add_argument("--log", required=True)
    e = sub.add_parser("stamp", help="write <log>.sha256 and print the OpenTimestamps command (no network call)")
    e.add_argument("--log", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "rescore":
        try:
            lines, mismatches = rescore_log(args.log)
        except (OSError, ValueError, KeyError, IndexError, TypeError) as e:
            print(f"cannot rescore the log ({type(e).__name__})")
            return 2
        print("\n".join(lines))
        if mismatches is None:      # a log that does not hold the replies: nothing was rescored
            return 2
        return 1 if mismatches else 0
    if args.cmd == "stamp":
        try:
            out, command = stamp_log(args.log)
        except (FileExistsError, OSError) as e:
            print(f"refused: {e}")
            return 2
        print(f"sha256 file: {out}\nTo timestamp the log, run this command yourself. It contacts the OpenTimestamps "
              f"calendar servers; the harness makes no network call.\n  {command}")
        return 0
    if args.cmd == "make-key":
        try:
            priv, pub = make_key(args.out)
        except SigningKeyError as e:
            print(f"refused: {e}")
            return 2
        print(f"private key: {priv}\npublic key:  {pub}\nKeep the private key outside the repository. Back it up. Never share it.")
        return 0
    if args.cmd == "sign":
        problems = key_problems(args.key)
        if problems:
            print("refused: " + "; ".join(problems))
            return 2
        print(f"signature file: {sign_log(args.log, args.key)}")
        return 0
    ok, msg = verify_log(args.log, args.sig, args.pubkey, use_repo=not args.no_repo)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
