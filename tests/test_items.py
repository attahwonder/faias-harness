"""The TP-SEC-001 item set in items/: its sizes, and a credit in NOTICE and a licence text for each outside source.
Then the loaders of the item and instrument files that ship: each refuses a JSON object with a repeated key
(jsonkeys.unique_keys), and each shipped file still loads. The changed files are neutral JSON in a temporary folder."""
import hashlib
import json
import re
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock

from faias_harness.engine import agt012, dat009, jsonkeys, mod011, run_mod011, sec001, sec002, sec006
from faias_harness.engine.sec001 import CANARY

REPO = Path(__file__).resolve().parents[1]
SET = json.loads((REPO / "items" / "TP-SEC-001.json").read_text(encoding="utf-8"))
NOTICE = (REPO / "NOTICE").read_text(encoding="utf-8")


def alnum(s):
    return re.sub(r"[^A-Z0-9]", "", s.upper())


class ItemSetTests(unittest.TestCase):
    def test_sizes(self):
        sizes = {"C1": 40, "C2": 25, "C3": 26, "C4": 40}
        self.assertEqual(Counter(i["class"] for i in SET["items"]), sizes)
        self.assertEqual(SET["sizes"], {**sizes, "baseline": 25})
        self.assertEqual(len(SET["baseline"]), 25)

    def test_ids_unique(self):
        ids = [i["id"] for i in SET["items"]] + [b["id"] for b in SET["baseline"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_each_outside_source_is_credited_with_its_licence_text(self):
        used = {i["source"] for i in SET["items"]}
        self.assertTrue(used <= set(SET["sources"]), used - set(SET["sources"]))
        for key in used:
            src = SET["sources"][key]
            if "licence_file" not in src:
                continue  # written for this harness: the repository's own licence
            self.assertTrue((REPO / src["licence_file"]).is_file(), src["licence_file"])
            self.assertIn(src["revision"], NOTICE, key)
            self.assertIn(src["licence_file"], NOTICE, key)

    def test_no_item_holds_the_canary_and_no_c1_item_holds_its_joined_marker(self):
        for i in SET["items"] + SET["baseline"]:
            self.assertNotIn(alnum(CANARY), alnum(i["text"]), i["id"])
            if i.get("marker"):
                self.assertNotIn(alnum(i["marker"]), alnum(i["text"]), i["id"])


class RepeatedKeyTests(unittest.TestCase):
    """Each loader of a shipped item or instrument file reads with jsonkeys.unique_keys. Plain json.loads keeps only
    the last value of a repeated key, so a person who reads a changed copy could read a value that the engine does not
    use. Each test writes a neutral copy with the key twice into a temporary folder and gives that folder as the
    repository, or patches the path of the file."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)

    def write(self, rel, text):
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def refused(self, call, key):
        with self.assertRaises(jsonkeys.RepeatedKey) as cm:
            call()
        self.assertEqual(str(cm.exception), f"a JSON object holds the key '{key}' more than once")

    def test_the_tp_sec_001_item_file(self):
        self.write("items/TP-SEC-001.json", '{"items": [], "items": [], "baseline": []}')
        self.refused(lambda: sec001.load_items("TP-SEC-001", self.repo), "items")

    def test_the_tp_sec_001_item_file_as_the_tp_sec_002_pool(self):
        self.write("items/TP-SEC-001.json", '{"activity_rule": {}, "activity_rule": {}}')
        with mock.patch.object(sec001, "load_items", lambda item_set, repo: ((), ())):
            self.refused(lambda: sec002._pool_set(self.repo), "activity_rule")

    def test_the_tp_sec_006_item_file(self):
        self.write(f"items/{sec006.ITEM_SET}.json", '{"items": [], "items": []}')
        self.refused(lambda: sec006.load_set(self.repo), "items")

    def test_the_tp_agt_012_item_file(self):
        self.write(agt012.ITEM_FILE, '{"items": [], "items": []}')
        self.refused(lambda: agt012.load_set(self.repo), "items")

    def test_the_tp_dat_009_item_file(self):
        self.write(dat009.ITEM_FILE, '{"items": [], "items": []}')
        self.refused(lambda: dat009.load_set(self.repo), "items")

    def test_the_tp_mod_011_item_file_is_refused_in_one_problem(self):
        """A copy with the key twice, with a digest file that names it and a pin of that digest file: the allow-list
        check gives one problem, in the form of the TP-SEC-002 refusal, and load_items refuses with it."""
        item = self.write("items/TP-MOD-011.json", '{"procedure": "TP-MOD-011", "procedure": "TP-MOD-011", '
                                                   '"items": []}')
        licence = REPO / mod011.ALLOW_LIST["gsm8k"]["licence_file"]
        digests = self.write("gsm8k.sha256", "".join(f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n"
                                                     for p in (licence, item)))
        entry = {**mod011.ALLOW_LIST["gsm8k"], "item_file": str(item), "digest_file": str(digests),
                 "digest_prefix": hashlib.sha256(digests.read_bytes()).hexdigest()[:mod011.DIGEST_PREFIX_HEX]}
        want = f"gsm8k: the item file {item} is refused: a JSON object holds the key 'procedure' more than once"
        with mock.patch.dict(mod011.ALLOW_LIST, {"gsm8k": entry}):
            self.assertEqual(mod011.benchmark_problems("gsm8k"), [want])
            with self.assertRaises(ValueError) as cm:
                mod011.load_items("gsm8k")
            self.assertEqual(str(cm.exception), want)

    def test_the_tp_mod_011_item_file_after_its_check(self):
        item = self.write("items/TP-MOD-011.json", '{"benchmark": "gsm8k", "benchmark": "gsm8k", "items": []}')
        with mock.patch.dict(mod011.ALLOW_LIST, {"gsm8k": {**mod011.ALLOW_LIST["gsm8k"], "item_file": str(item)}}), \
                mock.patch.object(mod011, "benchmark_problems", lambda benchmark, repo: []):
            self.refused(lambda: mod011.load_items("gsm8k"), "benchmark")
        self.refused(lambda: run_mod011._read_json(item), "benchmark")

    def test_the_tp_mod_011_instrument_file_is_refused_in_one_problem(self):
        path = self.write("instrument.json", '{"procedure": "TP-MOD-011", "procedure": "TP-MOD-011", "passages": []}')
        digests = self.write("instrument.sha256", f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n")
        prefix = hashlib.sha256(digests.read_bytes()).hexdigest()[:mod011.DIGEST_PREFIX_HEX]
        with mock.patch.object(mod011, "INSTRUMENT_FILE", str(path)), \
                mock.patch.object(mod011, "INSTRUMENT_DIGEST_FILE", str(digests)), \
                mock.patch.object(mod011, "INSTRUMENT_DIGEST_PREFIX", prefix):
            self.assertEqual(mod011.instrument_problems(),
                             [f"the instrument file {path} is refused: a JSON object holds the key 'procedure' more "
                              "than once"])

    def test_the_tp_mod_011_instrument_file_after_its_check(self):
        path = self.write("instrument.json", '{"passages": [], "passages": []}')
        with mock.patch.object(mod011, "INSTRUMENT_FILE", str(path)), \
                mock.patch.object(mod011, "instrument_problems", lambda repo: []):
            self.refused(lambda: mod011.load_instrument(), "passages")

    def test_each_shipped_item_and_instrument_file_still_loads(self):
        self.assertEqual(len(sec001.load_items("TP-SEC-001")[0]), len(SET["items"]))
        self.assertEqual(len(sec002._pool_set(REPO)[0]), len(SET["items"]))
        for loader in (sec006.load_set, agt012.load_set, dat009.load_set):
            with self.subTest(loader.__module__):
                self.assertIsInstance(loader(REPO), dict)
        self.assertEqual((mod011.benchmark_problems("gsm8k"), mod011.instrument_problems()), ([], []))
        self.assertEqual((len(mod011.load_items()), len(mod011.load_instrument())), (50, mod011.INSTRUMENT_COUNT))
        self.assertEqual(len(run_mod011._read_json(REPO / mod011.ITEM_FILE)["items"]), 50)


if __name__ == "__main__":
    unittest.main()
