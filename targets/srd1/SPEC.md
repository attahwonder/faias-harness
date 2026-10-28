# SRD-1 — specification

`targets/srd1/SPEC.md` · specification of a reference system, draft, version 0.2. **SRD-1 is planned. Only a minimal form is built, in `app/`**. It has C-01, C-02, C-04 and C-15 and, since 0.6, parts of C-07, C-08, C-09 and C-18. This file lists its components, data, documents and roles. `profile.yaml` in this folder maps the preconditions of the 45 procedures to the IDs below. The "worked case" is a planned safety case for SRD-1, built with FAIAS. It does not exist yet. Items marked "(assumption, to be validated)" are choices that nobody has confirmed.

Since 0.6.0, the minimal form also serves TP-SEC-006, with the marked placeholder of C-14 in its system prompt.

Since 0.6, the minimal form has one tool of C-08, the fee refund. It is off by default (`tools_enabled` in the settings). Parts of four components are built for it. C-07 (`app/entitlements.py`) checks the customer's entitlement before each tool call. C-08 (`app/mcp_client.py`, `tools.yaml`) has one tool with the MCP fields and the four annotations. Its tool server is the harness's MCP tool emulator, not a local SRD-1 server. C-09 (`app/gate.py`) applies the per-refund cap and the per-session limit in the orchestrator. C-18 is the kill-switch route of `app/server.py`. Part of S-01 is built in `data/synthetic_bank.yaml`, with 4 customers in two domains, 6 accounts and 10 fees. Part of A-03 is built in `risk_appetite.yaml`. Its fee-refund values are null until the owner declares them. With null values, the tool is not enabled. C-11 (the approval queue) is not built, so a held refund is recorded and nothing more.

## 1. What SRD-1 is

A synthetic retail-bank customer-service agent. A customer chats with it. It answers product, fee and policy questions from a document store, reads the customer's account summary, raises a complaint ticket, updates contact details, refunds a fee up to a cap, and hands the conversation to a human. It keeps case notes between sessions and reads documents that the customer uploads. All data is synthetic. It runs only in a test environment, and the only outside call is to the model host.

## 2. Principles

1. **SRD-1 is built as a reasonable firm would build it.** No weakness is planted on purpose. A weakness the tests find is a finding, and it is reported as one (assumption, to be validated).
2. **One logging configuration.** SRD-1 has no separate verbose test logging; the configuration under test is the one it "runs in production" with (TP-AGT-011).
3. **Thresholds are SRD-1's own declared values** (A-03), fixed before any test run. They are "the firm's own approved value" in the sense of `PLAYBOOK_OVERVIEW.md`:71.
4. **No production history exists.** Every limb that needs production data, telemetry over time or historical records is reported as not performed, with that reason.
5. **Stated limit.** The same person directs the build and the review. The worked case states this in its basis-of-review statement.
6. **Written for publication.** Every SRD-1 document is written as if it will be published. Only material under MIT, Apache-2.0, CC BY 4.0, the OASIS notice or OGL v3.0 is adapted into it, with attribution. Other sources give structure only, and no text is copied from them. No real firm's name, brand or wording enters SRD-1.

## 3. Components (C)

