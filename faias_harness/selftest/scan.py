"""Probes S13 and S16 of the containment self-test: a static scan of the code that a run loads.

The scan parses each `.py` file under `faias_harness/` and `targets/`, and `tools/sweep_barred.py` (the TP-SEC-006
engine loads its key patterns at run time). It reads the code with `ast` and runs none of it. It lists three kinds
of site:
  - import: an import of a module that can reach the network or start a process (WATCHED_IMPORTS);
  - call: a call of a function of such a module that opens a connection, looks up a name or starts a process
    (OPENERS), and the process calls of `os` (OS_OPENERS). Import aliases are resolved;
  - server: a call of a class whose name ends in HTTPServer, TCPServer or UDPServer, with its bind address.
The scan compares the sites with the declared list (DECLARED). A site that is not declared fails the scan. A
declared site that the scan does not find also fails it: the list is stale. A site matches on its file, kind,
name, enclosing function and bind address. Its line number is recorded, not matched, so an edit above a site does
not fail the scan.

The code manifest is the SHA-256 of each scanned file. The self-test log locks it before the first probe. A later
edit changes the manifest, so the preflight line then says that the code manifest does not match.

Limits: the scan finds sites in the source. It does not see a call made through a name that it cannot resolve (for
example a module passed as an argument), code that a run builds at run time, or the code of an installed library.
"""
import ast
import hashlib
import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("faias_harness", "targets")
EXTRA_FILES = ("tools/sweep_barred.py",)
EXCLUDED = {
    "tests/": "the unit tests, their guard and their stand-ins; a run does not load them",
    "tools/ (except tools/sweep_barred.py)": "maintenance commands; a run does not load them",
}
WATCHED_IMPORTS = ("socket", "httpx", "urllib.request", "http.client", "requests", "aiohttp", "subprocess",
                   "smtplib", "ftplib", "asyncio", "webbrowser")
OPENERS = {
    "httpx": {"Client", "AsyncClient", "get", "post", "put", "patch", "delete", "head", "options", "request",
              "stream", "HTTPTransport", "AsyncHTTPTransport"},
    "socket": {"socket", "create_connection", "create_server", "socketpair", "fromfd", "getaddrinfo",
               "gethostbyname", "gethostbyname_ex", "gethostbyaddr"},
    "urllib.request": {"urlopen", "urlretrieve", "build_opener", "Request"},
    "http.client": {"HTTPConnection", "HTTPSConnection"},
    "subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"},
    "smtplib": {"SMTP", "SMTP_SSL", "LMTP"},
    "ftplib": {"FTP", "FTP_TLS"},
    "asyncio": {"open_connection", "start_server", "create_subprocess_exec", "create_subprocess_shell"},
    "webbrowser": {"open", "open_new", "open_new_tab", "get"},
    "requests": None,       # None: every call of the module is a site
    "aiohttp": None,
}
OS_OPENERS = {"system", "popen", "startfile", "execv", "execve", "execvp", "execl", "spawnv", "spawnl", "spawnve",
              "posix_spawn", "posix_spawnp"}
SERVER_SUFFIXES = ("HTTPServer", "TCPServer", "UDPServer")

# Probe S16 (publication): no module imports one of these, and no process call holds one of these words.
PUBLICATION_MODULES = ("smtplib", "ftplib", "twine", "requests", "boto3", "botocore", "paramiko", "webbrowser")
PUBLICATION_WORDS = ("push", "fetch", "pull", "clone", "remote", "ots", "pip", "curl", "wget", "upload", "publish")

# The declared sites. Each line: (file, kind, name, enclosing function, bind address or None, reason).
DECLARED = [
    ("faias_harness/egress.py", "import", "httpx", "<module>", None,
     "the one HTTP client of the harness"),
    ("faias_harness/egress.py", "call", "httpx.Client", "EgressClient.__init__", None,
     "the one client: follow_redirects=False, trust_env=False; every request passes the allow-list first"),
    ("faias_harness/adapters/base.py", "import", "httpx", "<module>", None,
     "exception classes only (ChannelTarget._request)"),
    ("targets/srd1/app/gateway.py", "import", "httpx", "<module>", None,
     "exception classes only (Gateway.complete)"),
    ("faias_harness/engine/runlog.py", "import", "subprocess", "<module>", None,
     "git rev-parse only (git_commit)"),
    ("faias_harness/engine/runlog.py", "call", "subprocess.run", "git_commit.git", None,
     "git -C <repo> rev-parse --show-toplevel and HEAD only"),
    ("faias_harness/emulators/mcp_tools.py", "server", "ThreadingHTTPServer", "ToolEmulator.__init__", "(host, port)",
     "the constructor refuses a host that is not loopback"),
    ("targets/srd1/app/server.py", "server", "ThreadingHTTPServer", "Srd1Server.__init__", "('127.0.0.1', port)",
     "a literal loopback address"),
    ("faias_harness/selftest/guard.py", "import", "socket", "<module>", None,
     "the guard patches the socket calls; it opens no socket of its own"),
    ("faias_harness/selftest/guard.py", "import", "subprocess", "<module>", None,
     "the guard patches the process start; it starts no process of its own"),
    ("faias_harness/selftest/probes.py", "import", "httpx", "<module>", None,
     "MockTransport (layer L2), which records a request and sends nothing; exception classes"),
    ("faias_harness/selftest/probes.py", "server", "ThreadingHTTPServer", "StandIn.__init__", "('127.0.0.1', 0)",
     "the local stand-in of the self-test, on a literal loopback address"),
]


