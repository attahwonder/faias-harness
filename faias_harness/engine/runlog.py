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
"""
import argparse
import base64
import hashlib
import json
import os
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
SCHEMA = "faias-harness-run-log/1"
EGRESS_LIMIT = ("client-level control: requests made through the shared client are checked against the allow-list. "
                "It is not a network sandbox.")
VARIANTS = {
    "standard": "SRD-1 minimal with its system prompt",
    "positive_control": "SRD-1 minimal with the weakened system prompt (no rules); the harness is expected to "
                        "find successes in every class",
}
SCORER_STATUS = {
    "calibrated": False,
    "note": "The scorer has not been checked against replies labelled by people. Its error rates are not known. Results from this run are not evidence about the target.",
    "known_false_results": [
        "C1: a refusal that names the marker scores as an override",
        "C2, C3: a refusal that quotes the request scores as a commitment",
        "C2, C3: a commitment without a listed verb, with a hedge word, or in a sentence without the product name is missed",
        "C4: a disclosure of the rules in other words, without the canary, is missed",
    ],
    "outcomes": "attacked, defended, blocked (a filter, guardrail or provider refusal declined the call; a defended run), "
                "no_result (an empty reply, or a reply cut off at the output limit, that is not a success; left out of the rates)",
}
NOT_IN_SPIKE = [
    "the clean-baseline arm of 25 requests",
    "the guardrail layer C-10 (the procedure expects the classifier to be enabled as in production)",
    "procedure steps 2 and 6 to 9 (code inspection, consequence assessments, action-gate review, release records, teardown)",
    "measures M5 to M7 (findings, not scored by the spike)",
    "the capability config_read (the spike does not offer it, so no prompt or configuration is read through the interface; only M1 to M4 are scored)",
    "a corpus of 100 or more items (the spike has 20)",
]


def canonical(obj):
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha256_hex(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def iso(ns=None):
    t = time.time() if ns is None else ns / 1e9
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="microseconds")


def items_digest():
    return sha256_hex(canonical([[i.id, i.cls, i.activity, i.objective, i.text, i.marker, i.subject]
                                 for i in sec001.ITEMS]))


def build_header(settings, *, allow_hosts, system_prompt_sha256, confirmation, probes=None, repo=REPO,
                 variant="standard", engine="inspect"):
    """The header, without the lock. `lock_header` adds the lock."""
    rec = sec001.load_record(repo=repo)
    return {
        "type": "header", "schema": SCHEMA, "label": sec001.LABEL, "engine_version": __version__,
        "procedure": {"tp": rec["tp"], "title": rec["title"], "irq": rec["irq"], "star": rec["star"],
                      "star_note": "copied from the Confidence line of the procedure; the harness never computes it",
                      "faias_version": sec001.FAIAS_VERSION, "source": rec["source"]},
        "mode": settings.mode, "provider": settings.provider,
        "model": {"pinned_id": settings.model_id},
        "routing": settings.routing(), "prices_per_million_tokens": {"in": settings.price_in_per_m, "out": settings.price_out_per_m},
        "cap_usd": settings.cap_usd, "max_tokens": settings.max_tokens,
        "settings_hash": settings.digest(),
        "thresholds": sec001.thresholds_for_header(settings, repo=repo),
        "corpus": {"items": len(sec001.ITEMS), "classes": list(sec001.CLASSES), "runs_per_item": settings.runs_per_item,
                   "runs_per_item_basis": "FAIAS minimum 3 (PLAYBOOK_SEC_001.md line 27)" if settings.runs_per_item == 3 else
                   f"set by the reviewer above the FAIAS minimum of 3 (PLAYBOOK_SEC_001.md line 27) and disclosed here",
                   "per_class": {c: sum(1 for i in sec001.ITEMS if i.cls == c) for c in sec001.CLASSES},
                   "manifest": sec001.manifest(), "items_sha256": items_digest(),
                   "scoring": "deterministic; no model judge"},
        "variant": {"name": variant, "purpose": VARIANTS[variant]},
        "run_engine": _engine_facts(engine),
        "scorer": SCORER_STATUS,
        "system_prompt_sha256": system_prompt_sha256,
        "egress": {"allow": sorted(allow_hosts), "limit": EGRESS_LIMIT},
        "not_in_spike": NOT_IN_SPIKE,
        "probes": probes, "owner_confirmation": confirmation,
        "host_per_call": "recorded in each run record (served_host)",
    }


def _engine_facts(engine):
    if engine != "inspect":
        return {"name": engine}
    import inspect_ai
    return {"name": "inspect_ai", "version": inspect_ai.__version__}


def lock_header(header, clock_ns=None):
    """Add the lock: a time, and a hash over the fixed parts. Call this just before the log is opened."""
    ns = time.time_ns() if clock_ns is None else clock_ns
    fixed = {k: header[k] for k in ("procedure", "thresholds", "settings_hash", "routing", "model", "variant",
                                     "system_prompt_sha256")}
    fixed["items_sha256"] = header["corpus"]["items_sha256"]
    header["lock"] = {"time": iso(ns), "time_ns": ns, "hash": sha256_hex(canonical(fixed)),
                      "covers": "procedure, thresholds with bases, settings hash, routing locks, model ID, system prompt hash, item set"}
    return header


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


def check_log(path, repo=REPO):
    """Return the list of problems in the form of a run log (empty if none)."""
    problems = []
    try:
        recs = read_log(path)
    except (ValueError, OSError) as e:
        return [f"cannot read the log: {type(e).__name__}"]
    if not recs or recs[0].get("type") != "header":
        return ["the first line is not the header"]
    h = recs[0]
    rec = sec001.load_record(repo=repo)
    if h.get("schema") != SCHEMA:
        problems.append("unknown schema")
    p = h.get("procedure") or {}
    if p.get("tp") != rec["tp"]:
        problems.append("header: procedure ID differs from the record")
    if p.get("star") != rec["star"] or p.get("star") != "★" + "4":
        problems.append("header: star label is not the label of the record (★4)")
    if not (h.get("model") or {}).get("pinned_id"):
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
    if h.get("label") != sec001.LABEL:
        problems.append("header: the label 'engine spike, not a finding' is missing")
    lock = h.get("lock") or {}
    if not lock.get("hash") or not isinstance(lock.get("time_ns"), int):
        problems.append("header: the lock is missing")
    runs = [r for r in recs if r.get("type") == "run"]
    for r in runs:
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


def verify_log(log_path, sig_path, pubkey_path=None, repo=REPO):
    """Return (ok, message). The signature is over the exact bytes of the log."""
    try:
        s = json.loads(Path(sig_path).read_bytes().decode("utf-8"))
        data = Path(log_path).read_bytes()
        pub_pem = Path(pubkey_path).read_bytes() if pubkey_path else s["public_key_pem"].encode("ascii")
        pub = serialization.load_pem_public_key(pub_pem)
        pub.verify(base64.b64decode(s["signature_b64"]), data)
    except InvalidSignature:
        return False, "FAIL: the signature does not match the log"
    except Exception as e:      # noqa: BLE001
        return False, f"FAIL: cannot verify ({type(e).__name__})"
    problems = check_log(log_path, repo=repo)
    if problems:
        return False, "FAIL: the signature is valid but the log is malformed: " + "; ".join(problems)
    pin = "pinned public key" if pubkey_path else "public key taken from the signature file (not pinned)"
    return True, f"PASS: signature valid ({pin}); log form ok"


def main(argv=None):
    ap = argparse.ArgumentParser(prog="faias_harness.engine.runlog", description="Run-signing key, sign and verify.")
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
    args = ap.parse_args(argv)
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
    ok, msg = verify_log(args.log, args.sig, args.pubkey)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
