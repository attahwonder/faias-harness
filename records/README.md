# Records

This folder holds one generic record for each of 45 FAIAS test procedures. The schema is in `SCHEMA.md`.

## Attribution

The records quote text from **FAIAS — Financial AI Independent Assurance Standard**, version 1.3.3, Wonder Attah, licensed under CC BY 4.0 (<https://creativecommons.org/licenses/by/4.0/>). The standard is at <https://github.com/attahwonder/faias-standard>.

Each quoted item carries `src: <file>:<line>`. The file is a path in the FAIAS tree, and the line is the line of that file from which the text comes. The builder removes Markdown marks and table pipes. It does not change the words. The code in this repository is under Apache-2.0. The quoted FAIAS text stays under CC BY 4.0.

The licence of FAIAS grants no rights in the name "FAIAS". Do not present a changed copy of these records as FAIAS or as endorsed by its author. FAIAS is not a certification or approval scheme. The README of FAIAS states this.

## What is here

| File | Content |
|---|---|
| `TP-*.yaml` | The 45 records |
| `SCHEMA.md` | Fields and rules |
| `classification.tsv` | Class, human step and lever of each procedure |
| `OVERRIDES.yaml` | Threshold bases that the builder could not classify, each with a reason and a line read |
| `capability_needs.yaml` | The capabilities each procedure needs: an input file that a person keeps by hand. A draft. The builder does not write it. |

## Rebuild and check

```
py -3.14 -X utf8 tools/build_records.py --faias <path to the FAIAS v1.3.3 tree>
py -3.14 -X utf8 tools/check_records.py --faias <path to the FAIAS v1.3.3 tree>
```

A second build gives byte-identical files. The checker compares each record with its source lines in the FAIAS tree. It also checks the form of `capability_needs.yaml` and that every FAIAS line that the file cites exists.

## Limits

- The records copy what FAIAS states. They do not add test steps. FAIAS states the steps.
- The records copy the ★ rating of each procedure. The harness never computes it.
- The 45 procedures are those whose core observations come from running a system and that need no human labelling or judgement of outputs. FAIAS has 109 procedures. This folder does not cover the others.
- The capability needs are a draft: the needs on which two independent model passes agree. Nobody has checked them at each line. See `docs/ARCHITECTURE.md`, section 9.