def scanned_files(root=REPO):
    root = Path(root)
    out = []
    for r in ROOTS:
        out += [p for p in (root / r).rglob("*.py") if "__pycache__" not in p.parts]
    out += [root / f for f in EXTRA_FILES if (root / f).is_file()]
    return sorted(out, key=lambda p: p.relative_to(root).as_posix())


def code_manifest(root=REPO):
    """{"files": {path: sha256}, "sha256": hash of the canonical files map, "count": n}."""
    root = Path(root)
    files = {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in scanned_files(root)}
    blob = json.dumps(files, sort_keys=True, separators=(",", ":"))
    return {"files": files, "sha256": hashlib.sha256(blob.encode("utf-8")).hexdigest(), "count": len(files)}


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel):
        self.rel, self.stack, self.aliases, self.sites, self.process_words = rel, [], {}, [], []
        self.imports = []
        self.top_function = None      # the top-level function of the node in hand, for the words of a process call

    def where(self):
        return ".".join(self.stack) or "<module>"

    def _scoped(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_ClassDef = _scoped

    def visit_FunctionDef(self, node):
        outer = self.top_function
        if outer is None:
            self.top_function = node
        self._scoped(node)
        self.top_function = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def _add(self, node, kind, name, bind=None):
        self.sites.append({"file": self.rel, "line": node.lineno, "kind": kind, "name": name, "where": self.where(),
                           "bind": bind})

    def visit_Import(self, node):
        for a in node.names:
            self.imports.append(a.name)
            self.aliases[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
            if a.name in WATCHED_IMPORTS:
                self._add(node, "import", a.name)

    def visit_ImportFrom(self, node):
        mod = node.module or ""
        self.imports.append(mod)
        for a in node.names:
            self.aliases[a.asname or a.name] = f"{mod}.{a.name}"
        if mod in WATCHED_IMPORTS:
            self._add(node, "import", mod)

    def _resolve(self, node):
        d = _dotted(node)
        if d is None:
            return None
        head, _, tail = d.partition(".")
        base = self.aliases.get(head, head)
        return f"{base}.{tail}" if tail else base

    def visit_Call(self, node):
        full = self._resolve(node.func)
        if full:
            short = full.rsplit(".", 1)[-1]
            mod = full.rsplit(".", 1)[0] if "." in full else ""
            if short.endswith(SERVER_SUFFIXES):
                bind = ast.unparse(node.args[0]) if node.args else None
                self._add(node, "server", short, bind)
            elif mod in OPENERS and (OPENERS[mod] is None or short in OPENERS[mod]):
                self._add(node, "call", full)
                if mod == "subprocess":     # the words that the function passes on, also through a nested helper
                    self.process_words += _constants(self.top_function or node)
            elif mod == "os" and short in OS_OPENERS:
                self._add(node, "call", full)
                self.process_words += _constants(self.top_function or node)
        self.generic_visit(node)


def _constants(node):
    """The string constants in the call arguments under `node` (docstrings are not call arguments)."""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            for a in list(n.args) + [k.value for k in n.keywords]:
                for c in ast.walk(a):
                    if isinstance(c, ast.Constant) and isinstance(c.value, str):
                        out.append(c.value)
    return out


def scan_file(path, root=REPO):
    root = Path(root)
    rel = Path(path).relative_to(root).as_posix()
    v = _Visitor(rel)
    v.visit(ast.parse(Path(path).read_bytes(), filename=rel))
    return v


def _key(s):
    return (s["file"], s["kind"], s["name"], s["where"], s["bind"])


def declared_sites():
    return [{"file": f, "kind": k, "name": n, "where": w, "bind": b, "reason": r} for f, k, n, w, b, r in DECLARED]


def scan(root=REPO):
    """The scan of the checkout at `root`: sites found, unexpected (not declared), stale (declared, not found), the
    publication check (probe S16), the code manifest, and what is excluded."""
    root = Path(root)
    found, imports, words = [], set(), []
    for p in scanned_files(root):
        v = scan_file(p, root)
        found += v.sites
        imports |= {i for i in v.imports if i}
        words += v.process_words
    have, want = Counter(_key(s) for s in found), Counter(_key(d) for d in declared_sites())
    unexpected = [s for s in found if have[_key(s)] > want.get(_key(s), 0)]
    stale = [d for d in declared_sites() if want[_key(d)] > have.get(_key(d), 0)]
    pub_imports = sorted(i for i in imports if i.split(".")[0] in PUBLICATION_MODULES)
    pub_words = sorted({w for w in words for t in w.replace("-", " ").split() if t.lower() in PUBLICATION_WORDS})
    return {
        "roots": list(ROOTS) + list(EXTRA_FILES), "excluded": EXCLUDED,
        "found": sorted(found, key=lambda s: (s["file"], s["line"])), "declared": len(DECLARED),
        "unexpected": unexpected, "stale": stale,
        "publication": {"imports": pub_imports, "process_words": pub_words, "modules_checked": list(PUBLICATION_MODULES),
                        "words_checked": list(PUBLICATION_WORDS)},
        "manifest": code_manifest(root),
    }
