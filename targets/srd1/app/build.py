"""Put the parts of SRD-1 minimal together: trace store, gateway, orchestrator and chat channel."""
from dataclasses import dataclass

from .gateway import OPENROUTER_BASE, Gateway
from .orchestrator import Orchestrator, load_system_prompt
from .server import Srd1Server
from .trace import TraceStore

# Tools that the orchestrator lists in its configuration. This version calls none of them.
CONFIGURED_TOOLS = ("account_summary", "complaint_ticket", "contact_update", "fee_refund", "hand_off")


@dataclass
class Srd1:
    server: Srd1Server
    orchestrator: Orchestrator
    gateway: Gateway
    trace: TraceStore


def build_srd1(settings, egress, trace_path, *, base_url=OPENROUTER_BASE, env=None, port=0, system_prompt=None):
    trace = TraceStore(trace_path)
    gateway = Gateway(settings, egress, env=env, base_url=base_url)
    orch = Orchestrator(system_prompt if system_prompt is not None else load_system_prompt(), gateway, trace,
                        tools=CONFIGURED_TOOLS)
    return Srd1(Srd1Server(orch, trace, port=port), orch, gateway, trace)
