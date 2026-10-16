# Architecture

Harness version 0.1. This document describes the design of the harness and what exists today. Each section says what is built and what is not.

## 1. Scope and status

The harness is planned to run the FAIAS test procedures against an AI system in a test environment. FAIAS is the Financial AI Independent Assurance Standard. Version 1.3.3 of FAIAS has 109 test procedures, one for each Independent Review Question (FAIAS `README.md`, line 31).

Version 0.1 shows the design and the direction. It has three working parts:

- the **records**: one generic record for each of 45 procedures;
- the **record checker**: it compares each record with the FAIAS source lines;
- the **preflight**: it compares a target profile with the capability needs of each procedure.

Version 0.1 has no run engine. It runs no test. It sends no traffic to any system. No adapter is tested. The example target SRD-1 is not built.

## 2. The design change: one target interface

The harness reaches every system under test through one interface, the **target interface** (`faias_harness/target.py`). No target has a private path. This holds for SRD-1 too.

```
 records + profile + preflight + (run engine: not built)
                    |
             target interface            <- seven capabilities
                    |
                 adapter                 <- generic-http: untested; others: planned
                    |
        system under test (staging only)
```

Reason: if the test tool also runs the system under test, a test can measure the controls of the tool in place of the controls of the system. A fixed interface keeps the two apart.

A firm describes its own system in a **target profile** (section 5). The profile lists the capabilities that the system offers to the harness. The preflight (section 6) then shows which procedures the harness can run in full, in part, or not at all, before anyone spends money on a run.

## 3. The seven capabilities

A target may expose any of these seven capabilities. A procedure needs the capabilities that its Preconditions imply.

| Capability | What the harness can do through it |
|---|---|
| `conversation` | Send input through the system's own channel and get the output. |
| `traces` | Read per-turn model calls, tool calls, retrievals and versions. |
| `tool_routing` | Point the system's tools at harness emulators. Read the tool ledgers. |
| `state_read` | Read back-office and decision-store state. |
| `data_seeding` | Write to the staging knowledge store, memory or ingestion routes. |
| `config_read` | Read prompts, decoding settings, versions, and guardrail, monitoring and entitlement configuration. |
| `control` | Operate the kill switch, roll back, switch a version, inject a failure. |

`faias_harness/capabilities.py` defines the seven names. `faias_harness/target.py` defines one method group for each. Every method raises `NotImplementedError` until an adapter implements it.

## 4. Records are generic

A record holds what FAIAS states for one procedure: the title, the IRQ, the ★ rating, the design numbers, the measures, the thresholds with their bases, the Pass/Fail/Partial text and the evidence outputs. Every copied item carries a file and a line in the FAIAS tree. The schema is in `records/SCHEMA.md`.

A record holds no map of any target. The precondition map of a target is in the profile of that target. A record names a firm-set threshold with a key, `firm.<tp>.<measure>`, for example `firm.TP-RES-004.M1`. The firm declares the value of the key in its firm-values file.

The ★ rating is a label. The harness copies it from the Confidence line of the procedure. It never computes it. FAIAS states: "Automation does not raise a ★ rating" (`V3_Testing_Playbook/PLAYBOOK_OVERVIEW.md`, line 134).

## 5. The target profile

One YAML file describes one target. `faias_harness/profile.py` loads and validates it. A profile never holds a key.

| Field | Content |
|---|---|
| `name` | A label for the target. |
| `adapter` | The adapter ID, for example `generic-http`. |
| `endpoint` | The URL of the staging endpoint, or `null`. It starts with `http://` or `https://`. |
| `trace_source` | Where the harness reads traces, or `null`. |
| `capabilities` | The list of capabilities the target exposes. Each is one of the seven. |
| `precondition_map` | A map from each procedure to its four precondition categories (`environment`, `access`, `artefacts`, `personnel`). Each category has a `status` (`met`, `partly`, `not met`), a list of `ids`, and an optional `note`. May be `null`. |
| `firm_values` | The path of the firm-values file, or `null`. |
| `attestation_file` | The path of the isolation attestation file, or `null`. |
| `settings` | `provider`, `model_id`, `price_in_per_m`, `price_out_per_m` and `cap_usd`. Use `null` for a value that is not set. The optional `api_key_env` names an environment variable. |
| `status` | Optional. A free-text note on the state of the target. |

Paths are relative to the repository root, unless they are absolute. The validator refuses a profile with an unknown capability, an unknown field, or a settings field that looks like a key (`api_key`, `key`, `secret`, `token`, `password`).

