"""The judge log (schema `faias-judge-log/1`): the log of a judge pass (engine/judge.py).

Line 1 is the header. It holds the label, the code block (judge.py and this file, each with its SHA-256), the primary
measure, the source block (the file name, SHA-256, schema, label, procedure, star and settings hash of the source log,
its verify line, and the model under test with its families and their basis), the judge block (the pinned judge
model, its routing locks, prices, cap, decoding, families and their basis), the rubric block (file name, SHA-256,
labels, success label), the calibration block (status, reason, file SHA-256 and figures), the settings hash, the
egress allow-list and the lock. The lock hash covers each of these blocks.

Then one `not_judged` record for each run record of the source log that holds no reply text, one `judgement` record
for each reply sent to the judge (the line of the source log, item, run, the SHA-256 of the judged reply, the
deterministic score and outcome as the source log holds them, the verdict, the judge's reply and the facts of the
call), a `judge_stopped` record when the pass stopped, and a `summary` record at the end.

`runlog.check_log`, `verify` (with or without --no-repo) and `rescore` hand a log of this schema here. `check` reads
nothing from the checkout, so `--no-repo` changes nothing. A judge log is a second reading, so it is not rescored.
"""
from collections import Counter

from . import judge, runlog

LOCK_KEYS = ("schema", "label", "code", "primary_measure", "source", "judge", "rubric", "calibration", "settings_hash",
             "egress")
LOCK_COVERS = ("label, code (judge and judge log file hashes), primary measure, source log (SHA-256, procedure, model "
               "under test and its families), judge model (routing locks, prices, cap, decoding, families), rubric "
               "(SHA-256, labels), calibration (status, file SHA-256, figures), settings hash, egress allow-list")
RECORD_TYPES = ("judgement", "not_judged", "judge_stopped")


def lock_fixed(h):
    return {k: h.get(k) for k in LOCK_KEYS}


def lock_header(header, clock_ns):
    header["lock"] = {"time": runlog.iso(clock_ns), "time_ns": clock_ns,
                      "hash": runlog.sha256_hex(runlog.canonical(lock_fixed(header))), "covers": LOCK_COVERS}
    return header


def counts(records, not_judged, success_label):
    """The counts of a summary from the judgement records: the verdicts, the table of the deterministic `success`
    against the verdict, and the agreement count where both are known."""
    verdicts = Counter(r["verdict"] for r in records)
    table = {}
    agree = disagree = 0
    for r in records:
        success = ((r.get("deterministic") or {}).get("score") or {}).get("success")
        key = "none" if not isinstance(success, bool) else str(success).lower()
        row = table.setdefault(key, {})
        row[r["verdict"]] = row.get(r["verdict"], 0) + 1
        if isinstance(success, bool) and r["verdict"] not in (judge.UNPARSED, judge.NO_RESULT):
            if (r["verdict"] == success_label) == success:
                agree += 1
            else:
                disagree += 1
    return {"judged": len(records), "not_judged": not_judged, "verdicts": dict(sorted(verdicts.items())),
            "cross_table": {k: dict(sorted(v.items())) for k, v in sorted(table.items())},
            "agreement": {"compared": agree + disagree, "agree": agree, "disagree": disagree,
                          "note": "a count of the deterministic score against the judge verdict on this log; it is "
                                  "not a calibration, and it does not change the score"}}


def _calibration_problems(h):
    c = h.get("calibration") or {}
    out = []
    if c.get("status") not in ("calibrated", "uncalibrated") or not c.get("reason"):
        return ["header: the calibration block has no status or reason"]
    want = judge.LABEL_CALIBRATED if c["status"] == "calibrated" else judge.LABEL_UNCALIBRATED
    if h.get("label") != want:
        out.append(f"header: the label is not '{want}' for a calibration status of {c['status']}")
    if c["status"] == "calibrated":
        stats = c.get("stats") or {}
        held = stats.get("held_out") or {}
        if not runlog.HEX64.fullmatch(str(c.get("file_sha256"))):
            out.append("header: a calibrated judge log has no calibration file SHA-256")
        for k in ("human_human_kappa", "judge_human_kappa"):
            v = held.get(k)
            if not (isinstance(v, (int, float)) and not isinstance(v, bool) and v >= float(judge.KAPPA_BAR)):
                out.append(f"header: a calibrated judge log needs {k} of 0.80 or more on the held-out half")
        if stats.get("meets_bar") is not True:
            out.append("header: a calibrated judge log has figures that do not meet the bar")
    return out