| ID | Component | What it does | Needed by (examples) |
|---|---|---|---|
| C-01 | Customer channel | Chat API with multi-turn sessions, session-length and context limits; renders the agent's replies (Markdown to HTML) | SEC-001, SEC-002, CON-003, SEC-014 |
| C-02 | Agent orchestrator | The agent loop: model calls, tool choice, workflow steps with mandatory-step gates, an instruction channel for mid-task changes, re-anchoring checkpoints, retry and idempotency rules | AGT-001, AGT-002, AGT-010, RES-002 |
| C-03 | Decision-extraction layer | Parses the agent's structured decisions (refund yes or no and amount, escalate, evidence state, stated rationale) and validates them against a schema | MOD-005, MOD-009, MOD-014 |
| C-04 | Model gateway | Calls the hosted model by a pinned model ID with pinned decoding settings; records the model ID and version on every call; can switch to a second model version or host; handles rate limits | MOD-002, MOD-014, RES-002, TPR-004 |
| C-05 | Retrieval stack | Document store with a pinned embedding model (a second one available), chunking, retriever with set top-k, metadata (owner, review date, currency, version), a precedence rule, retrieval-state logs, a re-index trigger, an export function; a duplicate check at ingestion on content hash and version (a match is merged or rejected, and logged); reconciliation against the source repository on a set cadence and after each bulk import, with each divergence recorded and assigned to the document owner | DAT-007, DAT-008, DAT-012, SEC-003, TPR-006 |
| C-06 | Ingestion pipeline | Two routes into the document store, curated and bulk import, each with source vetting and anomaly checks; each route has a configured write permission and a named submitting role, separate from retrieval read access; a submission without permission is rejected and logged; each accepted document records its submitting role and route in its provenance metadata | DAT-006, SEC-005 |
| C-07 | Entitlement service | Two content domains (retail and wealth) and per-customer data; filters applied before retrieval and on every tool call; logs each denial | DAT-009, AGT-004 |
| C-08 | Tool servers (MCP, local) | Account summary (read), complaint ticket (write), contact-detail update (write), fee refund (write), hand-off (write); stdio transport; synthetic back-office stores with transaction ledgers; an emulation mode that can return crafted responses; a tool-registration step with change detection on tool definitions; each tool defined with the MCP fields (name, description, input and output schema) and the four MCP annotations (read-only, destructive, idempotent, open-world). The annotations are hints only, so C-07 and C-09 enforce | AGT-003, AGT-005, RES-003, SEC-010 |
| C-09 | Refund gate | In the orchestration layer, not the prompt: per-refund cap and per-session cumulative limit from A-03; refunds above the cap go to C-11 for approval; the irreversible-action list | AGT-004, AGT-012 |
| C-10 | Guardrails | An input check for injection patterns and an AI output classifier that gates what reaches the customer, each with configured thresholds; candidate library NeMo Guardrails (Apache-2.0), chosen at build | SEC-001, SEC-013, CON-009 |
| C-11 | Hand-off and approval queue | Routes to a human: vulnerability pathway, out-of-scope, evidence gaps, refunds above the cap; records each routing; the human side is emulated and recorded; a routing reason for a decision that rests materially on carried-forward case-note memory (C-12) not yet resolved to a source-system fact, with the memory entry and its provenance tag attached | CON-003, MOD-001, MOD-005, AGT-008 |
| C-12 | Case-note memory (L1) | Notes written and read back across sessions, with provenance tags (AI-written or system fact); derived embeddings, a cache and a backup; retention and erasure functions | AGT-008, DAT-010 |
| C-13 | Document upload (L2) | Customer uploads a document (PDF or image, with an OCR route) as evidence for a complaint or refund; the agent reads it; it never enters the document store | SEC-004 |
| C-14 | Agent identity and secrets | The agent's own credential per tool server, scoped, with a lifetime and a revocation function; secrets from environment variables; a marked synthetic placeholder secret for extraction tests | SEC-006, SEC-011 |
| C-15 | Trace and decision store | Every prompt, retrieval, tool call, output, model ID, prompt version and configuration version; retention settings; a hash chain for tamper evidence; a decision store that can be queried by model version, configuration, prompt version and date | AGT-011, GOV-003, GOV-010, GOV-013, SEC-008 |
| C-16 | Monitor and alerts | Metrics: input-topic mix, harmful-output rate, refusal rate, grounding failures, containment rate, alert counts; thresholds with trend logic; alerts to a test destination only; outcome metrics by customer subgroup (S-01 signals): refund grant rate and escalation rate, with the labels joined only in the monitoring calculation and never read by the agent | DAT-011, MOD-010, RES-006, CON-001, CON-002 |
| C-17 | Configuration and change control | Prompts, decoding settings, model pin, index build and memory schema in version control; a regression suite that gates each change; a rollback procedure per component | AGT-009, MOD-013, RES-004 |
| C-18 | Kill switch | An out-of-band stop for the agent and its in-flight tool calls, with a stated time-to-stop objective | AGT-012, RES-003 |
| C-19 | Degraded mode | Template replies and hand-off when retrieval, the model or a tool fails or is rate-limited | RES-002 |
| C-20 | Containment KPI (L6) | The containment rate (share of conversations closed without hand-off), stated as the target in the design and the system prompt, and tracked by C-16. The target pulls against FCA FG21/1 §4.8, which warns that automated helplines without a clear exit are hard for some customers to use; A-17 states this tension, and the worked case tests it | MOD-007 |

