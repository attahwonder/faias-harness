"""Tests of the run log of TP-MOD-011 (faias_harness/engine/mod011_log.py, schema 11): the lock, the form check with one
seeded fault for each rule, the rescore from the store file, the label and the dispatch in runlog.py.

The log is the one that the engine wrote in the shared run of tests/test_mod011_run.py (the local stand-in model host,
the items of items/TP-MOD-011.json and the neutral fixture comparators). Each fault is made on a copy. No live call, no
outside network, no key in any file.
"""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path

from faias_harness.engine import mod011, mod011_log, runlog
from tests.test_mod011_run import TRACE_SENTENCE, records, shared_run

REPO = Path(__file__).resolve().parents[1]


def sha(text):
    return runlog.sha256_hex(text)


def good():
    return copy.deepcopy(records(shared_run()[0]))


def store_lines():
    path = shared_run()[0].run_dir / mod011_log.STORE_FILE
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_log(recs, path):
    """The log as the engine writes it (runlog.RunLog), for the commands of runlog.py."""
    log = runlog.RunLog(path)
    for r in recs:
        log.write(r)
    log.close()
    return path


def write_store(lines, path):
    path.write_text("".join((x if isinstance(x, str) else runlog.canonical(x)) + "\n" for x in lines),
                    encoding="utf-8")
    return path


def relock(h):
    return runlog.lock_header(h, h["lock"]["time_ns"])


def runs(recs, arm=None, cls=None):
    return [r for r in recs if r.get("type") == "run" and arm in (None, r["arm"]) and cls in (None, r["class"])]


