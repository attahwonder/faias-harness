"""TP-SEC-002, the writer's kit (faias_harness/engine/sec002_items.py): the check of an item file at any path, the
form, fill-activity and the section of items/README.md. No network.

Every turn of these tests comes from the neutral fixture of tests/helpers.py (sec002_neutral_fixture). A fault in a
turn is a small edit of a fixture turn: one word or one token is added. No test holds a marker or a product name of
items/TP-SEC-001.json; each test reads them from that file. The form writes no turn text: its earlier turns are empty.
"""
import ast
import contextlib
import copy
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from faias_harness.engine import sec001, sec002, sec002_items, sec006
from tests import helpers

REPO = Path(__file__).resolve().parents[1]
BASE_DATA = json.loads((REPO / "items" / "TP-SEC-001.json").read_text(encoding="utf-8"))
BASE = {i["id"]: i for i in BASE_DATA["items"]}
FIXTURE = helpers.sec002_neutral_fixture(REPO)
POOL = [o["id"] for o in FIXTURE["objectives"]]
ELIGIBLE_B = sorted(o for o in POOL if BASE[o]["class"] in sec002.TRACK_B_CLASSES)     # 16; Track B takes 15
OUTSIDE = {c: next(k for k in sorted(BASE) if BASE[k]["class"] == c and k not in POOL) for c in ("C1", "C2")}
DAY = datetime.date(2026, 11, 2)
SET = sec002_items.SET


def seq(d, sid):
    return next(s for t in sec002.TRACKS for s in d["tracks"][t] if s.get("id") == sid)


def first_a(d, cls):
    """The first Track A sequence on an objective of class `cls`."""
    return next(s for s in d["tracks"]["A"] if BASE[s["objective"]]["class"] == cls)


def all_turns(d):
    """Each turn of a set that is a text that is not empty, read without the code under test."""
    tracks = d.get("tracks") if isinstance(d.get("tracks"), dict) else {}
    return [x for ss in tracks.values() if isinstance(ss, list) for s in ss if isinstance(s, dict)
            for x in s.get("turns") or [] if isinstance(x, str) and x]


def run(argv):
    """main(argv) in this process: (exit code, the text it printed)."""
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = sec002_items.main([str(a) for a in argv])
    return code, out.getvalue()


def blocks(out):
    """The output of check by group: {group: [problem, ...]}, from the header line of each group."""
    found, current = {}, None
    for line in out.splitlines()[1:-1]:
        if line.startswith("  - "):
            found[current].append(line[4:])
        else:
            current = line.rsplit(": ", 1)[0]
            found[current] = []
    return found


def add(pick, n, token):
    """A fault: `token` added to turn n of the sequence that `pick` gives."""
    def change(d):
        s = pick(d)
        s["turns"][n] = s["turns"][n] + token
    return change


def families_four(d):
    d["families"] = d["families"][:4]
    d["sizes"]["families"] = 4


def a_language(d):
    d["languages"] = ["fr"]
    d["sizes"]["languages"] = 1


def drop_last_a(d):
    d["tracks"]["A"].pop()
    d["sizes"]["A"] = 19


def swap_final(d):
    s = seq(d, "A-05")
    s["turns"][-1] = s["turns"][-1][0].swapcase() + s["turns"][-1][1:]


def other_activity(d):
    s = seq(d, "B-03")
    s["activity"] = next(r["activity"] for r in BASE_DATA["activity_rule"]["rules"] if r["activity"] != s["activity"])


FIRST_A = {c: first_a(FIXTURE, c) for c in ("C1", "C2", "C4")}
MARKER_PART = sec002.marker_parts(BASE[FIRST_A["C1"]["objective"]]["marker"])[0]
PRODUCT = BASE[FIRST_A["C2"]["objective"]]["product"]
ASK = "; the ask is at the final turn only (line 18)"
KEY_LIKE = "sk" + "-" + "A" * 24        # made here, so the sweep does not see a key shape in this file
MARK = sec006.CREDENTIAL_MARK.pattern.split("(")[0]

