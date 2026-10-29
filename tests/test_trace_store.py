"""The trace store of SRD-1 (targets/srd1/app/trace.py). entries() gives the lines that the store wrote from memory
while the file is as the store left it, and reads the file in any other case. Each answer equals a read of the file."""
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from targets.srd1.app import trace as tr

SESSIONS = [f"s{i}" for i in range(5)]


class TraceStoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "trace.jsonl"

    def from_file(self, session=None):
        return [e for e in tr.read_entries(self.path) if session is None or e["session"] == session]

    def assert_as_file(self, store, sessions=SESSIONS):
        for s in (None, *sessions):
            with self.subTest(session=s):
                self.assertEqual(store.entries(s), self.from_file(s))

    def test_the_entries_equal_the_file_after_many_appends_of_several_sessions(self):
        store = tr.TraceStore(self.path)

        def write(n):
            for k in range(40):
                store.append("turn", SESSIONS[(n + k) % 5], n=n, k=k, text="caf\u00e9", items=(1, 2.5, None, True))

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        with mock.patch.object(tr, "read_entries", side_effect=AssertionError("the file was read")):
            mine = {s: store.entries(s) for s in (None, *SESSIONS)}        # from memory: the file is not read
        self.assertEqual(len(mine[None]), 160)
        for s, got in mine.items():
            self.assertEqual(got, self.from_file(s))
        self.assertEqual(tr.verify_chain(self.path), (True, []))
        # A second writer appends a line, and then the store appends ten more. The entries are those of the file.
        with open(self.path, "ab") as f:
            f.write(json.dumps({"seq": 0, "kind": "other", "session": "s0"}).encode("utf-8") + b"\n")
        for k in range(10):
            store.append("turn", "s0", k=k)
        self.assert_as_file(store)
        self.assertEqual(len(store.entries("s0")), 32 + 1 + 10)

    def test_an_edit_of_the_file_in_the_same_process_shows_in_the_entries(self):
        store = tr.TraceStore(self.path)
        for k in range(5):
            store.append("turn", "s1", reply=f"reply {k}")
        self.assertEqual(store.entries("s1")[2]["reply"], "reply 2")
        self.path.write_bytes(self.path.read_bytes().replace(b"reply 2", b"reply two"))     # a longer line
        self.assertEqual(store.entries("s1")[2]["reply"], "reply two")
        self.assert_as_file(store, ["s1"])
        # An edit that keeps the size, with a later modification time.
        self.path.write_bytes(self.path.read_bytes().replace(b"reply 3", b"reply X"))
        st = os.stat(self.path)
        os.utime(self.path, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
        self.assertEqual(store.entries("s1")[3]["reply"], "reply X")
        self.assert_as_file(store, ["s1"])

    def test_a_restart_over_the_file_gives_the_entries_of_the_file(self):
        old = tr.TraceStore(self.path)
        for k in range(5):
            old.append("turn", "s1", k=k)
        new = tr.TraceStore(self.path)          # a restart: the new store starts the file again
        for k in range(3):
            new.append("turn", "s2", k=k)
        self.assertEqual(len(self.from_file()), 3)
        self.assertEqual(new.entries(), self.from_file())
        self.assertEqual(old.entries(), self.from_file())          # the old store sees the change and reads the file

    def test_a_line_that_another_writer_adds_with_the_store_s_own_write_shows_in_the_entries(self):
        """Another writer appends a line after the store's check and before its write. The file then grows by more
        than the store's own line. So the store reads the file from then on."""
        store = tr.TraceStore(self.path)
        store.append("turn", "s1", k=0)
        real_open = open

        def open_after_another_write(path, mode="r", *a, **k):
            if mode == "ab":
                with real_open(path, "ab") as f:
                    f.write(json.dumps({"seq": 0, "kind": "other", "session": "s2"}).encode("utf-8") + b"\n")
            return real_open(path, mode, *a, **k)

        with mock.patch.object(tr, "open", open_after_another_write, create=True):
            store.append("turn", "s1", k=1)
        self.assertEqual(len(self.from_file()), 3)
        self.assert_as_file(store, ["s1", "s2"])
        store.append("turn", "s1", k=2)
        self.assertEqual(len(store.entries()), 4)
        self.assert_as_file(store, ["s1", "s2"])


if __name__ == "__main__":
    unittest.main()
