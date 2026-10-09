# Decisions against the answer key

25 documents; the key has 193 certain decisions, 0 uncertain ones (not scored) and 26 certainly rejected candidates, judged from the runs haiku-1, opus-1, sonnet-1, sonnet-2, stored.

Recall: key decisions found. Precision: found / (found + run decisions the key rejects); it counts only run decisions in the key's pool, and the others are Unjudged. Over-split: extra run decisions on a key decision already found, per found decision. Fields: share of found decisions with the key's value, among those whose value the judges agreed on; handling often needs the rule's history, so the three fields without it are given too. Doc avg: averaged over documents. Stability: for runs of the same configuration (model, prompt and effort), the share of their decisions matched one to one between them, per document and pooled.

Not candidate runs: migrated, opus-v3-1, opus-v3-2. The judges never saw their decisions, so those no candidate run had are unjudged, and their precision covers only the rest.

| Run | Recall | Recall (doc avg) | Precision | Precision (doc avg) | Over-split | Kategori | Udfald | Handling | Niveau | All four | Three (no handling) | Found | False | Extra | Ignored | Unjudged | Stability |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| stored | 79.8% | 84.9% | 93.9% | 91.1% | 1.3% | 77.9% | 99.4% | 83.7% | 97.4% | 63.8% | 75.2% | 154 | 10 | 2 | 0 | 0 | – |
| migrated | 90.2% | 91.4% | 98.3% | 94.0% | 2.3% | 94.3% | 100.0% | 91.4% | 99.4% | 85.6% | 94.3% | 174 | 3 | 4 | 0 | 2 | 94.8% |
| opus-v3-1 | 92.2% | 94.3% | 97.3% | 92.1% | 1.7% | 89.3% | 100.0% | 94.4% | 98.9% | 83.1% | 88.7% | 178 | 5 | 3 | 0 | 2 | 94.8% |
| opus-v3-2 | 91.7% | 91.9% | 98.3% | 93.3% | 1.7% | 96.6% | 100.0% | 94.3% | 98.3% | 89.7% | 95.5% | 177 | 3 | 3 | 0 | 2 | 94.8% |
| median (min–max) | 90.9% (79.8%–92.2%) | 91.6% (84.9%–94.3%) | 97.8% (93.9%–98.3%) | 92.7% (91.1%–94.0%) | 1.7% (1.3%–2.3%) | 91.8% (77.9%–96.6%) | 100.0% (99.4%–100.0%) | 92.8% (83.7%–94.4%) | 98.6% (97.4%–99.4%) | 84.3% (63.8%–89.7%) | 91.5% (75.2%–95.5%) | | | | | | |

## Gate

A configuration passes when its worse run is at least the run stored on recall and on the three fields, at most 2 points below it on precision and at most 2 points above it on over-split, and its runs are at least as stable as two runs of today's pipeline (claude-opus-5-5, prompt v3, effort default).

| Configuration | Runs | Recall (worse) | Precision (worse) | Three (worse) | Over-split (worse) | Stability | Passes |
|---|---|--:|--:|--:|--:|--:|---|
| needs | | ≥ 79.8% | ≥ 91.9% | ≥ 75.2% | ≤ 3.3% | ≥ 94.8% | |
| claude-opus-5-5, prompt v3, effort default (today's pipeline) | migrated, opus-v3-1, opus-v3-2 | 90.2% | 97.3% | 88.7% | 2.3% | 94.8% | yes |

## Paired bootstrap against stored

2000 resamples of the documents, seed 2026.

| Run | Metric | Difference | 95% interval | Resamples ahead |
|---|---|--:|--:|--:|
| migrated | recall | +10.4 | +4.3 to +15.7 | 100% |
| migrated | precision | +4.4 | +1.6 to +7.9 | 100% |
| migrated | over_split | +1.0 | -2.1 to +4.1 | 73% |
| migrated | field_all | +21.8 | -3.6 to +44.9 | 94% |
| migrated | field_three | +19.1 | -9.5 to +44.7 | 85% |
| opus-v3-1 | recall | +12.4 | +6.3 to +19.9 | 100% |
| opus-v3-1 | precision | +3.4 | -0.1 to +7.1 | 97% |
| opus-v3-1 | over_split | +0.4 | -1.8 to +2.8 | 60% |
| opus-v3-1 | field_all | +19.2 | +4.0 to +32.0 | 100% |
| opus-v3-1 | field_three | +13.5 | -2.9 to +28.3 | 90% |
| opus-v3-2 | recall | +11.9 | +5.0 to +17.5 | 100% |
| opus-v3-2 | precision | +4.4 | +1.2 to +7.9 | 100% |
| opus-v3-2 | over_split | +0.4 | -2.6 to +3.2 | 57% |
| opus-v3-2 | field_all | +25.9 | +2.5 to +49.9 | 99% |
| opus-v3-2 | field_three | +20.3 | -4.1 to +45.1 | 88% |

## Candidates only one run found

How many the judges kept; a judge favouring one model would keep its lone decisions more often.

- haiku-1: 18 of 28 kept
- opus-1: 8 of 11 kept
- sonnet-1: 1 of 2 kept
- stored: 1 of 3 kept

## Per document

| Document | stored | migrated | opus-v3-1 | opus-v3-2 |
|---|--:|--:|--:|--:|
| 1071kongres | 18/29 found, 2 false | 23/29 found, 0 false | 20/29 found, 0 false | 24/29 found, 0 false |
| 490 | 38/42 found, 0 false | 41/42 found, 0 false | 42/42 found, 0 false | 42/42 found, 0 false |
| CoachResponsibility | 9/14 found, 0 false | 13/14 found, 0 false | 13/14 found, 0 false | 13/14 found, 0 false |
| Tilmeldingsfrister2026 | 4/9 found, 0 false | 7/9 found, 0 false | 9/9 found, 0 false | 7/9 found, 0 false |
| elite09092017 | 8/10 found, 1 false | 8/10 found, 0 false | 9/10 found, 0 false | 10/10 found, 0 false |
| elite10122024 | 4/5 found, 0 false | 5/5 found, 0 false | 5/5 found, 0 false | 5/5 found, 0 false |
| elite13022009 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| elite14122021 | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false |
| elite18052021 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refbest_09082014 | 5/8 found, 1 false | 6/8 found, 0 false | 7/8 found, 0 false | 5/8 found, 0 false |
| refbest_12092010 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| refbest_130321 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| refbest_150723 | 5/6 found, 0 false | 5/6 found, 0 false | 6/6 found, 0 false | 5/6 found, 0 false |
| refbest_230423 | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false |
| refbest_24082013 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refbest_30052010 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 1 false | 4/4 found, 0 false |
| refbest_310819 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refdom07012025 | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false |
| refdom12032026 | 6/6 found, 1 false | 4/6 found, 0 false | 4/6 found, 0 false | 4/6 found, 0 false |
| refmedie09062026 | 0/0 found, 2 false | 0/0 found, 1 false | 0/0 found, 1 false | 0/0 found, 1 false |
| refstaevne10092024 | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false |
| refstaevne24042025 | 3/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 1 false | 4/5 found, 1 false |
| refudstyr16092025 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| rep2013 | 13/14 found, 1 false | 14/14 found, 1 false | 14/14 found, 1 false | 13/14 found, 0 false |
| rep2019 | 14/18 found, 1 false | 17/18 found, 0 false | 18/18 found, 0 false | 18/18 found, 0 false |

## Corrections applied

Changes on top of the judges' answers (eval/key/corrections.json), each checked against the minutes.

None.
