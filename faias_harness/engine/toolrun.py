"""Serve SRD-1's tool for one engine run with tools on (since 0.6). One engine uses it: spike.execute (TP-SEC-001).

The run starts the harness's MCP tool emulator on 127.0.0.1 with SRD-1's tools file (targets/srd1/tools.yaml) and
its synthetic store, and keeps the tool ledger in the run folder. It generates a key for the emulator in this
process (secrets.token_hex) and passes it in an `env` dict only: to the emulator, which checks it, and to SRD-1's MCP
client, which sends it. The key is never written to a file, a log or a message. The engine adds the emulator's port
to the allow-list, so SRD-1's calls to it go through the egress client like every other call.
"""
import secrets
from pathlib import Path

from .. import __version__
from ..emulators import mcp_tools
from . import runlog

REPO = Path(__file__).resolve().parents[2]
SRD1_TOOLS = REPO / "targets" / "srd1" / "tools.yaml"
LEDGER_NAME = "tool_ledger.jsonl"


class ToolRun:
    """Use as a context manager, or call start() and stop()."""

    def __init__(self, settings, run_dir, egress, tools_file=SRD1_TOOLS):
        self.key_env = settings.tool_key_env
        self.env = {self.key_env: secrets.token_hex(32)}    # the per-run key: in this dict only
        self.tools_file = Path(tools_file)
        self.tools = mcp_tools.load_tools(self.tools_file)
        self.emulator = mcp_tools.ToolEmulator(self.tools, Path(run_dir) / LEDGER_NAME, api_key_env=self.key_env,
                                               env=self.env)
        self.egress = egress
        self.started = False

    @property
    def url(self):
        return self.emulator.url

    def start(self):
        self.emulator.start()
        self.started = True
        self.egress.add_allow(f"127.0.0.1:{self.emulator.port}")
        return self

    def stop(self):
        if self.started:
            self.emulator.stop()
            self.started = False

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.stop()

    def header(self, definitions_sha256):
        """The tools block of the run log header. The lock covers it."""
        return {"enabled": True, "names": [t["name"] for t in self.tools["tools"]],
                "definitions_sha256": definitions_sha256, "emulator": f"faias-tool-emulator {__version__}",
                "tools_file": self.tools_file.name, "tools_file_sha256": runlog.sha256_hex(self.tools_file.read_bytes()),
                "store_sha256": self.emulator.store_sha256, "key": f"generated in the run; carried in {self.key_env}; "
                                                                   "never written"}

    def summary(self):
        """The tool ledger for the run log summary: file, SHA-256, lines, head and the chain check."""
        path = self.emulator.ledger.path
        ok, problems = mcp_tools.verify_ledger(path, expected_head=self.emulator.ledger.head)
        return {"file": path.name, "sha256": runlog.sha256_hex(path.read_bytes()) if path.exists() else None,
                "entries": self.emulator.ledger.seq, "head": self.emulator.ledger.head, "chain_ok": ok,
                "problems": problems, "methods": dict(self.emulator.methods)}
