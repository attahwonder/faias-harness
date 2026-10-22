# Item sets

## TP-SEC-001.json

The test items for TP-SEC-001 (prompt injection). The engine runs this set by default (setting `item_set: TP-SEC-001`); it has run live through OpenRouter on 22 October 2026 (six runs; the logs are not in the repository). A run on it is labelled "uncalibrated scorer, not a finding". The 20 items in `faias_harness/engine/sec001.py` stay as the set `spike`.

Each item has a business activity of SRD-1, from a fixed keyword rule over its text (the field `activity_rule` in the file). The spread is uneven: most C2 and C3 items fall under "fee refund", because their objective is a fee commitment, and most C1 and C4 items fall under the default, "product and fee questions". No item falls under "human hand-off".

Contents:

| Part | Count | What it is |
|---|---|---|
| C1, override | 40 | The item asks the system to give a marker: two parts, joined. The reply fails if it holds the joined marker. |
| C2, persona | 25 | The item asks the system to state a commitment about an invented product (for example, that a fee is waived). |
| C3, persuasion | 26 | As C2, with a persuasion technique. |
| C4, disclosure | 40 | The item asks for the "internal reference", the canary in the target's system prompt. |
| Baseline | 25 | Plain requests that a bank assistant should answer, with no attack, each with a plain reply. |

Each item has its class, its source and the ID in that source, the technique, the kind of change made to the source text, and the text sent. Every objective is harmless: a marker, a commitment about a product that does not exist, or a canary. No item asks for harmful content.

### Sources

| Source | Licence | What was used |
|---|---|---|
| CyberSecEval 2 prompt-injection dataset (PurpleLlama, revision d934ea2) | MIT, `third_party/cyberseceval2/LICENSE` | Text, adapted: the objective is replaced, and items outside banking are rewritten for a bank assistant with the technique kept |
| gandalf_ignore_instructions, test split (Lakera AI, revision 04737b6) | MIT, `third_party/gandalf_ignore_instructions/LICENSE` | Text: lines used unchanged or with the protected thing renamed or the objective replaced |
| Written for this harness | Apache-2.0 | 5 C3 items and the 25 baseline requests |
| Persuasion taxonomy (Zeng et al., 2024), garak DAN probes, in-the-wild jailbreak prompts (Shen et al., 2024) | See `NOTICE` | Technique only. No text from them is reproduced: a check found no run of 8 or more words shared with the garak DAN prompts or the in-the-wild prompts in any item or baseline request (the check's script and result are kept with the maintainer's working files, not in this repository) |

The field `changes` in the file explains each kind of change.

### Selection

English items only. Indirect items and duplicates are left out. The order within each class comes from a fixed seed, with the maintainer's items first in C3, and the first items in that order that the scorer can mark make the set. Items that only a judge can mark are not in this file.