Harness-side items (built with the harness, not part of SRD-1):

| ID | Item | Needed by |
|---|---|---|
| H-01 | Harness sandbox: egress allow-list (the model host only), emulated publication paths, monitoring and alerts | AGT-014, every procedure's isolation attestation |
| H-02 | Task-scope statement template for each run (in-scope and out-of-scope, stated positively) | AGT-014 |
| H-03 | Test corpora built per procedure and destroyed at teardown: injection, extraction, poisoning, markers, canaries, drift series, defect signatures. Injection items adapted from AgentDojo banking, AgentThreatBench and LLMail-Inject (all MIT) and rewritten for SRD-1's tools, among them a large refund split into amounts below the cap | SEC-001 to SEC-014, DAT-006, GOV-013 |
| H-04 | Perturbation and repetition runner (one factor at a time; pinned repeats; pass^k over repeated trials, as in τ-Banking) | MOD-009, MOD-014, DAT-008 |
| H-05 | Scorers: two LLM judges from different model families, and independent human raters. A task with a set end state is graded on the back-office store, as in τ-Banking, with no judge; utility and security are scored apart, as in AgentThreatBench | the 13 human-step procedures, MOD-015 |

## 4. Synthetic data (S)

| ID | Data | Notes |
|---|---|---|
| S-01 | Customers and accounts | Two entitlement domains; tenure and value bands; subgroup signals for fairness tests, with labels held in a separate store |
| S-02 | Product, fee and policy documents | Terms, tariff of charges, rights information (the ground truth); superseded and current, draft and final, and regional pairs; complaint letters and emails. Procedures held as numbered steps with conditions, as in τ-Banking. The tariff uses the fee fields of the Open Banking PCA specification v2.4.0 (MIT), with synthetic values. Size at least τ-Banking's 698 documents, a public synthetic bank store (assumption, to be validated); no ratio to a real bank's store is available, so it stays a stated limit (TP-SEC-005) |
| S-03 | Task distribution | Ordinary queries, seeded from Banking77 (CC BY 4.0, with attribution); core, adjacent and out-of-scope strata; complaint, refund and contact-change journeys; SRD-1 serves this traffic from its first version, and the one logging configuration (C-15) records each event with its task type (the S-03 stratum), the synthetic user identity and the timestamp. These usage logs are production-representative, not production |
| S-04 | Session histories | At least 3 tenants × 5 users × 20 sessions, with traceable markers (TP-DAT-010) |
| S-05 | Labelled vulnerable-customer transcripts | Mapped to the four FG21/1 drivers; a second labeller |
| S-06 | Input time series | For drift and decay injection |
| S-07 | Refund-eligibility cases | 2 to 8 active constraints each, with per-constraint ground truth (TP-MOD-003) |

## 5. Written artefacts (A) — SRD-1's own documents

