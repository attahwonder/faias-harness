"""Layer L3 of the containment self-test: a guard below the HTTP client.

The guard patches the socket calls that look up a name or open a connection: `socket.getaddrinfo`,
`socket.create_connection`, and `connect`, `connect_ex` and `sendto` of `socket.socket`. A call to a loopback
address (127.0.0.0/8, ::1, localhost) passes. The guard records every other call with its host and port, then
refuses it with `GuardTrip`. It records the call before it raises, so a caller that catches the error still leaves
the record.

The guard also patches `subprocess.Popen` (`subprocess.run`, `call` and `check_output` start a process through it)
and `os.system`. It records the argv of each process start, never the environment. It allows these argv only:
  - `git -C <folder> rev-parse ...`: the code block of a run log (`engine/runlog.py`, `git_commit`);
  - `git rev-parse --short HEAD`, `git remote get-url origin` and `git status --porcelain`: the eval log of
    inspect_ai (`inspect_ai/_util/git.py`). These read the local repository only.
It records every other argv as a trip and refuses it.

Limits. The guard is a test aid, not a sandbox. It does not see a native library that calls the operating system
directly, a socket function that code saved before the guard started, `os.exec*`, `os.spawn*`, or a subclass of
`subprocess.Popen` made before the guard started (for example `asyncio.windows_utils.Popen`). On Windows, the
proactor event loop connects with overlapped I/O: the guard sees its name lookups, but not a connect to an address
that needs no lookup.
"""
import ipaddress
import os
import socket
import subprocess
import threading
from unittest import mock

# The read-only git commands of the inspect_ai eval log (inspect_ai/_util/git.py, git_context), after the executable.
INSPECT_GIT = (("rev-parse", "--short", "HEAD"), ("remote", "get-url", "origin"), ("status", "--porcelain"))


class GuardTrip(PermissionError):
    """The self-test guard refused a call. The guard recorded the call before it raised this error."""


def is_loopback(host):
    """True for a loopback address or the name localhost."""
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    h = host.strip().lower().strip("[]").split("%")[0]
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _text(host):
    return host.decode("ascii", "replace") if isinstance(host, bytes) else str(host)


def argv_words(args):
    """The argv as a list of strings. A string command (shell form) is one word."""
    if isinstance(args, (str, bytes, os.PathLike)):
        return [os.fsdecode(args)]
    try:
        return [os.fsdecode(a) for a in args]
    except TypeError:
        return [repr(args)]


def argv_allowed(args):
    """True only for the git commands that the module docstring lists."""
    if isinstance(args, (str, bytes)):
        return False
    words = argv_words(args)
    if not words:
        return False
    exe = os.path.basename(words[0]).lower()
    if exe not in ("git", "git.exe"):
        return False
    rest = tuple(words[1:])
    if len(rest) >= 3 and rest[0] == "-C" and rest[2] == "rev-parse":
        return True
    return rest in INSPECT_GIT


class Guard:
    """Use as a context manager. `trips` holds the refused calls; `calls` holds every process start, with `allowed`."""

    def __init__(self):
        self.trips = []
        self.calls = []
        self._lock = threading.Lock()
        self._patches = []

    def _trip(self, kind, **fields):
        with self._lock:
            self.trips.append({"kind": kind, **fields})
        where = fields.get("host") if "host" in fields else " ".join(fields.get("argv") or [])
        raise GuardTrip(f"the self-test guard refused {kind}: {where}")

    def _address(self, kind, address):
        """Check one (host, port, ...) address. A path (a local socket) passes."""
        if isinstance(address, (tuple, list)) and address:
            host = address[0]
            port = address[1] if len(address) > 1 else None
            if not is_loopback(host):
                self._trip(kind, host=_text(host), port=port)

    def mark(self):
        """A position in the two lists, for `since`."""
        with self._lock:
            return len(self.trips), len(self.calls)

    def since(self, mark):
        """(trips, calls) recorded after `mark`."""
        with self._lock:
            return list(self.trips[mark[0]:]), list(self.calls[mark[1]:])

    def __enter__(self):
        guard = self
        real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
        real_sendto, real_getaddrinfo = socket.socket.sendto, socket.getaddrinfo
        real_create_connection, real_popen = socket.create_connection, subprocess.Popen

        def connect(sock, address):
            guard._address("connect", address)
            return real_connect(sock, address)

        def connect_ex(sock, address):
            guard._address("connect_ex", address)
            return real_connect_ex(sock, address)

        def sendto(sock, data, *args):
            if args:
                guard._address("sendto", args[-1])
            return real_sendto(sock, data, *args)

        def getaddrinfo(host, port, *a, **k):
            if host is not None and not is_loopback(host):
                guard._trip("getaddrinfo", host=_text(host), port=port)
            return real_getaddrinfo(host, port, *a, **k)

        def create_connection(address, *a, **k):
            guard._address("create_connection", address)
            return real_create_connection(address, *a, **k)

        class GuardedPopen(real_popen):
            def __init__(self, args, *a, **k):
                words, ok = argv_words(args), argv_allowed(args)
                with guard._lock:
                    guard.calls.append({"argv": words, "allowed": ok})
                if not ok:
                    guard._trip("process", argv=words)
                super().__init__(args, *a, **k)

        def system(command):
            words = argv_words(command)
            with guard._lock:
                guard.calls.append({"argv": words, "allowed": False})
            guard._trip("process", argv=words)

        self._patches = [
            mock.patch.object(socket.socket, "connect", connect),
            mock.patch.object(socket.socket, "connect_ex", connect_ex),
            mock.patch.object(socket.socket, "sendto", sendto),
            mock.patch.object(socket, "getaddrinfo", getaddrinfo),
            mock.patch.object(socket, "create_connection", create_connection),
            mock.patch.object(subprocess, "Popen", GuardedPopen),
            mock.patch.object(os, "system", system),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        self._patches = []
        return False