# One fault for each problem that check_item_set and _sequence_problems name: (name, change, group, phrase). The phrase
# is in the problem as `check` prints it, under its group. A sequence problem is printed without its ID.
FAULTS = [
    ("procedure", lambda d: d.update(procedure="TP-SEC-001"), SET, "the set does not name the procedure TP-SEC-002"),
    ("authors", lambda d: d.update(authors="a model"), SET,
     "the authors value 'a model' is not one of the declared authorship values"),
    ("generated_by", lambda d: d["generated_by"].pop("date"), SET,
     "generated_by does not name the model family and the date"),
    ("objective not an item", lambda d: d["objectives"].append({"id": "C9-01", "class": "C9"}), SET,
     "objective C9-01: not an item of items/TP-SEC-001.json"),
    ("objective class", lambda d: d["objectives"][0].update({"class": "C2"}), SET,
     f"objective {POOL[0]}: the class is not C1, the class in items/TP-SEC-001.json"),
    ("objective twice", lambda d: d["objectives"].append(dict(d["objectives"][0])), SET,
     "the objective IDs are not unique"),
    ("pool spread", lambda d: d["objectives"].__setitem__(0, {"id": OUTSIDE["C2"], "class": "C2"}), SET,
     "the pool has 20 objectives (5 C1, 5 C2, 4 C3, 6 C4); it must have 20 (6 C1, 4 C2, 4 C3, 6 C4)"),
    ("families", families_four, SET, "the families are not the five of sec002_families.FAMILIES"),
    ("language", a_language, SET, "the set names a language"),
    ("activity rule", lambda d: d["activity_rule"]["rules"].pop(), SET,
     "the activity rule is not the one of items/TP-SEC-001.json"),
    ("tracks", lambda d: d["tracks"].update(C=[]), SET, "the tracks are not A and B"),
    ("sizes", lambda d: d["sizes"].update(A=21), SET, "the field sizes does not match the counts"),
    ("sequence IDs", lambda d: seq(d, "B-02").update(id="B-01"), SET, "the sequence IDs are not unique"),
    ("sequence ID form", lambda d: seq(d, "B-01").update(id="B1"), "B1", "a Track B sequence ID has the form B-<nn>"),
    ("Track A", drop_last_a, SET, "Track A does not have exactly one sequence for each pool objective"),
    ("Track B on C2", lambda d: seq(d, "B-01").update(objective=first_a(d, "C2")["objective"]), "B-01",
     "a Track B sequence is on a C2 objective"),
    ("Track B order", lambda d: seq(d, "B-15").update(objective=ELIGIBLE_B[15]), SET,
     "Track B does not have exactly one sequence for each of the first 15 C1, C3 and C4 pool objectives in ID order"),
    ("not a list of texts", lambda d: seq(d, "A-02")["turns"].__setitem__(0, ""), "A-02",
     "the turns are not a list of texts"),
    ("turn count", lambda d: seq(d, "A-01")["turns"].pop(0), "A-01", "5 turns; a sequence has 6 to 12 turns (line 18)"),
    ("technique", lambda d: seq(d, "A-03").update(technique=""), "A-03", "the sequence names no technique"),
    ("objective not in the pool", lambda d: seq(d, "A-04").update(objective=OUTSIDE["C1"]), "A-04",
     f"the objective {OUTSIDE['C1']} is not in the pool"),
    ("final turn", swap_final, "A-05", "the final turn is not the TP-SEC-001 text of"),
    ("marker part", add(lambda d: first_a(d, "C1"), 1, " " + MARKER_PART), FIRST_A["C1"]["id"],
     f"turn 2 holds a part of the marker ({MARKER_PART}){ASK}"),
    ("product name", add(lambda d: first_a(d, "C2"), 1, " " + PRODUCT), FIRST_A["C2"]["id"],
     f"turn 2 holds the product name{ASK}"),
    ("internal reference", add(lambda d: first_a(d, "C4"), 1, " internal reference"), FIRST_A["C4"]["id"],
     f'turn 2 holds the words "internal reference"{ASK}'),
    ("activity", other_activity, "B-03", "the activity is not the one that the activity rule gives"),
    ("banned stem", add(lambda d: seq(d, "B-04"), 0, " film"), "B-04", "banned stem 'film'"),
    ("key-like string", add(lambda d: seq(d, "B-05"), 0, " " + KEY_LIKE), "B-05", "a key-like string"),
    ("placeholder", add(lambda d: seq(d, "B-06"), 0, " " + MARK), "B-06", "a placeholder or its marking"),
    ("canary", add(lambda d: seq(d, "B-07"), 0, " " + sec001.CANARY), "B-07", "the canary of the system prompt"),
]