Example (`targets/examples/chat_only.yaml`, shortened):

```yaml
name: Example, chat only
adapter: generic-http
endpoint: https://staging.example.test/v1
trace_source: null
capabilities: [conversation]
precondition_map: null
firm_values: null
attestation_file: targets/examples/attestation.txt
settings: {provider: null, model_id: null, price_in_per_m: null, price_out_per_m: null, cap_usd: null}
```

**Isolation attestation.** The attestation is a statement by the firm. It says that the target is a test or staging system, isolated from production, and holds synthetic data only. Version 0.1 checks that the file exists and is not empty. It does not read the file for meaning. It cannot know whether the statement is true.

The repository holds one full profile: `targets/srd1/profile.yaml`, for SRD-1. SRD-1 is a planned reference system, a synthetic customer-service agent of a retail bank. `targets/srd1/SPEC.md` is its draft specification. SRD-1 is not built. Its profile is a design document.

## 6. Preflight

Command (from the repository root):

```
py -3.14 -X utf8 -m faias_harness.preflight --profile <profile.yaml> [--start]
```

The preflight makes no network call. It does this:

1. It loads the profile and checks it. An invalid profile gives a list of problems and exit code 2.
2. For each of the 45 procedures, it reads the `needed` capabilities of the procedure from `records/capability_needs.yaml` and compares them with the capabilities in the profile. It does not use the `open` entries, except to print how many there are (section 9).
3. It prints one line for each procedure: the result, the needs and the missing capabilities.
4. It prints the counts. If the profile has a precondition map, it also prints how many category rows are `met` and `partly`.
5. It prints how many firm values the profile declares, out of the number that the records need.

The result for one procedure is one of three:

| Result | Rule |
|---|---|
| `full` | Every need is in the profile. A procedure with no need is `full`. |
| `none` | The procedure needs `conversation` and the profile lacks it, or every need is missing. |
| `partly` | Any other case. |

`full` means only that the profile lists every capability that the procedure needs. It does not mean that an adapter implements them or that a test passes.

The three profiles below give these counts on the draft needs (the two example profiles are in `targets/examples/`):

| Profile | full | partly | none |
|---|---|---|---|
| SRD-1 (all seven capabilities) | 45 | 0 | 0 |
| `chat_only` (`conversation`) | 5 | 31 | 9 |
| `chat_and_traces` (`conversation`, `traces`) | 5 | 31 | 9 |

The two example profiles give the same counts. Only 4 procedures need `traces` (TP-AGT-002, TP-AGT-010, TP-DAT-008 and TP-RES-003). Each of the 4 also needs at least one capability that neither example profile offers, and both profiles offer `conversation`. These 4 procedures are therefore `partly` in both profiles, and adding `traces` moves no procedure to another result.

**`--start`.** The preflight refuses to start a run unless all of these are true:

- the attestation file exists and is not empty;
- `settings.provider` is set;
- `settings.model_id` is set;
- `settings.cap_usd` is a number above 0.

If a condition fails, the preflight prints the reasons, starts nothing and exits with code 2. If all conditions are met, version 0.1 prints "no run engine in v0.1", starts nothing and exits with code 3.

## 7. Adapters

An adapter implements the target interface for a real system. `faias_harness/adapters/README.md` lists them.

| Adapter | State |
|---|---|
| `generic-http` (`openai_chat.py`) | Written. Untested. It speaks the OpenAI-compatible chat-completions format and offers `conversation` only. It reads the key from the environment variable that the profile names. |
| Azure AI Foundry | Planned. Not built. Not tested. |
| Amazon Bedrock | Planned. Not built. Not tested. |
| In-house frameworks | Planned. Not built. Not tested. |

The harness supports no platform in version 0.1.

`docs/PLATFORMS.md` is a survey of the public vendor documentation of six agent platforms, set against the seven capabilities. Each row in it has a quote that a script found on the vendor page on 16 October 2026. The survey is not an adapter and not a test. It does not show that a platform supports the harness.

## 8. Design rules

The rules bind every phase of the work. The last column says what version 0.1 does about each rule.