| ID | Artefact |
|---|---|
| A-01 | System description: architecture with every failure point, data-flow map, logging design, downstream output boundaries and their encoding; a system-card part (intended uses, conditions for reliable operation, limitations, evaluations, safety components; layout from the Copilot Studio application card, no text copied); a reliance statement: for the customer reply (C-01), the decision record (C-03, C-15) and the complaint ticket (C-08), whether the model's stated rationale is relied on, and the caveat that applies |
| A-02 | Approval paper: operating envelope (designed-for and not-designed-for), use-case scope, conditions of use; restricted uses, known limitations, and the impact of failure and of misuse (structure from the Responsible AI Impact Assessment Template, no text copied) |
| A-03 | Risk-appetite file: every threshold SRD-1 declares (refund cap and session limit, hallucination, harmful-output, flip-rate and calibration tolerances, drift and decay alert thresholds, time-to-halt and time-to-alert, RTO and RPO, the containment target, fairness metrics and tolerance, judge-agreement threshold), each with its basis and the claim of the worked case that it bounds (pattern from "A sketch of an AI control safety case"). No public bank risk appetite with thresholds was found, so each value is set from first principles |
| A-04 | Workflow process maps with mandatory steps and how each is enforced; consequential and irreversible action lists |
| A-05 | Tool inventory with business justifications, schemas, permission set and a recertification record; one row per tool with the MCP fields and annotations of C-08; tool-server inventory |
| A-06 | Entitlement model |
| A-07 | Memory design: what is written, provenance, validation before reuse, retention and erasure procedure; escalation to C-11 where a decision rests materially on carried-forward memory |
| A-08 | Document-store procedure: metadata, precedence rule, ingestion routes, re-index policy, embedding pin; deduplication rule and reconciliation schedule |
| A-09 | Change control: process for prompts, decoding, model and index changes; regression suite and its promotion rule; rollback per component with RTO and RPO; it runs from SRD-1's first version. Its change log, held with C-17, has one entry per prompt, decoding, model pin, index or task-scoping change (version, date, reason, approver, regression result), and holds corpus change history, corpus volume and each re-index event. Build-phase changes are its first entries. No change is made only to fill the log |
| A-10 | Monitoring design and response playbooks: security playbooks (prompt injection, memory injection, retrieval poisoning) adapted from the CoSAI AI Incident Response Framework V1.0 under its OASIS notice; conduct playbooks (a wrong fee answer, a missed vulnerability) written new |
| A-11 | Containment design: kill switch, time-to-stop objective, compensating actions per tool |
| A-12 | Degraded-mode specification and reply templates |
| A-13 | Guardrail design: input check, output classifier, thresholds; the harm taxonomy applied (AILuminate v1.0 hazard categories, per TP-CON-009) and its tolerance |
| A-14 | Vulnerability policy mapped to the FG21/1 drivers, with the adapted pathways |
| A-15 | Model selection and validation record: model card and the benchmarks it cites, host terms, the statement that SRD-1 is not fine-tuned, the evaluation-environment record; an inventory of the decisions and reports that rest on the evaluation evidence, with the respects in which the evaluation environment differs from deployment; A-02 cites it |
| A-16 | Identity and credential design |
| A-17 | KPI definition (containment rate) and the business-intent statement it serves; the tension with FG21/1 §4.8 (C-20) and how the exit to a human stays open |
| A-18 | Supported languages: English only (assumption, to be validated) |
| A-19 | Fairness: outcome measures (refund grant rate, escalation rate), metric choice and its reasons |
| A-20 | Decision-store schema, query function, retention schedule, and the owner of cohort remediation; the trigger for proactive cohort remediation: a defect signature from monitoring (C-16) or review starts a decision-store query and remediation by the cohort-remediation owner, with no inbound complaint needed; each activation is recorded with its cohort count |
| A-21 | Data inventory: one row per store (case-note memory with its derived embeddings, cache, search index and backup; the conversation store; the document store; the trace and decision store (C-15), which holds the usage logs), each with its owning role, the data held, its lawful basis, its retention rule and its erasure route |

## 6. Roles (R)

| ID | Role in the procedures |
|---|---|
| R-01 | Reviewer (lead) |
| R-02 | Domain SME (scenarios, ground truth, plausibility bounds) |
| R-03 | Platform, agent, RAG, IAM and SRE engineer |
| R-04 | System and business owner of SRD-1 (tool justifications, thresholds, KPI intent, kill-switch authority) |
| R-05 | Model scorers |
| R-06 | Human raters and independent second reviewers |
| R-07 | Control functions: data protection (DPO or privacy lead), vendor management, legal and procurement, information-security approval, change control |

The roles are filled when SRD-1 is built. R-05 is two LLM judges from different model families. R-06 is independent human raters. R-07 is a set of declared control-function roles. A role whose value is independence or a specialist skill is not declared here.

## 7. The operating envelope (draft for A-02)

- **Designed for:** retail current-account and savings customers; questions on products, fees, terms and rights; complaints intake; contact-detail changes; fee refunds up to the cap; hand-off to a human.
- **Not designed for:** advice or product recommendation; credit, lending or affordability decisions; payments or transfers; wealth-domain servicing; anything requiring identity verification beyond the session.

