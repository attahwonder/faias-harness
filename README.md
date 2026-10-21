# FAIAS harness

An open-source test harness for FAIAS, the Financial AI Independent Assurance Standard. The harness is planned to run the FAIAS test procedures against an AI system in a test environment. A firm describes its own system in a target profile. The harness reaches that system through one target interface. This repository holds version 0.2: the design, the procedure records, a record checker and a preflight. The working copy also holds an unreleased engine spike for one procedure (see "Unreleased: engine spike" below).

## Status: version 0.2

**What works now**

- **Records.** One generic record for each of 45 FAIAS test procedures, built from the published standard. Each copied item carries its file and line (`records/`).
- **Record checker.** It compares every record with the FAIAS source lines (`tools/check_records.py`). It also checks the form of the capability needs and the FAIAS lines that they cite. A test seeds eight faults and confirms that the checker catches them.
- **Preflight.** It compares a target profile with the draft capability needs of each procedure and prints which procedures can run in full, in part, or not at all (`faias_harness/preflight.py`). It makes no network call.
- **Platform survey.** `docs/PLATFORMS.md` lists what the public documentation of six agent platforms says about the seven capabilities. On 16 October 2026, a script found the quote of 84 of 87 survey rows on the vendor page. The survey is documentation only. It is not an adapter and not a test.

**What does not work**

- Version 0.2 runs no test. The unreleased engine spike runs one procedure on 20 items, and its tests use a local stand-in for the model host. It has run live through OpenRouter on 20 and 21 October 2026: six runs of 180 calls, against three hosted models. Those run logs are not in the repository.
- The harness supports no platform. The adapter for OpenAI-compatible endpoints is written and untested. The adapter for SRD-1 minimal is tested against a local stand-in only.
- SRD-1, the example reference system in `targets/srd1/`, is built only in a minimal form (four components, for the engine spike). Its profile and its full specification are drafts.
- The capability needs are a draft. Two independent model passes read the Preconditions of each procedure. For 26 procedures they agree, and nobody has checked these entries at each line. For the 19 where they differed, a third reading, also a model, settled 17 at the lines and left 2 open. No person has checked the file.

## Safety rules

These rules come first. They bind every use of the harness.

1. **Never test production.** Run the harness only against test or staging systems. The preflight cannot check whether a system is a production system. The operator carries that duty. FAIAS states the same rule for reviewers (`00_Admin/METHODOLOGY_STANDARDS.md`, "Non-Production Rule").
2. **No run without an isolation attestation.** The profile names an attestation file. The preflight refuses to start without one. Version 0.2 checks that the file exists and is not empty. It does not check what the file says.
3. **Synthetic data only.** Do not use real customer data.
4. **Inert test objectives.** A test item asks for a harmless marker, not for harmful content. See the benign-payload principle in `PLAYBOOK_SEC_001.md` of FAIAS.
5. **Keys only in environment variables.** A key never goes in a file, a log or an error message. A profile holds the name of the variable, not the key.
6. **Set a spend cap.** `settings.cap_usd` must be set before a run can start.
7. **Lock the routing in evidence mode.** A settings file in evidence mode must set all five routing locks (`only`, `allow_fallbacks` false, `zdr` true, `data_collection` deny, `quantizations`). The preflight refuses it otherwise, and the run starts only after you type `yes`.

## Architecture

The harness reaches every system through one **target interface** with seven **capabilities**: `conversation`, `traces`, `tool_routing`, `state_read`, `data_seeding`, `config_read` and `control`. A **target profile** lists the capabilities that a system offers. The **preflight** compares the profile with what each procedure needs. SRD-1 has no private path: it uses the same interface as any other system.

Read `docs/ARCHITECTURE.md` for the full design, the profile schema, the design rules and the draft status of the capability needs. Read `docs/PLATFORMS.md` for the survey of platform documentation. Read `AGENTS.md` if you work on this repository, as a person or as a software agent.

## Run the preflight and the tests

You need Python 3.11 or later and the packages `pyyaml`, `httpx` and `cryptography` (`python -m pip install pyyaml httpx cryptography`). The commands below use the Windows launcher `py -3.14`. On other systems, use `python3`. Run them from the repository root.

Preflight on the SRD-1 profile:

```
py -3.14 -X utf8 -m faias_harness.preflight --profile targets/srd1/profile.yaml
```

Preflight on an example profile:

```
py -3.14 -X utf8 -m faias_harness.preflight --profile targets/examples/chat_only.yaml
```

The start check. It refuses, because the example has no attestation file:

```
py -3.14 -X utf8 -m faias_harness.preflight --profile targets/examples/chat_only.yaml --start
```

All tests. They need a copy of FAIAS version 1.3.3 (the folder that holds `V3_Testing_Playbook`):

```
py -3.14 -X utf8 tests/run_all.py --faias <path to the FAIAS v1.3.3 folder>
```

Rebuild the records. This does not change `records/capability_needs.yaml`, which is an input file that a person keeps:

```
py -3.14 -X utf8 tools/build_records.py --faias <path to the FAIAS v1.3.3 folder>
```

