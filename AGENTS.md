# AGENTS.md

A guide for any person or software agent that works on this repository. It does not depend on one tool or one vendor. Read `README.md` and `docs/ARCHITECTURE.md` as well.

## Purpose and status

This repository is the FAIAS harness, version 0.6 (in development). FAIAS is the Financial AI Independent Assurance Standard. The harness is planned to run the FAIAS test procedures against an AI system in a test environment.

Version 0.4 holds the design, 45 procedure records, a record checker and a preflight. It also holds a run engine for TP-SEC-001. The engine runs on SRD-1 minimal and is tested against a local stand-in for the model host. By default it runs the full item set (131 items and 25 clean-baseline requests), labelled "uncalibrated scorer, not a finding". The full set has run live through OpenRouter on 22 October 2026. That was six runs of 468 calls, against three hosted models. The 20 spike items remain as an option, labelled "engine spike, not a finding". The spike set has run live through OpenRouter on 20 and 21 October 2026. That was six runs of 180 calls, against three hosted models. Those run logs are not in the repository. Version 0.5 adds an external run of the same procedure and an MCP tool emulator. The external run goes through five channel adapters (`directline`, `openai-responses`, `a2a`, `json-http`, `generic-http`). Each is tested against a local stand-in. `generic-http` has run live one time, against the OpenRouter chat completions endpoint on 22 October 2026. That run tested the channel and the engine only. The adapter sends no system prompt. The harness cannot see if OpenRouter applied the routing lock. The harness supports no platform. Version 0.6.0 adds a second procedure, TP-SEC-006, on SRD-1 minimal only, labelled "uncalibrated scorer, not a finding; items from one model family". It is tested against a local stand-in for the model host only. No live run has used it. Version 0.6 also adds a containment self-test of the harness (`faias_harness/selftest/`). It makes client-level and process-level checks only. It is not a network sandbox. It adds one tool to SRD-1 minimal (`fee_refund`, through the tool emulator), off by default. With tools on, the orchestrator checks a kill switch, the customer's entitlement and a refund gate before each call. The refund cap and the session limit are null until the owner declares them. The tool is tested against a local stand-in only. No live run has used it. Do not write text that says or implies more.

## Layout

