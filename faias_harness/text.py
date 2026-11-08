"""Rules for text that the harness does not write itself.

A JSON body can hold a lone surrogate: the escape \\ud83d with no low half after it. Python reads it as one code point
from U+D800 to U+DFFF, and such a string cannot be written as UTF-8. A log line, a SHA-256 and a printed line all need
UTF-8.

- `clean` replaces each lone surrogate with U+FFFD (the Unicode replacement character). The harness applies it where
  it reads a reply from outside the machine: the body of a channel response (adapters/base.py, `ChannelTarget._json`)
  and the body of a model host response (targets/srd1/app/gateway.py). So no record, hash, score or printed line sees
  a lone surrogate from a reply. The canonical form of a log line does not change. The MCP tool emulator applies it
  to the body of each request (emulators/mcp_tools.py). The target sends that request from model output.
- `escape_streams` sets stdout and stderr to print a character that the stream cannot encode as an escape (for
  example \\ud83d), not to stop with a traceback. Each command of the harness calls it first. A file of the operator
  (a settings file, an item file, a log) can hold a lone surrogate too, and a command can print text from it.
- `has_lone_surrogate`, `lone_surrogate_keys` and `lone_surrogate_problem` find a lone surrogate in a file of the
  operator, so that its loader refuses the file with one problem that names the key. The JSON loaders use them
  through jsonkeys.unique_keys. The settings and profile loaders use them after the YAML load: libyaml refuses a YAML
  escape of a surrogate (a ScannerError), but the pure-Python loader reads it as a lone surrogate. So do the loaders
  of four more YAML files of the operator. Three are the firm-values file (profile.read_firm_values), and the tools
  file and the store of the tool emulator (emulators/). The fourth is the TP-SEC-002 declaration of the channel
  inputs (engine/run_sec002.py). The reader of the SRD-1 risk-appetite file uses them too
  (targets/srd1/app/gate.py, read_risk_appetite).
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


def has_lone_surrogate(value):
    """True when `value` holds a lone surrogate: a string, or a string in a list at any depth. A dict is not read:
    the JSON hook (jsonkeys.unique_keys) reads each object at its own call, and lone_surrogate_keys reads each
    mapping."""
    if isinstance(value, str):
        return LONE_SURROGATE.search(value) is not None
    if isinstance(value, list):
        return any(has_lone_surrogate(v) for v in value)
    return False


def lone_surrogate_keys(value):
    """The keys of each mapping in `value`, at any depth, whose key or value holds a lone surrogate
    (has_lone_surrogate), in the order of the file, each once."""
    out = []
    if isinstance(value, dict):
        for k, v in value.items():
            if has_lone_surrogate(k) or has_lone_surrogate(v):
                out.append(k)
            out += lone_surrogate_keys(v)
    elif isinstance(value, list):
        for v in value:
            out += lone_surrogate_keys(v)
    return list(dict.fromkeys(out))


def lone_surrogate_problem(holder, keys):
    """The one problem when `holder` holds a lone surrogate in the key or the value of each of `keys`.
    `holder` is "a JSON object", "a YAML mapping" or a name of the file, such as "the store". Each key is given by
    repr, so a key with a lone surrogate shows as an escape, for example 'a\\ud83d'. So the problem can be printed,
    hashed and written as UTF-8."""
    return f"{holder} holds a lone surrogate in the key or the value of {', '.join(repr(k) for k in keys)}"


def escape_streams():
    """Set stdout and stderr to errors="backslashreplace", where the stream allows it."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="backslashreplace")