class Folder(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def save(self, data, name="TP-SEC-002.json"):
        path = self.dir / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def assert_no_turn(self, out, *sets):
        """No turn of the sets is in the output. The final turns are the TP-SEC-001 texts, so they are checked too."""
        for d in sets:
            for turn in all_turns(d):
                self.assertNotIn(turn, out)


class CheckTests(Folder):
    """check: the check of an item file at any path."""

    def test_the_full_size_fixture_gives_exit_0(self):
        path = self.save(FIXTURE)
        code, out = run(["check", path])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines(), [f"TP-SEC-002 item file: {path}", "the set: no problem",
                                            "35 of 35 sequences with no problem"])
        self.assert_no_turn(out, FIXTURE)

    def test_the_module_runs_as_a_command(self):
        path = self.save(FIXTURE)
        command = [sys.executable, "-X", "utf8", "-m", "faias_harness.engine.sec002_items", "check", str(path)]
        p = subprocess.run(command, cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("35 of 35 sequences with no problem", p.stdout)

    def test_each_fault_is_named_under_its_group_with_exit_1_and_no_turn_text(self):
        names = [f[0] for f in FAULTS]
        self.assertEqual(len(names), len(set(names)))
        for name, change, where, phrase in FAULTS:
            with self.subTest(name):
                d = copy.deepcopy(FIXTURE)
                change(d)
                code, out = run(["check", self.save(d)])
                self.assertEqual(code, 1, out)
                found = blocks(out)
                self.assertIn(where, found, out)
                self.assertTrue(any(phrase in p for p in found[where]), (where, phrase, out))
                self.assert_no_turn(out, d, FIXTURE)

    def test_the_problems_are_grouped_by_sequence_and_counted(self):
        d = copy.deepcopy(FIXTURE)
        d["sizes"]["A"] = 21
        seq(d, "A-03")["technique"] = ""
        other_activity(d)
        seq(d, "A-03")["turns"][1] += " film"
        path = self.save(d)
        code, out = run(["check", path])
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines(), [
            f"TP-SEC-002 item file: {path}",
            "the set: 1 problem",
            "  - the field sizes does not match the counts",
            "A-03: 2 problems",
            "  - the sequence names no technique",
            "  - banned stem 'film'",
            "B-03: 1 problem",
            "  - the activity is not the one that the activity rule gives",
            "33 of 35 sequences with no problem"])

    def test_a_sequence_problem_alone_gives_exit_1(self):
        d = copy.deepcopy(FIXTURE)
        seq(d, "B-15")["technique"] = ""
        code, out = run(["check", self.save(d)])
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[1:], ["the set: no problem", "B-15: 1 problem",
                                                "  - the sequence names no technique",
                                                "34 of 35 sequences with no problem"])

    def test_a_file_that_is_not_one_json_object_is_refused(self):
        bad = self.dir / "bad.json"
        bad.write_text('{"procedure": "TP-SEC-002",', encoding="utf-8")
        listed = self.dir / "list.json"
        listed.write_text("[]", encoding="utf-8")
        twice = self.dir / "twice.json"
        text = json.dumps(FIXTURE, ensure_ascii=False)
        twice.write_text(text.replace('"turns": [', '"turns": [], "turns": [', 1), encoding="utf-8")
        cases = [(bad, "check refused: the file is not valid JSON"),
                 (listed, "check refused: the file does not hold one JSON object"),
                 (twice, "check refused: a JSON object holds the key 'turns' more than once"),
                 (self.dir / "absent.json", "check refused: the file cannot be read")]
        for path, want in cases:
            with self.subTest(path.name):
                code, out = run(["check", path])
                self.assertEqual(code, 1, out)
                self.assertTrue(out.startswith(want), out)
                self.assert_no_turn(out, FIXTURE)


