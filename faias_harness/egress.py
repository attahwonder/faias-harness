"""One HTTP client for SRD-1 and the engine, with an allow-list and a log of every request.

Limit: this is a control in the client. It is not a network sandbox. Code that opens its own socket, or a
library that bypasses this client, is not stopped. A full sandbox (H-01 of the SRD-1 specification) is a later
step.

A request to a host that is not on the list is refused: the client raises EgressRefused, sends nothing and logs
the refusal. A redirect is not followed. The client ignores proxy settings in the environment.
The log holds the method, scheme, host, port, path and status. It never holds a header, a body or a query.
"""
import json
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .errors import EgressRefused

LOOPBACK = ("127.0.0.1", "::1")
DEFAULT_PORTS = {"http": 80, "https": 443}


def _parse_entry(entry):
    """'host' or 'host:port' -> (host, port or None). An IPv6 literal is written [::1] or [::1]:port."""
    entry = entry.strip().lower()
    if entry.startswith("["):
        host, _, rest = entry[1:].partition("]")
        return host, (int(rest[1:]) if rest.startswith(":") else None)
    if entry.count(":") == 1:
        host, _, port = entry.partition(":")
        return host, int(port)
    return entry, None


class EgressLog:
    """An in-memory list of events, written to a JSON-lines file when a path is given."""

    def __init__(self, path=None):
        self.events = []
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_bytes(b"")

    def write(self, event):
        self.events.append(event)
        if self.path:
            with open(self.path, "ab") as f:
                f.write(json.dumps(event, sort_keys=True, ensure_ascii=False).encode("utf-8") + b"\n")

    def summary(self):
        allowed, refused = Counter(), Counter()
        for e in self.events:
            (allowed if e["allowed"] else refused)[f'{e["host"]}:{e["port"]}'] += 1
        return {"allowed": dict(sorted(allowed.items())), "refused": dict(sorted(refused.items())),
                "total": len(self.events)}


class EgressClient:
    def __init__(self, allow, *, log=None, transport=None, timeout=60.0):
        self.allow = [_parse_entry(e) for e in allow]
        self.allow_text = [e.strip().lower() for e in allow]
        self.log = log if log is not None else EgressLog()
        self.timeout = timeout
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)

    def add_allow(self, entry):
        """Add one host (or host:port) to the list, for example the local SRD-1 endpoint after it binds a port."""
        self.allow.append(_parse_entry(entry))
        self.allow_text.append(entry.strip().lower())

    def close(self):
        self._client.close()

    def _allowed(self, scheme, host, port):
        if scheme not in DEFAULT_PORTS:
            return False
        if scheme == "http" and host not in LOOPBACK:
            return False          # plain HTTP only to the local machine
        for h, p in self.allow:
            if h == host and (p if p is not None else DEFAULT_PORTS[scheme]) == port:
                return True
        return False

    def request(self, method, url, *, json=None, headers=None, timeout=None):
        """Send one request if its host is on the list. `timeout` (seconds) replaces the client's for this request."""
        parts = urlsplit(url)
        scheme = (parts.scheme or "").lower()
        host = (parts.hostname or "").lower()
        try:
            port = parts.port or DEFAULT_PORTS.get(scheme)
        except ValueError:
            port = None
        event = {"t_ns": time.time_ns(), "method": method.upper(), "scheme": scheme, "host": host, "port": port,
                 "path": parts.path, "allowed": self._allowed(scheme, host, port), "status": None, "error": None}
        if not event["allowed"]:
            self.log.write(event)
            raise EgressRefused(f"request to {host}:{port} refused: the host is not on the allow-list")
        try:
            resp = self._client.request(method, url, json=json, headers=headers,
                                        timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout)
        except httpx.HTTPError as e:
            event["error"] = type(e).__name__
            self.log.write(event)
            raise
        event["status"] = resp.status_code
        self.log.write(event)
        return resp
