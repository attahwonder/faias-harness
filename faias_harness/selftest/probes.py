"""The probes of the containment self-test. Each probe tests one path of the harness and records what it found.

Every target is a name under the reserved top-level name `.invalid` (RFC 2606, section 2: reserved for names that
are sure to be invalid) or an address in a documentation block of RFC 5737, section 3 (192.0.2.0/24, 198.51.100.0/24,
203.0.113.0/24; section 4 asks that they do not appear on the public internet). No probe names a real outside host.
The sources are https://www.rfc-editor.org/rfc/rfc2606.txt and https://www.rfc-editor.org/rfc/rfc5737.txt, read
28 Oct 2026.

Four layers. Each works without the others. A probe passes only when every layer that applies agrees.
  L1  the allow-list of the egress client: the probe gives a list that lacks the target;
  L2  a mock transport (httpx.MockTransport) that records each request it gets and sends nothing;
  L3  the guard (guard.py) below the client: it records and refuses a connect or a lookup that is not loopback;
  L4  the targets above, which do not resolve or route.

Each probe returns its observed values, whether it passed, and the egress attempts that it made to a target off the
allow-list (for measure M1). The `observed` and `counts` values are fixed for a given checkout and Python, so that
`rerun` can compare them. Ports, tokens and folder names are not in them.
"""
import hashlib
import inspect as pyinspect
import json
import os
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import httpx

from ..egress import EgressClient, EgressLog
from ..errors import EgressRefused
from . import scan as scan_mod

REPO = Path(__file__).resolve().parents[2]
SUFFIX = "harness-selftest.invalid"          # RFC 2606, section 2
TEST_NETS = ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")     # RFC 5737, section 3
MODEL_KEY_ENV = "FAIAS_SELFTEST_MODEL_KEY"      # names of variables in an env dict of this process; never set in os.environ
CHANNEL_KEY_ENV = "FAIAS_SELFTEST_CHANNEL_KEY"
EMULATOR_KEY_ENV = "FAIAS_SELFTEST_EMULATOR_KEY"
EXAMPLE_PROFILES = {"directline": "copilot_studio.yaml", "openai-responses": "responses_agent.yaml",
                    "a2a": "a2a_agent.yaml", "json-http": "json_app.yaml", "generic-http": "chat_only.yaml"}


def host(label):
    return f"{label}.{SUFFIX}"


def where(obj, repo=REPO):
    """'file:line (qualified name)' of a function or class, read from the code that runs."""
    try:
        path = Path(pyinspect.getsourcefile(obj)).resolve()
        rel = path.relative_to(Path(repo).resolve()).as_posix()
    except (TypeError, ValueError):
        rel = Path(pyinspect.getsourcefile(obj) or "?").name
    return f"{rel}:{pyinspect.getsourcelines(obj)[1]} ({obj.__qualname__})"


class _Mock:
    """Layer L2: an httpx transport that records the host of each request and raises. It never sends."""

    def __init__(self):
        self.hosts = []

    def __call__(self, request):
        self.hosts.append(f"{request.url.host}:{request.url.port}" if request.url.port else request.url.host)
        raise httpx.ConnectError("the self-test mock transport sends nothing", request=request)

    def transport(self):
        return httpx.MockTransport(self)


def _outcome(fn):
    """'EgressRefused', another error class name, or 'response <status>'."""
    try:
        r = fn()
    except EgressRefused:
        return "EgressRefused", None
    except Exception as e:      # noqa: BLE001 - the class name is the evidence
        return type(e).__name__, e
    return (f"response {r.status_code}" if hasattr(r, "status_code") else "returned"), None


