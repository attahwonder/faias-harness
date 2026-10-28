# Adapters

An adapter connects the target interface (`faias_harness/target.py`) to a real system. An adapter implements the method groups of the capabilities that the system exposes.

**No adapter is supported in v0.5.** Every channel adapter below except `generic-http` is untested against a platform. Each is tested against a local stand-in (`tests/standins.py`, `tests/test_adapters.py`, `tests/test_external.py`). `srd1-http` is tested only against SRD-1 minimal and a local stand-in. `docs/PLATFORMS.md` surveys the public documentation of six agent platforms. It is not a test and not support.

| Adapter | File | Channel | Capabilities | State |
|---|---|---|---|---|
| `directline` | `directline.py` | Direct Line 3.0 (Microsoft Copilot Studio agents, Azure Bot Service bots) | `conversation` | Added in 0.5. Untested against a platform. Tested against a local stand-in Direct Line service. |
| `openai-responses` | `openai_responses.py` | Responses API (OpenAI; Azure AI Foundry agents through a project's Responses endpoint) | `conversation` | Added in 0.5. Untested against a platform. Tested against a local stand-in. |
| `a2a` | `a2a.py` | Agent2Agent (A2A) JSON-RPC, protocol version 1.0 or 0.3 | `conversation` | Added in 0.5. Untested against a platform. Tested against a local stand-in agent of each version. |
| `json-http` | `json_http.py` | A firm's own chat endpoint, JSON over HTTP, from a request template and a reply path | `conversation` | Added in 0.5. Untested against a platform. Tested against a local stand-in. |
| `generic-http` | `openai_chat.py` | OpenAI-compatible chat completions | `conversation` | Written in 0.1. In 0.5 it moved onto the egress client and gained a test against a local stand-in. One live run: OpenRouter chat completions endpoint, 22 October 2026, 60 calls to openai/gpt-oss-120b. Channel and engine only: no system prompt; routing lock requested, not observed. |
| `srd1-http` | `srd1_http.py` | SRD-1 minimal chat channel | `conversation`, `traces` | Added in 0.3.0. Tested against SRD-1 minimal on 127.0.0.1, with a local stand-in for the model host, and used in the live runs of 20, 21 and 22 October 2026. It talks to no other system. |
| Amazon Lex V2, Dialogflow CX, contact-centre messaging, Agentforce Agent API | none | none | not decided | Not built. |
| Amazon Bedrock InvokeAgent | none | none | not decided | Not built. It needs AWS request signing. Where AgentCore exposes an A2A endpoint, `a2a` may reach it; that is not tested. |

`adapters/registry.py` lists the five channel adapters that an external run can use (`docs/ARCHITECTURE.md`, section 13). `srd1-http` is not in it: SRD-1 minimal runs through the SRD-1 engine.

## Pick the channel that customers or colleagues use

An external run tests the path through the channel that the adapter reaches. If customers reach an agent through Direct Line and a test reaches the same agent through A2A, the test measures a different path: a control in the customer channel is not in it. List `conversation` in a profile only for the channel that the firm's own front end uses.

## Profile fields for a channel adapter

- `endpoint`: the channel's base URL. Plain HTTP goes only to the local machine (the egress client refuses it otherwise). The profile validator refuses a query string in it (0.5), because a query can hold a key.
- `settings.api_key_env`: the name of the environment variable that holds the key or token, where the adapter needs one.
- `adapter_options`: the adapter's own settings. Each module's docstring lists them, and `validate_options` checks them. The profile validator refuses a field name from a fixed list of key-like names anywhere in `adapter_options`.

## What each adapter records for each call

After each `send`, `last_exchange` holds what the channel showed: the host that answered, the HTTP statuses, the time, the retries, and any block, finish signal, model name or token use that the channel's response names. It never holds a header or a key. The external engine copies it into the run record.

| Adapter | Names the model | Names a block | Notes |
|---|---|---|---|
| `directline` | no | no | A reply that the platform replaced with a fallback message counts as a reply. Other activity types are recorded by name. |
| `openai-responses` | yes (as the response names it) | refusal part; incomplete reason or HTTP 400 code `content_filter` | Incomplete reason `max_output_tokens` is finish reason `length`. |
| `a2a` | no | task state rejected (`a2a_rejected`) | A task that is not final is read again (not charged) until final, interrupted or timed out. |
| `json-http` | where `model_path` names it | where `blocked_path` is true | A response without the reply path is a channel error. |
| `generic-http` | yes (as the response names it) | finish reason or HTTP 400 code `content_filter` | |

## Rules for every adapter

- The adapter sends traffic only to the endpoints that the profile names, through the shared egress client.
- Keys come from environment variables. A key never goes in a file, a log, an error message or `last_exchange`.
- The adapter calls `before_send` before every attempt that may reach the agent, so the call budget counts it.
- It retries only HTTP 429 and 503, where the channel did not process the request, at most twice by default.
- The adapter declares the capabilities it implements in its `capabilities` attribute.
- The adapter does not reach into production systems.
