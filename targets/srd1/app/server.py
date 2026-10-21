"""C-01, minimal: a local HTTP chat channel for SRD-1.

It binds to 127.0.0.1 only. Routes:

  POST   /sessions                      -> 201 {"session_id": "s1"}
  POST   /sessions/<id>/messages        body {"message": "..."} -> 200 {"reply": "...", "turn": n}
  GET    /sessions/<id>/trace           -> 200 {"entries": [...]}   (the C-15 lines of the session)
  DELETE /sessions/<id>                 -> 200 {"closed": true}
  GET    /health                        -> 200 {"status": "ok"}

Errors: 400 bad request, 404 unknown session, 413 message too long, 429 too many turns in a session,
503 {"error": "cap_reached"}, 502 {"error": "model_call_failed"}. An error body never holds a key or a detail
of the upstream response.
This version does not render Markdown to HTML (a feature of C-01 in the specification).
"""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from faias_harness.errors import CapReached

from .orchestrator import MessageTooLong, TooManyTurns, UnknownSession

ROUTE_MESSAGES = re.compile(r"/sessions/([A-Za-z0-9_]+)/messages")
ROUTE_TRACE = re.compile(r"/sessions/([A-Za-z0-9_]+)/trace")
ROUTE_SESSION = re.compile(r"/sessions/([A-Za-z0-9_]+)")


def make_handler(orchestrator, trace):
    lock = threading.Lock()      # one request at a time reaches the orchestrator

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):      # no console log: the trace store is the record
            pass

        def _send(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n > 100_000:
                return None
            try:
                obj = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            except (ValueError, UnicodeDecodeError):
                return None
            return obj if isinstance(obj, dict) else None

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, {"status": "ok"})
            m = ROUTE_TRACE.fullmatch(self.path)
            if m:
                return self._send(200, {"entries": trace.entries(m.group(1))})
            self._send(404, {"error": "not_found"})

        def do_POST(self):
            if self.path == "/sessions":
                self._body()
                with lock:
                    sid = orchestrator.open_session()
                return self._send(201, {"session_id": sid})
            m = ROUTE_MESSAGES.fullmatch(self.path)
            if not m:
                return self._send(404, {"error": "not_found"})
            body = self._body()
            if body is None or not isinstance(body.get("message"), str):
                return self._send(400, {"error": "bad_request"})
            try:
                with lock:
                    reply, entry = orchestrator.handle(m.group(1), body["message"])
            except UnknownSession:
                return self._send(404, {"error": "unknown_session"})
            except MessageTooLong:
                return self._send(413, {"error": "message_too_long"})
            except TooManyTurns:
                return self._send(429, {"error": "too_many_turns"})
            except CapReached:
                return self._send(503, {"error": "cap_reached"})
            except Exception:       # noqa: BLE001 - no detail leaves the server
                return self._send(502, {"error": "model_call_failed"})
            self._send(200, {"reply": reply, "turn": entry["turn"]})

        def do_DELETE(self):
            m = ROUTE_SESSION.fullmatch(self.path)
            if not m:
                return self._send(404, {"error": "not_found"})
            with lock:
                orchestrator.close_session(m.group(1))
            self._send(200, {"closed": True})

    return Handler


class Srd1Server:
    """Runs the chat channel in a thread. Use as a context manager or call start() and stop()."""

    def __init__(self, orchestrator, trace, port=0):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(orchestrator, trace))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()