def seeded_faults(recs):
    """(name, change, changes the header, expected phrases) for the complete schema 11 log `recs`."""
    h = recs[0]
    lock_ns = h["lock"]["time_ns"]
    bench_id = runs(recs, "benchmark")[0]["item"]

    def first(arm, cls=None, field=None):
        return lambda rs: field(runs(rs, arm, cls)[0])

    def duplicate(rs):
        rs.insert(-1, copy.deepcopy(runs(rs, "benchmark")[0]))

    def drop_comparator_item(rs):
        item = runs(rs, "comparator")[0]["item"]
        rs[:] = [r for r in rs if not (r.get("type") == "run" and r["arm"] == "comparator" and r["item"] == item)]

    def drop_passage(rs):
        ins = rs[0]["instrument"]
        ins["passages"].pop()
        ins["summary"] = mod011.instrument_summary([p["class"] for p in ins["passages"]])

    def short_comparator_row(rs):
        corpus = rs[0]["corpus"]
        [m for m in corpus["manifest"] if m["arm"] == "comparator"][0]["remainder_words"] = (
            mod011.MIN_REMAINDER_WORDS - 1)
        corpus["items_sha256"] = mod011_log.items_digest(corpus["manifest"], corpus["scorer_keys"])

    def bench_hash_from_the_log(rs):
        corpus, new = rs[0]["corpus"], sha("another prefix")
        corpus["scorer_keys"][f"benchmark:{bench_id}"]["prefix_sha256"] = new
        corpus["items_sha256"] = mod011_log.items_digest(corpus["manifest"], corpus["scorer_keys"])
        for r in runs(rs, "benchmark"):
            if r["item"] == bench_id:
                r["prefix_sha256"] = new

    return [
        ("a comparator record that holds the reply", first("comparator", field=lambda r: r.update(reply="x")), False,
         ["a comparator record holds the reply (it holds reply_sha256 only)"]),
        ("a benchmark record without its reply", first("benchmark", field=lambda r: r.pop("reply")), False,
         ["a benchmark record must hold the reply with store_replies full, and only then"]),
        ("a key outside the frame", first("comparator", field=lambda r: r.update(request="text")), False,
         ["holds a key that is not in the frame of a run record (request)"]),
        ("a reply that is not its hash", first("benchmark", "exact", lambda r: r.update(reply=r["reply"] + " x")),
         False, ["the reply does not have the reply hash"]),
        ("a reply hash that is missing", first("comparator", field=lambda r: r.update(reply_sha256="abc")), False,
         ["the reply hash is missing"]),
        ("an item not in the manifest", first("benchmark", field=lambda r: r.update(item="gsm8k-test-x")), False,
         ["the item is not in the manifest of its arm", "the benchmark arm has 149 of 150 records"]),
        ("a second record for an attempt", duplicate, False, ["a second record for this arm, item and attempt"]),
        ("an attempt out of range", first("benchmark", field=lambda r: r.update(attempt=4)), False,
         ["the attempt number is not between 1 and runs_per_item", "the benchmark arm has 149 of 150 records"]),
        ("a class not of the scorer form", first("benchmark", "exact", lambda r: r.update(rouge_l=None)), False,
         ["the class, the ROUGE-L value or the longest run is not of the form of score_completion"]),
        ("a time at the lock", first("comparator", field=lambda r: r.update(t_ns=lock_ns)), False,
         ["time is not later than the lock time"]),
        ("a call fact missing", first("comparator", field=lambda r: r.pop("served_host")), False,
         ["served_host missing"]),
        ("a prefix hash that differs", first("benchmark", field=lambda r: r.update(prefix_sha256=sha("x"))), False,
         ["the benchmark or a hash differs from the manifest"]),
        ("M1 missing", lambda rs: rs[-1]["measures"]["M1"].pop("gsm8k"), False,
         ["summary: M1 is missing for a benchmark"]),
        ("M1 without its secondary measure", lambda rs: rs[-1]["measures"]["M1"]["gsm8k"].pop("secondary"),
         False, ["summary: M1 is missing for a benchmark"]),
        ("M1 without its comparator reading",
         lambda rs: rs[-1]["measures"]["M1"]["gsm8k"].pop("comparator_reading"), False,
         ["summary: M1 is missing for a benchmark"]),
        ("an arm short while complete", drop_comparator_item, False,
         ["summary: the status is complete, but the comparator arm has 147 of 150 records"]),
        ("no summary", lambda rs: rs.pop(), False, ["the last record is not the summary"]),
        ("a reading left out", lambda rs: rs[0]["readings"].pop(), True,
         ["a reading is missing from the readings block: the chance basis of line 18, reading (b)"]),
        ("a not_in_run line left out", lambda rs: rs[0]["not_in_run"].pop(0), True,
         ["a line is missing from not_in_run: step 1, the inventory of the evaluation evidence"]),
        ("another label", lambda rs: rs[0].update(label=mod011_log.LABEL_BASE), True,
         ["header: the label 'uncalibrated match rule, not a finding; absence of reproduction"]),
        ("an instrument summary that differs", lambda rs: rs[0]["instrument"]["summary"].update(exact_rate=1.0), True,
         ["header: the instrument summary does not match the class of each passage"]),
        ("an instrument reply that is not its hash",
         lambda rs: rs[0]["instrument"]["passages"][0].update(reply="x"), True,
         ["header: the instrument block (the file, its SHA-256, the data set and the class and reply"]),
        ("a comparator row with a remainder under the limit", short_comparator_row, True,
         ["header: the manifest rows comparator:C-00 have no valid arm, benchmark, pair, cut, word counts"]
         + [f"run comparator/C-00/{n}: the item is not in the manifest of its arm" for n in (1, 2, 3)]),
        ("an instrument passage left out", drop_passage, True,
         ["header: the instrument block (the file, its SHA-256, the data set and the class and reply"]),
        ("a comparator count that differs", lambda rs: rs[0]["comparators"].update(count={"gsm8k": 49}), True,
         ["header: the comparators block"]),
        ("a probe template that differs", lambda rs: rs[0]["probe"].update(template="x"), True,
         ["header: the probe block"]),
        ("a decode k that differs", lambda rs: rs[0]["decode"].update(k=4), True, ["header: the decode block"]),
        ("a store block that differs", lambda rs: rs[0]["store"].update(note="x"), True,
         ["header: the store block is not the schema 11 store block"]),
        ("no log module hash", lambda rs: rs[0]["code"].pop("log_sha256"), True,
         ["header: the code block does not name the TP-MOD-011 scorer, record and log module"]),
        ("no SME record hash in evidence mode", lambda rs: rs[0]["comparators"].update(sme_record_sha256=None), True,
         ["header: evidence mode: the comparators block has no SHA-256 of the SME record"]),
        ("no tau reason in evidence mode", lambda rs: rs[0]["match_rule"]["tau"].update(reason=None), True,
         ["header: evidence mode: tau has no value or no reason"]),
        ("no M1 bar reason in evidence mode", lambda rs: rs[0]["thresholds"]["M1"].update(value_reason=None), True,
         ["header: evidence mode: the M1 bar has no value or no reason"]),
        ("no cited block", lambda rs: rs[0].pop("cited"), True, ["header: the cited block is missing"]),
        ("a self-test of other code", lambda rs: rs[0]["isolation"]["selftest"].update(code_manifest_match=False),
         True, ["header: isolation.selftest is missing or incomplete"]),
        ("another variant", lambda rs: rs[0]["variant"].update(name="standard"), True,
         ["header: the variant is not model_probe"]),
        ("a benchmarks block that differs", lambda rs: rs[0]["benchmarks"]["gsm8k"]["selected"].pop(), True,
         ["header: the benchmarks block"]),
        ("a benchmark item that is not the item of the checkout", bench_hash_from_the_log, True,
         [f"header: the benchmark item {bench_id} differs from the item file in the repository"]),
        ("an instrument hash that is not the file of the checkout",
         lambda rs: rs[0]["instrument"].update(sha256=sha("another file")), True,
         ["header: the instrument hash differs from items/TP-MOD-011-instrument.json in the repository"]),
        ("a header change after the lock", lambda rs: rs[0].update(settings_hash=sha("x")), False,
         ["header: the lock hash does not match the fixed parts of the header"]),
    ]


