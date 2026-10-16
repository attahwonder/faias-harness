# FAIAS harness

An open-source test harness for FAIAS, the Financial AI Independent Assurance Standard. The harness is planned to run the FAIAS test procedures against an AI system in a test environment. A firm describes its own system in a target profile. The harness reaches that system through one target interface. This repository holds version 0.1: the design, the procedure records, a record checker and a preflight. It does not run tests.

## Status: version 0.1

**What works now**

- **Records.** One generic record for each of 45 FAIAS test procedures, built from the published standard. Each copied item carries its file and line (`records/`).
- **Record checker.** It compares every record with the FAIAS source lines (`tools/check_records.py`). It also checks the form of the capability needs and the FAIAS lines that they cite. A test seeds six faults and confirms that the checker catches them.
- **Preflight.** It compares a target profile with the draft capability needs of each procedure and prints which procedures can run in full, in part, or not at all (`faias_harness/preflight.py`). It makes no network call.
- **Platform survey.** `docs/PLATFORMS.md` lists what the public documentation of six agent platforms says about the seven capabilities. On 16 October 2026, a script found the quote of 76 of 79 survey rows on the vendor page. The survey is documentation only. It is not an adapter and not a test.

**What does not work**

- The harness runs no test. It has no run engine.
- No adapter is tested. One adapter is written and untested. The harness supports no platform.
- SRD-1, the example reference system in `targets/srd1/`, is not built. Its profile and specification are drafts.
- The capability needs are a draft. They hold the needs on which two independent model passes agree. Nobody has checked them at each line. For 19 of the 45 procedures the two passes differ, and the file shows both sets as an open entry.

## Safety rules

These rules come first. They bind every use of the harness.

1. **Never test production.** Run the harness only against test or staging systems. The preflight cannot check whether a system is a production system. The operator carries that duty. FAIAS states the same rule for reviewers (`00_Admin/METHODOLOGY_STANDARDS.md`, "Non-Production Rule").
2. **No run without an isolation attestation.** The profile names an attestation file. The preflight refuses to start without one. Version 0.1 checks that the file exists and is not empty. It does not check what the file says.
3. **Synthetic data only.** Do not use real customer data.
4. **Inert test objectives.** A test item asks for a harmless marker, not for harmful content. See the benign-payload principle in `PLAYBOOK_SEC_001.md` of FAIAS.
5. **Keys only in environment variables.** A key never goes in a file, a log or an error message. A profile holds the name of the variable, not the key.
6. **Set a spend cap.** `settings.cap_usd` must be set before a run can start.

## Architecture

The harness reaches every system through one **target interface** with seven **capabilities**: `conversation`, `traces`, `tool_routing`, `state_read`, `data_seeding`, `config_read` and `control`. A **target profile** lists the capabilities that a system offers. The **preflight** compares the profile with what each procedure needs. SRD-1 has no private path: it uses the same interface as any other system.

Read `docs/ARCHITECTURE.md` for the full design, the profile schema, the design rules and the draft status of the capability needs. Read `docs/PLATFORMS.md` for the survey of platform documentation. Read `AGENTS.md` if you work on this repository, as a person or as a software agent.

## Run the preflight and the tests

You need Python 3.11 or later and the package `pyyaml` (`python -m pip install pyyaml`). The commands below use the Windows launcher `py -3.14`. On other systems, use `python3`. Run them from the repository root.

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

## Relation to FAIAS

The records are built from FAIAS version 1.3.3, published at <https://github.com/attahwonder/faias-standard> under CC BY 4.0. The records quote FAIAS text. Each quoted item carries the FAIAS file and the line number. `records/README.md` gives the attribution.

FAIAS is not a certification or approval scheme. Running this harness does not certify, accredit or approve anything. The harness copies the ★ rating of each procedure from FAIAS and never computes it.

## Roadmap

This list gives the order of the work. It gives no dates.

1. A run engine that drives one target through one procedure from end to end. It writes a log with the procedure, the ★ label, the pinned model ID, and the threshold with its basis. The run stops at the spend cap.
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
