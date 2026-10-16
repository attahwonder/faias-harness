# Changelog

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
