"""faias_harness/yamlio.py: YAML with libyaml where PyYAML has it, and the same objects as the pure-Python loader."""
import types
import unittest
from pathlib import Path
from unittest import mock

import yaml

from faias_harness import yamlio

REPO = Path(__file__).resolve().parents[1]


def yaml_files():
    return sorted(p for p in REPO.rglob("*") if p.suffix in (".yaml", ".yml") and p.is_file()
                  and not {".git", "runs", "__pycache__"} & set(p.relative_to(REPO).parts))


class YamlioTests(unittest.TestCase):
    def test_both_loaders_give_equal_objects_for_every_yaml_file_of_the_repository(self):
        files = yaml_files()
        self.assertGreater(len(files), 50)
        for p in files:
            text = p.read_text(encoding="utf-8")
            with self.subTest(file=p.relative_to(REPO).as_posix()):
                self.assertEqual(yamlio.load(text), yaml.load(text, Loader=yaml.SafeLoader))
                self.assertEqual(yamlio.load(text.encode("utf-8")), yaml.load(text, Loader=yaml.SafeLoader))

    def test_the_helper_uses_libyaml_where_pyyaml_has_it(self):
        self.assertIs(yamlio.loader(), getattr(yaml, "CSafeLoader", yaml.SafeLoader))

    def test_without_libyaml_the_helper_falls_back_to_the_safe_loader(self):
        no_c = types.SimpleNamespace(SafeLoader=yaml.SafeLoader, load=yaml.load)     # PyYAML built without libyaml
        with mock.patch.object(yamlio, "yaml", no_c):
            self.assertIs(yamlio.loader(), yaml.SafeLoader)
            self.assertEqual(yamlio.load("a: [1, 2]\nb: null\n"), {"a": [1, 2], "b": None})

    def test_the_loader_is_safe(self):
        with self.assertRaises(yaml.YAMLError):
            yamlio.load("!!python/object/apply:os.system ['echo x']")

    def test_no_product_or_tool_file_calls_safe_load(self):
        sites = [f"{p.relative_to(REPO).as_posix()}:{n}" for d in ("faias_harness", "targets", "tools")
                 for p in sorted((REPO / d).rglob("*.py"))
                 for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if "yaml.safe_load(" in line]
        self.assertEqual(sites, [])


if __name__ == "__main__":
    unittest.main()