# The faults that the check without the repository does not see: it reads no item file and no instrument file.
REPO_ONLY = ("a benchmark item that is not the item of the checkout",
             "an instrument hash that is not the file of the checkout")


class CheckTests(unittest.TestCase):
    def test_the_good_log_passes_with_and_without_the_repository(self):
        recs = good()
        self.assertEqual(mod011_log.check(recs, repo=REPO), [])
        self.assertEqual(mod011_log.check(recs, repo=REPO, use_repo=False), [])

    def test_each_seeded_fault_gives_its_problems(self):
        faults = seeded_faults(good())
        self.assertEqual(len(faults), 39)
        for name, change, in_header, expected in faults:
            with self.subTest(fault=name):
                changed = good()
                change(changed)
                if in_header:
                    relock(changed[0])
                problems = mod011_log.check(changed, repo=REPO)
                self.assertEqual(len(problems), len(expected), problems)
                for want, got in zip(expected, problems):
                    self.assertIn(want, got)
                if name in REPO_ONLY:
                    self.assertEqual(mod011_log.check(changed, repo=REPO, use_repo=False), [])

    def test_runlog_hands_a_schema_11_log_to_this_module(self):
        self.assertEqual(runlog.SCHEMA_MOD011, mod011_log.SCHEMA)
        self.assertIn(mod011_log.SCHEMA, runlog.CODE_SCHEMAS)
        self.assertIn("model_probe", runlog.VARIANTS)
        h = good()[0]
        fixed = runlog.lock_fixed(h)
        self.assertEqual({k: fixed[k] for k in mod011_log.lock_extra(h)}, mod011_log.lock_extra(h))
        with tempfile.TemporaryDirectory() as tmp:
            path = write_log(good(), Path(tmp) / "run_log.jsonl")
            self.assertEqual(runlog.check_log(path, repo=REPO), [])
            self.assertTrue(runlog._no_repo_lines(path).startswith(
                "items hash: matches (the manifest and the scorer keys in the log); comparator text is checked by hash "
                "only"))
            changed = good()
            changed[0]["cited"]["declared"] = []        # a block that only lock_extra covers
            self.assertEqual(runlog.check_log(write_log(changed, Path(tmp) / "changed.jsonl"), repo=REPO),
                             ["header: the lock hash does not match the fixed parts of the header"])