class StandIn:
    """A local HTTP server on 127.0.0.1 for the probes: a model host (POST .../chat/completions), a redirect
    (GET /redirect) and an echo (any other path). With `token` set, it notes whether a request carried the token in
    its query, a header or its body."""

    def __init__(self, tool_reply=None, provider="SelftestHost"):
        self.requests, self.token, self.token_seen, self.tool_reply, self.provider = [], None, set(), tool_reply, provider
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body=None, headers=None):
                data = b"" if body is None else json.dumps(body).encode("utf-8")
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if data:
                    self.wfile.write(data)

            def _note(self, raw):
                outer.requests.append(self.path.split("?")[0])
                t = outer.token
                if t:
                    if t in self.path:
                        outer.token_seen.add("query")
                    if any(t in str(v) for v in self.headers.values()):
                        outer.token_seen.add("header")
                    if t.encode("utf-8") in raw:
                        outer.token_seen.add("body")

            def do_GET(self):
                self._note(b"")
                if self.path.startswith("/redirect"):
                    return self._send(302, None, {"Location": f"https://{host('redirect')}/"})
                return self._send(200, {"ok": True})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self._note(raw)
                if self.path.endswith("/chat/completions"):
                    return self._send(200, outer.chat(json.loads(raw.decode("utf-8"))))
                return self._send(200, {"ok": True})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def chat(self, body):
        """A reply in the form of the model host. With tools in the request and `tool_reply` set, the message is
        what `tool_reply(messages)` returns."""
        if body.get("tools") and self.tool_reply:
            msg = self.tool_reply(body["messages"])
        else:
            msg = {"role": "assistant", "content": "ready"}
        return {"id": "selftest-1", "model": body.get("model"), "provider": self.provider,
                "choices": [{"message": msg, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    @property
    def entry(self):
        return f"127.0.0.1:{self.port}"

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)


@dataclass
class Ctx:
    """What the probes share: the guard, a temporary folder, the checkout and the manifest that the lock holds."""
    guard: object
    tmp: Path
    repo: Path = REPO
    locked_manifest: object = None
    scan_result: object = None


def _result(observed, ok, attempts=0, refused=0, notes=None):
    return {"observed": observed, "ok": bool(ok), "attempts": attempts, "refused": refused, "notes": notes or {}}


def _settings(**changes):
    """Settings for the probes that need a gateway: explore mode, no real model, no real host."""
    from .. import settings as settings_mod
    raw = {"provider": "openrouter", "model_id": "selftest/no-model", "mode": "explore",
           "routing": {"only": ["selftesthost"]}, "price_in_per_m": 1.0, "price_out_per_m": 1.0, "cap_usd": 1.0,
           "api_key_env": MODEL_KEY_ENV, "max_tokens": 50, "item_set": "spike", **changes}
    problems = settings_mod.validate(raw)
    if problems:
        raise ValueError("; ".join(problems))
    return settings_mod.from_raw(raw)


def _srd1():
    from ..engine import spike
    return spike._srd1()


# ---------- N1: the egress client ----------

def s1(ctx):
    m, log = _Mock(), EgressLog(ctx.tmp / "s1_egress.jsonl")
    c = EgressClient([], log=log, transport=m.transport())
    out, _ = _outcome(lambda: c.request("GET", f"https://{host('egress')}/v1"))
    c.close()
    ev = log.events
    ok = out == "EgressRefused" and len(ev) == 1 and ev[0]["allowed"] is False and not m.hosts
    return _result({"outcome": out, "events": len(ev), "events_allowed": [e["allowed"] for e in ev],
                    "transport_reached": len(m.hosts)}, ok, 1, int(ok))


def s2(ctx):
    m, log = _Mock(), EgressLog()
    c = EgressClient(["203.0.113.10"], log=log, transport=m.transport())
    out, _ = _outcome(lambda: c.request("GET", "http://203.0.113.10/"))
    c.close()
    ev = log.events
    ok = out == "EgressRefused" and len(ev) == 1 and ev[0]["allowed"] is False and not m.hosts
    return _result({"outcome": out, "events": len(ev), "events_allowed": [e["allowed"] for e in ev],
                    "transport_reached": len(m.hosts)}, ok, 1, int(ok))


def s3(ctx):
    with StandIn() as st:
        log = EgressLog()
        c = EgressClient([st.entry], log=log)
        out, _ = _outcome(lambda: c.request("GET", st.url + "/redirect"))
        c.close()
        paths = list(st.requests)
    ev = log.events
    ok = out == "response 302" and len(ev) == 1 and ev[0]["status"] == 302 and paths == ["/redirect"]
    return _result({"outcome": out, "events": len(ev), "event_status": [e["status"] for e in ev],
                    "stand_in_paths": paths, "redirect_target": host("redirect")}, ok, 1, int(ok))


def s4(ctx):
    proxy = f"http://{host('proxy')}:3128"
    with StandIn() as st, mock.patch.dict(os.environ, {"HTTPS_PROXY": proxy, "HTTP_PROXY": proxy, "ALL_PROXY": proxy,
                                                       "https_proxy": proxy, "http_proxy": proxy, "all_proxy": proxy}):
        log = EgressLog()
        c = EgressClient([st.entry], log=log)
        out, _ = _outcome(lambda: c.request("GET", st.url + "/echo"))
        c.close()
        paths = list(st.requests)
    ok = out == "response 200" and paths == ["/echo"]
    return _result({"outcome": out, "stand_in_paths": paths, "proxy_in_environment": host("proxy") + ":3128"},
                   ok, 1, int(ok))


def s5(ctx):
    token = secrets.token_hex(16)
    path = ctx.tmp / "s5_egress.jsonl"
    with StandIn() as st:
        st.token = token
        c = EgressClient([st.entry], log=EgressLog(path))
        out, _ = _outcome(lambda: c.request("POST", f"{st.url}/echo?q={token}", json={"probe": token},
                                            headers={"X-Probe": token}))
        c.close()
        seen = sorted(st.token_seen)
    in_log = token.encode("utf-8") in path.read_bytes()
    ok = out == "response 200" and seen == ["body", "header", "query"] and not in_log
    return _result({"outcome": out, "token_sent_in": seen, "token_in_log": in_log}, ok)


def s6(ctx):
    m, log = _Mock(), EgressLog()
    c = EgressClient(["[::1]:2", "198.51.100.8"], log=log, transport=m.transport())
    parsed = [list(e) for e in c.allow]
    outs = [_outcome(lambda u=u: c.request("GET", u))[0] for u in ("https://[::1]:1/", "https://198.51.100.7/")]
    c.close()
    refused = sum(1 for o in outs if o == "EgressRefused")
    ok = outs == ["EgressRefused", "EgressRefused"] and parsed == [["::1", 2], ["198.51.100.8", None]] and not m.hosts
    return _result({"outcomes": outs, "allow_parsed": parsed, "transport_reached": len(m.hosts)}, ok, 2,
                   refused if not m.hosts else 0)


# ---------- N2: the gateway of SRD-1 ----------

def s7(ctx):
    _, gw, _, _ = _srd1()
    key = secrets.token_hex(16)
    path = ctx.tmp / "s7_egress.jsonl"
    m = _Mock()
    egress = EgressClient([], log=EgressLog(path), transport=m.transport())
    g = gw.Gateway(_settings(), egress, env={MODEL_KEY_ENV: key}, base_url=f"https://{host('model-host')}/api/v1")
    out, err = _outcome(lambda: g.complete([{"role": "user", "content": "probe"}]))
    egress.close()
    key_in_log, key_in_error = key.encode("utf-8") in path.read_bytes(), bool(err is not None and key in str(err))
    ok = out == "EgressRefused" and not m.hosts and not key_in_log and not key_in_error
    return _result({"outcome": out, "transport_reached": len(m.hosts), "key_in_log": key_in_log,
                    "key_in_error": key_in_error}, ok, 1, int(ok))


# ---------- N3: the channel adapters of an external run ----------

def _adapter_profile(adapter_id, repo):
    from ..profile import load_profile
    p = load_profile(Path(repo) / "targets" / "examples" / EXAMPLE_PROFILES[adapter_id])
    opts = dict(p.adapter_options or {})
    if opts.get("token_endpoint"):
        opts["token_endpoint"] = f"https://{host(adapter_id + '-token')}/directline/token"
    settings = dict(p.settings)
    if settings.get("api_key_env"):
        settings["api_key_env"] = CHANNEL_KEY_ENV
    if adapter_id == "generic-http":
        settings["model_id"] = "selftest/no-model"      # the example leaves the model unset; the adapter needs one
    return replace(p, endpoint=f"https://{host(adapter_id)}", adapter_options=opts or None, settings=settings)


def _drive(target):
    """open_session, then send; returns (outcome, the call that raised)."""
    step = "open_session"
    try:
        sid = target.open_session()
        step = "send"
        target.send(sid, "probe")
    except EgressRefused:
        return "EgressRefused", step
    except Exception as e:      # noqa: BLE001
        return type(e).__name__, step
    return "returned", None


def s8(ctx):
    from ..adapters import registry
    env, rows, refused = {CHANNEL_KEY_ENV: secrets.token_hex(16)}, {}, 0
    for aid in registry.ADAPTERS:
        profile = _adapter_profile(aid, ctx.repo)
        problems = registry.problems(profile)
        m, sends = _Mock(), []
        egress = EgressClient([], log=EgressLog(), transport=m.transport())
        t = registry.build(profile, egress, before_send=lambda: sends.append(1), env=env, sleep=lambda s: None)
        out, step = _drive(t)
        egress.close()
        allow = registry.allow_hosts(profile)
        m2 = _Mock()
        egress2 = EgressClient(allow, log=EgressLog(), transport=m2.transport())
        out2, _ = _drive(registry.build(profile, egress2, env=env, sleep=lambda s: None))
        egress2.close()
        ok = (not problems and out == "EgressRefused" and not m.hosts and (step == "open_session" or sends)
              and m2.hosts and set(m2.hosts) <= set(allow))
        refused += int(out == "EgressRefused" and not m.hosts)
        rows[aid] = {"profile_problems": problems, "outcome": out, "refused_at": step, "before_send_calls": len(sends),
                     "transport_reached": len(m.hosts), "with_allow_list": {"allow": allow, "outcome": out2,
                                                                            "transport_hosts": m2.hosts},
                     "ok": bool(ok)}
    ok = len(rows) == 5 and all(r["ok"] for r in rows.values())
    return _result({"adapters": rows}, ok, len(rows), refused)


# ---------- N4: the srd1-http adapter ----------

def s9(ctx):
    from ..adapters.srd1_http import Srd1ChatTarget
    m = _Mock()
    egress = EgressClient([], log=EgressLog(), transport=m.transport())
    out, step = _drive(Srd1ChatTarget(f"https://{host('srd1')}", egress))
    egress.close()
    ok = out == "EgressRefused" and step == "open_session" and not m.hosts
    return _result({"outcome": out, "refused_at": step, "transport_reached": len(m.hosts)}, ok, 1, int(ok))


# ---------- N5, N6: the two local servers ----------

class _NoGateway:
    def complete(self, messages, *, tools=None):
        raise RuntimeError("the self-test makes no model call here")


def s10(ctx):
    _, _, orch, trace = _srd1()
    from targets.srd1.app.server import Srd1Server
    t = trace.TraceStore(ctx.tmp / "s10_trace.jsonl")
    s = Srd1Server(orch.Orchestrator("probe", _NoGateway(), t), t)
    bind = s.httpd.server_address[0]
    s.httpd.server_close()
    return _result({"bind": bind}, bind == "127.0.0.1")


def _emulator_tools(repo):
    from ..emulators import mcp_tools
    return mcp_tools.load_tools(Path(repo) / "targets" / "examples" / "mcp_tools.yaml")


def s11(ctx):
    from ..emulators import mcp_tools as mt
    tools, refused_hosts = _emulator_tools(ctx.repo), {}
    for i, h in enumerate(("0.0.0.0", "192.0.2.1")):
        try:
            mt.ToolEmulator(tools, ctx.tmp / f"s11_refused_{i}.jsonl", host=h).httpd.server_close()
            refused_hosts[h] = "bound"
        except ValueError:
            refused_hosts[h] = "ValueError"
    env = {EMULATOR_KEY_ENV: secrets.token_hex(16)}
    with mt.ToolEmulator(tools, ctx.tmp / "s11_ledger.jsonl", api_key_env=EMULATOR_KEY_ENV, env=env) as e:
        bind = e.httpd.server_address[0]
        c = EgressClient([f"127.0.0.1:{e.port}"], log=EgressLog())
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        r1 = c.request("POST", e.url, json=init, headers={"Origin": f"https://{host('origin')}",
                                                          "X-Api-Key": env[EMULATOR_KEY_ENV]})
        r2 = c.request("POST", e.url, json=init)
        c.close()
        lines = len(mt.read_ledger(e.ledger.path))
    obs = {"refused_binds": refused_hosts, "bind": bind, "origin_status": r1.status_code,
           "origin_connection": r1.headers.get("Connection"), "no_key_status": r2.status_code,
           "no_key_connection": r2.headers.get("Connection"), "ledger_lines": lines}
    ok = (refused_hosts == {"0.0.0.0": "ValueError", "192.0.2.1": "ValueError"} and bind == "127.0.0.1"
          and r1.status_code == 403 and r2.status_code == 401 and obs["origin_connection"] == "close" and lines == 0)
    return _result(obs, ok)


def s12(ctx):
    from ..emulators import mcp_tools as mt
    with mt.ToolEmulator(_emulator_tools(ctx.repo), ctx.tmp / "s12_ledger.jsonl") as e:
        c = EgressClient([f"127.0.0.1:{e.port}"], log=EgressLog())
        r = c.request("POST", e.url, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                           "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                      "clientInfo": {"name": "faias-selftest", "version": "1"}}})
        h = {"Mcp-Session-Id": r.headers.get("Mcp-Session-Id", ""), "MCP-Protocol-Version": "2025-06-18"}
        statuses = [r.status_code]
        for body in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                     {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                     {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "get_account_summary", "arguments": {"account_ref": "TEST-0001"}}}):
            statuses.append(c.request("POST", e.url, json=body, headers=h).status_code)
        statuses.append(c.request("DELETE", e.url, headers=h).status_code)
        c.close()
        path, head = e.ledger.path, e.ledger.head
    ok_chain, problems = mt.verify_ledger(path, expected_head=head)
    lines = len(mt.read_ledger(path))
    ok = statuses == [200, 202, 200, 200, 200] and lines == 1 and ok_chain
    return _result({"statuses": statuses, "ledger_lines": lines, "chain_ok": ok_chain, "problems": problems}, ok)


# ---------- N9 and P1: static ----------

def s13(ctx):
    r = scan_mod.scan(ctx.repo)
    ctx.scan_result = r
    same = ctx.locked_manifest is None or r["manifest"]["sha256"] == ctx.locked_manifest
    ok = not r["unexpected"] and not r["stale"] and same
    return _result({"sites_found": len(r["found"]), "declared": r["declared"],
                    "unexpected": [{k: s[k] for k in ("file", "kind", "name", "where")} for s in r["unexpected"]],
                    "stale": [{k: s[k] for k in ("file", "kind", "name", "where")} for s in r["stale"]],
                    "files_scanned": r["manifest"]["count"], "manifest_equals_lock": same}, ok)


PUBLICATION_READING = ("structurally absent in this version; a tool that publishes would add a path and a probe. "
                       "This is a reading of PLAYBOOK_AGT_014.md line 55 that the owner has not yet confirmed.")


def s16(ctx):
    r = ctx.scan_result or scan_mod.scan(ctx.repo)
    pub = r["publication"]
    ok = not pub["imports"] and not pub["process_words"]
    return _result({"publication_modules_imported": pub["imports"], "process_words_found": pub["process_words"],
                    "modules_checked": pub["modules_checked"], "words_checked": pub["words_checked"],
                    "channel": PUBLICATION_READING}, ok)


# ---------- N7: the engine ----------

def s14(ctx):
    from ..engine import runlog, sec001, spike
    settings = _settings(runs_per_item=3)
    engine = "inspect" if spike.inspect_available() else "direct"
    one = sec001.load_items("spike", repo=ctx.repo)[0][:1]
    with StandIn() as st, mock.patch.object(spike, "run_items", lambda s, repo=None: list(one)), \
            mock.patch.object(runlog, "sign_log", lambda *a, **k: None):
        res = spike.execute(settings, run_dir=ctx.tmp / "s14_run", sign_key=ctx.tmp / "s14_no_key",
                            confirmation={"typed": "selftest", "time": runlog.iso()},
                            base_url=st.url + "/api/v1", env={MODEL_KEY_ENV: secrets.token_hex(16)},
                            repo=ctx.repo, engine=engine)
    header = runlog.read_log(res.log_path)[0]
    s = res.summary
    allowed = (s.get("egress") or {}).get("summary", {}).get("allowed", {})
    allow = (header.get("egress") or {}).get("allow", [])
    loop = all(k.startswith("127.0.0.1:") for k in allowed) and all(a.startswith("127.0.0.1:") for a in allow)
    eng = s.get("run_engine") or {}
    obs = {"engine": eng.get("name"), "inspect_version": eng.get("version"), "status": res.status,
           "model_calls": s.get("model_calls"), "hosts_allowed": len(allowed), "header_allow_entries": len(allow),
           "all_loopback": loop, "refused": len((s.get("egress") or {}).get("summary", {}).get("refused", {})),
           "items": 1, "runs_per_item": 3, "signature": "not made: the engine log of this probe is not evidence"}
    ok = res.status == "complete" and loop and len(allow) == 2 and obs["refused"] == 0 and s.get("model_calls") == 3
    return _result(obs, ok)


# ---------- N8: git ----------

def s15(ctx):
    from ..engine import runlog
    repo = Path(ctx.repo)
    mark = ctx.guard.mark()
    runlog.git_commit(repo)
    _, calls = ctx.guard.since(mark)
    argv = [[("<repo>" if w == str(repo) else w) for w in c["argv"]] for c in calls]
    f = ctx.tmp / "s15_stamp.txt"
    f.write_bytes(b"selftest\n")
    mark = ctx.guard.mark()
    runlog.stamp_log(f)
    _, stamp_calls = ctx.guard.since(mark)
    form = all(len(a) >= 4 and Path(a[0]).name.lower() in ("git", "git.exe") and a[1:3] == ["-C", "<repo>"]
               and a[3] == "rev-parse" for a in argv)
    ok = bool(argv) and form and all(c["allowed"] for c in calls) and not stamp_calls
    return _result({"git_commit_argv": argv, "stamp_process_starts": len(stamp_calls)}, ok)


# ---------- N10 to N12: SRD-1's tool (since 0.6) ----------

def s17(ctx):
    """N10: SRD-1's MCP client to an emulator URL that is not on the allow-list."""
    from targets.srd1.app.mcp_client import McpClient
    _srd1()
    key = secrets.token_hex(16)
    path, m = ctx.tmp / "s17_egress.jsonl", _Mock()
    egress = EgressClient([], log=EgressLog(path), transport=m.transport())
    out, err = _outcome(lambda: McpClient(f"https://{host('emulator')}/mcp", egress, key_env=EMULATOR_KEY_ENV,
                                          env={EMULATOR_KEY_ENV: key}).open())
    egress.close()
    key_in_log, key_in_error = key.encode("utf-8") in path.read_bytes(), bool(err is not None and key in str(err))
    ok = out == "EgressRefused" and not m.hosts and not key_in_log and not key_in_error
    return _result({"outcome": out, "transport_reached": len(m.hosts), "key_in_log": key_in_log,
                    "key_in_error": key_in_error}, ok, 1, int(ok))


REFUND_CALL = {"id": "selftest-call-1", "type": "function",
               "function": {"name": "fee_refund",
                            "arguments": json.dumps({"account_ref": "SYN-A-0001", "fee_id": "SYN-F-0001",
                                                     "amount_gbp": 12.0, "reason": "self-test"})}}
# The gate values of the probes are their own. The risk-appetite file declares none (all null), so SRD-1 would not
# enable its tool from it; these values are not a declaration.
PROBE_GATE = (45.0, 60.0)


def _tool_world(ctx, name, tool_reply):
    """SRD-1 minimal with tools on, the stand-in model host and the emulator with SRD-1's tools file, all on
    127.0.0.1. Returns (stand-in, emulator, srd1, egress, target, the store's bytes before)."""
    from ..adapters.srd1_http import Srd1ChatTarget
    from ..emulators import mcp_tools as mt
    from ..engine import toolrun
    build, _, _, _ = _srd1()
    from targets.srd1.app.gate import RefundGate
    tools = mt.load_tools(toolrun.SRD1_TOOLS)
    store_before = Path(tools["store"]).read_bytes()
    env = {MODEL_KEY_ENV: secrets.token_hex(16), EMULATOR_KEY_ENV: secrets.token_hex(16)}
    standin = StandIn(tool_reply=tool_reply).__enter__()
    emu = mt.ToolEmulator(tools, ctx.tmp / f"{name}_ledger.jsonl", api_key_env=EMULATOR_KEY_ENV, env=env).start()
    egress = EgressClient([standin.entry, f"127.0.0.1:{emu.port}"], log=EgressLog(ctx.tmp / f"{name}_egress.jsonl"))
    srd1 = build.build_srd1(_settings(tools_enabled=True, tool_key_env=EMULATOR_KEY_ENV), egress,
                            ctx.tmp / f"{name}_trace.jsonl", base_url=f"{standin.url}/api/v1", env=env,
                            tools_url=emu.url, tool_env=env, gate=RefundGate(*PROBE_GATE))
    srd1.server.start()
    egress.add_allow(f"127.0.0.1:{srd1.server.port}")
    target = Srd1ChatTarget(srd1.server.url, egress, tools_url=emu.url, ledger_path=emu.ledger.path)
    return standin, emu, srd1, egress, target, store_before, env


def _close_world(standin, emu, srd1, egress):
    srd1.server.stop()
    emu.stop()
    egress.close()
    standin.__exit__(None, None, None)


def s18(ctx):
    """N10, N11: one tool round of SRD-1's orchestrator against the local emulator."""
    from ..emulators import mcp_tools as mt
    reply = lambda messages: ({"role": "assistant", "content": "done"} if messages[-1]["role"] == "tool"
                              else {"role": "assistant", "content": None, "tool_calls": [REFUND_CALL]})
    standin, emu, srd1, egress, target, before, env = _tool_world(ctx, "s18", reply)
    try:
        target.send(target.open_session(), "Please refund the fee.")
    finally:
        _close_world(standin, emu, srd1, egress)
    model_calls = sum(1 for p in standin.requests if p.endswith("/chat/completions"))
    lines = mt.read_ledger(emu.ledger.path)
    calls = [e for e in srd1.trace.entries() if e["kind"] == "tool_call"]
    after = Path(emu.store_path).read_bytes()
    keys_found = [n for n, v in env.items() if any(v.encode("utf-8") in p.read_bytes()
                                                   for p in ctx.tmp.glob("s18_*") if p.is_file())]
    obs = {"model_calls": model_calls, "ledger_lines": len(lines),
           "tool_call_lines": [{"decision": c["decision"], "mcp_call_seq": c.get("mcp_call_seq"),
                                "status": c.get("status")} for c in calls],
           "ledger_seq": [x["seq"] for x in lines], "store_sha256_before": hashlib.sha256(before).hexdigest(),
           "store_sha256_after": hashlib.sha256(after).hexdigest(), "keys_in_files": keys_found,
           "gate_values": {"cap_gbp": PROBE_GATE[0], "session_limit_gbp": PROBE_GATE[1],
                           "basis": "the probe's own values; the risk-appetite file declares none"}}
    ok = (model_calls == 2 and len(lines) == 1 and [c["decision"] for c in calls] == ["sent"]
          and calls[0].get("mcp_call_seq") == lines[0]["seq"] and before == after and not keys_found)
    return _result(obs, ok)


def s19(ctx):
    """N12: the kill switch, on before the tool call."""
    from ..emulators import mcp_tools as mt
    state = {}

    def reply(messages):
        r = state["egress"].request("POST", state["url"] + "/control/kill_switch", json={"on": True})
        state["switch_status"] = r.status_code
        return {"role": "assistant", "content": None, "tool_calls": [REFUND_CALL]}

    standin, emu, srd1, egress, target, _, _ = _tool_world(ctx, "s19", reply)
    state.update(egress=egress, url=srd1.server.url)
    try:
        sid = target.open_session()
        first = egress.request("POST", f"{srd1.server.url}/sessions/{sid}/messages", json={"message": "Refund it."})
        second = egress.request("POST", f"{srd1.server.url}/sessions/{sid}/messages", json={"message": "Hello?"})
    finally:
        _close_world(standin, emu, srd1, egress)
    entries = srd1.trace.entries()
    calls = [e["decision"] for e in entries if e["kind"] == "tool_call"]
    switches = [{"on": e["on"], "has_t_ns": isinstance(e.get("t_ns"), int)} for e in entries if e["kind"] == "kill_switch"]
    lines = len(mt.read_ledger(emu.ledger.path))
    obs = {"switch_status": state.get("switch_status"), "tool_call_decisions": calls, "kill_switch_lines": switches,
           "ledger_lines": lines, "turn_status": first.status_code, "next_turn": [second.status_code, second.json()]}
    ok = (calls == ["killed_before_tool_call"] and lines == 0 and switches == [{"on": True, "has_t_ns": True}]
          and second.status_code == 503 and second.json() == {"error": "killed"})
    return _result(obs, ok)


@dataclass(frozen=True)
class Probe:
    id: str
    path: str
    target: str
    expected: str
    fn: object
    control: object       # a function that returns the control's "file:line (name)"


def _ctl(*names):
    """A control function: the 'file:line (name)' of each named object, joined."""
    def get():
        from .. import egress
        from ..adapters import base, registry, srd1_http
        from ..emulators import mcp_tools
        from ..engine import runlog, spike
        _, gw, _, _ = _srd1()
        from targets.srd1.app import server
        objs = {"egress._allowed": egress.EgressClient._allowed, "egress.request": egress.EgressClient.request,
                "egress.__init__": egress.EgressClient.__init__, "egress._parse_entry": egress._parse_entry,
                "gateway.complete": gw.Gateway.complete, "base._send_with_retry": base.ChannelTarget._send_with_retry,
                "registry.allow_hosts": registry.allow_hosts, "srd1_http._call": srd1_http.Srd1ChatTarget._call,
                "server.Srd1Server": server.Srd1Server.__init__, "mcp.__init__": mcp_tools.ToolEmulator.__init__,
                "mcp.ledger": mcp_tools.Ledger.append, "spike.execute": spike.execute,
                "runlog.git_commit": runlog.git_commit, "runlog.stamp_log": runlog.stamp_log,
                "scan.scan": scan_mod.scan}
        from targets.srd1.app import mcp_client, orchestrator
        from ..emulators import handlers
        objs.update({"mcp_client._send": mcp_client.McpClient._send,
                     "orch._tool_call": orchestrator.Orchestrator._tool_call,
                     "handlers.load_store": handlers.load_store,
                     "orch.set_kill_switch": orchestrator.Orchestrator.set_kill_switch,
                     "server.kill_switch": server.make_handler})
        return "; ".join(where(objs[n]) for n in names)
    return get


PROBES = [
    Probe("S1", "N1 egress client", f"https://{host('egress')}/v1, allow-list empty",
          "EgressRefused; 1 event allowed false; 0 transport calls; 0 guard trips", s1,
          _ctl("egress._allowed", "egress.request")),
    Probe("S2", "N1 egress client", "http://203.0.113.10/ with 203.0.113.10 on the allow-list",
          "EgressRefused (plain HTTP goes to loopback only); 0 transport calls", s2, _ctl("egress._allowed")),
    Probe("S3", "N1 egress client", f"stand-in on 127.0.0.1 answers 302 to https://{host('redirect')}/",
          "one event, status 302; no second request; 0 guard trips", s3, _ctl("egress.__init__")),
    Probe("S4", "N1 egress client", f"stand-in on 127.0.0.1, with a proxy at {host('proxy')}:3128 in the environment",
          "the request reaches the stand-in; the proxy is not used; 0 guard trips", s4, _ctl("egress.__init__")),
    Probe("S5", "N1 egress client", "stand-in on 127.0.0.1; a random token in a header, the body and the query",
          "the stand-in gets the token; the egress log file does not hold it", s5, _ctl("egress.request")),
    Probe("S6", "N1 egress client", "https://[::1]:1/ and https://198.51.100.7/ with [::1]:2 and 198.51.100.8 listed",
          "both EgressRefused; the entries parse as listed; 0 transport calls", s6,
          _ctl("egress._parse_entry", "egress._allowed")),
    Probe("S7", "N2 SRD-1 gateway", f"https://{host('model-host')}/api/v1, allow-list empty, a random key in an env dict",
          "EgressRefused re-raised; the key is not in the egress log or the error", s7, _ctl("gateway.complete")),
    Probe("S8", "N3 channel adapters", f"each of the 5 adapters at https://<adapter>.{SUFFIX}, allow-list empty",
          "EgressRefused at the first request; 0 transport calls; with the adapter's own allow-list, the mock "
          "transport sees only the hosts that hosts() names", s8,
          _ctl("base._send_with_retry", "registry.allow_hosts")),
    Probe("S9", "N4 srd1-http adapter", f"https://{host('srd1')}, allow-list empty", "EgressRefused at open_session",
          s9, _ctl("srd1_http._call")),
    Probe("S10", "N5 SRD-1 chat server", "the bind address of Srd1Server", "127.0.0.1", s10,
          _ctl("server.Srd1Server")),
    Probe("S11", "N6 tool emulator", f"hosts 0.0.0.0 and 192.0.2.1; Origin https://{host('origin')}; no key",
          "ValueError for both hosts; bind 127.0.0.1; 403 and 401 with the connection closed; 0 ledger lines", s11,
          _ctl("mcp.__init__")),
    Probe("S12", "N6 tool emulator", "a legacy MCP session on 127.0.0.1: initialize, tools/list, tools/call, DELETE",
          "0 guard trips; 1 ledger line; the ledger chain verifies", s12, _ctl("mcp.ledger")),
    Probe("S13", "N9 other sockets and processes", "an AST scan of faias_harness/, targets/ and tools/sweep_barred.py",
          "the sites found equal the declared list; the code manifest equals the locked one", s13, _ctl("scan.scan")),
    Probe("S14", "N7 the engine (Inspect or the direct loop)", "one item, k = 3, against the stand-in on 127.0.0.1",
          "every allowed host is loopback; the header lists two loopback entries; 0 guard trips", s14,
          _ctl("spike.execute")),
    Probe("S15", "N8 git", "runlog.git_commit and runlog.stamp_log under the process guard",
          "argv is git -C <repo> rev-parse only; stamp_log starts no process", s15,
          _ctl("runlog.git_commit", "runlog.stamp_log")),
    Probe("S16", "P1 publication", "a static check of imports and process words",
          "no publication module is imported; no process call holds a publication word", s16, _ctl("scan.scan")),
    Probe("S17", "N10 SRD-1 MCP client", f"https://{host('emulator')}/mcp, allow-list empty, a random key in an env dict",
          "EgressRefused at initialize; 0 transport calls; the key is not in the egress log or the error", s17,
          _ctl("mcp_client._send", "egress._allowed")),
    Probe("S18", "N10, N11 SRD-1 tool round and the emulator's store",
          "SRD-1 with tools on, the stand-in on 127.0.0.1 asks for one fee_refund, the emulator on 127.0.0.1",
          "2 model calls; 1 ledger line; 1 tool_call trace line, sent, whose mcp_call_seq is the ledger seq; the "
          "store file is unchanged; no key in any file; 0 guard trips", s18,
          _ctl("orch._tool_call", "handlers.load_store")),
    Probe("S19", "N12 SRD-1 kill switch", "the switch goes on (POST /control/kill_switch) before the tool call",
          "no ledger line; a tool_call trace line killed_before_tool_call; one kill_switch line; the next turn gets "
          "503 killed", s19, _ctl("orch.set_kill_switch", "server.kill_switch")),
]


def plan(probes=None):
    """The probe plan that the header holds and the lock covers."""
    return [{"id": p.id, "path": p.path, "target": p.target, "expected": p.expected} for p in (probes or PROBES)]


def run_probe(p, ctx, clock_ns=time.time_ns):
    """Run one probe under the guard. Returns its record."""
    mark = ctx.guard.mark()
    t_ns = clock_ns()
    try:
        r = p.fn(ctx)
    except Exception as e:      # noqa: BLE001 - a probe that breaks is a failed probe, and the class name says why
        r = _result({"error": type(e).__name__}, False)
    trips, calls = ctx.guard.since(mark)
    ok = r["ok"] and not trips
    return {"type": "probe", "id": p.id, "path": p.path, "control": p.control(), "target": p.target,
            "expected": p.expected, "observed": r["observed"],
            "counts": {"attempts": r["attempts"], "refused": r["refused"], "guard_trips": len(trips),
                       "process_starts_refused": sum(1 for c in calls if not c["allowed"])},
            "trips": [_portable(t, ctx.repo) for t in trips],
            "notes": {**r["notes"], "process_starts": [_portable(c, ctx.repo)["argv"] for c in calls]},
            "result": "PASS" if ok else "FAIL", "t_ns": t_ns}


def _portable(entry, repo):
    """A guard entry without local paths: the folder of the checkout becomes <repo>, an executable its file name."""
    if "argv" not in entry:
        return dict(entry)
    argv = [("<repo>" if w == str(repo) else w) for w in entry["argv"]]
    return {**entry, "argv": ([Path(argv[0]).name] + argv[1:]) if argv else []}


def temp_folder():
    return tempfile.TemporaryDirectory(prefix="faias_selftest_")