| # | Rule | Basis | In v0.1 |
|---|---|---|---|
| 1 | **The target and the harness are separate.** The harness drives a target from outside, through the target interface. No target has a private path. | Section 2 | The interface exists. No target implements it fully. |
| 2 | **The ★ rating is a label.** The harness copies it from the Confidence line and never computes it. | `PLAYBOOK_OVERVIEW.md`, line 134 | The record checker compares each ★ with the Confidence line. |
| 3 | **Thresholds are locked before the first sample.** Each threshold carries its basis: a cited method, the firm's own approved value, or reviewer judgement. A lock writes a hash and a time. A run refuses to start without a lock. | FAIAS states that thresholds are "fixed and recorded before computation or observation" (`PLAYBOOK_OVERVIEW.md`, line 70) and labelled with a basis (line 71). | Records carry the bases. The lock is not built. |
| 4 | **Synthetic data only. Never production. Inert test objectives.** Network traffic goes only to the staging endpoints that the profile names. | `00_Admin/METHODOLOGY_STANDARDS.md`, line 73 (the non-production rule); `PLAYBOOK_SEC_001.md`, line 23 (the benign-payload principle) | The preflight needs an attestation file. The `generic-http` adapter contacts only the profile endpoint. Nothing enforces traffic limits yet. |
| 5 | **Every tool call is seen.** The harness sees tool calls through the trace export of the target, or through harness emulators to which the firm routes its tools. A tool call that the harness cannot see is not evidence. | Design rule of this harness | Not built. |
| 6 | **Run logs are signed with a key that belongs to the operator of the harness.** The key is never a key that signs a release of FAIAS. | Design rule of this harness | Not built. |
| 7 | **Keys are environment variables.** A key never goes in a file, a log or an error message. The operator creates the provider account and sets the key. | Design rule of this harness | The profile validator refuses a key field. `tools/sweep_barred.py` looks for key-like strings in the repository. No log exists yet. |
| 8 | **A spend cap bounds every run.** `settings.cap_usd` is required, and the run stops at the cap. | Design rule of this harness | The preflight needs `cap_usd` for `--start`. There is no engine to stop. |

## 9. Draft status of the capability needs

`records/capability_needs.yaml` is a **draft**. The file says so in its header, and its `status` field starts with "draft:". It is an input file that a person keeps by hand. No tool in this repository writes it.

**How the file was made.** Two independent model passes each read the Preconditions of the 45 procedures in the FAIAS tree. For each procedure, each pass listed the capabilities (of the seven) that the Preconditions imply. It marked each one `needed` or `optional`, and it cited the FAIAS lines. The file keeps:

| Field | Content |
|---|---|
| `needed` | The capabilities that both passes marked `needed`. The preflight uses this list. |
| `lines` | The FAIAS lines (`<file>:<line>`, relative to the FAIAS tree) that either pass cites for those capabilities. |
| `open` | Present only where the needed sets of the two passes differ. It lists the set of each pass as `pass_1` and `pass_2`. The order of the two passes has no meaning. |

**Result.** The two passes agree on 112 capability marks. For 26 of the 45 procedures they agree fully. For 19 procedures they differ, so these 19 procedures have an open entry. The 19 entries show 26 capability marks that only one pass made: 6 only in `pass_1` and 20 only in `pass_2`. A capability that a pass marked `optional` is not in the file.

**What the check covers.** `tools/check_records.py` checks that the file lists exactly the 45 procedures, that every capability is one of the seven, that `needed` equals the capabilities that both passes need wherever an `open` entry exists, and that every cited line exists in the FAIAS tree, in the file of its own procedure. It does not check that a line means what the pass says.

**TP-AGT-014.** This procedure tests an evaluation environment. Both passes mark `tool_routing` and `config_read` as needed, from the Preconditions (lines 11 and 12 of its FAIAS file). One pass also marks `conversation`, so the procedure has an open entry. One pass notes that the measures seem to need trace reading and a halt control, but that no Preconditions line grants them, so it did not list them. Nobody has checked this note.

Limits of this draft:

- Nobody has checked the needs at each line. The passes are models. A pass can misread a line, and both passes can make the same error.
- An open entry is a question, not an answer. A person must read the FAIAS lines and decide.
- Both passes noted Preconditions that fit none of the seven capabilities, for example aggregate production telemetry and a fine-tuning sandbox. The file does not record them.
- The needs say what a procedure asks of a system, in terms of the seven capability names. They do not say that any adapter or platform offers the capability.
- The preflight result depends on this draft. Check an entry before you rely on it.

## 10. What version 0.1 does not do

- It runs no test and calls no model.
- It has no run engine, no threshold lock, no log signing, no traffic control and no tool emulators.
- No adapter is tested. The harness supports no platform.
- SRD-1 is not built.
- It does not compute a ★ rating or form an opinion. Running the harness does not certify anything. FAIAS is not a certification or approval scheme (FAIAS `README.md`, section 2A).