class LockTests(unittest.TestCase):
    CHANGES = {
        "the selected benchmark items": lambda h: h["benchmarks"]["gsm8k"]["selected"].reverse(),
        "the comparator file hash": lambda h: h["comparators"].update(file_sha256=sha("another file")),
        "the writer families": lambda h: h["comparators"].update(writer_families=["family-w", "family-v"]),
        "the temperature": lambda h: h["decode"].update(temperature=0.0),
        "the prompt hash": lambda h: h["probe"].update(prompt_sha256=sha("another prompt")),
        "tau": lambda h: h["match_rule"]["tau"].update(value=0.9),
        "the cited benchmarks": lambda h: h["cited"].update(declared=[]),
        "a reading": lambda h: h["readings"].pop(),
        "a not_in_run line": lambda h: h["not_in_run"].pop(),
        "an instrument reply": lambda h: h["instrument"]["passages"][0].update(reply="x"),
        "the store block": lambda h: h["store"].update(benchmark_replies="hash_only"),
        "the log module hash": lambda h: h["code"].update(log_sha256=sha("another module")),
        "the self-test log hash": lambda h: h["isolation"]["selftest"].update(sha256=sha("another self-test")),
    }

    def test_each_covered_part_changes_the_locked_part(self):
        h = good()[0]
        fixed = runlog.canonical(mod011_log.lock_extra(h))
        for name, change in self.CHANGES.items():
            with self.subTest(name):
                changed = copy.deepcopy(h)
                change(changed)
                self.assertNotEqual(runlog.canonical(mod011_log.lock_extra(changed)), fixed)
        other = copy.deepcopy(h)
        other.update(probes={"note": "outside the lock"}, owner_confirmation={"typed": "no"})
        self.assertEqual(runlog.canonical(mod011_log.lock_extra(other)), fixed)

    def test_the_lock_names_what_it_covers(self):
        h = good()[0]
        self.assertTrue(h["lock"]["covers"].startswith(mod011_log.LOCK_COVERS))
        for words in ("benchmarks", "comparators", "decode", "probe", "match rule", "cited benchmarks", "readings",
                      "not_in_run", "instrument check", "log module hash", "self-test log hash", "reply storage"):
            self.assertIn(words, mod011_log.LOCK_COVERS)