class FormTests(Folder):
    """form: the draft file."""

    def form_file(self, objectives=POOL, authors="a person", name="form.json"):
        out = self.dir / name
        return (*run(["form", "--objectives", *objectives, "--authors", authors, "--out", out]), out)

    def test_the_form_of_a_valid_pool(self):
        code, printed, out = self.form_file()
        self.assertEqual(code, 0, printed)
        self.assertEqual(printed.strip(), f"wrote {out}: 20 Track A and 15 Track B sequences, each with 5 empty "
                                          "earlier turns and its final turn")
        raw = out.read_bytes()
        d = json.loads(raw.decode("utf-8"))
        self.assertEqual(list(d), ["procedure", "authors", "generated_by", "objectives", "tracks", "families",
                                   "languages", "activity_rule", "sizes"])
        self.assertEqual((d["procedure"], d["authors"], d["languages"]), ("TP-SEC-002", "a person", []))
        self.assertEqual(d["generated_by"], {"family": "none: a person wrote the turns",
                                             "date": datetime.date.today().isoformat(),
                                             "prompt": "none: no model prompt; a person wrote the turns"})
        self.assertEqual(d["objectives"], FIXTURE["objectives"])
        for key in ("families", "activity_rule", "sizes"):
            self.assertEqual(d[key], FIXTURE[key], key)
        self.assertEqual(d["activity_rule"], BASE_DATA["activity_rule"])
        self.assertEqual([s["objective"] for s in d["tracks"]["A"]], POOL)
        self.assertEqual([s["objective"] for s in d["tracks"]["B"]], ELIGIBLE_B[:15])
        for t in sec002.TRACKS:
            self.assertEqual([s["id"] for s in d["tracks"][t]], [s["id"] for s in FIXTURE["tracks"][t]])
            for s in d["tracks"][t]:
                self.assertEqual(list(s), ["id", "objective", "activity", "technique", "turns"])
                self.assertEqual((s["activity"], s["technique"], s["turns"][:-1]), (None, "", [""] * 5))
                final = BASE[s["objective"]]["text"]
                self.assertEqual(s["turns"][-1].encode("utf-8"), final.encode("utf-8"))
                self.assertIn(json.dumps(final, ensure_ascii=False).encode("utf-8"), raw)
        # The form holds no text of a turn other than the final turns.
        self.assertEqual(sorted(set(all_turns(d))), sorted({BASE[o]["text"] for o in POOL}))

    def test_the_check_of_a_form_names_only_its_empty_turns_and_then_its_techniques_and_activities(self):
        """check_item_set stops at the empty turns of a sequence. With the earlier turns from the fixture, it names the
        empty technique and the activity of each sequence, and nothing else."""
        d = sec002_items.form(POOL, "a person", today=DAY, repo=REPO)
        self.assertEqual(d["generated_by"]["date"], "2026-11-02")
        problems = sec002.check_item_set(d, REPO)
        ids = [s["id"] for t in sec002.TRACKS for s in d["tracks"][t]]
        self.assertEqual(problems, [f"{i}: the turns are not a list of texts" for i in ids])
        for t in sec002.TRACKS:
            for s, f in zip(d["tracks"][t], FIXTURE["tracks"][t]):
                self.assertEqual((s["id"], s["objective"], s["turns"][-1]), (f["id"], f["objective"], f["turns"][-1]))
                s["turns"] = f["turns"][:-1] + s["turns"][-1:]
        self.assertEqual(sec002.check_item_set(d, REPO),
                         [p for i in ids for p in (f"{i}: the sequence names no technique",
                                                   f"{i}: the activity is not the one that the activity rule gives")])

    def test_track_a_takes_the_order_given_and_track_b_the_id_order(self):
        d = sec002_items.form(POOL[::-1], "a person", today=DAY, repo=REPO)
        self.assertEqual([s["objective"] for s in d["tracks"]["A"]], POOL[::-1])
        self.assertEqual([s["objective"] for s in d["tracks"]["B"]], ELIGIBLE_B[:15])
        self.assertEqual(sec002_items.group(d, sec002.check_item_set(d, REPO))[SET], [])

    def test_a_pool_that_breaks_the_spread_is_refused_with_the_reason(self):
        c1 = [o for o in POOL if BASE[o]["class"] == "C1"]
        c2 = [o for o in POOL if BASE[o]["class"] == "C2"]
        cases = [
            (POOL[:-1], "form refused: the pool has 19 objectives (6 C1, 4 C2, 4 C3, 5 C4); it must have 20 "
                        "(6 C1, 4 C2, 4 C3, 6 C4)"),
            (POOL + [OUTSIDE["C1"]], "form refused: the pool has 21 objectives (7 C1, 4 C2, 4 C3, 6 C4)"),
            ([OUTSIDE["C1"] if o == c2[0] else o for o in POOL],
             "form refused: the pool has 20 objectives (7 C1, 3 C2, 4 C3, 6 C4)"),
            (POOL[:-1] + [c1[0]], "the objective IDs are not unique"),
            (POOL[:-1] + ["C9-01"], "form refused: not an item of items/TP-SEC-001.json: C9-01"),
        ]
        for n, (objectives, want) in enumerate(cases):
            with self.subTest(want):
                code, printed, out = self.form_file(objectives, name=f"pool{n}.json")
                self.assertEqual(code, 1, printed)
                self.assertIn(want, printed)
                self.assertFalse(out.exists())

    def test_the_form_refuses_another_authors_value_and_a_path_that_exists(self):
        code, printed, out = self.form_file(authors="one model family")
        self.assertEqual(code, 1)
        self.assertIn("form refused: the form writes generated_by for the authors value 'a person' only, not "
                      "'one model family'", printed)
        self.assertFalse(out.exists())
        out.write_text("{}", encoding="utf-8")
        code, printed, _ = self.form_file()
        self.assertEqual(code, 1)
        self.assertIn("exists; the form does not write over a file", printed)
        self.assertEqual(out.read_text(encoding="utf-8"), "{}")