def check(recs, repo=None, use_repo=True):
    """The problems in the form of a judge log (empty if none)."""
    h = recs[0]
    problems = []
    if h.get("schema") != judge.SCHEMA:
        problems.append("header: the schema is not the judge log schema")
    if h.get("label") not in judge.LABELS:
        problems.append("header: the label is not a judge log label")
    if h.get("primary_measure") != judge.PRIMARY:
        problems.append("header: the primary measure is not the deterministic score of the source log")
    c = h.get("code") or {}
    for k in ("judge_sha256", "judge_log_sha256"):
        if not runlog.HEX64.fullmatch(str(c.get(k))):
            problems.append(f"header: the code block has no {k}")
    problems += _calibration_problems(h)
    s, j, rub = h.get("source") or {}, h.get("judge") or {}, h.get("rubric") or {}
    if not runlog.HEX64.fullmatch(str(s.get("log_sha256"))):
        problems.append("header: the source block has no log SHA-256")
    if not s.get("procedure") or not s.get("star") or not s.get("model_id"):
        problems.append("header: the source block has no procedure, star or model under test")
    if not j.get("pinned_id") or not j.get("cap_usd"):
        problems.append("header: the judge block has no pinned model or cap")
    sf, jf = s.get("families"), j.get("families")
    if not (isinstance(sf, list) and sf and isinstance(jf, list) and jf):
        problems.append("header: the families of the judge or of the model under test are missing")
    elif set(sf) & set(jf):
        problems.append("header: the judge is of the family of the model under test")
    if j.get("provider_prefix") and j.get("provider_prefix") == s.get("provider_prefix"):
        problems.append("header: the judge and the model under test have the same provider prefix")
    if not runlog.HEX64.fullmatch(str(rub.get("sha256"))):
        problems.append("header: the rubric block has no SHA-256")
    labels = rub.get("labels") if isinstance(rub.get("labels"), list) else []
    if len(labels) < 2 or rub.get("success_label") not in labels:
        problems.append("header: the rubric block has no labels or success label")
    if not h.get("settings_hash"):
        problems.append("header: the settings hash is missing")
    lock = h.get("lock") or {}
    if not lock.get("hash") or not isinstance(lock.get("time_ns"), int):
        problems.append("header: the lock is missing")
    elif lock["hash"] != runlog.sha256_hex(runlog.canonical(lock_fixed(h))):
        problems.append("header: the lock hash does not match the fixed parts of the header")
    body = recs[1:-1] if recs[-1].get("type") == "summary" else recs[1:]
    verdicts = set(labels) | {judge.UNPARSED, judge.NO_RESULT}
    records = []
    for r in body:
        where = f"{r.get('type')} at source line {r.get('source_line')}"
        if r.get("type") not in RECORD_TYPES:
            problems.append(f"a record of type {r.get('type')} is not a judge log record")
            continue
        if not isinstance(r.get("t_ns"), int) or r["t_ns"] <= lock.get("time_ns", 0):
            problems.append(f"{where}: time is not later than the lock time")
        if r["type"] == "judgement":
            records.append(r)
            if r.get("verdict") not in verdicts:
                problems.append(f"{where}: the verdict is not a rubric label, {judge.UNPARSED} or {judge.NO_RESULT}")
            if not runlog.HEX64.fullmatch(str(r.get("reply_sha256"))) or "deterministic" not in r:
                problems.append(f"{where}: the reply SHA-256 or the deterministic score is missing")
            if "served_host" not in r or "served_model" not in r:
                problems.append(f"{where}: served host or served model is not recorded")
        elif r["type"] == "not_judged" and not r.get("reason"):
            problems.append(f"{where}: no reason")
    if recs[-1].get("type") != "summary":
        return problems + ["the last record is not the summary (the pass did not finish cleanly)"]
    summ = recs[-1]
    if summ.get("label") != h.get("label"):
        problems.append("summary: the label differs from the header")
    want = counts(records, sum(1 for r in body if r.get("type") == "not_judged"), rub.get("success_label"))
    for k in ("judged", "not_judged", "verdicts", "cross_table", "agreement"):
        if summ.get(k) != want[k]:
            problems.append(f"summary: {k} does not follow from the records")
    return problems


def summary_line(recs):
    n = sum(1 for r in recs if r.get("type") == "judgement")
    return (f"judgement records: {n} (a judge log has no item set; the source log is named by its SHA-256 "
            f"{(recs[0].get('source') or {}).get('log_sha256')})")
