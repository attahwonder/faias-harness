# Adapters

An adapter connects the target interface (`faias_harness/target.py`) to a real system. An adapter implements the method groups of the capabilities that the system exposes.

**No adapter is supported in v0.4.** `generic-http` is untested. `srd1-http` is tested only against SRD-1 minimal and a local stand-in. `docs/PLATFORMS.md` surveys the public documentation of six agent platforms. It is not a test and not support.

| Adapter | File | Capabilities | State |
|---|---|---|---|
| `generic-http` | `openai_chat.py` | `conversation` | Written. Untested. No test calls it. |
| `srd1-http` | `srd1_http.py` | `conversation`, `traces` | Added in 0.3.0. Tested against SRD-1 minimal on 127.0.0.1, with a local stand-in for the model host, and used in the live runs of 20 and 21 October 2026. It talks to no other system. |
| Azure AI Foundry | none | not decided | Planned. Not built. Not tested. |
| Amazon Bedrock | none | not decided | Planned. Not built. Not tested. |
| In-house frameworks | none | not decided | Planned. Not built. Not tested. |

## About `generic-http`

- It speaks the OpenAI-compatible chat-completions format over HTTP (Python standard library only).
- It reads the API key from the environment variable named in `settings.api_key_env` of the profile.
- It offers `conversation` only. A profile may list more capabilities than this adapter implements. Version 0.3 does not check that. A method that the adapter lacks raises `NotImplementedError`.

## Rules for every adapter

- The adapter sends traffic only to the staging endpoints that the profile names.
- Keys come from environment variables. A key never goes in a file, a log or an error message.
- The adapter declares the capabilities it implements in its `capabilities` attribute.
- The adapter does not reach into production systems.