def filled_form():
    """The form of the fixture pool, with the earlier turns and the technique of each sequence from the fixture."""
    d = sec002_items.form(POOL, "a person", today=DAY, repo=REPO)
    for t in sec002.TRACKS:
        for s, f in zip(d["tracks"][t], FIXTURE["tracks"][t]):
            s["turns"] = f["turns"][:-1] + s["turns"][-1:]
            s["technique"] = f["technique"]
    return d


class FillActivityTests(Folder):
    """fill-activity, and the path from the form to a file that check accepts."""

    def test_form_then_fixture_turns_then_fill_activity_gives_exit_0(self):
        code, _ = run(["form", "--objectives", *POOL, "--authors", "a person", "--out", self.dir / "x.json"])
        self.assertEqual(code, 0)
        d = json.loads((self.dir / "x.json").read_text(encoding="utf-8"))
        for t in sec002.TRACKS:
            for s, f in zip(d["tracks"][t], FIXTURE["tracks"][t]):
                s["turns"] = f["turns"][:-1] + s["turns"][-1:]
                s["technique"] = f["technique"]
        path = self.save(d)
        code, printed = run(["fill-activity", path])
        self.assertEqual(code, 0)
        self.assertEqual(printed.strip(), f"{path}: the activity of 35 sequences is set from the activity rule")
        code, out = run(["check", path])
        self.assertEqual(code, 0, out)
        self.assertEqual(out.splitlines()[-1], "35 of 35 sequences with no problem")
        after = json.loads(path.read_text(encoding="utf-8"))
        for t in sec002.TRACKS:
            self.assertEqual(after["tracks"][t], FIXTURE["tracks"][t])     # the activity of the fixture builder too

    def test_fill_activity_changes_only_the_activity(self):
        d = filled_form()
        d["status"] = FIXTURE["status"]                     # a key that the form does not write is kept
        seq(d, "A-02")["activity"] = "fee refund"
        seq(d, "B-01")["turns"][0] = 7                     # not a list of texts: its activity is kept
        seq(d, "B-01")["activity"] = "kept"
        before = copy.deepcopy(d)
        path = self.save(d)
        code, printed = run(["fill-activity", path])
        self.assertEqual(code, 0)
        self.assertIn("34 sequences is set from the activity rule; 1 kept their activity, because their turns are "
                      "not a list of texts: B-01", printed)
        after = json.loads(path.read_text(encoding="utf-8"))
        rule = BASE_DATA["activity_rule"]
        for t in sec002.TRACKS:
            for s in after["tracks"][t]:
                want = "kept" if s["id"] == "B-01" else sec006.activity_of(" ".join(s["turns"]), rule)
                self.assertEqual(s["activity"], want, s["id"])
        for d2 in (before, after):
            for s in sec002_items.sequences(d2):
                s["activity"] = None
        self.assertEqual(after, before)
        self.assertEqual(list(after), list(before))
        self.assertEqual([list(s) for s in sec002_items.sequences(after)],
                         [list(s) for s in sec002_items.sequences(before)])

    def test_fill_activity_refuses_a_file_that_is_not_one_json_object_and_leaves_it(self):
        path = self.dir / "bad.json"
        path.write_text("[1]", encoding="utf-8")
        code, printed = run(["fill-activity", path])
        self.assertEqual(code, 1)
        self.assertIn("fill-activity refused: the file does not hold one JSON object", printed)
        self.assertEqual(path.read_text(encoding="utf-8"), "[1]")
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["bad.json"])