class RescoreTests(unittest.TestCase):
    """rescore --replies reads the store file: 0 differences on the store of the run; each changed store line is
    refused and named."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.log = write_log(good(), self.dir / "run_log.jsonl")

    def rescore(self, lines=None, recs=None):
        log = self.log if recs is None else write_log(recs, self.dir / "changed_log.jsonl")
        store = write_store(store_lines() if lines is None else lines, self.dir / "store.jsonl")
        return runlog.rescore_log(log, repo=REPO, replies=str(store))

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runlog.main(list(argv))
        return code, out.getvalue()

    def test_the_store_of_the_run_rescores_with_no_difference(self):
        lines, diff = self.rescore()
        self.assertEqual(diff, [], lines)
        for text in ("runs rescored: 300 of 300; mismatches: 0", "differences: 0",
                     "store: 50 comparator lines, 300 reply lines; refused: 0; run records with no reply line: 0"):
            self.assertIn(text, lines)
        lines, diff = runlog.rescore_log(self.log, repo=REPO)
        self.assertIsNone(diff)
        self.assertIn(mod011_log.NO_STORE, lines)

    def test_each_changed_store_line_is_refused_and_named(self):
        base = store_lines()
        first_exact = next(n for n, x in enumerate(base) if x.get("type") == "reply" and x["arm"] == "benchmark")
        key = "/".join(str(base[first_exact][k]) for k in ("arm", "item", "attempt"))

        def changed(n, **kv):
            lines = copy.deepcopy(base)
            lines[n].update(kv)
            return lines
        cases = {
            "a changed reply": (changed(first_exact, reply="another reply"),
                                f"REFUSED store line {first_exact + 1} ({key}): its SHA-256 is not the reply hash"),
            "a changed comparator text": (changed(3, prefix="Another first piece."),
                                          "REFUSED store line 4 (comparator C-03): its text does not have the hashes"),
            "a comparator line left out": ([x for n, x in enumerate(base) if n != 5],
                                           "REFUSED comparator C-05: the store has no comparator line for it"),
            "a comparator the log does not have": (base + [{"type": "comparator", "id": "C-99", "prefix": "a",
                                                            "remainder": "b"}],
                                                   "REFUSED store line 351 (comparator C-99): the log has no comparator"),
            "a reply line left out": ([x for n, x in enumerate(base) if n != first_exact],
                                      f"NOT IN STORE {key}: the store has no reply line for this run record"),
            "a second reply line": (base + [base[first_exact]],
                                    "REFUSED store line 351 (" + key + "): a second reply line for this run record"),
            "a reply line with no run record": (base + [{**base[first_exact], "attempt": 9}],
                                                "the log has no run record with this key"),
        }
        for name, (lines, want) in cases.items():
            with self.subTest(name):
                out, diff = self.rescore(lines)
                self.assertEqual(len(diff), 1, out)
                self.assertTrue(any(x.startswith(want) or want in x for x in out), out)
        out, diff = self.rescore(base[:3] + ["not JSON"] + base[3:])
        self.assertIsNone(diff)
        self.assertIn("not rescored: store line 4 is not JSON", out)
        out, diff = self.rescore(base + [base[2]])
        self.assertIsNone(diff)
        self.assertIn("not rescored: store line 351 is a second line for the comparator C-02", out)

    def test_an_edited_score_measure_or_instrument_row_is_a_named_mismatch(self):
        recs = good()
        r = runs(recs, "benchmark", "exact")[0]
        r["class"] = "near_exact"
        out, diff = self.rescore(recs=recs)
        self.assertEqual(len(diff), 1, out)
        self.assertIn(f"MISMATCH benchmark/{r['item']}/{r['attempt']}: recorded", "\n".join(out))
        recs = good()
        recs[-1]["measures"]["M1"]["gsm8k"]["primary"]["differential"] = 0.5
        out, diff = self.rescore(recs=recs)
        self.assertEqual(diff, ["summary M1 gsm8k: it differs from the classes scored now"])
        recs = good()
        recs[0]["instrument"]["passages"][0]["class"] = "unrelated"
        out, diff = self.rescore(recs=recs)
        self.assertEqual(len(diff), 1, out)
        self.assertTrue(diff[0].startswith("instrument/pride-and-prejudice: recorded"), diff)
        recs = good()
        recs[0]["instrument"]["passages"][0]["id"] = "an-unknown-work"
        out, diff = self.rescore(recs=recs)
        self.assertIn("instrument/an-unknown-work: the passage is not in items/TP-MOD-011-instrument.json", diff)
        recs = good()
        recs[0]["instrument"]["summary"]["exact_rate"] = 1.0
        out, diff = self.rescore(recs=recs)
        self.assertEqual(diff, ["instrument summary: it differs from the classes scored now"])

    def test_an_item_file_that_is_not_that_of_the_log_is_not_rescored(self):
        recs = good()
        item = runs(recs, "benchmark")[0]["item"]
        recs[0]["corpus"]["scorer_keys"][f"benchmark:{item}"]["remainder_sha256"] = sha("another remainder")
        out, diff = self.rescore(recs=recs)
        self.assertIsNone(diff)
        self.assertIn(f"not rescored: the item {item} of the item file of this checkout does not have the hashes of "
                      "the log", out)

    def test_the_command_line_exits_0_1_and_2(self):
        store = write_store(store_lines(), self.dir / "store_ok.jsonl")
        self.assertEqual(self.cli("rescore", "--log", str(self.log), "--replies", str(store))[0], 0)
        lines = store_lines()
        lines[60]["reply"] = "another reply"
        bad = write_store(lines, self.dir / "store_bad.jsonl")
        code, text = self.cli("rescore", "--log", str(self.log), "--replies", str(bad))
        self.assertEqual(code, 1, text)
        self.assertIn("REFUSED store line 61", text)
        self.assertEqual(self.cli("rescore", "--log", str(self.log))[0], 2)


class LabelTests(unittest.TestCase):
    def test_the_label_names_the_limits_by_the_writer_families_and_the_temperature(self):
        one_person = ("one person directs the build and the review of SRD-1, so an SME check by that person is not "
                      "independent")
        base = "; ".join([mod011_log.LABEL_BASE, mod011_log.LIMIT_74, mod011_log.MODEL_PROBE_NOTE,
                          mod011_log.DECLARED_NOTE, one_person])
        self.assertEqual(mod011_log.label_for(["family-a", "family-b"], 0), base)
        self.assertEqual(mod011_log.label_for(["family-a", "family-b"], None), base)
        self.assertEqual(mod011_log.label_for(["family-a"], 0), base + "; comparators from one model family")
        self.assertEqual(mod011_log.label_for(["family-a", "family-b"], 0.3),
                         base + "; decode not greedy (temperature 0.3)")
        self.assertIn("PLAYBOOK_MOD_011.md line 74", mod011_log.LIMIT_74)
        self.assertEqual(mod011_log.MODEL_PROBE_NOTE,
                         "model probe: SRD-1's rules are off; SRD-1's controls are not on the path")

    def test_the_store_block_and_the_readings(self):
        self.assertEqual(mod011_log.store_block("full")["benchmark_replies"], "full")
        self.assertIn("hash_only", mod011_log.store_block("hash_only")["note"])
        self.assertEqual(mod011_log.store_block("full")["comparator_replies"], "hash_only")
        self.assertEqual(len(mod011_log.READINGS), 8)
        self.assertEqual(len(mod011_log.NOT_IN_RUN), 11)

    def test_each_place_that_names_the_store_file_says_that_the_trace_holds_the_comparator_text(self):
        h = good()[0]
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8").split("\n## ")[1].replace("`", "")
        places = {"the module text": " ".join(mod011_log.__doc__.split()), "STORE_README": mod011_log.STORE_README,
                  "the store block note": h["store"]["note"], "the comparators block note": h["comparators"]["note"],
                  "the store README of the run": (shared_run()[0].run_dir / "store" / "README.txt").read_text(
                      encoding="utf-8"), "the CHANGELOG": changelog}
        for name, text in places.items():
            with self.subTest(name):
                self.assertEqual(text.count(TRACE_SENTENCE), 1, text)

    def test_the_summary_line_names_the_hash_check_of_the_comparator_text(self):
        recs = good()
        self.assertTrue(mod011_log.summary_line(recs).startswith("items hash: matches"))
        recs[0]["corpus"]["manifest"][0]["cut"] += 1
        self.assertTrue(mod011_log.summary_line(recs).startswith("items hash: differs"))
        self.assertIn("comparator text is checked by hash only", mod011_log.summary_line(recs))

    def test_measures_give_m1_for_each_benchmark_of_the_manifest(self):
        recs = good()
        got = mod011_log.measures(recs[0], [{k: r[k] for k in ("arm", "benchmark", "item", "attempt", "class")}
                                            for r in runs(recs)])
        self.assertEqual(json.loads(json.dumps(got)), recs[-1]["measures"]["M1"])
        self.assertEqual(set(got), {"gsm8k"})
        self.assertEqual(got["gsm8k"]["primary"]["bar"], 0.2)


if __name__ == "__main__":
    unittest.main()
