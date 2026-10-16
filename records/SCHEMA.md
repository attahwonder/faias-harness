# Procedure record schema

`records/SCHEMA.md` · harness v0.2. One record per procedure, `TP-<DOM>-<NNN>.yaml`. `tools/build_records.py` builds the records. `tools/check_records.py` checks them. A record is generic: it holds no precondition map for any target. The precondition map of a target is in the profile of that target.

## Source-line rule

Every item copied from FAIAS carries `src: <file>:<line>`. `<file>` is relative to the root of the FAIAS tree, and `<line>` starts at 1. The `text` of an item is the source line, with Markdown emphasis and table pipes removed. Every number in `text` appears in that line. The records are built from FAIAS v1.3.3 (CC BY 4.0). See `README.md` in this folder.

## Files in this folder

| File | Content |
|---|---|
| `TP-*.yaml` | The 45 records. Generated. Do not edit. |
| `classification.tsv` | `tp`, `class`, `human_step`, `lever` for the 45 procedures. |
| `OVERRIDES.yaml` | Threshold bases that the builder's keyword rules could not classify. Each entry has the basis, the reason and the line read. |
| `capability_needs.yaml` | The capabilities each procedure needs. An input file that a person keeps by hand; the builder does not write it. A draft. |

## Fields

| Field | Type | Content | Source |
|---|---|---|---|
| `tp` | string | Procedure ID, for example `TP-SEC-001` | Line 1 of the procedure |
| `title` | string | Procedure title | Line 1 |
| `irq` | string | IRQ ID, for example `IRQ-SEC-001` | The `**IRQ:**` line |
| `star` | string | `★1` to `★5`, copied. The harness never computes it. | The `**Confidence:**` line |
| `method_class` | string | The method class named on the Confidence line | The same line |
| `source` | string | The procedure file, relative to the FAIAS tree root | n/a |
| `class` | string | `H`: the core observations of the procedure come from running a system under test | `classification.tsv` |
| `human_step` | string | `N`: the procedure needs no human labelling or judgement of outputs. All 45 records have `N`. | `classification.tsv` |
| `lever` | string | An optional feature of the example target SRD-1 that brings the procedure into scope: `L1` case-note memory, `L2` document upload, `L7` tools served over MCP. `-` if none. | `classification.tsv` |
| `design` | list | Each sentence or bullet of Test design that holds a number: `text`, `src`, `basis` | Test design section |
| `measures` | list | Each measure: `id` (M1, M2, ...), `text`, `src` | Observations & measures section |
| `thresholds` | list | One item per measure: `measure` (its `id`), `text` (the threshold as the procedure states it), `basis`, `basis_source` (the Basis cell of a measures table, or "the measure's own text"), `src`. Also `firm_value_key` when the basis is `firm`. Also `override` (`reason`, `read`) when `OVERRIDES.yaml` set the basis. | The measure's line; overrides in `OVERRIDES.yaml` |
| `interpretation` | list | Each Pass, Fail and Partial item: `outcome`, `text`, `src` | Interpretation section |
| `evidence_outputs` | list | Each output: `text`, `level` (`L1` to `L5`, or a range such as `L2–L3`), `src` | Evidence outputs section |

## `capability_needs.yaml`

`status` starts with `draft:`. `needs` has one entry for each of the 45 procedures. An entry has these fields:

| Field | Type | Content |
|---|---|---|
| `needed` | list | Capabilities (from the seven) that the procedure needs: the set that both passes marked as needed, or, in a `settled` entry, the set that the third reading decided |
| `lines` | list | The FAIAS lines, `<file>:<line>` relative to the FAIAS tree root, that a pass or a note cites. Each line exists, in the file of the procedure |
| `open` | map, optional | Present where the needed sets of the two passes differ and nothing is decided: `pass_1` and `pass_2`, each a list of capabilities, and an optional `note`. `needed` is then what both passes need. The order of the passes has no meaning |
| `settled` | map, optional | Present where the two passes differed and a third reading decided at the lines: `pass_1`, `pass_2` and a `note` with the reason and the lines. `needed` keeps every capability that both passes need and adds only capabilities that one pass needs. An entry has `open` or `settled`, not both |

`tools/check_records.py` checks these rules.

## Basis values

For a threshold, FAIAS names three bases (`V3_Testing_Playbook/PLAYBOOK_OVERVIEW.md`, line 71):

| Value | Meaning |
|---|---|
| `cited` | The value comes from the canvas or from a named external source. `basis_source` names it. |
| `firm` | The firm's own approved value. The record holds the key `firm.<tp>.<measure>` in `firm_value_key`. The firm declares the value and its basis in the firm-values file of the target, before any run. |
| `reviewer` | "reviewer judgement — no established threshold" |
| `not stated` | The line states no basis. Either it gives no threshold (often a binary or categorical finding that the Interpretation section judges), or it gives a threshold with no basis label. This is a fact about the text. Nobody filled the gap. |

For a design number:

| Value | Meaning |
|---|---|
| `reviewer-set design minimum` | The sentence says so |
| `cited` | The sentence names the canvas or an external source for the number |
| `not stated` | The sentence gives no basis |