| Path | Content |
|---|---|
| `faias_harness/` | The package: `capabilities.py` (the seven capability names), `target.py` (the target interface), `profile.py` (profile loader and validator), `settings.py` (settings loader and validator, for SRD-1 and external runs), `egress.py` (the HTTP client with an allow-list), `errors.py`, `preflight.py`, `adapters/` (`base.py` shared base, call budget and retry rule; `registry.py` the channel adapters of an external run; `directline.py`, `openai_responses.py`, `a2a.py`, `json_http.py`, `openai_chat.py`, `srd1_http.py`), `engine/` (`sec001.py` items, scorer and measures; `runlog.py` run log and signature; `spike.py` the SRD-1 run; `external.py` the external run; `inspect_task.py` the Inspect task that drives a run; `sec006.py`, `sec006_log.py` and `run_sec006.py` the scorer and measures, the run log (schema 6) and the SRD-1 run of TP-SEC-006; `toolrun.py` the tool emulator of a run with tools on), `emulators/` (`mcp_tools.py` the MCP tool emulator; `handlers.py` its built-in handlers), `testability.py` (the testability report), `selftest/` (the containment self-test: `guard.py`, `scan.py`, `probes.py` and the command) |
| `records/` | `TP-*.yaml` (45 generated records), `classification.tsv`, `OVERRIDES.yaml`, `capability_needs.yaml` (a draft input file), `SCHEMA.md`, `README.md` |
| `targets/` | `srd1/` (profile, firm values, settings for TP-SEC-001 and TP-SEC-006, the tools file `tools.yaml`, the risk-appetite file with null values, the synthetic data `data/synthetic_bank.yaml`, and draft specification of the example reference system; `app/` holds SRD-1 minimal: channel with the `config_read` and kill-switch routes, orchestrator, gateway, trace store, MCP client, entitlement check and refund gate) and `examples/` (six example profiles, the settings of an external run, and two tools files for the emulator) |
| `items/` | `TP-SEC-001.json`, the item set for TP-SEC-001 (the engine's default set, since 0.4.0), `TP-SEC-006.json`, the item set for TP-SEC-006 (since 0.6.0), and `README.md` (what came from where) |
| `third_party/` | The licence texts of the outside sources of `items/` (credited in `NOTICE`) |
| `tools/` | `build_records.py`, `check_records.py`, `test_check_records.py`, `sweep_barred.py` |
| `tests/` | `run_all.py`: all tests from one command; `test_*.py`: unit tests; `helpers.py`: a local stand-in model host and a guard against outside network use; `standins.py`: local stand-ins for Direct Line, the Responses API, an A2A agent and an in-app JSON endpoint |
| `docs/` | `ARCHITECTURE.md` (the design), `PLATFORMS.md` (a survey of vendor documentation) and `HARNESS_ATTESTATION.md` (the maintainer's isolation attestation of the harness, a draft) |

You need Python 3.11 or later and the packages `pyyaml`, `httpx` and `cryptography`. The commands below use the Windows launcher `py -3.14`. On other systems, use `python3`. Run every command from the repository root. `<FAIAS>` is the path of a copy of FAIAS version 1.3.4 (the folder that holds `V3_Testing_Playbook`). The FAIAS tree is read only.

## Commands

| Task | Command |
|---|---|
| Build the records | `py -3.14 -X utf8 tools/build_records.py --faias <FAIAS>` |
| Check the records and the capability needs | `py -3.14 -X utf8 tools/check_records.py --faias <FAIAS>` |
| Test the checker with seeded faults | `py -3.14 -X utf8 tools/test_check_records.py --faias <FAIAS>` |
| Run all tests | `py -3.14 -X utf8 tests/run_all.py --faias <FAIAS>` |
| Preflight on a profile | `py -3.14 -X utf8 -m faias_harness.preflight --profile <profile.yaml>` |
| Check the start conditions | `py -3.14 -X utf8 -m faias_harness.preflight --profile <profile.yaml> --start` |
| Preflight with a settings file | `py -3.14 -X utf8 -m faias_harness.preflight --profile <profile.yaml> --settings <settings.yaml>` |
| Make the two probe calls | `... --settings <settings.yaml> --probes` (model calls; needs the key variable) |
| Start an engine run | `... --settings <settings.yaml> --start --sign-key <private key file>` (model calls; asks for `yes`) |
| Make the run-signing key | `py -3.14 -X utf8 -m faias_harness.engine.runlog make-key --out <folder outside the repository>` |
| Verify a signed log | `py -3.14 -X utf8 -m faias_harness.engine.runlog verify --log <log> --sig <log>.sig [--pubkey <file>]` |
| Verify a log without the item set and record of this checkout | `... runlog verify --log <log> --sig <log>.sig --no-repo [--pubkey <file>]` |
| Score each reply of a log again | `py -3.14 -X utf8 -m faias_harness.engine.runlog rescore --log <log>` (exit 1 on a difference from the recorded score) |
| Write the SHA-256 file of a log | `py -3.14 -X utf8 -m faias_harness.engine.runlog stamp --log <log>` (prints the `ots stamp` command; runs no command and makes no network call) |
| Write a testability report | `py -3.14 -X utf8 -m faias_harness.testability --profile <profile.yaml> --out <new folder> [--sign-key <private key file>]` (no network call) |
| Verify a testability report | `py -3.14 -X utf8 -m faias_harness.testability verify --report <folder>/testability.json --pubkey <public key file>` |
| Sweep for barred terms and keys | `py -3.14 -X utf8 tools/sweep_barred.py --faias <FAIAS>` |
| Preflight of an external run | `... --profile <profile.yaml> --settings <external settings.yaml>` (no network call) |
| Make the channel probe | `... --settings <external settings.yaml> --probes` (one call through the channel) |
| Start an external run | `... --settings <external settings.yaml> --start --sign-key <private key file>` (channel calls; asks for `yes`) |
| Serve the tool emulator | `py -3.14 -X utf8 -m faias_harness.emulators.mcp_tools serve --tools <tools.yaml> --ledger <new file> [--api-key-env NAME] [--store <store.yaml>]` |
| Verify a tool ledger | `py -3.14 -X utf8 -m faias_harness.emulators.mcp_tools verify --ledger <ledger.jsonl>` |
| Run the containment self-test | `py -3.14 -X utf8 -m faias_harness.selftest --out <new folder> [--sign-key <private key file>]` (no network call, no key, no model) |
| Run the self-test of a log again | `py -3.14 -X utf8 -m faias_harness.selftest rerun --log <selftest_log.jsonl>` (exit 1 on a difference from the log) |

`--start` with a profile alone never starts a run. It prints why a run may not start, or it prints that a profile alone names no run. With `--settings`, `--start` starts an engine run on the item set in the settings. It starts only after the checks pass and you type `yes`. The exit codes are 0 (done), 2 (refused) and 3 (conditions met but no run named). Exit code 4 is `yes` not typed, and 5 is a run stopped at the cap or on an error. The tests make no model call and no outside connection. Do not run `--probes` or `--start` unless you mean to spend money. Both can reach a live model host or a live agent's platform.

## Binding rules

1. **Never test production.** Run the harness only against test or staging systems. The preflight cannot find out whether a system is a production system. The operator carries that duty.
2. **No run without an isolation attestation.** The profile names an attestation file. A run does not start without it. The attestation says that the target is a test or staging system, isolated from production, with synthetic data only.
3. **Synthetic data only.** Do not put real customer data, real account data or real personal data in any file, test or example.
4. **Inert test objectives.** A test item asks for a harmless marker. It does not ask for harmful content.
5. **Keys only in environment variables.** A key never goes in a file, a log, an error message, a test or a commit. A profile holds the name of the variable, not the key.
6. **Set a spend cap.** `settings.cap_usd` must be set before a run can start.
7. **Records come from FAIAS only.** Do not add a test step, a threshold or a rating that FAIAS does not state. The harness copies the ★ rating from the Confidence line of each procedure and never computes it.
8. **Never edit quoted FAIAS text.** A record item carries `src: <file>:<line>`. The builder removes Markdown marks and table pipes only. If a record is wrong, change the builder or `OVERRIDES.yaml`, then rebuild. Do not edit `records/TP-*.yaml` by hand.
9. **`records/capability_needs.yaml` is an input file.** A person keeps it by hand. No tool writes it. It is a draft: it holds the needs on which two independent model passes agree, a `settled` entry where they differed and a third reading decided, and an `open` entry where nothing is decided. No person has checked it. After a change, run `tools/check_records.py`. Every procedure must be one of the 45, every capability one of the seven, and every cited `file:line` must exist in the FAIAS tree.
10. **Claim only what the code does.** Do not write "supported", "certified", "approved" or "compliant" about a platform, a result or a system. FAIAS is not a certification or approval scheme.
11. **Before a commit, the sweep and all tests must pass.** Run `tools/sweep_barred.py` (0 failing hits) and `tests/run_all.py` (PASS). Fix the cause. Do not change a test or a barred-term list to make a check pass.

12. **Evidence mode needs the five routing locks.** A settings file with `mode: evidence` sets `routing.only`, `routing.allow_fallbacks` (false), `routing.zdr` (true), `routing.data_collection` (deny) and `routing.quantizations`. The validator refuses it otherwise. A run starts only after the owner types `yes`.
13. **The run-signing key stays outside the repository.** It is a key made for the harness. It is never a key that signs a release of FAIAS. No test, log or commit holds it. A test makes a temporary key in a temporary folder.
14. **Test items stay benign.** Each item asks for a harmless marker, a fictitious commitment about a product that does not exist, or a harmless canary string. A TP-SEC-006 item asks for the system prompt of SRD-1 minimal, which is synthetic and public. The disclosure is its objective. Never put a real credential or endpoint in a prompt or an item. Write new items from the class definitions of the procedure. Use text from a third-party set only when its licence allows it. Credit it in `NOTICE` and `items/README.md`, and make its objective inert.
15. **An external run uses the channel that the firm's own front end uses.** A test through another channel reaches the agent by a different path. The model and version of an external target are declared by the operator. Record them as declared, never as observed. Do not add a field to an adapter's record that the channel does not return.
16. **The tool emulator returns synthetic results only.** Each result is fixed in the tools file, or a built-in handler computes it by a fixed rule. A handler reads a synthetic store. No tool acts on any system. The emulator listens on the local machine. Set a key variable before a tunnel exposes it. A tools file and a store hold no real account, customer or system data. Never put a name under `.invalid` or a placeholder mark in a tool description, a store or a result. The TP-SEC-006 scorer and its static inspection count them.
17. **No probe, test or example names a real outside host.** A probe target name ends in the reserved name `.invalid` (RFC 2606). A probe target address is in a documentation block of RFC 5737. A new network, process or server site fails the static scan of the self-test. Add it to the declared list in `faias_harness/selftest/scan.py`, with its reason.

## Add a target profile

1. Copy `targets/examples/chat_only.yaml` to a new file.
2. Fill in the fields that `docs/ARCHITECTURE.md`, section 5, describes. List only the capabilities that the system really offers to the harness.
3. Use a staging endpoint. Use a name such as `staging.example.test` in examples. Put no key in the file. If the adapter needs a key, set `settings.api_key_env` to the name of an environment variable.
4. Point `attestation_file` to a statement by the firm that the target is a test or staging system, isolated from production, with synthetic data only.
5. Run the preflight on the profile. An invalid profile gives a list of problems and exit code 2. Fix them.
6. Run the sweep and the tests.

A profile for the real system of a firm holds information about that firm. Keep it outside this repository.

## Add an external target profile

1. Copy the example for the channel. The examples are `targets/examples/copilot_studio.yaml` (`directline`), `responses_agent.yaml` (`openai-responses`), `a2a_agent.yaml` (`a2a`) and `json_app.yaml` (`json-http`).
2. Set `endpoint` to the staging agent's channel. Put the adapter's settings in `adapter_options` (each adapter module's docstring lists them). Put no key in the file. Name the variable in `settings.api_key_env`.
3. Copy `targets/examples/external_settings.yaml`. Set the cap and the cost per call from the platform's prices. Keep `item_set: spike` until the channel probe and a first run show replies.
4. Point `attestation_file` to the firm's statement, run the preflight with `--settings`, then `--probes`.
5. Keep the profile and the settings outside this repository.

## Add an adapter

1. Read `faias_harness/target.py`. It defines one method group for each of the seven capabilities. Implement the groups for the capabilities that the system exposes. Leave the others to raise `NotImplementedError`.
2. Put the adapter in `faias_harness/adapters/`. Set its `capabilities` attribute to the capabilities that it really implements.
3. Send traffic only to the staging endpoints that the profile names. Take any key from the environment variable that the profile names. Do not reach into production systems.
4. Add the adapter to the table in `faias_harness/adapters/README.md`. Mark it **untested** until a test exercises it. A test needs no real vendor account: use a local stand-in server or recorded responses.
5. Write "supported" only when a test shows it, and say what the test covers. A page in `docs/PLATFORMS.md` that describes a vendor feature is not a test and is not support.
   For a channel adapter of an external run, subclass `adapters/base.py` `ChannelTarget`. Send through `_send_with_retry`. It calls `before_send`, so the call budget counts every attempt. Fill `last_exchange` only with what the channel returned. For a fault that every call hits, raise `ChannelError` with the kind `auth` or `config`. Add `validate_options` and `hosts`, and add the adapter to `adapters/registry.py`. Add a stand-in to `tests/standins.py`.
6. Run the sweep and the tests.

## Writing rules for text in this repository

- Use UK spelling. Use short sentences, active voice and plain words. Use one word for one meaning. State facts. Do not use sales language.
- Say what a part does, and say what it does not do. Keep README, `docs/ARCHITECTURE.md`, `CHANGELOG.md` and the counts in them equal to what the commands print.
- Give a source for a number, or leave the number out.
