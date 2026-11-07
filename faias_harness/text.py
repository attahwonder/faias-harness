"""One rule for text that the harness does not write itself.

A JSON body can hold a lone surrogate: the escape \\ud83d with no low half after it. Python reads it as one code point
from U+D800 to U+DFFF, and such a string cannot be written as UTF-8. A log line, a SHA-256 and a printed line all need
UTF-8.

- `clean` replaces each lone surrogate with U+FFFD (the Unicode replacement character). The harness applies it where
  it reads a reply from outside the machine: the body of a channel response (adapters/base.py, `ChannelTarget._json`)
  and the body of a model host response (targets/srd1/app/gateway.py). So no record, hash, score or printed line sees
  a lone surrogate from a reply. The canonical form of a log line does not change.
"""
import re

LONE_SURROGATE = re.compile("[\ud800-\udfff]")
REPLACEMENT = "\ufffd"


def clean(value):
    """`value` with each lone surrogate replaced with U+FFFD: in a string, and in each key and value of a dict or a
    list, at any depth. Other values are returned as they are."""
    if isinstance(value, str):
        return LONE_SURROGATE.sub(REPLACEMENT, value)
    if isinstance(value, dict):
        return {clean(k): clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    return value