## 8. Prior art used

Each item borrowed, its licence and the IDs it changed.

| Item | Owner | Licence | IDs changed |
|---|---|---|---|
| τ-Banking (τ³-bench `banking_knowledge`) | Sierra Research | MIT; paper CC BY 4.0 | S-02, H-04, H-05 |
| AgentDojo banking suite | ETH Zurich SPY Lab | MIT | H-03 |
| AgentThreatBench | UK AISI (`inspect_evals`) | MIT | H-03, H-05 |
| LLMail-Inject | Microsoft | MIT | H-03 |
| Banking77 | PolyAI | CC BY 4.0 | S-03 |
| Personal Current Account specification v2.4.0 | Open Banking | MIT | S-02 |
| NeMo Guardrails (candidate) | NVIDIA | Apache-2.0 | C-10 |
| A sketch of an AI control safety case | UK AISI and Redwood Research authors | CC BY 4.0 | A-03 |
| Responsible AI Impact Assessment Template (structure only) | Microsoft | Internal reference use; no text used | A-02 |
| Copilot Studio application card (structure only) | Microsoft | Learn terms; no text used | A-01 |
| MCP specification: tools and annotations | Model Context Protocol (LF Projects) | Apache-2.0 | C-08, A-05 |
| AI Incident Response Framework V1.0 | CoSAI (OASIS Open Project) | OASIS notice | A-10 |
| AI Governance Framework v2 | FINOS | CC BY 4.0 | §9 |
| FG21/1, fair treatment of vulnerable customers (cited, not copied) | FCA | All rights reserved | C-20, A-17 |

## 9. Cross-reference to the FINOS AI Governance Framework v2

Source: FINOS AI Governance Framework v2, https://air-governance-framework.finos.org/, licensed CC BY 4.0. IDs and titles are taken unchanged from each item's front matter in github.com/finos/ai-governance-framework, read 14 Oct 2026. The mapping to SRD-1 is new: it is the judgement of the SRD-1 authors, made from the titles, not a FINOS statement. FINOS maps its items to ISO 42001, NIST and the EU AI Act, not to UK rules.

| SRD-1 | FINOS risk | FINOS mitigation |
|---|---|---|
| C-04 | AIR-OP-005 Foundation Model Versioning; AIR-RC-001 Information Leaked To Hosted Model | AIR-PREV-010 AI Model Version Pinning |
| C-05, C-06 | AIR-SEC-009 Data Poisoning | AIR-PREV-002 Data Filtering From External Knowledge Bases |
| C-07 | — | AIR-PREV-012 Role-Based Access Control for AI Data; AIR-DET-016 Preserving Source Data Access Controls in AI Systems |
| C-08 | AIR-SEC-025 Tool Chain Manipulation and Injection; AIR-SEC-026 MCP Server Supply Chain Compromise | AIR-PREV-019 Tool Chain Validation and Sanitization; AIR-PREV-020 MCP Server Security Governance |
| C-09, C-11 | AIR-SEC-024 Agent Action Authorization Bypass | AIR-PREV-024 Human-in-the-Loop Action Approval Gate (FINOS status: Draft); AIR-PREV-018 Agent Authority Least Privilege Framework |
| C-10 | AIR-SEC-010 Prompt Injection | AIR-PREV-003 User/App/Model Firewalling/Filtering; AIR-PREV-017 AI Firewall Implementation and Management |
| C-12 | AIR-SEC-027 Agent State Persistence Poisoning | — |
| C-14 | AIR-SEC-029 Agent-Mediated Credential Discovery and Harvesting | AIR-PREV-023 Agentic System Credential Protection Framework |
| C-15 | — | AIR-DET-021 Agent Decision Audit and Explainability; AIR-DET-013 Providing Citations and Source Traceability for AI-Generated Information |
| C-16 | — | AIR-DET-004 AI System Observability; AIR-DET-009 AI System Alerting and Denial of Wallet (DoW) / Spend Monitoring |
| C-17 | — | AIR-PREV-005 System Acceptance Testing |
| H-05 | — | AIR-DET-015 Using Large Language Models for Automated Evaluation (LLM-as-a-Judge) |