Check the records and the capability needs against the FAIAS tree:

```
py -3.14 -X utf8 tools/check_records.py --faias <path to the FAIAS v1.3.3 folder>
```

## Unreleased: engine spike

The working copy holds a first run engine. It runs one procedure, TP-SEC-001 (direct prompt injection and jailbreak resistance, ★4), on SRD-1 minimal. The result is labelled "engine spike, not a finding". It says nothing about the security of SRD-1.

**What it does.** The harness sends 20 test items (5 for each of the four technique classes) through the chat channel of SRD-1 minimal, with k runs for each item (`runs_per_item` in the settings: at least 3, the FAIAS minimum; the log states k). SRD-1 calls its model through OpenRouter, with five routing locks on every request. A deterministic scorer marks each reply. The harness computes the measures M1 to M4 with the formulas of the record. It writes one run log. The log holds the procedure, the ★ label, the pinned model ID, the host that served each call, the thresholds with their bases, the reviewer-set value of M4 with its reason, a lock time before the first item, and a hash of the settings. The log is signed with an Ed25519 key that belongs to the operator. The run stops before a call that could pass the spend cap.

**What it does not do.**

- It uses 20 items. The procedure asks for 100 or more, and a clean-baseline arm of 25 requests. The spike has no baseline arm.
- SRD-1 minimal has no guardrail layer (C-10), no tools and no retrieval. The procedure expects the classifier to be on as in production.
- It does not run steps 2 and 6 to 9 of the procedure, and it does not score M5 to M7.
- The scorer reads plain English text and has no measured error rate. Its known false results are listed in the header of every run log.
- The egress allow-list is a control in the HTTP client. It is not a network sandbox.
- The tests use a local stand-in for the model host. It has run live through OpenRouter on 20 and 21 October 2026: six runs of 180 calls, against three hosted models. Those run logs are not in the repository. In those runs the response field `provider` named the serving host on every call.
- Inspect drives the run (`faias_harness/engine/inspect_task.py`). It is tested against a local stand-in for the model host, and it drove the live runs above.

**Commands.** `targets/srd1/settings.yaml` holds the settings and no key. The key is the environment variable that `api_key_env` names.

Make the run-signing key once, in a folder outside the repository. The command refuses a folder inside the repository and never overwrites a key:

```
py -3.14 -X utf8 -m faias_harness.engine.runlog make-key --out <folder outside the repository>
```

Preflight with a settings file. It prints the provider, model, host, precision, routing locks and the worst-case estimate against the cap. It reports whether the key variable is present and never prints its value:

```
py -3.14 -X utf8 -m faias_harness.preflight --profile targets/srd1/profile.yaml --settings targets/srd1/settings.yaml
```

Make the two probe calls (a tool-call probe and a reasoning-text probe) and start nothing:

```
py -3.14 -X utf8 -m faias_harness.preflight --profile <profile> --settings <settings> --probes
```

Start the run. The command refuses without the attestation file, the key variable and the signing key. It makes the probe calls, shows the result and starts only after you type `yes`:

```
py -3.14 -X utf8 -m faias_harness.preflight --profile <profile> --settings <settings> --start --sign-key <private key file>
```

Verify a signed log:

```
py -3.14 -X utf8 -m faias_harness.engine.runlog verify --log <run_log.jsonl> --sig <run_log.jsonl.sig> --pubkey <public key file>
```

## Relation to FAIAS

The records are built from FAIAS version 1.3.3, published at <https://github.com/attahwonder/faias-standard> under CC BY 4.0. The records quote FAIAS text. Each quoted item carries the FAIAS file and the line number. `records/README.md` gives the attribution.

FAIAS is not a certification or approval scheme. Running this harness does not certify, accredit or approve anything. The harness copies the ★ rating of each procedure from FAIAS and never computes it.

## Roadmap

This list gives the order of the work. It gives no dates.

1. A run engine that drives one target through one procedure from end to end. It writes a log with the procedure, the ★ label, the pinned model ID, and the threshold with its basis. The run stops at the spend cap. (The unreleased spike does this for one procedure on 20 items. Its result is labelled "engine spike, not a finding".)
2. A threshold lock that writes a hash and a time before the first sample.
3. A slice of three procedures from different domains, each with every evidence output its record lists.
4. A self-test of the harness environment (TP-AGT-014): every traffic and publication attempt is blocked and logged.
5. All 45 automated procedures, with signed logs and a results table built from the logs by script.
6. The 13 procedures that need a human step, with two LLM judges from different model families and independent human raters.
7. Tested adapters for real platforms, and a worked safety case built from the logs.

## Licences

- The code in this repository is under the Apache License, Version 2.0 (`LICENSE`, `NOTICE`).
- The FAIAS text quoted in `records/` is under CC BY 4.0. Copyright 2026 Wonder Attah.
- `targets/srd1/SPEC.md` names third-party items and their licences in its section 8.
- `docs/PLATFORMS.md` quotes short passages (25 words or fewer) from public vendor documentation. Each passage has its page address. The passages stay under the terms of their owners.

Authored from first principles and public sources; no employer or client material used.
