"""Two rules for text that the harness does not write itself.

A JSON body can hold a lone surrogate: the escape \\ud83d with no low half after it. Python reads it as one code point
from U+D800 to U+DFFF, and such a string cannot be written as UTF-8. A log line, a SHA-256 and a printed line all need
UTF-8.

- `clean` replaces each lone surrogate with U+FFFD (the Unicode replacement character). The harness applies it where
  it reads a reply from outside the machine: the body of a channel response (adapters/base.py, `ChannelTarget._json`)
  and the body of a model host response (targets/srd1/app/gateway.py). So no record, hash, score or printed line sees
  a lone surrogate from a reply. The canonical form of a log line does not change.
- `escape_streams` sets stdout and stderr to print a character that the stream cannot encode as an escape (for
  example \\ud83d), not to stop with a traceback. Each command of the harness calls it first. A file of the operator
  (a settings file, an item file, a log) can hold a lone surrogate too, and a command can print text from it.
"""
import re
import sys

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


def escape_streams():
    """Set stdout and stderr to errors="backslashreplace", where the stream allows it."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
