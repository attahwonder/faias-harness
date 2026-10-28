"""Put the parts of SRD-1 minimal together: trace store, gateway, orchestrator and chat channel.

With `tools_enabled` in the settings (since 0.6), it also builds the refund gate (C-09) from the risk-appetite file,
and opens the MCP client (C-08) to the tool server whose URL the harness gives at build time (`tool_routing`). A
null cap or limit stops the build with "refund cap not declared" before any file is written.
"""
from dataclasses import dataclass

from .entitlements import DATA_FILE, Entitlements
from .gate import RISK_FILE, RefundGate
from .gateway import OPENROUTER_BASE, Gateway
from .mcp_client import McpClient
from .orchestrator import PROMPT_FILE, Orchestrator, load_system_prompt
from .server import Srd1Server
from .trace import TraceStore

# Tools that the orchestrator lists in its configuration. With tools off, this version calls none of them; with
# tools on, it calls fee_refund only.
CONFIGURED_TOOLS = ("account_summary", "complaint_ticket", "contact_update", "fee_refund", "hand_off")


@dataclass
class Srd1:
    server: Srd1Server
    orchestrator: Orchestrator
    gateway: Gateway
    trace: TraceStore
    mcp: McpClient = None


def build_srd1(settings, egress, trace_path, *, base_url=OPENROUTER_BASE, env=None, port=0, system_prompt=None,
               prompt_source=None, tools_url=None, tool_env=None, gate=None, risk_file=RISK_FILE,
               data_file=DATA_FILE):
    """`tools_url`, `tool_env`: the tool server's URL and the environment that holds its key (tools on only).
    `gate`: a RefundGate to use in place of the risk-appetite file (tests set their own values)."""
    tools_on = bool(getattr(settings, "tools_enabled", False))
    if tools_on:
        if not tools_url:
            raise ValueError("tools on: the harness gives the tool server's URL when SRD-1 is built (tool_routing)")
        gate = gate or RefundGate.from_file(risk_file)      # GateNotDeclared: "refund cap not declared"
    entitlements = Entitlements(data_file)
    trace = TraceStore(trace_path)
    gateway = Gateway(settings, egress, env=env, base_url=base_url)
    if system_prompt is None:
        system_prompt, prompt_source = load_system_prompt(), PROMPT_FILE.name
    mcp = McpClient(tools_url, egress, key_env=settings.tool_key_env, env=tool_env).open() if tools_on else None
    orch = Orchestrator(system_prompt, gateway, trace, tools=CONFIGURED_TOOLS, prompt_source=prompt_source, mcp=mcp,
                        entitlements=entitlements, gate=gate if tools_on else None)
    return Srd1(Srd1Server(orch, trace, port=port), orch, gateway, trace, mcp)
