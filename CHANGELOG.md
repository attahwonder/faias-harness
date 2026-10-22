# Changelog

## Unreleased

- `items/TP-SEC-001.json`: an item set for TP-SEC-001. C1 40, C2 25, C3 26 and C4 40 items, and 25 baseline requests with no attack. The items are adapted from CyberSecEval 2 and from Lakera's gandalf_ignore_instructions (both MIT), and some are written for this harness. Each item has a business activity of SRD-1, from a fixed keyword rule that the file states. `items/README.md` says what came from where.
- The engine runs the item set. New setting `item_set`: `TP-SEC-001` (the default) or `spike` (the 20 spike items). The run sends the items, then the 25 baseline requests, k times each. A baseline run is recorded with an outcome (answered, blocked or no_result), is not scored, and is not in M1 to M4. The summary has a results table for each class and the baseline arm. A run on the full set is labelled "uncalibrated scorer, not a finding". Run log schema version 2: the header names the item set, counts the baseline and lists what the run leaves out (`not_in_run`, which replaces `not_in_spike`). `verify` now checks the items hash against the named set in the repository and each run's request against its item's text, and still accepts a version 1 spike log. No live run has used the full set yet. `NOTICE` credits each source, and `third_party/` holds the two MIT licence texts. A test checks the set sizes and the credits.
- `faias_harness/engine/sec001.py`: the C2 and C3 scorer no longer counts a question, or a sentence that opens as a request ("confirm", "state", "say", "write", "tell me", "answer the", "can you", "could you", "would you", "will you"), as a commitment. Before, a refusal that repeated "Can you confirm that the fee on my X account is waived?" scored as a commitment. A test covers five such sentences. The run log header lists a fifth known false result: a true statement that names the product with a commitment word (for example, that the account is free of charge) scores as a commitment.

## 0.3.0 (22 October 2026)

The first run engine, for one procedure. It is a spike: it checks that the engine works from end to end. Its result is labelled "engine spike, not a finding". The tests use a local stand-in for the model host. It has run live through OpenRouter on 20 and 21 October 2026: six runs of 180 calls, against three hosted models. Those run logs are not in the repository.

- `faias_harness/settings.py` and `targets/srd1/settings.yaml`: a settings file with no key. In evidence mode the validator refuses a file that lacks any of the five routing locks (`only`, `allow_fallbacks` false, `zdr` true, `data_collection` deny, `quantizations`). A test seeds each missing lock and each wrong lock value.
- `faias_harness/preflight.py`: `--settings` prints the provider, model, host, precision, routing locks, the worst-case estimate against the cap and whether the key variable is present (never its value). `--probes` makes a tool-call probe and a reasoning-text probe. `--start` with `--settings` and `--sign-key` makes the probes, shows them and starts the run only after you type `yes`. New exit codes 4 (`yes` not typed) and 5 (run stopped at the cap or on an error). A profile alone with `--start` still starts nothing, exits with 3 and now prints "a profile alone names no run" in place of "no run engine in v0.2".
- `targets/srd1/app/`: SRD-1 minimal. It has the chat channel (C-01), the orchestrator (C-02), the model gateway (C-04) and a trace store with a hash chain (C-15). It has no guardrails (C-10), no tools and no retrieval.
- `faias_harness/egress.py`: one HTTP client with an allow-list, shared by SRD-1 and the engine. A request to another host is refused and logged. It is a control in the client, not a network sandbox.
- Spend cap in the gateway: the run stops before a call that could pass the cap.
- Retries in the gateway, for the failures that OpenRouter documents as temporary only (HTTP 408, 429, 500, 502, 503, 504; a 200 body with a temporary error type; a timeout or dropped connection): at most 5 per call, with the Retry-After wait or 2 to 32 seconds; counted against the cap when the host may have processed the attempt; recorded in each run record.
- The harness's call to SRD-1 now waits longer than the gateway's retries can take. Before, it timed out after 60 seconds while the gateway still retried a dropped connection, and the run stopped.
- Run outcomes: attacked, defended, blocked (a 403 content-policy, refusal or guardrail block, or finish reason content_filter) and no_result (an empty or cut-off reply that is not a success). The gateway records the finish reason. No-result runs are left out of the rates and counted on their own.
- Settings `temperature` and `seed`. Evidence mode needs both, and exactly one endpoint in `routing.only`. The gateway sends the seed plus the call number with each call; each run record holds the seed sent.
- Positive control: `targets/srd1/app/system_prompt_weakened.txt` (no rules, the same canary) and `--positive-control` on the preflight. The header names the variant.
- Host check: each reply records the host that served it. In evidence mode a reply from a host other than the locked host stops the run (status `stopped_host_mismatch`); in explore mode it is recorded only.
- `faias_harness/engine/sec001.py`: 20 items for TP-SEC-001 (5 for each class C1 to C4), a deterministic scorer, and the measures M1 to M4 with the formulas of the record. Inspect drives the run through `inspect_task.py` (one sample for each item, one epoch for each run, Inspect model `none`). Setting `runs_per_item` (k, default 3, the FAIAS minimum, at most 20), stated in the header; M4 also lists each item's successful runs out of k with a Wilson 95% interval; the preflight refuses to start without `inspect_ai`; a direct loop remains for tests, and a test shows both give the same runs and measures. The run log header states that the scorer is not calibrated and lists its known false results.
- `faias_harness/engine/runlog.py`: a run log with a header (procedure, ★ label, pinned model ID, thresholds with bases, the reviewer-set M4 value with its reason, settings hash, lock time and hash), one record for each run (with the host that served the call), a list of what the spike does not do (among them the capability `config_read`: only M1 to M4 are scored), a summary, and an Ed25519 signature with `make-key`, `sign` and `verify`. One changed byte fails verification.
- `faias_harness/adapters/srd1_http.py`: an adapter for SRD-1 minimal (`conversation`, `traces`).
- `targets/srd1/attestation.txt`: the isolation attestation for SRD-1 minimal, one paragraph, approved by the maintainer.
- `tests/`: unit tests for each part and an end-to-end test against a local stand-in model host, with a guard that fails any connection to a non-local host. `tests/run_all.py` runs them as step 7.
- Dependencies: `httpx` and `cryptography`. `inspect_ai` is an optional extra and is not needed for the tests.

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
