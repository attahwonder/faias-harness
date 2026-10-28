"""Adapter for the SRD-1 minimal chat channel. It offers `conversation`, `traces` and, since 0.6, `config_read`.

Tested against SRD-1 minimal on 127.0.0.1, with a local stand-in for the model host (tests/), and used in the
live runs of 20, 21 and 22 October 2026 (before `config_read` existed). It talks to no other system.
Every request goes through the shared egress client, so the allow-list applies to it.
`config_read` reads one kind of configuration: "prompt" (the system prompt of the active variant, its SHA-256 and
the tools, GET /config/prompt) and, since 0.6, "tools", "gate" and "entitlements". It is tested against SRD-1
minimal only.

Since 0.6 it also offers `tool_routing` and `control`, in part:
- `route_tools_to(url)`: SRD-1 minimal takes the tool server's URL when it is built. The adapter accepts the URL
  given at build time and refuses any other.
- `read_tool_ledger(tool)`: the lines of the run's tool ledger (the emulator's file), for one tool.
- `set_kill_switch(on)`: POST /control/kill_switch (C-18). `rollback`, `switch_version` and `inject_failure` are
  not built and raise NotImplementedError.
"""
from ..emulators.mcp_tools import read_ledger
from ..errors import CapReached
from ..target import Target


class Srd1ChatError(Exception):
    pass


class Srd1ChatTarget(Target):
    capabilities = frozenset({"conversation", "traces", "config_read", "tool_routing", "control"})
    CONFIG_KINDS = ("prompt", "tools", "gate", "entitlements")

    def __init__(self, base_url, egress, timeout=None, *, tools_url=None, ledger_path=None):
        """`timeout` (seconds) for each call to SRD-1; it must be longer than the gateway's retries can take.
        `tools_url` and `ledger_path`: the tool server that SRD-1 was built with, and its ledger file."""
        self.base_url = base_url.rstrip("/")
        self.egress = egress
        self.timeout = timeout
        self.tools_url = tools_url
        self.ledger_path = ledger_path

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

    # config_read
    def read_config(self, kind):
        if kind not in self.CONFIG_KINDS:
            raise NotImplementedError(f"SRD-1 minimal offers no configuration of the kind '{kind}'")
        return self._call("GET", f"/config/{kind}")

    # tool_routing
    def route_tools_to(self, emulator_url):
        if self.tools_url is None or emulator_url != self.tools_url:
            raise NotImplementedError("SRD-1 minimal takes the tool server's URL when it is built; "
                                      "it cannot be routed to another after the build")

    def read_tool_ledger(self, tool):
        if self.ledger_path is None:
            raise NotImplementedError("this SRD-1 was built with tools off: there is no tool ledger")
        return read_ledger(self.ledger_path, tool=tool)

    # control
    def set_kill_switch(self, on):
        self._call("POST", "/control/kill_switch", {"on": bool(on)})
