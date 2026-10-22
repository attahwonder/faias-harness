"""The TP-SEC-001 item set in items/: its sizes, and a credit in NOTICE and a licence text for each outside source."""
import json
import re
import unittest
from collections import Counter
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
