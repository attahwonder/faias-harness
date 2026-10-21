"""C-15, minimal: a trace store of JSON lines with a hash chain.

Each line holds `seq`, `prev` (the hash of the previous line), `hash` (the SHA-256 of `prev` and the line without
`hash`), and the event. `verify_chain` finds an edited line, a removed line, an inserted line or a reordered line.
The chain gives tamper evidence for the file. It does not stop a person who rewrites the whole file and the
final hash that was recorded elsewhere.
"""
import hashlib
import json
import threading
import time
from pathlib import Path

GENESIS = "0" * 64


def _line_hash(prev, entry):
    blob = json.dumps(entry, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256((prev + blob).encode("utf-8")).hexdigest()


class TraceStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"")
        self._lock = threading.Lock()
        self._seq = 0
        self._prev = GENESIS

    @property
    def head(self):
        return self._prev

    def append(self, kind, session, **data):
        with self._lock:
            self._seq += 1
            entry = {"seq": self._seq, "t_ns": time.time_ns(), "kind": kind, "session": session, "prev": self._prev}
            entry.update(data)
            entry["hash"] = _line_hash(self._prev, {k: v for k, v in entry.items() if k != "hash"})
            with open(self.path, "ab") as f:
                f.write(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")
            self._prev = entry["hash"]
            return entry

    def entries(self, session=None):
        return [e for e in read_entries(self.path) if session is None or e["session"] == session]


def read_entries(path):
    out = []
    for line in Path(path).read_bytes().split(b"\n"):
        if line.strip():
            out.append(json.loads(line.decode("utf-8")))
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
