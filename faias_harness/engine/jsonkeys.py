"""Two rules for the JSON files that a person writes and the harness does not pin by hash. First, a JSON object with a
repeated key is refused. Plain json.loads keeps only the last value of such a key, so a person who reads the file could
read a value that the engine does not use. Second, a JSON object with a lone surrogate is refused (the last paragraph).

These readers use unique_keys as the object_pairs_hook of json.loads: the TP-SEC-002 item file (sec002.load_set) and
the writer's kit (sec002_items.read), which name the helper sec002.unique_keys; the TP-SEC-003 item file
(sec003.load_set, run_sec003.load_items); and the TP-MOD-011 comparator file and overlap result
(run_mod011.comparator_problems, run_mod011.estimate). Two tools use it too: tools/mod011_overlap.py for the comparator
file, and tools/sweep_barred.py for a line of a .jsonl file (only_digests).

The loaders of the item and instrument files that ship use it too, although the repository holds those files: the
TP-SEC-001 item file (sec001.load_items, and sec002._pool_set for the TP-SEC-002 pool), the TP-SEC-006, TP-AGT-012 and
TP-DAT-009 item files (sec006.load_set, agt012.load_set, dat009.load_set), and the TP-MOD-011 item and instrument files
(mod011.benchmark_problems, mod011.load_items, mod011.instrument_problems, mod011.load_instrument and
run_mod011.load_items). A changed copy of such a file is refused in the same way.

The same hook refuses a JSON object that holds a lone surrogate in a key or a text value (LoneSurrogate, a subclass of
RepeatedKey). A lone surrogate is the escape \\ud83d with no low half after it. Python reads it as one code point from
U+D800 to U+DFFF. Such a text cannot be written as UTF-8, so a hash, a log line or a written file of it would stop
with a traceback. Each reader above catches RepeatedKey. So it refuses such a file at the same point, and in the same
form, as a file with a repeated key. A surrogate pair (for example the two escapes of an emoji) reads as one character,
and it passes. So does U+FFFD.
"""
from ..text import has_lone_surrogate, lone_surrogate_problem


class RepeatedKey(ValueError):
    """A JSON object with a repeated key. The message names the key. It holds no value. Its subclass LoneSurrogate is
    a JSON object with a lone surrogate, so a reader that catches RepeatedKey refuses both."""


class LoneSurrogate(RepeatedKey):
    """A JSON object with a lone surrogate in a key or a text value. The message names the key. It holds no value."""


def unique_keys(pairs):
    """The pairs of one JSON object as a dict: the object_pairs_hook of json.loads, so it applies to each object at any
    depth. Raises RepeatedKey for a key that the object holds more than once. Then raises LoneSurrogate for each key
    whose key or value holds a lone surrogate: a text, or a text in a list (text.has_lone_surrogate). An object in the
    value was read at its own call."""
    keys = [k for k, _ in pairs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise RepeatedKey(f"a JSON object holds the key {', '.join(repr(k) for k in repeated)} more than once")
    lone = [k for k, v in pairs if has_lone_surrogate(k) or has_lone_surrogate(v)]
    if lone:
        raise LoneSurrogate(lone_surrogate_problem("a JSON object", lone))
    return dict(pairs)
