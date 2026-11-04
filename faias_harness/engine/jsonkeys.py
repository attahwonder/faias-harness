"""One rule for the JSON files that a person writes and the harness does not pin by hash: a JSON object with a repeated
key is refused. Plain json.loads keeps only the last value of such a key, so a person who reads the file could read a
value that the engine does not use.

These readers use unique_keys as the object_pairs_hook of json.loads: the TP-SEC-002 item file (sec002.load_set) and
the writer's kit (sec002_items.read), which name the helper sec002.unique_keys; the TP-SEC-003 item file
(sec003.load_set, run_sec003.load_items); and the TP-MOD-011 comparator file and overlap result
(run_mod011.comparator_problems, run_mod011.estimate). Two tools use it too: tools/mod011_overlap.py for the comparator
file, and tools/sweep_barred.py for a line of a .jsonl file (only_digests).
"""


class RepeatedKey(ValueError):
    """A JSON object with a repeated key. The message names the key. It holds no value."""


def unique_keys(pairs):
    """The pairs of one JSON object as a dict: the object_pairs_hook of json.loads, so it applies to each object at any
    depth. Raises RepeatedKey for a key that the object holds more than once."""
    keys = [k for k, _ in pairs]
    repeated = sorted({k for k in keys if keys.count(k) > 1})
    if repeated:
        raise RepeatedKey(f"a JSON object holds the key {', '.join(repr(k) for k in repeated)} more than once")
    return dict(pairs)
