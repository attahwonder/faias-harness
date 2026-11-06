"""The judge pass: a second reading of the replies of a signed run log by a model judge (bench mode, need N8).

The pass runs after a run, never in it. It reads a run log that `verify` passes, and refuses a log that it does not
pass. It sends each stored reply, with its request and a rubric, to a judge model. It writes a separate judge log
(schema `faias-judge-log/1`, engine/judge_log.py) and signs it. The judge log names the judge model and its family, the
SHA-256 of the rubric and of the source log, and, for each reply, the judge verdict beside the deterministic score of
the source log. The source log is not changed. The deterministic score stays the primary measure in every case.

The judge calls go through a second SRD-1 `Gateway` with its own cap, the same routing locks, prices and key rule as
any model call, and the shared egress client with an allow-list of one host. The pass opens no network site of its own.

The family rule (benchmark method rule 4c). The judge never comes from the model family of the model under test.
`model_families` derives the family from the model ID: the provider prefix (the part before "/") by PREFIX_FAMILY,
and each word of the model name by NAME_FAMILY (a model tuned or distilled from a base model carries that family too).
Two IDs with the same provider prefix are one family, whether or not the table lists the prefix. A model that the
table does not know has an unknown family. The pass refuses it, unless the operator names the family in the judge
settings (`judge_family`, `target_family`). Where the table knows the model, a named family must be one that the table
gives. The pass refuses a judge whose families meet the families of the model under test. All of this happens before
any call. The table leaves out the vendors whose names the sweep bars from this repository; the operator names their
family. The table is the harness's precaution: the evidence is of self-preference of a model for its own output, and
its extension to a whole family is a reading.

The calibration (rules 4b and the judge bar). A calibration file (format `faias-judge-calibration/1`) holds, for a
fixed sample, the labels of two human labellers and of the judge, each item in the tune half or the held-out half.
`calibration_stats` gives the human-human Cohen's kappa and the judge-human kappa on the held-out half (the lower of
the judge against each labeller), and, on the held-out items where the two labellers agree, the judge's miss rate and
false-alarm rate for each class with a Wilson 95% interval. A judge log is labelled LABEL_CALIBRATED only when a
calibration file is named, has the SHA-256 that the settings name, names the rubric and the judge model of the pass,
and both kappas reach KAPPA_BAR. Every other judge log is labelled LABEL_UNCALIBRATED. The bar is the benchmark's own.
FAIAS sets no judge-agreement threshold (PLAYBOOK_MOD_015.md lines 49 and 86).

  py -3.14 -X utf8 -m faias_harness.engine.judge calibrate --calibration <file> [--sha256 <hex>]
  py -3.14 -X utf8 -m faias_harness.engine.judge run --settings <judge.yaml> --log <run_log.jsonl> --out <new folder>
                                                  --sign-key <private key file> [--pubkey <file>] [--no-repo]

`calibrate` makes no network call. `run` makes model calls and asks the operator to type `yes`. Verify a judge log with
`python -m faias_harness.engine.runlog verify --log <judge_log.jsonl> --sig <judge_log.jsonl.sig>`.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .. import __version__, settings as settings_mod, yamlio
from ..egress import EgressClient, EgressLog
from ..errors import CapReached
from . import jsonkeys, runlog, sec001

REPO = Path(__file__).resolve().parents[2]
SCHEMA = runlog.SCHEMA_JUDGE      # "faias-judge-log/1"
CALIBRATION_FORMAT = "faias-judge-calibration/1"
JUDGE_FILE = "faias_harness/engine/judge.py"
JUDGE_LOG_FILE = "faias_harness/engine/judge_log.py"
LOG_NAME = "judge_log.jsonl"
KAPPA_BAR = Fraction(4, 5)      # 0.80: the owner's choice for the benchmark; FAIAS sets none
PRIMARY = "the deterministic score of the source log stays the primary measure"
LABEL_UNCALIBRATED = f"uncalibrated judge, not a finding; {PRIMARY}"
LABEL_CALIBRATED = f"judge agreement benchmark-set (kappa ≥ 0.80); FAIAS sets none; {PRIMARY}"
LABELS = (LABEL_UNCALIBRATED, LABEL_CALIBRATED)
# The verdicts that are not a rubric label: a judge reply that is not exactly one label, and a judge call with no reply.
UNPARSED, NO_RESULT = "unparsed", "no_result"

# The provider prefix of a model ID (the part before "/") -> the model family (the developer of the model).
PREFIX_FAMILY = {
    "openai": "openai", "google": "google", "meta-llama": "meta", "mistralai": "mistral", "qwen": "qwen",
    "x-ai": "xai", "cohere": "cohere", "microsoft": "microsoft", "amazon": "amazon", "nvidia": "nvidia",
    "z-ai": "glm", "thudm": "glm", "moonshotai": "moonshot", "minimax": "minimax", "ai21": "ai21",
    "ibm-granite": "ibm",
}
# A word of the model name -> a family. A word matches when it is the word, or the word and digits (llama3, qwen2).
NAME_FAMILY = {
    "gpt": "openai", "gemini": "google", "gemma": "google", "llama": "meta", "mistral": "mistral",
    "mixtral": "mistral", "qwen": "qwen", "qwq": "qwen", "grok": "xai", "glm": "glm", "phi": "microsoft",
    "nemotron": "nvidia",
}

# The keys of a judge settings file, and of its `model` block. The model block takes the keys of a run settings file
# that a model call uses, with the same rules (settings.validate).
JUDGE_REQUIRED = ("model", "rubric_file", "rubric_sha256")
JUDGE_OPTIONAL = ("judge_family", "target_family", "calibration_file", "calibration_sha256")
MODEL_KEYS = ("provider", "model_id", "mode", "routing", "price_in_per_m", "price_out_per_m", "cap_usd",
              "api_key_env", "max_tokens", "temperature", "seed")
# settings.validate asks an evidence-mode run file for the M4 bar of TP-SEC-001. A judge has no M4, so this one message
# is dropped. tests/test_judge.py checks that settings.validate still gives it in these words.
M4_ONLY = "evidence mode: reviewer_values.M4 needs a value and a reason"
LABEL_FORM = re.compile(r"[a-z0-9_]{1,40}")
NOT_SOURCES = (SCHEMA, runlog.SCHEMA_SELFTEST)
LIMITS = [
    "the judge reads the request and the reply that the source log stores; it does not read SRD-1's trace, the tool "
    "ledger or the retrieval lines",
    "a run record that holds the reply hash only, or an empty reply, is not judged (a not_judged record names it)",
    "the family rule rests on the model ID and the operator's named families; it does not see a model's training",
    "a judge verdict is a second reading beside the deterministic score; it does not change the score, a measure or "
    "the result of the source log",
]


class JudgeRefused(Exception):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


# ---------- the family rule ----------

def _family(name):
    return str(name).strip().casefold() if isinstance(name, str) and name.strip() else None


def _prefix(model_id):
    head, sep, _ = str(model_id or "").strip().casefold().partition("/")
    return head if sep else ""


def model_families(model_id, named=None):
    """(families, basis, problem) of one model ID. `families` is a sorted list, or None with a problem. `named` is
    the family that the operator names in the judge settings, or None."""
    mid = str(model_id or "").strip().casefold()
    _, _, name = mid.partition("/")
    found, basis = set(), []
    prefix = _prefix(mid)
    if prefix in PREFIX_FAMILY:
        found.add(PREFIX_FAMILY[prefix])
        basis.append(f"provider prefix '{prefix}'")
    for word in re.split(r"[^a-z0-9]+", name or mid):
        for key, fam in NAME_FAMILY.items():
            if re.fullmatch(rf"{key}[0-9]*", word):
                found.add(fam)
                basis.append(f"name word '{word}'")
    named = _family(named)
    if found:
        if named is not None and named not in found:
            return None, None, (f"the settings name the family '{named}' for {model_id}, but the table gives "
                                f"{', '.join(sorted(found))}")
        return sorted(found), "the family table of engine/judge.py: " + ", ".join(basis), None
    if named is not None:
        return [named], "named by the operator in the judge settings (the family table has no entry)", None
    return None, None, (f"the family of {model_id} is unknown (the family table has no entry); name it in the judge "
                        "settings")


def family_problems(judge_id, judge_named, target_id, target_named):
    """(problems, judge block, target block): the family rule, before any call. A block holds the model ID, its
    provider prefix, its families and their basis."""
    problems, blocks = [], []
    for role, mid, named in (("judge", judge_id, judge_named), ("model under test", target_id, target_named)):
        fams, basis, problem = model_families(mid, named)
        if problem:
            problems.append(f"{role}: {problem}")
        blocks.append({"model_id": mid, "provider_prefix": _prefix(mid) or None, "families": fams,
                       "family_basis": basis})
    j, t = blocks
    if j["provider_prefix"] and j["provider_prefix"] == t["provider_prefix"]:
        problems.append(f"the judge and the model under test have the same provider prefix '{j['provider_prefix']}'"
                        ", so they are one family")
    shared = sorted(set(j["families"] or ()) & set(t["families"] or ()))
    if shared:
        problems.append(f"the judge is of the family of the model under test ({', '.join(shared)})")
    return problems, j, t


# ---------- agreement statistics ----------

def kappa_exact(a, b):
    """Cohen's kappa of two equal lists of labels, as a Fraction, or None when the chance agreement is 1 (each rater
    gave one and the same label to every item, so kappa is not defined) or the lists are empty."""
    n = len(a)
    if n == 0 or n != len(b):
        return None
    po = Fraction(sum(1 for x, y in zip(a, b) if x == y), n)
    ca, cb = Counter(a), Counter(b)
    pe = sum(Fraction(ca[k] * cb[k], n * n) for k in set(ca) | set(cb))
    return None if pe == 1 else (po - pe) / (1 - pe)


def _r4(x):
    return None if x is None else round(float(x), 4)


def wilson95(c, n):
    """The Wilson 95% interval for c of n, rounded to 4 places (the interval of sec001), or None when n is 0."""
    return sec001._wilson95(c, n)


def _rate(c, n):
    return {"count": c, "n": n, "rate": _r4(Fraction(c, n)) if n else None, "wilson95": wilson95(c, n)}


def calibration_stats(data):
    """The agreement figures of a calibration file that `calibration_problems` passes."""
    labels = list(data["labels"])
    halves = {h: [s for s in data["sample"] if s["half"] == h] for h in ("tune", "held_out")}
    out = {"labels": labels, "bar": "kappa ≥ 0.80 for human-human and for judge-human, both on the held-out half; "
                                     "benchmark-set; FAIAS sets none"}
    for h, rows in halves.items():
        a, b, j = ([s[k] for s in rows] for k in ("human_a", "human_b", "judge"))
        ka, kb = kappa_exact(j, a), kappa_exact(j, b)
        kj = None if ka is None or kb is None else min(ka, kb)
        out[h] = {"n": len(rows), "human_human_kappa": _r4(kappa_exact(a, b)), "judge_human_a_kappa": _r4(ka),
                  "judge_human_b_kappa": _r4(kb), "judge_human_kappa": _r4(kj),
                  "judge_human_kappa_rule": "the lower of the judge against labeller A and against labeller B"}
    held = halves["held_out"]
    agreed = [s for s in held if s["human_a"] == s["human_b"]]
    per_class = {}
    for c in labels:
        pos = [s for s in agreed if s["human_a"] == c]
        neg = [s for s in agreed if s["human_a"] != c]
        per_class[c] = {"miss": _rate(sum(1 for s in pos if s["judge"] != c), len(pos)),
                        "false_alarm": _rate(sum(1 for s in neg if s["judge"] == c), len(neg))}
    out["held_out"]["per_class"] = per_class
    out["held_out"]["per_class_basis"] = (f"the {len(agreed)} held-out items on which the two labellers agree; "
                                          "miss: the judge gave another label to an item of the class; false alarm: "
                                          "the judge gave the class to an item of another class")
    hh = kappa_exact([s["human_a"] for s in held], [s["human_b"] for s in held])
    ja = kappa_exact([s["judge"] for s in held], [s["human_a"] for s in held])
    jb = kappa_exact([s["judge"] for s in held], [s["human_b"] for s in held])
    jh = None if ja is None or jb is None else min(ja, jb)
    out["meets_bar"] = hh is not None and jh is not None and hh >= KAPPA_BAR and jh >= KAPPA_BAR
    out["sample_size_note"] = ("the bar does not check the sample size; the counts of each half are above. A kappa "
                               "on a small sample has a wide interval")
    return out


def calibration_problems(data):
    """The problems in the form of a calibration file (empty if none)."""
    if not isinstance(data, dict):
        return ["the calibration file does not hold one JSON object"]
    problems = []
    if data.get("format") != CALIBRATION_FORMAT:
        problems.append(f"format must be '{CALIBRATION_FORMAT}'")
    if not runlog.HEX64.fullmatch(str(data.get("rubric_sha256"))):
        problems.append("rubric_sha256 must be the SHA-256 of the rubric file (64 lower-case hex characters)")
    if not (isinstance(data.get("judge_model_id"), str) and data["judge_model_id"].strip()):
        problems.append("judge_model_id must name the judge model whose labels the file holds")
    labels = data.get("labels")
    if not (isinstance(labels, list) and len(labels) >= 2 and len(set(map(str, labels))) == len(labels)
            and all(isinstance(x, str) and LABEL_FORM.fullmatch(x) for x in labels)):
        return problems + ["labels must be a list of two or more different labels (a-z, 0-9, _)"]
    sample = data.get("sample")
    if not isinstance(sample, list) or not sample:
        return problems + ["sample must be a list of labelled items"]
    seen = set()
    for n, s in enumerate(sample, 1):
        if not isinstance(s, dict):
            problems.append(f"sample item {n}: not an object")
            continue
        sid = s.get("id")
        if not (isinstance(sid, str) and sid.strip()):
            problems.append(f"sample item {n}: no id")
        elif sid in seen:
            problems.append(f"sample item {sid}: the id is repeated")
        seen.add(sid)
        if s.get("half") not in ("tune", "held_out"):
            problems.append(f"sample item {sid}: half must be 'tune' or 'held_out'")
        for k in ("human_a", "human_b", "judge"):
            if s.get(k) not in labels:
                problems.append(f"sample item {sid}: {k} is not one of the labels")
    for h in ("tune", "held_out"):
        if not any(isinstance(s, dict) and s.get("half") == h for s in sample):
            problems.append(f"the sample has no item in the {h} half")
    return problems


def _inside(path, repo):
    p, r = Path(path).resolve(), Path(repo).resolve()
    return p == r or r in p.parents


def read_calibration(path, sha256=None, repo=REPO):
    """(data, file SHA-256, problems). The file must be outside the repository, of the SHA-256 `sha256` when given,
    JSON with no repeated key, and pass calibration_problems."""
    if _inside(path, repo):
        return None, None, ["the calibration file is inside the repository; keep it outside until it is reviewed"]
    if not Path(path).is_file():
        return None, None, ["the calibration file is absent"]
    raw = Path(path).read_bytes()
    sha = runlog.sha256_hex(raw)
    if sha256 is not None and sha != sha256:
        return None, sha, ["the calibration file does not have the SHA-256 that the settings name"]
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey as e:
        return None, sha, [f"the calibration file is refused: {e}"]
    except ValueError:
        return None, sha, ["the calibration file is not UTF-8 JSON"]
    return data, sha, calibration_problems(data)


def calibration_block(js, rubric_sha256, repo=REPO):
    """The calibration block of a judge log header: calibrated only when every condition holds; else the reason."""
    def uncal(reason, sha=None, stats=None):
        return {"status": "uncalibrated", "reason": reason, "file_sha256": sha, "stats": stats}
    if not js.calibration_file:
        return uncal("no calibration file is named")
    if not js.calibration_sha256:       # validate_judge asks for it; a settings object built without validate_judge may not
        return uncal("the settings name a calibration file with no SHA-256 (calibration_sha256)")
    data, sha, problems = read_calibration(js.calibration_file, js.calibration_sha256, repo=repo)
    if problems:
        return uncal("; ".join(problems), sha)
    if data["rubric_sha256"] != rubric_sha256:
        return uncal("the calibration file names another rubric", sha)
    if data["judge_model_id"] != js.model.model_id:
        return uncal("the calibration file holds the labels of another judge model", sha)
    stats = calibration_stats(data)
    if not stats["meets_bar"]:
        return uncal("a kappa on the held-out half is below 0.80, or not defined", sha, stats)
    return {"status": "calibrated", "reason": "both kappas on the held-out half reach 0.80 (benchmark-set; FAIAS sets "
                                              "none)", "file_sha256": sha, "stats": stats}


# ---------- the judge settings and the rubric ----------

@dataclass(frozen=True)
class JudgeSettings:
    model: object               # settings.Settings of the judge model
    model_raw: dict
    rubric_file: str
    rubric_sha256: str
    judge_family: object = None
    target_family: object = None
    calibration_file: object = None
    calibration_sha256: object = None

    def canonical(self):
        m = self.model
        return {"model": {"provider": m.provider, "model_id": m.model_id, "mode": m.mode, "routing": m.routing(),
                          "price_in_per_m": m.price_in_per_m, "price_out_per_m": m.price_out_per_m,
                          "cap_usd": m.cap_usd, "api_key_env": m.api_key_env, "max_tokens": m.max_tokens,
                          "temperature": m.temperature, "seed": m.seed},
                "rubric_sha256": self.rubric_sha256, "judge_family": self.judge_family,
                "target_family": self.target_family, "calibration_sha256": self.calibration_sha256}

    def digest(self):
        return runlog.sha256_hex(runlog.canonical(self.canonical()))


def validate_judge(raw, repo=REPO):
    """The problems in a judge settings mapping (empty if none)."""
    if not isinstance(raw, dict):
        return ["the judge settings are not a mapping"]
    problems = [f"missing field '{k}'" for k in JUDGE_REQUIRED if k not in raw]
    problems += [f"unknown field '{k}'" for k in raw if k not in JUDGE_REQUIRED + JUDGE_OPTIONAL]
    model = raw.get("model")
    if "model" in raw:
        if not isinstance(model, dict):
            problems.append("model must be a mapping")
        else:
            problems += [f"model: '{k}' is not a key of a judge model (keys: {', '.join(MODEL_KEYS)})"
                         for k in model if k not in MODEL_KEYS and k not in settings_mod.FORBIDDEN]
            problems += [f"model: {p}" for p in settings_mod.validate(model) if p != M4_ONLY]
    if "rubric_file" in raw and not (isinstance(raw["rubric_file"], str) and raw["rubric_file"].strip()):
        problems.append("rubric_file must be a path")
    if "rubric_sha256" in raw and not runlog.HEX64.fullmatch(str(raw["rubric_sha256"])):
        problems.append("rubric_sha256 must be 64 lower-case hex characters")
    for k in ("judge_family", "target_family"):
        if raw.get(k) is not None and _family(raw[k]) is None:
            problems.append(f"{k} must be a non-empty text or null")
    cal, cal_sha = raw.get("calibration_file"), raw.get("calibration_sha256")
    if cal is not None:
        if not (isinstance(cal, str) and cal.strip()):
            problems.append("calibration_file must be a path or null")
        elif _inside(cal, repo):
            problems.append("the calibration file is inside the repository; keep it outside until it is reviewed")
        if not runlog.HEX64.fullmatch(str(cal_sha)):
            problems.append("calibration_file needs calibration_sha256 (64 lower-case hex characters)")
    elif cal_sha is not None:
        problems.append("calibration_sha256 is set but no calibration_file is named")
    return problems


def raw_of(js):
    """The judge settings mapping of a JudgeSettings, for validate_judge: the model block and each field that is set."""
    raw = {"model": dict(js.model_raw), "rubric_file": js.rubric_file, "rubric_sha256": js.rubric_sha256}
    raw.update({k: getattr(js, k) for k in JUDGE_OPTIONAL if getattr(js, k) is not None})
    return raw


def judge_from_raw(raw):
    model = dict(raw["model"])
    return JudgeSettings(model=settings_mod.from_raw(model), model_raw=model, rubric_file=raw["rubric_file"],
                         rubric_sha256=raw["rubric_sha256"], judge_family=raw.get("judge_family"),
                         target_family=raw.get("target_family"), calibration_file=raw.get("calibration_file"),
                         calibration_sha256=raw.get("calibration_sha256"))


def load_judge_settings(path, repo=REPO):
    path = Path(path)
    try:
        raw = yamlio.load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise settings_mod.SettingsError([f"cannot read {path.name}: {type(e).__name__}"])
    problems = validate_judge(raw, repo=repo)
    if problems:
        raise settings_mod.SettingsError(problems)
    return judge_from_raw(raw)


def read_rubric(js):
    """(rubric, problems). The rubric file is JSON with no repeated key: `labels` (two or more), `success_label` (the
    label that says the objective of the item was met) and `instruction` (the judge's system text). Its SHA-256 must be
    the one that the settings name."""
    p = Path(js.rubric_file)
    if not p.is_file():
        return None, ["the rubric file is absent"]
    raw = p.read_bytes()
    if runlog.sha256_hex(raw) != js.rubric_sha256:
        return None, ["the rubric file does not have the SHA-256 that the settings name (rubric_sha256)"]
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=jsonkeys.unique_keys)
    except jsonkeys.RepeatedKey as e:
        return None, [f"the rubric file is refused: {e}"]
    except ValueError:
        return None, ["the rubric file is not UTF-8 JSON"]
    if not isinstance(data, dict):
        return None, ["the rubric file does not hold one JSON object"]
    problems = []
    labels = data.get("labels")
    if not (isinstance(labels, list) and len(labels) >= 2 and len(set(map(str, labels))) == len(labels)
            and all(isinstance(x, str) and LABEL_FORM.fullmatch(x) for x in labels)):
        problems.append("labels must be a list of two or more different labels (a-z, 0-9, _)")
    elif set(labels) & {UNPARSED, NO_RESULT}:
        problems.append(f"labels may not hold '{UNPARSED}' or '{NO_RESULT}'")
    elif data.get("success_label") not in labels:
        problems.append("success_label must be one of the labels")
    if not (isinstance(data.get("instruction"), str) and data["instruction"].strip()):
        problems.append("instruction must be a non-empty text")
    return (None if problems else data), problems


def judge_messages(rubric, request, reply):
    labels = ", ".join(rubric["labels"])
    system = f"{rubric['instruction'].strip()}\nAnswer with exactly one of these labels and nothing else: {labels}."
    user = f"Request:\n{request if isinstance(request, str) else ''}\n\nReply:\n{reply}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_verdict(text, labels):
    """The label of a judge reply: its first line, without spaces, quotes, a full stop or a "label:" prefix, in lower
    case, when that is exactly one label; else UNPARSED. An empty reply is NO_RESULT."""
    text = (text or "").strip()
    if not text:
        return NO_RESULT
    first = text.splitlines()[0].strip().strip("\"'`*.").strip().casefold()
    first = re.sub(r"^label\s*:\s*", "", first).strip("\"'`*.").strip()
    return first if first in labels else UNPARSED


# ---------- the source log ----------

def source_model(h):
    """The model under test of a source log header: the pinned ID, or the declared ID of an external run."""
    m = h.get("model") or {}
    return m.get("pinned_id") or m.get("declared_id")


def source_problems(log_path, pubkey=None, repo=REPO, use_repo=True):
    """(problems, verify message, header). The source log must pass `verify` (signature and form) and be a run log."""
    log_path = Path(log_path)
    sig = log_path.with_name(log_path.name + ".sig")
    ok, msg = runlog.verify_log(log_path, sig, pubkey, repo=repo, use_repo=use_repo)
    if not ok:
        return [f"the source log does not pass verify: {msg.splitlines()[0]}"], msg, None
    h = runlog.read_log(log_path)[0]
    if h.get("schema") in NOT_SOURCES:
        return [f"the source log is of schema {h.get('schema')}, which holds no model replies"], msg, h
    return [], msg, h


def _run_key(r):
    return r.get("run", r.get("attempt"))


def plan(source_path):
    """The run records of the source log, each with its line number: (to judge, not to judge with the reason)."""
    to_judge, skip = [], []
    for n, r in enumerate(runlog.read_log(source_path), 1):
        if r.get("type") != "run":
            continue
        if isinstance(r.get("reply"), str) and r["reply"].strip():
            to_judge.append((n, r))
        elif "reply_sha256" in r and "reply" not in r:
            skip.append((n, r, "the source log holds the reply hash only"))
        else:
            skip.append((n, r, "the source log holds no reply text for this run (an empty reply or none)"))
    return to_judge, skip


def estimate(js, rubric, source_path):
    """The worst-case cost of the pass, in USD: each judge call at its estimated input and the full output limit."""
    from . import spike
    _, gw, _, _ = spike._srd1()
    to_judge, _ = plan(source_path)
    m = js.model
    tokens = sum(gw.estimate_tokens(judge_messages(rubric, r.get("request"), r["reply"])) for _, r in to_judge)
    return (tokens * m.price_in_per_m + len(to_judge) * m.max_tokens * m.price_out_per_m) / 1_000_000


# ---------- the pass ----------

def start_problems(js, source_path, *, sign_key, pubkey=None, repo=REPO, use_repo=True, env=None):
    """(problems, facts). Every check that comes before a file is written or a call is made."""
    problems, facts = [], {}
    # The settings again: a JudgeSettings built without validate_judge is checked here (the calibration file and its
    # hash, the model block). This also refuses a calibration file inside the repository.
    problems += validate_judge(raw_of(js), repo=repo)
    m = js.model
    for k in ("price_in_per_m", "price_out_per_m"):
        if getattr(m, k) is None:
            problems.append(f"model: settings.{k} is not set")
    if not isinstance(m.cap_usd, (int, float)) or isinstance(m.cap_usd, bool) or m.cap_usd <= 0:
        problems.append("model: settings.cap_usd is not set to a number above 0")
    if not isinstance(m.max_tokens, int) or isinstance(m.max_tokens, bool) or m.max_tokens < 1:
        problems.append("model: settings.max_tokens is not set to a whole number above 0")
    src, msg, h = source_problems(source_path, pubkey, repo=repo, use_repo=use_repo)
    problems += src
    facts["verify"] = msg
    if h is not None and not src:
        target_id = source_model(h)
        fam, judge_block, target_block = family_problems(js.model.model_id, js.judge_family, target_id,
                                                         js.target_family)
        problems += fam
        facts.update(header=h, judge_block=judge_block, target_block=target_block)
        to_judge, _ = plan(source_path)
        if not to_judge:
            seen = Counter(str(r.get("type")) for r in runlog.read_log(source_path))
            problems.append("the source log holds no reply text to judge (records by type: "
                            + ", ".join(f"{t} {n}" for t, n in sorted(seen.items()))
                            + "); the pass reads the reply text of records of type 'run' only")
    rubric, rp = read_rubric(js)
    problems += rp
    facts["rubric"] = rubric
    problems += runlog.key_problems(sign_key, repo=repo) if sign_key else ["no signing key is named"]
    if not (env if env is not None else os.environ).get(js.model.api_key_env):
        problems.append(f"the environment variable {js.model.api_key_env} is not set")
    return problems, facts


@dataclass
class JudgeResult:
    status: str
    log_path: Path
    sig_path: object
    summary: dict


def _host_entry(url):
    p = urlsplit(url)
    return p.hostname + (f":{p.port}" if p.port else "")


def build_header(js, facts, source_path, calibration, allow_hosts, confirmation, repo=REPO):
    h, rubric = facts["header"], facts["rubric"]
    proc = h.get("procedure") or {}
    m = js.model
    return {
        "type": "header", "schema": SCHEMA,
        "label": LABEL_CALIBRATED if calibration["status"] == "calibrated" else LABEL_UNCALIBRATED,
        "engine_version": __version__,
        "code": {"engine_version": __version__, "git_commit": runlog.git_commit(repo),
                 "judge_file": JUDGE_FILE, "judge_sha256": runlog.sha256_hex((Path(repo) / JUDGE_FILE).read_bytes()),
                 "judge_log_file": JUDGE_LOG_FILE,
                 "judge_log_sha256": runlog.sha256_hex((Path(repo) / JUDGE_LOG_FILE).read_bytes())},
        "primary_measure": PRIMARY,
        "source": {"file": Path(source_path).name, "log_sha256": runlog.sha256_hex(Path(source_path).read_bytes()),
                   "schema": h.get("schema"), "label": h.get("label"), "procedure": proc.get("tp"),
                   "star": proc.get("star"), "settings_hash": h.get("settings_hash"),
                   "verify": facts["verify"].splitlines()[0], **facts["target_block"]},
        "judge": {"provider": m.provider, "pinned_id": m.model_id, "mode": m.mode, "routing": m.routing(),
                  "prices_per_million_tokens": {"in": m.price_in_per_m, "out": m.price_out_per_m},
                  "cap_usd": m.cap_usd, "max_tokens": m.max_tokens, "temperature": m.temperature, "seed": m.seed,
                  "provider_prefix": facts["judge_block"]["provider_prefix"],
                  "families": facts["judge_block"]["families"],
                  "family_basis": facts["judge_block"]["family_basis"],
                  "family_rule": "the judge's families do not meet those of the model under test, and the provider "
                                 "prefixes differ (engine/judge.py, model_families)"},
        "rubric": {"file": Path(js.rubric_file).name, "sha256": js.rubric_sha256, "labels": rubric["labels"],
                   "success_label": rubric["success_label"], "id": rubric.get("id")},
        "calibration": calibration,
        "settings_hash": js.digest(),
        "egress": {"allow": sorted(allow_hosts), "limit": runlog.EGRESS_LIMIT},
        "limits": LIMITS, "owner_confirmation": confirmation,
    }


def run(js, source_path, *, out_dir, sign_key, confirmation, pubkey=None, base_url=None, env=None,
        clock_ns=time.time_ns, repo=REPO, use_repo=True):
    """The judge pass over the source log. Raises JudgeRefused, before any file is written and any call is made, on a
    start problem. Returns a JudgeResult. `base_url` and `env` are for tests."""
    from . import judge_log, spike
    problems, facts = start_problems(js, source_path, sign_key=sign_key, pubkey=pubkey, repo=repo, use_repo=use_repo,
                                     env=env)
    out_dir = Path(out_dir)
    if (out_dir / LOG_NAME).exists():
        problems.append(f"{LOG_NAME} exists in the output folder; the pass never overwrites a judge log")
    if problems:
        raise JudgeRefused(problems)
    _, gw, _, _ = spike._srd1()
    rubric = facts["rubric"]
    calibration = calibration_block(js, js.rubric_sha256, repo=repo)
    base_url = base_url or gw.OPENROUTER_BASE
    out_dir.mkdir(parents=True, exist_ok=True)
    elog = EgressLog(out_dir / "egress.jsonl")
    egress = EgressClient([_host_entry(base_url)], log=elog)
    gateway = gw.Gateway(js.model, egress, env=env, base_url=base_url)
    header = build_header(js, facts, source_path, calibration, egress.allow_text, confirmation, repo=repo)
    judge_log.lock_header(header, clock_ns())
    to_judge, skip = plan(source_path)
    log_path = out_dir / LOG_NAME
    log = runlog.RunLog(log_path)
    status, error, records = "complete", None, []
    hosts, mismatches = Counter(), 0
    try:
        log.write(header)
        runlog.wait_past_lock(header, clock_ns)
        for n, r, reason in skip:
            rec = {"type": "not_judged", "source_line": n, "item": r.get("item"), "run": _run_key(r),
                   "t_ns": clock_ns(), "reason": reason}
            log.write(rec)
        for n, r in to_judge:
            t_ns = clock_ns()
            try:
                res = gateway.complete(judge_messages(rubric, r.get("request"), r["reply"]))
            except CapReached:
                status = "stopped_at_cap"
            except Exception as e:      # noqa: BLE001 - only the class name is kept
                status, error = "stopped_error", type(e).__name__
            if status != "complete":
                log.write({"type": "judge_stopped", "source_line": n, "t_ns": t_ns, "reason": status, "error": error})
                break
            verdict = NO_RESULT if res.blocked else parse_verdict(res.reply, rubric["labels"])
            rec = {"type": "judgement", "source_line": n, "item": r.get("item"), "run": _run_key(r),
                   "t_ns": t_ns, "time": runlog.iso(t_ns),
                   "reply_sha256": runlog.sha256_hex(r["reply"]),
                   "deterministic": {"score": r.get("score"), "outcome": r.get("outcome")},
                   "verdict": verdict, "judge_reply": res.reply, "finish_reason": res.finish_reason,
                   "blocked": res.blocked, "served_host": res.served_host, "served_model": res.model_served,
                   "host_ok": res.host_ok, "prompt_tokens": res.prompt_tokens,
                   "completion_tokens": res.completion_tokens, "usage_missing": res.usage_missing,
                   "cost_usd": res.cost_usd, "latency_ms": res.latency_ms, "seed": res.seed, "retries": res.retries}
            log.write(rec)
            records.append(rec)
            hosts[str(res.served_host)] += 1
            mismatches += 1 if res.host_ok is False else 0
            if res.host_ok is False and js.model.mode == "evidence":
                status = "stopped_host_mismatch"
                log.write({"type": "judge_stopped", "source_line": n, "t_ns": t_ns, "reason": status, "error": None})
                break
        egress.close()
        summary = {"type": "summary", "label": header["label"], "status": status, "error": error,
                   **judge_log.counts(records, len(skip), rubric["success_label"]),
                   "cost_usd": round(gateway.cap.spent_usd, 8), "model_calls": gateway.cap.calls,
                   "cap_usd": js.model.cap_usd, "served_hosts": dict(hosts), "host_mismatches": mismatches,
                   "egress": {"file": elog.path.name, "sha256": runlog.sha256_hex(elog.path.read_bytes()),
                              "summary": elog.summary()},
                   "finished": runlog.iso()}
        log.write(summary)
    finally:
        log.close()
    sig = runlog.sign_log(log_path, sign_key)
    return JudgeResult(status, log_path, sig, summary)


# ---------- commands ----------

def main(argv=None):
    ap = argparse.ArgumentParser(prog="faias_harness.engine.judge",
                                 description="The judge pass over a signed run log, and its calibration figures.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("calibrate", help="print the agreement figures of a calibration file (no network call)")
    c.add_argument("--calibration", required=True)
    c.add_argument("--sha256")
    r = sub.add_parser("run", help="run the judge pass (model calls; asks for yes)")
    r.add_argument("--settings", required=True)
    r.add_argument("--log", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--sign-key", required=True)
    r.add_argument("--pubkey")
    r.add_argument("--no-repo", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "calibrate":
        data, sha, problems = read_calibration(args.calibration, args.sha256)
        if problems:
            print("refused: " + "; ".join(problems))
            return 2
        stats = calibration_stats(data)
        print(f"calibration file sha256: {sha}")
        print(json.dumps(stats, indent=2, ensure_ascii=False))
        print("bar: " + ("met" if stats["meets_bar"] else "not met") + " (kappa ≥ 0.80 human-human and judge-human on "
              "the held-out half; benchmark-set; FAIAS sets none)")
        return 0
    try:
        js = load_judge_settings(args.settings)
    except settings_mod.SettingsError as e:
        print("refused: " + "; ".join(e.problems))
        return 2
    problems, facts = start_problems(js, args.log, sign_key=args.sign_key, pubkey=args.pubkey,
                                     use_repo=not args.no_repo)
    if problems:
        print("refused: " + "; ".join(problems))
        return 2
    from . import spike
    spike._srd1()
    to_judge, skip = plan(args.log)
    cal = calibration_block(js, js.rubric_sha256)
    print(f"source log: {args.log}\n{facts['verify'].splitlines()[0]}")
    print(f"model under test: {facts['target_block']['model_id']} (families {facts['target_block']['families']})")
    print(f"judge: {js.model.model_id} (families {facts['judge_block']['families']}); cap {js.model.cap_usd} USD")
    print(f"replies to judge: {len(to_judge)}; not judged: {len(skip)}")
    print(f"worst-case cost: {estimate(js, facts['rubric'], args.log):.6f} USD")
    print(f"calibration: {cal['status']} ({cal['reason']})")
    print("label: " + (LABEL_CALIBRATED if cal["status"] == "calibrated" else LABEL_UNCALIBRATED))
    typed = input("Type yes to start the judge pass: ").strip()
    if typed != "yes":
        print("not started: yes was not typed")
        return 4
    try:
        res = run(js, args.log, out_dir=args.out, sign_key=args.sign_key, pubkey=args.pubkey,
                  confirmation={"typed": "yes", "time": runlog.iso()}, use_repo=not args.no_repo)
    except JudgeRefused as e:
        print("refused: " + "; ".join(e.problems))
        return 2
    print(f"status: {res.status}\njudge log: {res.log_path}\nsignature: {res.sig_path}")
    return 0 if res.status == "complete" else 5


if __name__ == "__main__":
    sys.exit(main())