def code_messages():
    """The constant parts of each problem message of check_item_set, _sequence_problems and authorship_problem, read
    from the source of sec002.py: one list for each problems.append(...) or returned text."""
    tree = ast.parse((REPO / "faias_harness" / "engine" / "sec002.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    out = []
    for name in ("check_item_set", "_sequence_problems", "authorship_problem"):
        for node in ast.walk(funcs[name]):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append"
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == "problems"):
                arg = node.args[0]
            elif isinstance(node, ast.Return) and node.value is not None:
                arg = node.value.elts[0] if isinstance(node.value, ast.List) else node.value
            else:
                continue
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.append([arg.value])
            elif isinstance(arg, ast.JoinedStr):
                out.append([v.value for v in arg.values if isinstance(v, ast.Constant)])
    return out


def writer_section():
    text = (REPO / "items" / "README.md").read_text(encoding="utf-8")
    start = text.index("\n## TP-SEC-002.json: for the writer\n")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


CHECK_TABLE = "| The problem holds | What to do |"


def check_rows():
    """The first cell of each row of the table of checks in the section, without its backticks. A row that is not of
    the form | `<start of a message>` | <words> | stays whole, so it matches no message."""
    lines = writer_section().splitlines()
    rows = []
    for line in lines[lines.index(CHECK_TABLE) + 2:]:
        if not line.startswith("|"):
            break
        m = re.fullmatch(r"\| `([^`|]+)` \| [^|]*\S \|", line)
        rows.append(m.group(1) if m else line)
    return rows


class WriterSectionTests(unittest.TestCase):
    """The section "TP-SEC-002.json: for the writer" of items/README.md."""

    def test_each_check_has_one_row_in_the_section(self):
        messages = code_messages()
        self.assertEqual(len(messages), 28)
        rows = check_rows()
        self.assertEqual(len(rows), len(messages), rows)
        for parts in messages:
            with self.subTest(parts):
                self.assertEqual(len([r for r in rows if any(r in p for p in parts)]), 1)
        for r in rows:
            with self.subTest(r):
                self.assertEqual(len([parts for parts in messages if any(r in p for p in parts)]), 1)

    def test_each_fault_of_the_check_test_is_a_row(self):
        rows = check_rows()
        for name, _, _, phrase in FAULTS:
            with self.subTest(name):
                self.assertTrue(any(r in phrase for r in rows), phrase)

    def test_the_section_names_the_commands_and_holds_no_turn_text(self):
        section = writer_section()
        for command in ("python -m faias_harness.engine.sec002_items form --objectives <20 IDs> --authors "
                        '"a person" --out <file>', "python -m faias_harness.engine.sec002_items fill-activity <file>",
                        "python -m faias_harness.engine.sec002_items check <file>"):
            self.assertIn(command, section)
        for turn in list(helpers.SEC002_NEUTRAL_TURNS) + [i["text"] for i in BASE_DATA["items"]]:
            self.assertNotIn(turn, section)


if __name__ == "__main__":
    unittest.main()
