"""Put the parts of SRD-1 minimal together: trace store, gateway, orchestrator and chat channel.

With `tools_enabled` in the settings (since 0.6), it also builds the refund gate (C-09) from the risk-appetite file,
and opens the MCP client (C-08) to the tool server whose URL the harness gives at build time (`tool_routing`). A
null cap or limit stops the build with "refund cap not declared" before any file is written.

`gate_layer="prompt"` (since 0.7, the TP-AGT-012 positive control) builds the prompt-only gate. The system prompt is
the prompt-gate template (orchestrator.PROMPT_GATE_FILE) with the gate's cap and limit, and the orchestrator does not
check C-09. A template that does not exist stops the build.

`retrieval_mode` (since 0.7, for TP-DAT-009) is off (the default), pipeline or tool. With off, the build is as
before. With pipeline or tool, the build reads the document store (C-05 minimal, retrieval.py) and gives C-07 the
document rule. Then the mode decides the tool, not `tools_enabled`. With pipeline, the model gets no tool, so the
build opens no MCP client. With tool, the build opens the MCP client to the URL that the harness gives. The tools
then come from that server (document_search), and no refund gate is read. `retrieval_filter` is the stage of the
filter, and `delegation_credential` is the principal that C-07 presents on the tool path (orchestrator.py).
"""
from dataclasses import dataclass
from pathlib import Path

from .entitlements import DATA_FILE, Entitlements
from .gate import RISK_FILE, RefundGate
from .gateway import OPENROUTER_BASE, Gateway
from .mcp_client import McpClient
from .orchestrator import (PROMPT_FILE, PROMPT_GATE_FILE, RETRIEVAL_MODES, Orchestrator, load_system_prompt,
                           render_prompt_gate)
from .retrieval import DOCUMENTS_FILE, DocumentStore, Retrieval
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
               data_file=DATA_FILE, gate_layer="orchestrator", prompt_gate_file=PROMPT_GATE_FILE,
               retrieval_mode="off", retrieval_filter="pre_retrieval", delegation_credential="user",
               documents_file=DOCUMENTS_FILE, cap=None):
    """`tools_url`, `tool_env`: the tool server's URL and the environment that holds its key (tools on only).
    `gate`: a RefundGate to use in place of the risk-appetite file (tests set their own values).
    `gate_layer`: "orchestrator" (the default) or "prompt" (the prompt-only gate, from `prompt_gate_file`).
    `retrieval_mode`, `retrieval_filter`, `delegation_credential` and `documents_file` set the retrieval of
    TP-DAT-009 (see the module text). The default off changes nothing.
    `cap` is a gateway.CapMeter to share. A TP-DAT-009 run builds SRD-1 twice and both builds count against one cap.
    None (the default) makes a new one from the settings."""
    if retrieval_mode not in RETRIEVAL_MODES:
        raise ValueError(f"retrieval_mode must be one of {', '.join(RETRIEVAL_MODES)}")
    retrieval_on = retrieval_mode != "off"
    tools_on = retrieval_mode == "tool" if retrieval_on else bool(getattr(settings, "tools_enabled", False))
    if tools_on:
        if not tools_url:
            raise ValueError("tools on: the harness gives the tool server's URL when SRD-1 is built (tool_routing)")
        if not retrieval_on:
            gate = gate or RefundGate.from_file(risk_file)      # GateNotDeclared: "refund cap not declared"
    if gate_layer == "prompt":
        if not tools_on or system_prompt is not None or retrieval_on:
            raise ValueError("gate_layer prompt needs tools on, and its prompt comes from the prompt-gate template")
        prompt_gate_file = Path(prompt_gate_file)
        if not prompt_gate_file.is_file():
            raise ValueError(f"the prompt-gate template {prompt_gate_file.name} is not written yet")
        system_prompt = render_prompt_gate(load_system_prompt(prompt_gate_file), gate)
        prompt_source = prompt_gate_file.name
    retrieval = None
    if retrieval_on:
        entitlements = Entitlements(data_file, documents_file=documents_file, retrieval_stage=retrieval_filter)
        retrieval = Retrieval(DocumentStore(documents_file), entitlements, retrieval_filter, mode=retrieval_mode,
                              credential=delegation_credential)
    else:
        entitlements = Entitlements(data_file)
    trace = TraceStore(trace_path)
    gateway = Gateway(settings, egress, env=env, base_url=base_url, cap=cap)
    if system_prompt is None:
        system_prompt, prompt_source = load_system_prompt(), PROMPT_FILE.name
    mcp = McpClient(tools_url, egress, key_env=settings.tool_key_env, env=tool_env).open() if tools_on else None
    orch = Orchestrator(system_prompt, gateway, trace, tools=CONFIGURED_TOOLS, prompt_source=prompt_source, mcp=mcp,
                        entitlements=entitlements, gate=gate if tools_on else None, gate_layer=gate_layer,
                        retrieval=retrieval, retrieval_mode=retrieval_mode)
    return Srd1(Srd1Server(orch, trace, port=port), orch, gateway, trace, mcp)
