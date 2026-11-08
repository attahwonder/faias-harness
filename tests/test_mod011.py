"""TP-MOD-011, Component 1: the allow-list and its refusals, the item file and its tool, the probe template, the match
scorer, M1, the comparator check, the overlap tool, the settings and the model-probe prompt. No network and no model
call.

The fixtures are neutral arithmetic sentences written for these tests. No fixture holds text of GSM8K or of any other
benchmark; a test checks that the fixture comparators share no run of 8 words with the shipped items.
"""
import copy
import datetime
import hashlib
import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from faias_harness import settings as sm
from faias_harness.engine import mod011, sec001, sec006
from targets.srd1.app import orchestrator
from tests.helpers import EVIDENCE_RAW, StrictStdout

REPO = Path(__file__).resolve().parents[1]
ENTRY = mod011.ALLOW_LIST["gsm8k"]
ITEM_PATH = REPO / mod011.ITEM_FILE
TAU = 0.8
CUTOFF = "2026-01-31"
TARGET_FAMILY = "family-t"
OBJECTS = ("crates", "parcels", "lamps", "jars", "kites")


def load_tool(name):
    spec = importlib.util.spec_from_file_location(f"faias_tool_{name}", REPO / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ITEMS_TOOL, OVERLAP_TOOL = load_tool("mod011_items"), load_tool("mod011_overlap")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fixture_question(n):
    """A neutral arithmetic word problem of three sentences. A cut after the first sentence leaves 29 words, and a cut
    after the second leaves 14."""
    obj = OBJECTS[n % len(OBJECTS)]
    return (f"A workshop makes {40 + n} {obj} on day {n}. It sends {3 + n % 7} {obj} to a market stall and keeps "
            f"{2 + n % 5} {obj} for a display. How many {obj} does the workshop have left at the end of day {n}?")


def fixture_comparator(n, pair):
    q = fixture_question(n)
    cut = mod011.sentence_ends(q)[0]
    prefix, remainder = mod011.split_at(q, cut)
    return {"id": f"C-{n:02d}", "benchmark": "gsm8k", "pair": pair, "question": q, "cut": cut, "prefix": prefix,
            "remainder": remainder, "created": "2026-11-03", "writer_family": "family-w", "steps": 2,
            "answer": 40 + n - (3 + n % 7) - (2 + n % 5)}


class AllowListTests(unittest.TestCase):
    def setUp(self):
        self.unpin()
        self.addCleanup(self.unpin)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        for rel in (ENTRY["item_file"], ENTRY["licence_file"], ENTRY["digest_file"]):
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(REPO / rel, self.repo / rel)

    def unpin(self):
        """Stop the pin of a rewritten digest file, if one is active. Each subTest calls setUp again."""
        while getattr(self, "pins", None):
            self.pins.pop().stop()
        self.pins = []

    def test_the_shipped_entry_passes_and_its_digests_are_those_of_the_files(self):
        self.assertEqual(mod011.benchmark_problems("gsm8k"), [])
        self.assertEqual(mod011.benchmark_problems("gsm8k", self.repo), [])
        e = mod011.entry("gsm8k")
        self.assertEqual((e["split"], e["kind"], e["licence"]), ("test", "capability", "MIT"))
        self.assertTrue(e["source"].endswith("grade_school_math/data/test.jsonl"))
        self.assertEqual(e["item_sha256"], sha(ITEM_PATH.read_bytes()))
        self.assertEqual(e["licence_sha256"], sha((REPO / ENTRY["licence_file"]).read_bytes()))
        self.assertRegex(e["source_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(sorted(e["overlap_sha256"]), ["test.jsonl", "train.jsonl"])
        self.assertEqual(e["overlap_sha256"]["test.jsonl"], e["source_sha256"])
        self.assertEqual(len(mod011.load_items()), 50)

    def _rewrite_item_file(self, change):
        path = self.repo / ENTRY["item_file"]
        data = json.loads(path.read_text(encoding="utf-8"))
        change(data)
        raw = ITEMS_TOOL.dump(data)
        path.write_bytes(raw)
        digests = self.repo / ENTRY["digest_file"]
        lines = [f"{raw_sha}  {name}" for name, raw_sha in mod011.read_digests(digests).items()]
        lines = [x if not x.endswith("  TP-MOD-011.json") else f"{sha(raw)}  TP-MOD-011.json" for x in lines]
        digests.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        # The new digest file needs a new pin in the code (mod011.DIGEST_PREFIX_HEX characters of its SHA-256).
        pin = mock.patch.dict(ENTRY, {"digest_prefix": sha(digests.read_bytes())[:mod011.DIGEST_PREFIX_HEX]})
        pin.start()
        self.pins.append(pin)

    def test_each_refusal_is_one_problem_with_its_reason(self):
        def item_bytes_changed():
            path = self.repo / ENTRY["item_file"]
            path.write_bytes(path.read_bytes() + b"\n")

        cases = {
            "an ID not in the map": (None, "mmlu", "is not on the allow-list"),
            "an item file whose SHA-256 differs": (item_bytes_changed, "gsm8k", "does not have the SHA-256"),
            "a split other than the entry's": (lambda: self._rewrite_item_file(lambda d: d.update(split="train")),
                                               "gsm8k", "names the split 'train'"),
            "an absent licence file": (lambda: (self.repo / ENTRY["licence_file"]).unlink(), "gsm8k",
                                       "the licence file third_party/gsm8k/LICENSE is absent"),
            "a changed licence file": (lambda: (self.repo / ENTRY["licence_file"]).write_bytes(b"other text\n"),
                                       "gsm8k", "licence file third_party/gsm8k/LICENSE does not have the SHA-256"),
            "an absent digest file": (lambda: (self.repo / ENTRY["digest_file"]).unlink(), "gsm8k",
                                      "the digest file third_party/gsm8k/gsm8k.sha256 is absent"),
            "an absent item file": (lambda: (self.repo / ENTRY["item_file"]).unlink(), "gsm8k",
                                    "the item file items/TP-MOD-011.json is absent"),
        }
        for name, (fault, benchmark, want) in cases.items():
            with self.subTest(name):
                self.setUp()
                if fault:
                    fault()
                problems = mod011.benchmark_problems(benchmark, self.repo)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(want, problems[0])
                with self.assertRaises(ValueError):
                    mod011.load_items(benchmark, self.repo)

    def test_the_item_check_finds_a_bad_cut_with_a_matching_digest(self):
        def bad(d):
            d["items"][0]["cut"] += 1
        self._rewrite_item_file(bad)
        problems = mod011.benchmark_problems("gsm8k", self.repo)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("are not the question cut at", problems[0])

    def test_a_pin_of_another_length_matches_no_file(self):
        digests = REPO / ENTRY["digest_file"]
        full = sha(digests.read_bytes())
        self.assertTrue(mod011.pin_matches(digests, full[:mod011.DIGEST_PREFIX_HEX]))
        for name, prefix in {"an empty pin": "", "a pin one character short": full[:mod011.DIGEST_PREFIX_HEX - 1],
                             "a pin one character long": full[:mod011.DIGEST_PREFIX_HEX + 1]}.items():
            with self.subTest(name):
                self.assertFalse(mod011.pin_matches(digests, prefix))


class ItemFileTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(ITEM_PATH.read_text(encoding="utf-8"))

    def test_fifty_items_each_cut_at_a_sentence_end_with_a_remainder_of_15_words_or_more(self):
        items = self.data["items"]
        self.assertEqual(len(items), 50)
        self.assertEqual(mod011.check_item_set(self.data, "gsm8k"), [])
        for i in items:
            with self.subTest(i["id"]):
                self.assertIn(i["cut"], mod011.sentence_ends(i["question"]))
                self.assertEqual(mod011.split_at(i["question"], i["cut"]), (i["prefix"], i["remainder"]))
                self.assertGreaterEqual(len(mod011.words(i["remainder"])), 15)
                self.assertEqual(i["id"], f"gsm8k-test-{i['source_index']:04d}")
        self.assertEqual(len({i["source_index"] for i in items}), 50)

    def test_the_file_records_the_seed_and_the_tool_version_and_holds_no_answer_text(self):
        g = self.data["generated_by"]
        self.assertEqual((g["tool"], g["version"], g["seed"], g["count"]),
                         ("tools/mod011_items.py", ITEMS_TOOL.VERSION, ITEMS_TOOL.SEED, 50))
        self.assertEqual((self.data["split"], self.data["benchmark"]), ("test", "gsm8k"))
        self.assertEqual(set(self.data["items"][0]), {"id", "source_index", "question", "cut", "prefix", "remainder",
                                                      "steps", "answer"})
        text = ITEM_PATH.read_text(encoding="utf-8")
        self.assertNotIn("####", text)
        self.assertNotIn("<<", text)

    def test_the_tool_gives_the_same_bytes_from_the_same_rows_and_seed(self):
        rows = []
        for n in range(60):
            q = fixture_question(n) if n % 3 else f"A shop opens on day {n}. It sells {n} lamps."
            rows.append((n, {"question": q, "answer": f"{n} - 2 = <<{n}-2={n - 2}>>{n - 2}\n#### {n - 2}"}))
        one, two = ITEMS_TOOL.dump(ITEMS_TOOL.build(rows, 7, 5)), ITEMS_TOOL.dump(ITEMS_TOOL.build(rows, 7, 5))
        self.assertEqual(one, two)
        self.assertNotEqual(one, ITEMS_TOOL.dump(ITEMS_TOOL.build(rows, 8, 5)))
        data = json.loads(one)
        self.assertEqual((data["generated_by"]["qualifying_rows"], len(data["items"])), (40, 5))
        for i in data["items"]:
            self.assertIn(i["cut"], mod011.qualifying_cuts(i["question"]))
            self.assertEqual((i["steps"], i["answer"]), (1, i["source_index"] - 2))
        self.assertEqual([i["source_index"] for i in data["items"]],
                         sorted(i["source_index"] for i in data["items"]))

    def test_the_tool_refuses_a_source_that_is_not_the_pinned_file(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "test.jsonl"
            src.write_text(json.dumps({"question": fixture_question(1), "answer": "#### 1"}) + "\n", encoding="utf-8")
            with redirect_stdout(io.StringIO()) as out:
                rc = ITEMS_TOOL.main(["--source", str(src), "--out", str(Path(d) / "new.json")])
            self.assertEqual(rc, 2)
            self.assertIn("refused", out.getvalue())
            self.assertFalse((Path(d) / "new.json").exists())

    def test_the_answer_features_are_the_annotations_and_the_final_number(self):
        self.assertEqual(mod011.answer_features("a <<2+3=5>>5 b <<5*2=10>>10\n#### 10"), (2, 10))
        self.assertEqual(mod011.answer_features("no annotation\n#### 1,200"), (0, 1200))
        self.assertEqual(mod011.answer_features("#### 2.5"), (0, 2.5))


class ProbeTests(unittest.TestCase):
    def test_the_template_names_the_data_set_and_split_gives_the_first_piece_and_asks_for_the_second(self):
        t = mod011.PROBE_TEMPLATE
        for part in ("{split}", "{dataset}", "{first_piece}", "Second piece:", "in the words that the data set holds"):
            self.assertIn(part, t)
        self.assertEqual(mod011.TEMPLATE_SHA256, sha(t.encode("utf-8")))
        text = mod011.probe_text("gsm8k", "A workshop makes 4 kites.")
        self.assertIn("the test split of the GSM8K data set", text)
        self.assertTrue(text.endswith("First piece: A workshop makes 4 kites.\nSecond piece:"))


class ScorerTests(unittest.TestCase):
    PREFIX = "A workshop makes 41 parcels on day 1."
    REMAINDER = ("It sends 4 parcels to a market stall and keeps 3 parcels for a display. How many parcels does the "
                 "workshop have left at the end of day 1?")

    def score(self, reply, tau=TAU, **kw):
        return mod011.score_completion(reply, self.PREFIX, self.REMAINDER, tau, **kw)

    def test_each_class_on_fixture_replies(self):
        self.assertEqual(len(mod011.words(self.REMAINDER)), 29)
        changed = self.REMAINDER.replace("market", "corner").replace("display", "window")
        cases = {
            "exact": (self.REMAINDER, "exact"),
            "exact, case and punctuation differ, words follow": (self.REMAINDER.upper().replace(".", ";") + " So 34.",
                                                                 "exact"),
            "exact after an echoed first piece and both labels": (
                f"First piece: {self.PREFIX}\nSecond piece: {self.REMAINDER}", "exact"),
            "exact after the second-piece label only": (f"Second piece: {self.REMAINDER}", "exact"),
            "exact after the echoed prefix only": (f"{self.PREFIX} {self.REMAINDER}", "exact"),
            "near-exact: two words changed": (changed, "near_exact"),
            "unrelated: another sentence": ("The weather is mild and the river is calm today.", "unrelated"),
            "unrelated: the echo alone": (f"First piece: {self.PREFIX}", "unrelated"),
            "no_result: empty": ("", "no_result"),
            "no_result: spaces": ("   \n", "no_result"),
            "no_result: none": (None, "no_result"),
        }
        for name, (reply, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.score(reply)["class"], want)
        with self.subTest("no_result: an error or a block with text"):
            self.assertEqual(self.score(self.REMAINDER, failed=True),
                             {"class": "no_result", "rouge_l": None, "longest_run": None})
        with self.subTest("no tau: the near-exact reply is unrelated, and its ROUGE-L value stays"):
            s = self.score(changed, tau=None)
            self.assertEqual(s["class"], "unrelated")
            self.assertAlmostEqual(s["rouge_l"], 27 / 29)
        with self.subTest("near-exact at a ROUGE-L value equal to tau"):
            self.assertEqual(self.score(changed, tau=s["rouge_l"])["class"], "near_exact")

    def test_the_rouge_l_value_and_the_longest_run_by_hand(self):
        s = self.score(self.REMAINDER)
        self.assertEqual((s["rouge_l"], s["longest_run"]), (1.0, 29))
        # Two of 29 words changed (the 7th and the 15th): LCS 27, so P = R = F1 = 27/29. The runs are 6, 7 and 14.
        s = self.score(self.REMAINDER.replace("market", "corner").replace("display", "window"))
        self.assertAlmostEqual(s["rouge_l"], 27 / 29)
        self.assertEqual(s["longest_run"], 14)
        # Six words of preface: the window of 29 reply words holds the first 23 remainder words in order (LCS 23).
        # 23/29 is about 0.79: near-exact at tau 0.75, unrelated at tau 0.8. The whole remainder is still a run.
        s = self.score("Here is the rest you asked: " + self.REMAINDER, tau=0.75)
        self.assertAlmostEqual(s["rouge_l"], 23 / 29)
        self.assertEqual((s["class"], s["longest_run"]), ("near_exact", 29))
        self.assertEqual(self.score("Here is the rest you asked: " + self.REMAINDER)["class"], "unrelated")


def records(arm, spec, k=3):
    """Run records of one arm: `spec` maps an item to its classes, one for each attempt."""
    return [{"arm": arm, "item": item, "attempt": n, "class": c} for item, cs in spec.items()
            for n, c in enumerate(cs, 1)]


class M1Tests(unittest.TestCase):
    # Benchmark: 6 items with an exact attempt, 2 near-exact at best, 1 unrelated, 1 with no result (left out).
    BENCH = {**{f"b{n}": ["unrelated", "exact", "no_result"] for n in range(6)},
             "b6": ["near_exact", "unrelated", "unrelated"], "b7": ["unrelated", "near_exact", "no_result"],
             "b8": ["unrelated"] * 3, "b9": ["no_result"] * 3}
    # Comparator: 1 exact, 1 near-exact, 7 unrelated, 1 incomplete item (2 attempts; left out).
    COMP = {"c0": ["exact", "unrelated", "unrelated"], "c1": ["near_exact"] * 3,
            **{f"c{n}": ["unrelated"] * 3 for n in range(2, 9)}, "c9": ["exact", "exact"]}

    def m1(self, bar=0.3, tau=TAU):
        return mod011.compute_m1(records("benchmark", self.BENCH) + records("comparator", self.COMP),
                                 runs_per_item=3, bar=bar, tau=tau)

    def test_the_counts_rates_intervals_and_fisher_p_against_hand_values(self):
        m = self.m1()
        b, c = m["benchmark"], m["comparator"]
        self.assertEqual(b["counts"], {"exact": 6, "near_exact": 2, "unrelated": 12, "no_result": 10})
        self.assertEqual((b["items"], b["exact_items"], b["exact_or_near_exact_items"], b["items_without_result"]),
                         (9, 6, 8, 1))
        self.assertEqual(b["item_counts"], {"exact": 6, "near_exact": 2, "unrelated": 1})
        self.assertEqual((c["items"], c["exact_items"], c["exact_or_near_exact_items"], c["items_incomplete"]),
                         (9, 1, 2, 1))
        self.assertEqual(c["counts"]["exact"], 3)       # the incomplete item's attempts are counted, not rated
        self.assertEqual(b["exact_wilson95"], [0.3542, 0.8794])
        self.assertEqual(b["exact_or_near_exact_wilson95"], [0.565, 0.9801])
        self.assertEqual(c["exact_wilson95"], [0.0199, 0.435])
        self.assertEqual(c["exact_or_near_exact_wilson95"], [0.0632, 0.5474])
        # Per attempt: the benchmark's rated items have 20 attempts with a result; 6 exact, 8 exact or near-exact.
        self.assertAlmostEqual(b["attempt_exact_rate"], 6 / 20)
        self.assertAlmostEqual(b["attempt_exact_or_near_exact_rate"], 8 / 20)
        p, s = m["primary"], m["secondary"]
        self.assertAlmostEqual(p["differential"], 5 / 9)
        # P(X >= 6), X hypergeometric: 9 draws from 18 with 7 successes: (C(7,6)C(11,3) + C(7,7)C(11,2)) / C(18,9).
        self.assertAlmostEqual(p["fisher_p_one_sided"], (7 * 165 + 1 * 55) / 48620)
        self.assertAlmostEqual(s["differential"], 6 / 9)
        self.assertAlmostEqual(s["fisher_p_one_sided"], (45 * 8 + 10 * 1) / 48620)
        self.assertEqual((p["bar"], p["materially_higher"], s["materially_higher"]), (0.3, True, True))
        r = m["comparator_reading"]
        self.assertEqual(r["reading"], "comparator arm at or near chance (a reading, not a measure)")
        self.assertEqual((r["exact_wilson95"], r["exact_or_near_exact_wilson95"]), ([0.0199, 0.435], [0.0632, 0.5474]))
        self.assertNotIn("pass", json.dumps(m).lower())

    def test_materially_higher_against_the_bar(self):
        cases = {"under the differential": (0.5, True, True), "above the primary only": (0.6, False, True),
                 "no bar": (None, None, None)}
        for name, (bar, primary, secondary) in cases.items():
            with self.subTest(name):
                m = self.m1(bar=bar)
                self.assertEqual((m["primary"]["materially_higher"], m["secondary"]["materially_higher"]),
                                 (primary, secondary))
        with self.subTest("a differential equal to the bar is not materially higher (the form of sec002)"):
            bar = self.m1()["primary"]["differential"]
            self.assertIs(self.m1(bar=bar)["primary"]["materially_higher"], False)
        with self.subTest("an arm with no item"):
            m = mod011.compute_m1(records("benchmark", self.BENCH), runs_per_item=3, bar=0.1, tau=TAU)
            self.assertEqual((m["primary"]["differential"], m["primary"]["fisher_p_one_sided"],
                              m["primary"]["materially_higher"]), (None, None, None))

    def test_with_no_tau_the_secondary_form_has_no_value(self):
        s = self.m1(tau=None)["secondary"]
        self.assertEqual((s["differential"], s["materially_higher"], s["tau"]), (None, None, None))
        self.assertIn("no tau", s["reason"])

    def test_fisher_p_by_hand(self):
        self.assertAlmostEqual(mod011.fisher_one_sided(6, 9, 1, 9), 11 / 442)
        self.assertAlmostEqual(mod011.fisher_one_sided(0, 5, 0, 5), 1.0)
        self.assertAlmostEqual(mod011.fisher_one_sided(5, 5, 0, 5), 1 / 252)
        self.assertIsNone(mod011.fisher_one_sided(0, 0, 1, 5))


def overlap_result(comps, file_sha, rows=None):
    pins = mod011.entry("gsm8k")["overlap_sha256"]
    return {"limit": 8, "comparator_file": {"name": "comparators.json", "sha256": file_sha},
            "gsm8k_files": [{"name": n, "sha256": h} for n, h in pins.items()],
            "results": rows if rows is not None else [{"id": c["id"], "longest_run": 3} for c in comps]}


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        self.items = mod011.load_items()
        self.data = {"comparators": [fixture_comparator(n, self.items[n].id) for n in range(50)]}
        self.file_sha = sha(json.dumps(self.data).encode("utf-8"))

    def check(self, data=None, **kw):
        data = data or self.data
        args = {"file_sha256": self.file_sha, "training_cutoff": CUTOFF, "target_family": TARGET_FAMILY,
                "overlap": overlap_result(data["comparators"], self.file_sha), **kw}
        return mod011.check_comparators(data, self.items, **args)

    def test_the_fixture_passes_in_explore_and_in_evidence_mode(self):
        self.assertEqual(self.check(), [])
        self.assertEqual(self.check(evidence=True, sme_record_sha256=sha(b"an SME record")), [])

    def test_one_problem_for_each_seeded_fault(self):
        def change(n, **kv):
            d = copy.deepcopy(self.data)
            d["comparators"][n].update(kv)
            return d

        q = fixture_question(5)
        late = mod011.sentence_ends(q)[1]
        mid = q.index(" to a market")
        missing = copy.deepcopy(self.data)
        del missing["comparators"][0]["answer"]
        rows = [{"id": c["id"], "longest_run": 8 if c["id"] == "C-07" else 7} for c in self.data["comparators"]]
        cases = {
            "fewer than 50": ({"data": {"comparators": self.data["comparators"][:49]}}, "has 49 comparators"),
            "a date on the cut-off": ({"data": change(3, created=CUTOFF)}, "on or before the declared training"),
            "a date before the cut-off": ({"data": change(3, created="2025-12-01")}, "on or before"),
            "an overlap at the limit": ({"overlap": overlap_result(self.data["comparators"], self.file_sha, rows)},
                                        "C-07: a run of 8 words in common"),
            "a remainder under 15 words": ({"data": change(5, cut=late, prefix=q[:late], remainder=q[late:].strip())},
                                           "C-05: the remainder has 14 words; the minimum is 15"),
            "a cut not at a sentence end": ({"data": change(5, cut=mid, prefix=q[:mid], remainder=q[mid:].strip())},
                                            "is not at a sentence end"),
            "a prefix that is not the question cut": ({"data": change(5, prefix="Another first piece.")},
                                                      "are not the question cut"),
            "the writer family of the target": ({"data": change(9, writer_family=" Family-T ")},
                                                "C-09: the writer family is the declared family of the target"),
            "no SME record hash in evidence mode": ({"evidence": True}, "the SHA-256 of the SME record"),
            "no overlap result": ({"overlap": None}, "no overlap result"),
            "an overlap result of another file": ({"file_sha256": sha(b"another file")}, "for another comparator"),
            "an overlap result of other GSM8K files": (
                {"overlap": {**overlap_result(self.data["comparators"], self.file_sha),
                             "gsm8k_files": [{"name": "test.jsonl", "sha256": sha(b"x")}]}}, "does not read the files"),
            "a comparator with no overlap row": (
                {"overlap": overlap_result(self.data["comparators"], self.file_sha,
                                           [{"id": c["id"], "longest_run": 2} for c in self.data["comparators"][1:]])},
                "C-00: the overlap result has no row"),
            "a missing key": ({"data": missing}, "C-00: the key(s) answer are missing"),
            "an ID twice": ({"data": change(1, id="C-02")}, "the comparator ID C-02 is in the file more than once"),
            "a pair that is not an item": ({"data": change(1, pair="gsm8k-test-9999")}, "is not an item of gsm8k"),
            "a benchmark not on the allow-list": ({"data": change(1, benchmark="mmlu")}, "is not on the allow-list"),
        }
        for name, (kw, want) in cases.items():
            with self.subTest(name):
                problems = self.check(**kw)
                if name == "a benchmark not on the allow-list":
                    # The comparator moves to another benchmark, so GSM8K also falls under 50.
                    self.assertEqual(len(problems), 2, problems)
                    self.assertIn("has 49 comparators", problems[1])
                else:
                    self.assertEqual(len(problems), 1, problems)
                self.assertIn(want, problems[0])

    def test_an_overlap_field_of_the_wrong_type_is_read_as_absent_and_named(self):
        """By the rule of the TP-SEC-002 item check, a field of the overlap result that has the wrong type is read as
        absent, so the problem of an absent field names it. Before, such a field stopped the check with a TypeError or
        an AttributeError."""
        good = overlap_result(self.data["comparators"], self.file_sha)
        other_file = ["the overlap result is for another comparator file (its SHA-256 differs)"]
        no_files = ["the overlap result does not read the files train.jsonl, test.jsonl of gsm8k that the digest file "
                    "names"]
        no_rows = [f"{c['id']}: the overlap result has no row for it" for c in self.data["comparators"]]
        cases = {"comparator_file a number": ({"comparator_file": 5}, other_file),
                 "comparator_file a list": ({"comparator_file": [1]}, other_file),
                 "comparator_file a text": ({"comparator_file": "ab"}, other_file),
                 "gsm8k_files a number": ({"gsm8k_files": 5}, no_files),
                 "gsm8k_files with a sha256 that is a list": ({"gsm8k_files": [{"name": "x", "sha256": [1]}]},
                                                              no_files),
                 "results a number": ({"results": 5}, no_rows),
                 "results with an id that is a list": ({"results": [{"id": [1], "longest_run": 3}]}, no_rows)}
        for name, (change, want) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.check(overlap={**good, **change}), want)

    def test_an_empty_comparator_list_falls_under_the_minimum(self):
        problems = self.check({"comparators": []}, overlap=overlap_result([], self.file_sha))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("the benchmark gsm8k has 0 comparators; the minimum is 50", problems[0])

    def test_no_declared_cutoff_or_family_compares_neither(self):
        d = copy.deepcopy(self.data)
        d["comparators"][0].update(created="2020-01-01", writer_family=TARGET_FAMILY)
        self.assertEqual(self.check(d, training_cutoff=None, target_family=None), [])

    def test_the_match_features_for_each_arm_and_pair_refuse_no_pair(self):
        d = copy.deepcopy(self.data)
        d["comparators"][0]["answer"] = 123456
        self.assertEqual(self.check(d), [])
        m = mod011.match_features(self.items, d["comparators"])
        self.assertEqual(m["features"], list(mod011.FEATURES))
        self.assertEqual(len(m["pairs"]), 50)
        f = mod011.features(fixture_question(0), d["comparators"][0]["prefix"], d["comparators"][0]["remainder"], 2,
                            123456)
        self.assertEqual(f, {"prefix_words": 8, "remainder_words": 29, "sentences": 3, "numbers": 5, "steps": 2,
                             "answer_digits": 6})
        item = self.items[0]
        want = mod011.features(item.question, item.prefix, item.remainder, item.steps, item.answer)
        self.assertEqual(m["pairs"][0]["difference"], {k: abs(f[k] - want[k]) for k in mod011.FEATURES})
        self.assertEqual(m["largest_pair_difference"]["answer_digits"],
                         max(p["difference"]["answer_digits"] for p in m["pairs"]))
        for arm in ("benchmark", "comparator"):
            self.assertEqual(set(m[arm]), set(mod011.FEATURES))
            self.assertEqual(set(m[arm]["steps"]), {"mean", "min", "max"})


class OverlapToolTests(unittest.TestCase):
    TRAIN = [{"question": "A farmer plants 12 rows of beans and each row holds 9 plants in the spring.",
              "answer": "12 * 9 = <<12*9=108>>108\n#### 108"},
             {"question": "A library lends 30 books on Monday and 25 books on Tuesday.",
              "answer": "the planted run of answer words sits in this answer text only\n#### 55"}]
    TEST = [{"question": "A school bus carries 40 pupils to the park and 35 pupils back.", "answer": "#### 75"}]
    COMPARATORS = [{"id": "C-01", "question": "The farmer plants 12 rows of beans and each week the rows grow taller."},
                   {"id": "C-02", "question": "Ten pupils say that the planted run of answer words sits in this "
                                              "answer text only today."}]

    def test_a_planted_run_in_question_text_is_found_and_answer_text_is_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name, rows in (("train.jsonl", self.TRAIN), ("test.jsonl", self.TEST)):
                (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            comp = d / "comparators.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS}), encoding="utf-8")
            out = d / "overlap.json"
            args = ["--comparators", str(comp), "--gsm8k", str(d / "train.jsonl"), str(d / "test.jsonl"),
                    "--out", str(out)]
            with redirect_stdout(io.StringIO()):
                self.assertEqual(OVERLAP_TOOL.main(args), 1)
                self.assertEqual(OVERLAP_TOOL.main(args), 2)       # the result exists: refused
            res = json.loads(out.read_text(encoding="utf-8"))
            rows = {r["id"]: r for r in res["results"]}
            self.assertEqual(rows["C-01"]["longest_run"], 8)
            self.assertEqual(rows["C-01"]["at"], {"file": "train.jsonl", "line_index": 0})
            self.assertLess(rows["C-02"]["longest_run"], 8)        # the 12-word run is in an answer only
            self.assertEqual(res["at_or_above_limit"], ["C-01"])
            self.assertEqual(res["comparator_file"]["sha256"], sha(comp.read_bytes()))
            self.assertEqual([(f["name"], f["sha256"]) for f in res["gsm8k_files"]],
                             [(n, sha((d / n).read_bytes())) for n in ("train.jsonl", "test.jsonl")])
            text = out.read_text(encoding="utf-8")
            for c in self.COMPARATORS + self.TRAIN + self.TEST:
                self.assertNotIn(c["question"], text)

    def test_a_comparator_file_with_a_repeated_key_is_refused_and_no_result_is_written(self):
        """The engine refuses a comparator file in which a JSON object holds a key more than once
        (run_mod011.comparator_problems). The tool refuses it too, with the key, and writes no result (exit 2). Plain
        json.loads keeps the last value, which is the valid one in each case. The output holds no comparator text."""
        text = json.dumps({"comparators": self.COMPARATORS})
        cases = {"the top object": (('{"comparators": ', '{"comparators": [], "comparators": '), "'comparators'"),
                 "a comparator": (('"id": ', '"id": "X", "id": '), "'id'")}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name, rows in (("train.jsonl", self.TRAIN), ("test.jsonl", self.TEST)):
                (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            for n, (name, ((old, new), key)) in enumerate(cases.items()):
                with self.subTest(name):
                    self.assertIn(old, text)
                    comp, out = d / f"comparators_{n}.json", d / f"overlap_{n}.json"
                    comp.write_text(text.replace(old, new, 1), encoding="utf-8")
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = OVERLAP_TOOL.main(["--comparators", str(comp), "--gsm8k", str(d / "train.jsonl"),
                                                str(d / "test.jsonl"), "--out", str(out)])
                    self.assertEqual((rc, out.exists()), (2, False), buf.getvalue())
                    self.assertEqual(buf.getvalue(), f"refused: the comparator file {comp}: a JSON object holds the "
                                                     f"key {key} more than once\n")
                    for c in self.COMPARATORS:
                        self.assertNotIn(c["question"], buf.getvalue())

    def test_a_comparator_file_with_a_lone_surrogate_is_refused_in_one_line_and_no_result_is_written(self):
        """A comparator ID that holds a lone surrogate (the JSON escape \\ud83d alone). The tool refuses the file in one
        line that names the key, with exit 2, and writes no file. A stdout that encodes as strict UTF-8 prints the
        line. Before, the tool stopped with UnicodeEncodeError before it wrote the result."""
        comps = copy.deepcopy(self.COMPARATORS)
        comps[0]["id"] += "\ud83d"
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name, rows in (("train.jsonl", self.TRAIN), ("test.jsonl", self.TEST)):
                (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            comp, out = d / "comparators.json", d / "overlap.json"
            comp.write_text(json.dumps({"comparators": comps}), encoding="ascii")
            buf = StrictStdout()
            with redirect_stdout(buf.stream):
                rc = OVERLAP_TOOL.main(["--comparators", str(comp), "--gsm8k", str(d / "train.jsonl"),
                                        str(d / "test.jsonl"), "--out", str(out)])
            self.assertEqual((rc, buf.text()), (2, f"refused: the comparator file {comp}: a JSON object holds a lone "
                                                   "surrogate in the key or the value of 'id'\n"))
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["comparators.json", "test.jsonl", "train.jsonl"])

    def test_a_comparator_file_that_cannot_be_read_is_refused_in_the_words_of_tp_sec_002_and_no_result_is_written(self):
        """A comparator file that is not UTF-8 JSON, one that does not hold one JSON object, and one whose field
        comparators is not a list of objects: the tool refuses each, with the TP-SEC-002 words, and writes no result
        (exit 2). Before, each stopped the tool with a traceback, or (no list) gave a result of no row."""
        not_json, not_object = r"the file is not UTF-8 JSON \(.+\)", "the file does not hold one JSON object"
        not_list = "the field comparators is not a list of objects"
        cases = {"not JSON": (b"not JSON", not_json), "not UTF-8": (b"\xff\xfe{}", not_json),
                 "a list": (b"[1]", not_object), "null": (b"null", not_object),
                 "no field comparators": (b"{}", not_list), "comparators null": (b'{"comparators": null}', not_list),
                 "comparators a number": (b'{"comparators": 5}', not_list),
                 "comparators a text": (b'{"comparators": "ab"}', not_list),
                 "a comparator that is a number": (json.dumps({"comparators": self.COMPARATORS + [5]}).encode(),
                                                   not_list)}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            for name, rows in (("train.jsonl", self.TRAIN), ("test.jsonl", self.TEST)):
                (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            for n, (name, (raw, reason)) in enumerate(cases.items()):
                with self.subTest(name):
                    comp, out = d / f"comparators_{n}.json", d / f"overlap_{n}.json"
                    comp.write_bytes(raw)
                    buf = io.StringIO()
                    with redirect_stdout(buf):
                        rc = OVERLAP_TOOL.main(["--comparators", str(comp), "--gsm8k", str(d / "train.jsonl"),
                                                str(d / "test.jsonl"), "--out", str(out)])
                    self.assertEqual((rc, out.exists()), (2, False), buf.getvalue())
                    self.assertRegex(buf.getvalue(), rf"^refused: the comparator file {re.escape(str(comp))}: "
                                                     rf"{reason}\n$")
                    for c in self.COMPARATORS:
                        self.assertNotIn(c["question"], buf.getvalue())

    def gsm8k_files(self, d):
        """The neutral train and test files of this class in folder `d`. Returns their paths."""
        out = []
        for name, rows in (("train.jsonl", self.TRAIN), ("test.jsonl", self.TEST)):
            (d / name).write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
            out.append(d / name)
        return out

    def run_tool(self, comp, gsm8k, out):
        """(the exit code, the output) of the tool on the comparator file `comp` and the GSM8K files `gsm8k`."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = OVERLAP_TOOL.main(["--comparators", str(comp), "--gsm8k", *map(str, gsm8k), "--out", str(out)])
        return rc, buf.getvalue()

    def test_a_comparator_whose_question_is_not_a_text_is_read_as_absent_with_a_run_of_0(self):
        """By the rule of the TP-SEC-002 item check, a question of a comparator that is not a text is read as absent. It
        gives no words, so the longest run is 0 and the row names no place. mod011.check_comparators still refuses such
        a comparator. Before, a number, a list, an object or a boolean stopped the tool with a TypeError."""
        for value in (5, [1], {"a": 1}, True, None):
            with self.subTest(question=value), tempfile.TemporaryDirectory() as d:
                d = Path(d)
                comp, out = d / "comparators.json", d / "overlap.json"
                comp.write_text(json.dumps({"comparators": [{"id": "C", "question": value}]}), encoding="utf-8")
                rc, text = self.run_tool(comp, self.gsm8k_files(d), out)
                self.assertEqual(rc, 0, text)
                self.assertTrue(text.startswith(f"wrote {out}: 1 comparators; 0 at or above the limit"), text)
                self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["results"],
                                 [{"id": "C", "longest_run": 0, "at": None}])
        self.assertIn("question", mod011._comparator_form({"id": "C", "question": 5}))

    def test_a_comparator_or_gsm8k_file_that_is_absent_or_cannot_be_read_is_refused_and_no_result_is_written(self):
        """An absent comparator file, a comparator path that is a folder, and an absent GSM8K file: the tool refuses
        each with a line that names the file, and writes no result (exit 2). An absent file has the words of the
        preflight for an absent item file. Before, each stopped the tool with a traceback."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gsm = self.gsm8k_files(d)
            comp = d / "comparators.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS}), encoding="utf-8")
            (d / "folder").mkdir()
            absent, folder, absent_gsm = d / "absent.json", d / "folder", d / "absent.jsonl"
            cases = {"an absent comparator file": (
                         absent, gsm, re.escape(f"the comparator file {absent} is not found")),
                     "a comparator path that is a folder": (
                         folder, gsm, re.escape(f"the comparator file {folder} cannot be read (") + r".+\)"),
                     "an absent GSM8K file": (comp, [gsm[0], absent_gsm],
                                              re.escape(f"the GSM8K file {absent_gsm} is not found"))}
            for n, (name, (c, files, reason)) in enumerate(cases.items()):
                with self.subTest(name):
                    out = d / f"overlap_{n}.json"
                    rc, text = self.run_tool(c, files, out)
                    self.assertEqual((rc, out.exists()), (2, False), text)
                    self.assertRegex(text, rf"^refused: {reason}\n$")

    def test_a_gsm8k_line_that_cannot_be_read_is_refused_with_its_file_and_line_index(self):
        """A GSM8K file that is not UTF-8, and a line that is not JSON, that does not hold one JSON object, or whose
        field question is not a text (absent included): the tool refuses each, in the words of the TP-SEC-002
        refusals, with the file and the line index (from 0; a blank line counts). It writes no result (exit 2). It does
        not read such a question as absent, because an absent question would hide an overlap. Before, each stopped the
        tool with a traceback, and an absent or null question gave no words."""
        good = json.dumps(self.TEST[0]) + "\n"
        line = "the GSM8K file test.jsonl, line index 1: "
        cases = {"a file that is not UTF-8": (b"\xff\xfe" + good.encode(),
                                              r"the GSM8K file test\.jsonl: the file is not UTF-8 JSON \(.+\)"),
                 "a line that is not JSON": (good + "{not JSON\n",
                                             re.escape(line) + r"the line is not UTF-8 JSON \(.+\)"),
                 "a blank line, then a line that is not JSON": (
                     good + "\n{not JSON\n", re.escape("the GSM8K file test.jsonl, line index 2: ")
                     + r"the line is not UTF-8 JSON \(.+\)"),
                 "a list": (good + "[1]\n", re.escape(line + "the line does not hold one JSON object")),
                 "null": (good + "null\n", re.escape(line + "the line does not hold one JSON object")),
                 "a question that is a number": (good + '{"question": 5}\n',
                                                 re.escape(line + "the field question is not a text")),
                 "a question that is a list": (good + '{"question": ["a"]}\n',
                                               re.escape(line + "the field question is not a text")),
                 "a question that is null": (good + '{"question": null}\n',
                                             re.escape(line + "the field question is not a text")),
                 "no question": (good + '{"answer": "#### 5"}\n', re.escape(line + "the field question is not a text"))}
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            train = self.gsm8k_files(d)[0]
            comp = d / "comparators.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS}), encoding="utf-8")
            for n, (name, (raw, reason)) in enumerate(cases.items()):
                with self.subTest(name):
                    test, out = d / f"case_{n}" / "test.jsonl", d / f"overlap_{n}.json"
                    test.parent.mkdir()
                    test.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
                    rc, text = self.run_tool(comp, [train, test], out)
                    self.assertEqual((rc, out.exists()), (2, False), text)
                    self.assertRegex(text, rf"^refused: {reason}\n$")
                    for c in self.COMPARATORS + self.TRAIN + self.TEST:
                        self.assertNotIn(c["question"], text)

    def refused_before_any_input(self, make, want):
        """Run the tool with the --out path that make(folder) gives, with read_file stubbed to fail. Check the one line
        that want(out) gives, exit 2, no input read and no result written."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gsm = self.gsm8k_files(d)
            comp = d / "comparators.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS}), encoding="utf-8")
            out = make(d)
            with mock.patch.object(OVERLAP_TOOL, "read_file", side_effect=AssertionError("an input was read")) as read:
                rc, text = self.run_tool(comp, gsm, out)
            self.assertEqual((rc, text), (2, want(out)))
            read.assert_not_called()
            self.assertFalse(out.exists())

    def test_an_out_path_whose_folder_is_not_found_is_refused_before_any_input_is_read(self):
        """An --out path in a folder that does not exist: the tool refuses it in one line, in the form of its refusal
        of an --out path that exists, before it reads the comparator file or a GSM8K file. It writes no result (exit
        2). Before, it read the inputs and then stopped with a FileNotFoundError traceback."""
        self.refused_before_any_input(lambda d: d / "absent" / "overlap.json",
                                      lambda out: f"refused: the folder {out.parent} of {out} is not found\n")

    def test_an_out_path_whose_folder_is_a_file_is_refused_before_any_input_is_read(self):
        """An --out path under a file: the tool says that the file is not a folder, in one line, before it reads any
        input. It writes no result (exit 2). Before, it read the inputs and then stopped with a traceback."""
        def make(d):
            (d / "a_file").write_text("x", encoding="utf-8")
            return d / "a_file" / "overlap.json"
        self.refused_before_any_input(make, lambda out: f"refused: {out.parent} is not a folder\n")

    @staticmethod
    def writes_as(make):
        """Each open of a file in binary write mode gives make(the real file object) in its place. The tool opens
        its result file in this way, and the reads of the inputs are not changed."""
        real_open = io.open

        def opener(file, mode="r", *a, **k):
            f = real_open(file, mode, *a, **k)
            return make(f) if "w" in mode and "b" in mode else f
        return mock.patch("io.open", opener)

    def test_a_result_that_cannot_be_written_is_refused_in_one_line(self):
        """The system refuses the write of the result (a PermissionError here): the tool refuses in one line, in the
        words of its refusal of a file that it cannot read, and exits 2. Before, it stopped with a traceback."""
        def refused(f):
            f.close()
            raise PermissionError(13, "Permission denied")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gsm = self.gsm8k_files(d)
            comp, out = d / "comparators.json", d / "overlap.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS}), encoding="utf-8")
            with self.writes_as(refused):
                rc, text = self.run_tool(comp, gsm, out)
            self.assertEqual((rc, text), (2, f"refused: {out} cannot be written (Permission denied)\n"))
            self.assertFalse(out.exists())

    def test_a_write_that_fails_part_way_leaves_no_file_and_a_second_run_writes_the_result(self):
        """The write of the result stops part way, as on a full disk. Half of the bytes reach the file, and then the
        write raises OSError. The tool refuses in one line and exits 2. No file stays at --out, and no temporary
        file stays in its folder. So a second run with the same --out writes the whole result. Before, the part of the
        result stayed at --out, and the second run refused it because it existed."""
        class Half:
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                self.f.close()

            def write(self, data):
                self.f.write(bytes(data)[:len(data) // 2])
                self.f.flush()
                raise OSError(28, "No space left on device")
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gsm = self.gsm8k_files(d)
            comp, out = d / "comparators.json", d / "overlap.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS}), encoding="utf-8")
            before = sorted(p.name for p in d.iterdir())
            with self.writes_as(Half):
                rc, text = self.run_tool(comp, gsm, out)
            self.assertEqual((rc, text), (2, f"refused: {out} cannot be written (No space left on device)\n"))
            self.assertEqual(sorted(p.name for p in d.iterdir()), before)
            rc, text = self.run_tool(comp, gsm, out)
            self.assertEqual(rc, 1, text)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["at_or_above_limit"], ["C-01"])
            self.assertEqual(sorted(p.name for p in d.iterdir()), sorted(before + [out.name]))

    def test_an_out_file_made_while_the_tool_runs_is_not_overwritten(self):
        """Another process makes the --out file while the tool runs. This comes after the check that --out does not
        exist, and before the move of the result. The tool refuses in one line and exits 2. The line is the one for an
        --out path that exists. The other file keeps its bytes, and no temporary file stays in the folder. Before, the
        tool overwrote that file."""
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gsm = self.gsm8k_files(d)
            comp, out = d / "comparators.json", d / "overlap.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS}), encoding="utf-8")
            before, real_result = sorted(p.name for p in d.iterdir()), OVERLAP_TOOL.result

            def result(*a):
                out.write_bytes(b"another file\n")      # made while the tool runs
                return real_result(*a)
            with mock.patch.object(OVERLAP_TOOL, "result", result):
                rc, text = self.run_tool(comp, gsm, out)
            self.assertEqual((rc, text), (2, f"refused: {out} exists\n"))
            self.assertEqual(out.read_bytes(), b"another file\n")
            self.assertEqual(sorted(p.name for p in d.iterdir()), sorted(before + [out.name]))

    def test_a_temporary_file_that_cannot_be_removed_after_a_good_move_is_a_note_not_a_refusal(self):
        """On POSIX the move is a hard link (HARD_LINK), and the result is then at --out. Then the system does not let
        the tool remove the temporary file (a PermissionError here). The result stays at --out. The tool prints the
        line of a written result. It then prints one note line with the path of the temporary file and the reason. The
        exit code is that of the result (0 here). The test takes the POSIX move on Windows too. Before, the tool
        refused with `refused: <out> cannot be written (...)` and exit 2. The whole result was at --out then."""
        real_unlink, failed = OVERLAP_TOOL.os.unlink, []

        def unlink(path, *a, **k):
            if str(path).endswith(".tmp") and not failed:
                failed.append(str(path))
                raise PermissionError(13, "Permission denied")
            return real_unlink(path, *a, **k)
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            gsm = self.gsm8k_files(d)
            comp, out = d / "comparators.json", d / "overlap.json"
            comp.write_text(json.dumps({"comparators": self.COMPARATORS[1:]}), encoding="utf-8")     # under the limit
            with mock.patch.object(OVERLAP_TOOL, "HARD_LINK", True), \
                    mock.patch.object(OVERLAP_TOOL.os, "unlink", unlink):
                rc, text = self.run_tool(comp, gsm, out)
            self.assertEqual(len(failed), 1)
            self.assertEqual((rc, text), (0, f"wrote {out}: 1 comparators; 0 at or above the limit of 8 words: none\n"
                                             f"note: the temporary file {failed[0]} cannot be removed (Permission "
                                             "denied)\n"))
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["results"][0]["id"], "C-02")
            self.assertEqual(Path(failed[0]).read_bytes(), out.read_bytes())       # a second name of the result
            real_unlink(failed[0])

    def test_a_run_never_spans_two_questions(self):
        files = [("a.jsonl", b'{"question": "one two three four"}\n{"question": "five six seven eight"}\n')]
        rows = OVERLAP_TOOL.overlap([{"id": "X", "question": "three four five six"}], files)
        self.assertEqual(rows[0]["longest_run"], 2)


def mod_raw(mode="evidence", **changes):
    raw = copy.deepcopy(EVIDENCE_RAW)
    raw.pop("item_set")
    raw.update(mode=mode, procedure="TP-MOD-011", benchmarks=["gsm8k"],
               reviewer_values={"M1": {"value": 0.2, "reason": "test value"}, "tau": {"value": TAU, "reason": "r"},
                                "match_tolerance": {"value": {"steps": 1, "answer_digits": 1}, "reason": "r"}},
               declared={"training_cutoff": CUTOFF, "training_cutoff_source": "the model card, section 2",
                         "target_family": TARGET_FAMILY},
               sme_record_sha256=sha(b"an SME record"), comparator_sha256=sha(b"a comparator file"),
               comparator_file="C:/evidence/comparators.json", overlap_result="C:/evidence/overlap.json",
               cited=[{"benchmark": "gsm8k", "source": "the model card, table 1", "score": 0.9}],
               store_replies="hash_only")
    raw.update(changes)
    return raw


class SettingsTests(unittest.TestCase):
    def test_a_full_evidence_file_and_a_bare_explore_file_load(self):
        self.assertEqual(sm.validate(mod_raw()), [])
        s = sm.from_raw(mod_raw(declared={"training_cutoff": datetime.date(2026, 1, 31),
                                          "training_cutoff_source": "card", "target_family": TARGET_FAMILY}))
        self.assertEqual((s.procedure, s.item_set, s.benchmarks, s.training_cutoff, s.tau_value),
                         ("TP-MOD-011", "TP-MOD-011", ("gsm8k",), CUTOFF, TAU))
        c = s.canonical()
        self.assertEqual(c["reviewer_values"]["tau"], {"value": TAU, "reason": "r"})
        self.assertEqual(c["declared"]["training_cutoff"], CUTOFF)
        bare = {k: v for k, v in mod_raw("explore").items()
                if k not in ("reviewer_values", "declared", "sme_record_sha256", "benchmarks", "cited")}
        self.assertEqual(sm.validate(bare), [])
        self.assertEqual(sm.from_raw(bare).benchmarks, ("gsm8k",))

    def test_the_validator_refuses_each_fault_for_tp_mod_011(self):
        cases = {
            "tools_enabled true": ({"tools_enabled": True, "tool_key_env": "TOOL_KEY"}, "probes the model alone"),
            "retrieval_filter": ({"retrieval_filter": "pre_retrieval"},
                                 "retrieval_filter applies to the procedures TP-DAT-009 and TP-SEC-003 only"),
            "arms": ({"arms": ["pipeline"]}, "arms applies to the procedures TP-DAT-009 and TP-SEC-003 only"),
            "a benchmark not in the map": ({"benchmarks": ["gsm8k", "bench-x"]}, "an ID is not on the allow-list"),
            "benchmarks not a list": ({"benchmarks": "gsm8k"}, "benchmarks must be a non-empty list"),
            "another item set": ({"item_set": "spike"}, "item_set must be TP-MOD-011"),
            "an M4 value": ({"reviewer_values": {"M4": {"value": 0.5, "reason": "r"}}}, "may hold M1 and"),
            "a tau above 1": ({"reviewer_values": {**mod_raw()["reviewer_values"], "tau": {"value": 2, "reason": "r"}}},
                              "reviewer_values.tau.value must be a number from 0 to 1"),
            "a tolerance of an unknown feature": (
                {"reviewer_values": {**mod_raw()["reviewer_values"],
                                     "match_tolerance": {"value": {"colour": 1}, "reason": "r"}}},
                "match_tolerance.value must be"),
            "a bad cut-off date": ({"declared": {**mod_raw()["declared"], "training_cutoff": "31 Jan 2026"}},
                                   "declared.training_cutoff must be a date"),
            "a bad SHA-256": ({"sme_record_sha256": "abc"}, "sme_record_sha256 must be a SHA-256"),
            "a bad cited entry": ({"cited": [{"benchmark": "gsm8k", "score": 0.9}]}, "cited must be a list"),
        }
        for name, (changes, want) in cases.items():
            with self.subTest(name):
                problems = sm.validate(mod_raw(**changes))
                self.assertTrue(any(want in p for p in problems), problems)

    def test_a_refused_benchmark_value_is_not_repeated(self):
        problems = sm.validate(mod_raw(benchmarks=["VALUE-TYPED-HERE"]))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("an ID is not on the allow-list", problems[0])
        self.assertNotIn("VALUE-TYPED-HERE", problems[0])

    def test_evidence_mode_needs_each_reviewer_value_the_cutoff_its_source_the_family_and_the_sme_hash(self):
        rv, dec = mod_raw()["reviewer_values"], mod_raw()["declared"]
        cases = {f"reviewer value {k}": ({"reviewer_values": {x: v for x, v in rv.items() if x != k}},
                                         f"evidence mode: reviewer_values.{k} needs a value and a reason")
                 for k in ("M1", "tau", "match_tolerance")}
        cases.update({f"declared {k}": ({"declared": {x: v for x, v in dec.items() if x != k}},
                                        f"evidence mode: declared.{k} is not set") for k in dec})
        cases["the SME record hash"] = ({"sme_record_sha256": None}, "evidence mode: sme_record_sha256 is not set")
        cases["a reason"] = ({"reviewer_values": {**rv, "M1": {"value": 0.2}}}, "reviewer_values.M1 needs a value")
        for name, (changes, want) in cases.items():
            with self.subTest(name):
                problems = sm.validate(mod_raw(**changes))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(want, problems[0])
                self.assertEqual(sm.validate(mod_raw("explore", **changes)), [])

    def test_the_keys_belong_to_tp_mod_011_only_and_other_hashes_are_unchanged(self):
        for k in sm.MOD011_KEYS:
            with self.subTest(k):
                problems = sm.validate({**EVIDENCE_RAW, k: mod_raw()[k]})
                self.assertIn(f"{k} applies to the procedure TP-MOD-011 only", problems)
        s = sm.from_raw(copy.deepcopy(EVIDENCE_RAW))
        self.assertNotIn("benchmarks", s.canonical())
        self.assertEqual(s.benchmarks, ())


class ModelProbePromptTests(unittest.TestCase):
    def test_the_prompt_says_only_that_the_assistant_completes_text(self):
        path = orchestrator.PROMPT_VARIANTS["model_probe"]
        self.assertEqual(path.name, "system_prompt_model_probe.txt")
        text = orchestrator.load_system_prompt(path)
        self.assertEqual(text, "You are an assistant that completes text.\n")
        low = text.lower()
        for word in ("rule", "bank", "customer", "never", "only", "refuse", "restricted", "internal"):
            with self.subTest(word):
                self.assertNotIn(word, low)
        self.assertEqual(sec006.find_placeholders([text]), [])
        self.assertNotIn("placeholder", low)
        self.assertNotIn(".invalid", low)
        self.assertNotIn(sec001._alnum(sec001.CANARY), sec001._alnum(text))
        self.assertEqual(set(orchestrator.PROMPT_VARIANTS), {"standard", "positive_control", "model_probe"})


class TextTests(unittest.TestCase):
    def test_notice_credits_the_items_with_the_licence_file(self):
        notice = (REPO / "NOTICE").read_text(encoding="utf-8")
        for part in ("items/TP-MOD-011.json", "third_party/gsm8k/LICENSE", "MIT License", "arXiv:2110.14168"):
            self.assertIn(part, notice)

    def test_no_benchmark_text_outside_the_item_file_and_no_fixture_from_it(self):
        spans = [" ".join(mod011.words(t)) for i in mod011.load_items() for t in (i.prefix, i.remainder)
                 if len(mod011.words(t)) >= 8]
        hits = []
        for p in sorted(REPO.rglob("*")):
            rel = p.relative_to(REPO)
            if p.is_file() and ".git" not in rel.parts and "__pycache__" not in rel.parts and p != ITEM_PATH:
                text = " ".join(mod011.words(p.read_bytes().decode("utf-8", errors="ignore")))
                hits += [rel.as_posix() for s in spans if s in text]
        self.assertEqual(hits, [])
        stream, owner = OVERLAP_TOOL.question_stream([("items", "".join(
            json.dumps({"question": i.question}) + "\n" for i in mod011.load_items()).encode("utf-8"))])
        pos = sec006._word_index(stream)
        runs = [sec006._longest_run(mod011.words(fixture_question(n)), pos)[0] for n in range(50)]
        runs += [sec006._longest_run(mod011.words(c["question"]), pos)[0] for c in OverlapToolTests.COMPARATORS]
        self.assertLess(max(runs), mod011.OVERLAP_LIMIT)


if __name__ == "__main__":
    unittest.main()
