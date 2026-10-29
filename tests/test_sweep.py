"""The sweep for barred terms and key-like strings (tools/sweep_barred.py): a digest in a signed log or its SHA-256
file is not a key-like string, and every other run of 40 or more hex characters still is. No network."""
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

    def test_the_other_key_patterns_still_read_a_digest_field(self):
        fake = "sk-" + "a1" * 12            # the shape of a vendor key; not a key
        self.assertEqual(len(hits({"run_log.jsonl": line({"reply_sha256": fake})})), 1)
        self.assertEqual(len(hits({"run_log.jsonl": line({"reply_sha256": DIGEST, "reply": "-----BEGIN "
                                                          "PRIVATE KEY-----"})})), 1)


if __name__ == "__main__":
    unittest.main()
