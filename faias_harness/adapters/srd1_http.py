"""Adapter for the SRD-1 minimal chat channel. It offers `conversation` and `traces`.

Tested against SRD-1 minimal on 127.0.0.1, with a local stand-in for the model host (tests/), and used in the
live runs of 20 and 21 October 2026. It talks to no other system.
Every request goes through the shared egress client, so the allow-list applies to it.
"""
from ..errors import CapReached
from ..target import Target


class Srd1ChatError(Exception):
    pass


class Srd1ChatTarget(Target):
    capabilities = frozenset({"conversation", "traces"})

    def __init__(self, base_url, egress, timeout=None):
        """`timeout` (seconds) for each call to SRD-1; it must be longer than the gateway's retries can take."""
        self.base_url = base_url.rstrip("/")
        self.egress = egress
        self.timeout = timeout

    def _call(self, method, path, body=None):
        r = self.egress.request(method, self.base_url + path, json=body, timeout=self.timeout)
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code == 503 and data.get("error") == "cap_reached":
            raise CapReached("SRD-1 reports that the spend cap is reached")
        if r.status_code >= 400:
            raise Srd1ChatError(f"SRD-1 returned HTTP {r.status_code} ({data.get('error', 'no error name')})")
        return data

    # conversation
    def open_session(self):
        return self._call("POST", "/sessions")["session_id"]

    def send(self, session_id, message):
        return self._call("POST", f"/sessions/{session_id}/messages", {"message": message})["reply"]

    def close_session(self, session_id):
        self._call("DELETE", f"/sessions/{session_id}")

    # traces
    def get_trace(self, session_id):
        return self._call("GET", f"/sessions/{session_id}/trace")["entries"]
