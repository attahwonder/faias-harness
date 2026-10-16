# AGENTS.md

A guide for any person or software agent that works on this repository. It does not depend on one tool or one vendor. Read `README.md` and `docs/ARCHITECTURE.md` as well.

## Purpose and status

This repository is the FAIAS harness, version 0.2. FAIAS is the Financial AI Independent Assurance Standard. The harness is planned to run the FAIAS test procedures against an AI system in a test environment.

Version 0.2 holds the design, 45 procedure records, a record checker and a preflight. It has no run engine and it runs no test. No adapter is tested. The harness supports no platform. Do not write text that says or implies more.

## Layout

| Path | Content |
|---|---|
| `faias_harness/` | The package: `capabilities.py` (the seven capability names), `target.py` (the target interface), `profile.py` (profile loader and validator), `preflight.py`, `adapters/` |
| `records/` | `TP-*.yaml` (45 generated records), `classification.tsv`, `OVERRIDES.yaml`, `capability_needs.yaml` (a draft input file), `SCHEMA.md`, `README.md` |
| `targets/` | `srd1/` (profile, firm values and draft specification of the example reference system) and `examples/` (two example profiles) |
| `tools/` | `build_records.py`, `check_records.py`, `test_check_records.py`, `sweep_barred.py` |
| `tests/` | `run_all.py`: all tests from one command |
| `docs/` | `ARCHITECTURE.md` (the design) and `PLATFORMS.md` (a survey of vendor documentation) |

You need Python 3.11 or later and the package `pyyaml`. The commands below use the Windows launcher `py -3.14`. On other systems, use `python3`. Run every command from the repository root. `<FAIAS>` is the path of a copy of FAIAS version 1.3.3 (the folder that holds `V3_Testing_Playbook`). The FAIAS tree is read only.

## Commands

| Task | Command |
|---|---|
| Build the records | `py -3.14 -X utf8 tools/build_records.py --faias <FAIAS>` |
| Check the records and the capability needs | `py -3.14 -X utf8 tools/check_records.py --faias <FAIAS>` |
| Test the checker with seeded faults | `py -3.14 -X utf8 tools/test_check_records.py --faias <FAIAS>` |
| Run all tests | `py -3.14 -X utf8 tests/run_all.py --faias <FAIAS>` |
| Preflight on a profile | `py -3.14 -X utf8 -m faias_harness.preflight --profile <profile.yaml>` |
| Check the start conditions | `py -3.14 -X utf8 -m faias_harness.preflight --profile <profile.yaml> --start` |
| Sweep for barred terms and keys | `py -3.14 -X utf8 tools/sweep_barred.py --faias <FAIAS>` |

`--start` never starts a run in version 0.2. It prints why a run may not start, or it prints that there is no run engine.

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

## Add a target profile

1. Copy `targets/examples/chat_only.yaml` to a new file.
2. Fill in the fields that `docs/ARCHITECTURE.md`, section 5, describes. List only the capabilities that the system really offers to the harness.
3. Use a staging endpoint. Use a name such as `staging.example.test` in examples. Put no key in the file. If the adapter needs a key, set `settings.api_key_env` to the name of an environment variable.
4. Point `attestation_file` to a statement by the firm that the target is a test or staging system, isolated from production, with synthetic data only.
5. Run the preflight on the profile. An invalid profile gives a list of problems and exit code 2. Fix them.
6. Run the sweep and the tests.

A profile for the real system of a firm holds information about that firm. Keep it outside this repository.

## Add an adapter

1. Read `faias_harness/target.py`. It defines one method group for each of the seven capabilities. Implement the groups for the capabilities that the system exposes. Leave the others to raise `NotImplementedError`.
2. Put the adapter in `faias_harness/adapters/`. Set its `capabilities` attribute to the capabilities that it really implements.
3. Send traffic only to the staging endpoints that the profile names. Take any key from the environment variable that the profile names. Do not reach into production systems.
4. Add the adapter to the table in `faias_harness/adapters/README.md`. Mark it **untested** until a test exercises it. A test needs no real vendor account: use a local stand-in server or recorded responses.
5. Write "supported" only when a test shows it, and say what the test covers. A page in `docs/PLATFORMS.md` that describes a vendor feature is not a test and is not support.
6. Run the sweep and the tests.

## Writing rules for text in this repository

- Use UK spelling. Use short sentences, active voice and plain words. Use one word for one meaning. State facts. Do not use sales language.
- Say what a part does, and say what it does not do. Keep README, `docs/ARCHITECTURE.md`, `CHANGELOG.md` and the counts in them equal to what the commands print.
- Give a source for a number, or leave the number out.
