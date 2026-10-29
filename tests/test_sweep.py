"""Tests of the sweep for barred terms and key-like strings (tools/sweep_barred.py). A digest in a line of a .jsonl,
.jsonl.sig or .sha256 file is not a key-like string. Nor is the commit that pins an action in a .yml file in
.github/workflows/. Every other run of 40 or more hex characters still is. No network."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("faias_harness_sweep_test", REPO / "tools" / "sweep_barred.py")
sweep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sweep)
DIGEST = "ab" * 32          # 64 lower-case hex characters, as a SHA-256 digest
COMMIT = "cd" * 20          # 40, as a git commit


def hits(files):
    """Write the files to a new folder, sweep it, and return the FAIL lines."""
    with tempfile.TemporaryDirectory() as d:
        for name, text in files.items():
            (Path(d) / name).parent.mkdir(parents=True, exist_ok=True)
            (Path(d) / name).write_text(text, encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sweep.main(["--root", d])
    return [line for line in buf.getvalue().splitlines() if line.startswith("FAIL")]


def line(obj):
    return json.dumps(obj, separators=(",", ":")) + "\n"


class DigestTests(unittest.TestCase):

    def test_a_digest_field_in_a_json_line_is_not_key_like(self):
        rec = {"reply_sha256": DIGEST, "lock": {"hash": DIGEST, "time_ns": 1}, "trace": {"head": DIGEST},
               "code": {"git_commit": COMMIT, "scorer_sha256": DIGEST}, "settings_hash": DIGEST, "sha256": DIGEST}
        self.assertEqual(hits({"run_log.jsonl": line(rec)}), [])

    def test_hex_outside_a_digest_field_still_fails(self):
        for rec in ({"reply": DIGEST}, {"reply": f"the key is {DIGEST}"}, {"note": [DIGEST]},
                    {DIGEST: "a key as a field name"}, {"reply_sha256": DIGEST.upper()},
                    {"reply_sha256": DIGEST + "ab"}, {"reply_sha256": f"{DIGEST} {DIGEST}"},
                    {"reply_sha256": DIGEST, "request": COMMIT}):
            with self.subTest(rec=rec):
                self.assertEqual(len(hits({"run_log.jsonl": line(rec)})), 1)

    def test_a_line_that_is_not_a_json_object_still_fails(self):
        for text in (DIGEST + "\n", f"sha256: {DIGEST}\n", line([DIGEST]), "{" + DIGEST + "\n"):
            with self.subTest(text=text):
                self.assertEqual(len(hits({"notes.txt": text})), 1)

    def test_a_sha256sum_line_passes_in_a_sha256_file_only(self):
        self.assertEqual(hits({"run_log.jsonl.sha256": f"{DIGEST}  run_log.jsonl\n"}), [])
        self.assertEqual(len(hits({"notes.txt": f"{DIGEST}  run_log.jsonl\n"})), 1)
        for text in (f"{DIGEST} run_log.jsonl\n", f"{DIGEST}  run_log.jsonl {DIGEST}\n", f"{DIGEST}\n"):
            with self.subTest(text=text):
                self.assertEqual(len(hits({"run_log.jsonl.sha256": text})), 1)

    def test_the_json_rule_reads_a_jsonl_file_and_its_sig_file_only(self):
        rec = line({"reply_sha256": DIGEST, "code": {"git_commit": COMMIT}})
        for name in ("settings.yaml", "notes.md", "config.json", "x.py", "anything.txt", "run_log.json"):
            with self.subTest(name=name):
                self.assertEqual(len(hits({name: rec})), 1)
        self.assertEqual(hits({"run_log.jsonl.sig": line({"file": "run_log.jsonl", "sha256": DIGEST})}), [])

    def test_a_repeated_key_in_a_json_line_is_key_like(self):
        for text in ('{"reply":"%s","reply":"ok"}\n' % DIGEST, '{"sha256":"%s","sha256":"%s"}\n' % (DIGEST, DIGEST),
                     '{"lock":{"hash":"%s","hash":"%s"}}\n' % (DIGEST, COMMIT)):
            with self.subTest(text=text):
                self.assertEqual(len(hits({"run_log.jsonl": text})), 1)

    def test_a_key_name_in_quotes_with_a_value_is_key_like(self):
        value = "QUJDREVGR0hJSktMTU5PUA=="      # base64 of a made-up text, not a key
        for name, text in (("run_log.jsonl", '{"secret":"%s"}\n' % value),
                           ("config.json", '{"note": "x", "api_key": "%s"}\n' % value),
                           ("x.py", "SETTINGS = {'token': '%s'}\n" % value),
                           ("notes.md", '"password" = "%s"\n' % value),
                           ("settings.yaml", 'secret: "%s"\n' % value)):
            with self.subTest(text=text):
                self.assertEqual(len(hits({name: text})), 1)

    def test_the_other_key_patterns_still_read_a_digest_field(self):
        fake = "sk-" + "a1" * 12            # the shape of a vendor key, not a key
        self.assertEqual(len(hits({"run_log.jsonl": line({"reply_sha256": fake})})), 1)
        self.assertEqual(len(hits({"run_log.jsonl": line({"reply_sha256": DIGEST, "reply": "-----BEGIN "
                                                          "PRIVATE KEY-----"})})), 1)

    def test_a_json_number_of_40_or_more_digits_is_key_like(self):
        digits = "1" * 40
        for text in ('{"n":%s}\n' % digits, '{"x":0.%s}\n' % digits, '{"x":-%s}\n' % ("9" * 64),
                     '{"hash":%s}\n' % digits, '{"reply_sha256":"%s","n":%s}\n' % (DIGEST, digits)):
            with self.subTest(text=text):
                self.assertEqual(len(hits({"run_log.jsonl": text})), 1)
        self.assertEqual(hits({"run_log.jsonl": '{"reply_sha256":"%s","n":%s}\n' % (DIGEST, "1" * 39)}), [])

    def test_a_sha256sum_line_with_hex_in_its_name_is_key_like(self):
        for name in (DIGEST, COMMIT, DIGEST.upper(), COMMIT + ".jsonl"):
            with self.subTest(name=name):
                self.assertEqual(len(hits({"run_log.jsonl.sha256": f"{DIGEST}  {name}\n"})), 1)
        self.assertEqual(hits({"run_log.jsonl.sha256": f"{DIGEST}  {'ab' * 19}a.jsonl\n"}), [])     # 39 hex

    def test_an_action_pinned_by_commit_passes_in_a_workflow_file_only(self):
        ok = f"      - uses: actions/checkout@{COMMIT}  # v6.0.0\n"
        self.assertEqual(hits({".github/workflows/tests.yml": ok}), [])
        self.assertEqual(hits({".github/workflows/tests.yml": f"    uses: actions/setup-python@{COMMIT}\n"}), [])
        for name in ("tests.yml", ".github/tests.yml", ".github/workflows/tests.yaml", "docs/workflows/tests.yml",
                     ".github/workflows/notes.md"):
            with self.subTest(name=name):
                self.assertEqual(len(hits({name: ok})), 1)
        for text in (f"      - uses: actions/checkout@{DIGEST}\n", f"      - uses: actions/checkout@{COMMIT.upper()}\n",
                     f"      - uses: actions/checkout@{COMMIT}  # {COMMIT}\n", f"      - run: echo {COMMIT}\n",
                     f"      - uses: actions@{COMMIT}\n", f"      - uses: a/b/c@{COMMIT}\n",
                     f"      - uses: actions/checkout@{COMMIT} {COMMIT}\n"):
            with self.subTest(text=text):
                self.assertEqual(len(hits({".github/workflows/tests.yml": text})), 1)


if __name__ == "__main__":
    unittest.main()
