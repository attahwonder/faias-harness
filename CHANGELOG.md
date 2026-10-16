# Changelog

## 0.2.0 (16 October 2026)

Second release. It settles most of the capability needs, completes the platform survey and adds to the SRD-1 specification. It still runs no test.

- `records/capability_needs.yaml`: a third reading, also a model, settled 17 of the 19 entries where the two passes differed, at the Preconditions lines. Each settled entry keeps both sets and a note with the reason and the lines. 2 entries stay open (TP-AGT-014 and TP-TPR-006). 5 of the 26 agreed entries were spot-checked, and no error was found.
- `tools/check_records.py` checks the `settled` entries. Its seeded-fault test has 8 faults.
- The preflight counts in `docs/ARCHITECTURE.md` follow the settled needs.
- `targets/srd1/SPEC.md` (version 0.2): rows A-09 and S-03 state that SRD-1 keeps a change log and usage logs from its first version, and A-21 lists the trace and decision store. `targets/srd1/profile.yaml`: 6 rows changed, 3 of them to `met` (TP-GOV-003 access and artefacts, TP-MOD-014 artefacts).
- `docs/PLATFORMS.md` lists the two OpenTelemetry rows. The check now also reads the project's own repository for that specification, and found both quotes on 16 October 2026.
- `docs/PLATFORMS.md`: a second round asked both passes about OpenAI `control`. The check found all 8 quotes on the page, so every platform now has a verified row for each of the seven capabilities. The survey holds 84 of 87 rows.

## 0.1.0 (16 October 2026)

First release. It shows the design and the direction. It runs no test.

- The target interface with seven capabilities (`faias_harness/target.py`, `faias_harness/capabilities.py`).
- The target profile schema and its validator (`faias_harness/profile.py`).
- The preflight: it compares a profile with the draft capability needs of 45 procedures. `--start` refuses without an isolation attestation file and without the settings `provider`, `model_id` and `cap_usd`. There is no run engine.
- 45 generic procedure records built from FAIAS version 1.3.3, with `tools/build_records.py` (second run byte-identical) and `tools/check_records.py` (with a seeded-fault test of six faults).
- `records/capability_needs.yaml`: a draft input file. It holds the needs on which two independent model passes agree, the FAIAS lines they cite, and an open entry for each of the 19 procedures where the passes differ. `tools/check_records.py` checks its form and its cited lines.
- `docs/PLATFORMS.md`: a survey of the public documentation of six agent platforms. A script found the quote of 76 of 79 rows on the vendor page on 16 October 2026. It is not an adapter and not a test.
- `AGENTS.md`: a guide for people and software agents that work on the repository.
- An untested adapter for OpenAI-compatible chat-completions endpoints (`generic-http`).
- A draft profile and specification for the example reference system SRD-1. SRD-1 is not built.
- Two example profiles in `targets/examples/`.
- `tests/run_all.py`: all tests from one command.
- `tools/sweep_barred.py`: a sweep for barred terms and key-like strings.
