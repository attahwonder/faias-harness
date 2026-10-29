"""C-15, minimal: a trace store of JSON lines with a hash chain.

Each line holds `seq`, `prev` (the hash of the previous line), `hash` (the SHA-256 of `prev` and the line without
`hash`), and the event. `verify_chain` finds an edited line, a removed line, an inserted line or a reordered line.
The chain gives tamper evidence for the file. It does not stop a person who rewrites the whole file and the
final hash that was recorded elsewhere.

Each line is JSON as RFC 8259 defines it. `append` refuses a NaN or an infinity (ValueError). `read_entries` refuses
a line that holds the token NaN, Infinity or -Infinity, or a number such as 1e400 that reads as an infinity, so
`verify_chain` fails such a file.

`entries` gives the lines that this store wrote from memory while the file's size and modification time are those
after the store's last write, so a trace request does not read the whole file again. In any other case (the file
was edited, replaced or removed), it reads the file. After a write that finds such a change, it always reads the
file. A change that keeps both the size and the modification time is not seen. `verify_chain` always reads the
file.
"""
import hashlib
import json
import os
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


def _not_json(token):
    raise ValueError(f"{token} is not a JSON number")


def _line_hash(prev, entry):
    blob = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256((prev + blob).encode("utf-8")).hexdigest()


class TraceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS
        self._lines = []        # (session, line) for each line this store wrote; None once the file changed
        self._stamp = self._file_stamp()

    def _file_stamp(self):
        st = os.stat(self.path)
        return st.st_size, st.st_mtime_ns

    def _unchanged(self):
        """True while the file's size and modification time are those after this store's last write."""
        try:
            return self._file_stamp() == self._stamp
        except OSError:
            return False

    @property
    def head(self):
        return self._prev

    def append(self, kind, session, **data):
        with self._lock:
            entry = {"seq": self._seq + 1, "t_ns": time.time_ns(), "kind": kind, "session": session, "prev": self._prev}
            entry.update(data)
            entry["hash"] = _line_hash(self._prev, {k: v for k, v in entry.items() if k != "hash"})
            line = json.dumps(entry, sort_keys=True, ensure_ascii=False, allow_nan=False).encode("utf-8") + b"\n"
            if self._lines is not None and not self._unchanged():
                self._lines = None      # the file changed outside this store: entries() reads it from now on
            with open(self.path, "ab") as f:      # a refused line (ValueError above) leaves the chain as it was
                f.write(line)
            self._seq += 1
            self._prev = entry["hash"]
            if self._lines is not None:
                self._lines.append((session, line))
                self._stamp = self._file_stamp()
            return entry

    def entries(self, session=None):
        """The entries of `session`, or of every session. They are the entries that read_entries gives. The module
        docstring says when they come from memory."""
        with self._lock:
            mine = list(self._lines) if self._lines is not None and self._unchanged() else None
        if mine is None:
            return [e for e in read_entries(self.path) if session is None or e["session"] == session]
        return [json.loads(line) for s, line in mine if session is None or s == session]


def read_entries(path):
    out = []
    for line in Path(path).read_bytes().split(b"\n"):
        if line.strip():
            e = json.loads(line.decode("utf-8"), parse_constant=_not_json)
            json.dumps(e, allow_nan=False)      # 1e400 reads as an infinity, which parse_constant does not see
            out.append(e)
    return out


def verify_chain(path, expected_head=None):
    """Return (ok, problems). Checks the sequence, the links and every hash.

    A removed last line leaves a valid chain. Pass `expected_head`, the head hash that was recorded elsewhere
    (the run log keeps it), to catch that case.
    """
    problems = []
    prev = GENESIS
    seq = 0
    try:
        entries = read_entries(path)
    except (ValueError, OSError) as e:
        return False, [f"cannot read the trace: {type(e).__name__}"]
    for e in entries:
        seq += 1
        if e.get("seq") != seq:
            problems.append(f"line {seq}: sequence number is {e.get('seq')}, expected {seq}")
        if e.get("prev") != prev:
            problems.append(f"line {seq}: link to the previous line is broken")
        body = {k: v for k, v in e.items() if k != "hash"}
        if e.get("hash") != _line_hash(e.get("prev", ""), body):
            problems.append(f"line {seq}: the hash does not match the content")
        prev = e.get("hash", "")
    if expected_head is not None and prev != expected_head:
        problems.append("the last hash differs from the head that was recorded (a line was removed or added)")
    return not problems, problems
