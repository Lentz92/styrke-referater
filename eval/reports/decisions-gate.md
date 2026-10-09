# Decisions against the answer key

25 documents; the key has 193 certain decisions, 0 uncertain ones (not scored) and 26 certainly rejected candidates, judged from the runs haiku-1, opus-1, sonnet-1, sonnet-2, stored.

Recall: key decisions found. Precision: found / (found + run decisions the key rejects); it counts only run decisions in the key's pool, and the others are Unjudged. Over-split: extra run decisions on a key decision already found, per found decision. Fields: share of found decisions with the key's value, among those whose value the judges agreed on; handling often needs the rule's history, so the three fields without it are given too. Doc avg: averaged over documents. Stability: for runs of the same configuration (model, prompt and effort), the share of their decisions matched one to one between them, per document and pooled.

Not candidate runs: sonnet-v3-1, sonnet-v3-2, opus-v3-1, opus-v3-2, migrated. The judges never saw their decisions, so those no candidate run had are unjudged, and their precision covers only the rest.

| Run | Recall | Recall (doc avg) | Precision | Precision (doc avg) | Over-split | Kategori | Udfald | Handling | Niveau | All four | Three (no handling) | Found | False | Extra | Ignored | Unjudged | Stability |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| stored | 79.8% | 84.9% | 93.9% | 91.1% | 1.3% | 77.9% | 99.4% | 83.7% | 97.4% | 63.8% | 75.2% | 154 | 10 | 2 | 0 | 0 | – |
| sonnet-1 | 67.4% | 81.5% | 92.2% | 85.7% | 0.8% | 86.2% | 99.2% | 86.2% | 96.2% | 69.2% | 81.5% | 130 | 11 | 1 | 0 | 0 | 91.7% |
| sonnet-2 | 71.5% | 82.9% | 93.9% | 87.0% | 0.7% | 89.1% | 99.3% | 85.4% | 97.1% | 73.5% | 85.4% | 138 | 9 | 1 | 0 | 0 | 91.7% |
| haiku-1 | 91.7% | 90.4% | 92.7% | 94.6% | 1.7% | 57.1% | 100.0% | 81.9% | 91.0% | 44.6% | 54.2% | 177 | 14 | 3 | 0 | 0 | – |
| opus-1 | 91.2% | 92.5% | 96.7% | 92.5% | 1.7% | 96.0% | 100.0% | 97.7% | 98.9% | 93.7% | 95.4% | 176 | 6 | 3 | 0 | 0 | – |
| sonnet-v3-1 | 79.8% | 86.3% | 93.3% | 86.2% | 0.0% | 77.9% | 100.0% | 87.6% | 97.4% | 68.6% | 75.3% | 154 | 11 | 0 | 0 | 2 | 93.4% |
| sonnet-v3-2 | 80.8% | 86.0% | 96.3% | 88.0% | 0.6% | 79.5% | 99.4% | 85.8% | 97.4% | 66.5% | 76.3% | 156 | 6 | 1 | 0 | 2 | 93.4% |
| opus-v3-1 | 92.2% | 94.3% | 97.3% | 92.1% | 1.7% | 89.3% | 100.0% | 94.4% | 98.9% | 83.1% | 88.7% | 178 | 5 | 3 | 0 | 2 | 94.8% |
| opus-v3-2 | 91.7% | 91.9% | 98.3% | 93.3% | 1.7% | 96.6% | 100.0% | 94.3% | 98.3% | 89.7% | 95.5% | 177 | 3 | 3 | 0 | 2 | 94.8% |
| migrated | 90.2% | 91.4% | 98.3% | 94.0% | 2.3% | 94.3% | 100.0% | 91.4% | 99.4% | 85.6% | 94.3% | 174 | 3 | 4 | 0 | 2 | 94.8% |
| median (min–max) | 85.5% (67.4%–92.2%) | 88.4% (81.5%–94.3%) | 95.1% (92.2%–98.3%) | 91.6% (85.7%–94.6%) | 1.5% (0.0%–2.3%) | 87.6% (57.1%–96.6%) | 100.0% (99.2%–100.0%) | 86.9% (81.9%–97.7%) | 97.4% (91.0%–99.4%) | 71.4% (44.6%–93.7%) | 83.5% (54.2%–95.5%) | | | | | | |

## Gate

A configuration passes when its worse run is no worse, on recall, precision, the three fields and over-split, than the run migrated less its slack or than the worst run of today's pipeline (claude-opus-5-5, prompt v3, effort default), whichever is looser, and its runs are at least as stable as the runs of today's pipeline. Runs of today's pipeline differ from migrated on noise alone, so a candidate as good as its worst run passes.

| Configuration | Runs | Recall (worse) | Precision (worse) | Three (worse) | Over-split (worse) | Stability | Passes |
|---|---|--:|--:|--:|--:|--:|---|
| needs | | ≥ 90.2% | ≥ 96.3% | ≥ 88.7% | ≤ 4.3% | ≥ 94.8% | |
| slack (points) | | 0.0 | 2.0 | 0.0 | 2.0 | | |
| today's pipeline, worst run per figure | opus-v3-1, opus-v3-2, migrated | 90.2% | 97.3% | 88.7% | 2.3% | | |
| claude-sonnet-5-5, prompt v2, effort default | sonnet-1, sonnet-2 | 67.4% | 92.2% | 81.5% | 0.8% | 91.7% | no: recall, precision, field_three, stability |
| claude-sonnet-5-5, prompt v3, effort default | sonnet-v3-1, sonnet-v3-2 | 79.8% | 93.3% | 75.3% | 0.6% | 93.4% | no: recall, precision, field_three, stability |
| claude-opus-5-5, prompt v3, effort default (today's pipeline) | opus-v3-1, opus-v3-2, migrated | 90.2% | 97.3% | 88.7% | 2.3% | 94.8% | yes (today's pipeline: the reference) |

## Paired bootstrap against stored

2000 resamples of the documents, seed 2026.

| Run | Metric | Difference | 95% interval | Resamples ahead |
|---|---|--:|--:|--:|
| sonnet-1 | recall | -12.4 | -23.8 to +0.0 | 2% |
| sonnet-1 | precision | -1.7 | -4.8 to +1.5 | 13% |
| sonnet-1 | over_split | -0.5 | -2.5 to +0.3 | 23% |
| sonnet-1 | field_all | +5.4 | -7.6 to +13.6 | 73% |
| sonnet-1 | field_three | +6.4 | -7.4 to +16.1 | 67% |
| sonnet-2 | recall | -8.3 | -14.6 to +1.4 | 4% |
| sonnet-2 | precision | -0.0 | -3.0 to +2.7 | 50% |
| sonnet-2 | over_split | -0.6 | -2.5 to +0.2 | 22% |
| sonnet-2 | field_all | +9.7 | -4.9 to +22.2 | 79% |
| sonnet-2 | field_three | +10.2 | -4.5 to +23.4 | 79% |
| haiku-1 | recall | +11.9 | +1.7 to +21.4 | 99% |
| haiku-1 | precision | -1.2 | -7.3 to +5.6 | 41% |
| haiku-1 | over_split | +0.4 | -1.8 to +3.0 | 60% |
| haiku-1 | field_all | -19.2 | -34.3 to -2.6 | 1% |
| haiku-1 | field_three | -20.9 | -34.9 to -7.2 | 0% |
| opus-1 | recall | +11.4 | +5.0 to +17.3 | 100% |
| opus-1 | precision | +2.8 | -0.5 to +6.9 | 95% |
| opus-1 | over_split | +0.4 | -1.8 to +2.7 | 62% |
| opus-1 | field_all | +29.9 | +7.3 to +51.2 | 100% |
| opus-1 | field_three | +20.3 | -3.1 to +44.2 | 91% |
| sonnet-v3-1 | recall | +0.0 | -5.1 to +5.4 | 49% |
| sonnet-v3-1 | precision | -0.6 | -4.4 to +2.3 | 36% |
| sonnet-v3-1 | over_split | -1.3 | -4.0 to +0.0 | 0% |
| sonnet-v3-1 | field_all | +4.8 | -2.1 to +10.1 | 92% |
| sonnet-v3-1 | field_three | +0.2 | -5.8 to +4.2 | 50% |
| sonnet-v3-2 | recall | +1.0 | -1.9 to +4.9 | 66% |
| sonnet-v3-2 | precision | +2.4 | -0.7 to +5.5 | 94% |
| sonnet-v3-2 | over_split | -0.7 | -3.3 to +1.6 | 22% |
| sonnet-v3-2 | field_all | +2.6 | -3.6 to +7.7 | 80% |
| sonnet-v3-2 | field_three | +1.1 | -5.0 to +4.4 | 63% |
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
| migrated | recall | +10.4 | +4.3 to +15.7 | 100% |
| migrated | precision | +4.4 | +1.6 to +7.9 | 100% |
| migrated | over_split | +1.0 | -2.1 to +4.1 | 73% |
| migrated | field_all | +21.8 | -3.6 to +44.9 | 94% |
| migrated | field_three | +19.1 | -9.5 to +44.7 | 85% |

## Candidates only one run found

How many the judges kept; a judge favouring one model would keep its lone decisions more often.

- haiku-1: 18 of 28 kept
- opus-1: 8 of 11 kept
- sonnet-1: 1 of 2 kept
- stored: 1 of 3 kept

## Per document

| Document | stored | sonnet-1 | sonnet-2 | haiku-1 | opus-1 | sonnet-v3-1 | sonnet-v3-2 | opus-v3-1 | opus-v3-2 | migrated |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 1071kongres | 18/29 found, 2 false | 14/29 found, 2 false | 13/29 found, 1 false | 29/29 found, 9 false | 22/29 found, 0 false | 14/29 found, 2 false | 17/29 found, 1 false | 20/29 found, 0 false | 24/29 found, 0 false | 23/29 found, 0 false |
| 490 | 38/42 found, 0 false | 21/42 found, 0 false | 29/42 found, 0 false | 42/42 found, 0 false | 42/42 found, 0 false | 37/42 found, 0 false | 37/42 found, 0 false | 42/42 found, 0 false | 42/42 found, 0 false | 41/42 found, 0 false |
| CoachResponsibility | 9/14 found, 0 false | 7/14 found, 0 false | 5/14 found, 0 false | 12/14 found, 0 false | 13/14 found, 0 false | 10/14 found, 0 false | 10/14 found, 0 false | 13/14 found, 0 false | 13/14 found, 0 false | 13/14 found, 0 false |
| Tilmeldingsfrister2026 | 4/9 found, 0 false | 5/9 found, 0 false | 5/9 found, 0 false | 8/9 found, 0 false | 7/9 found, 0 false | 4/9 found, 0 false | 4/9 found, 0 false | 9/9 found, 0 false | 7/9 found, 0 false | 7/9 found, 0 false |
| elite09092017 | 8/10 found, 1 false | 6/10 found, 1 false | 8/10 found, 1 false | 8/10 found, 1 false | 8/10 found, 0 false | 8/10 found, 1 false | 8/10 found, 0 false | 9/10 found, 0 false | 10/10 found, 0 false | 8/10 found, 0 false |
| elite10122024 | 4/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 0 false | 5/5 found, 0 false | 5/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 0 false | 5/5 found, 0 false | 5/5 found, 0 false | 5/5 found, 0 false |
| elite13022009 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| elite14122021 | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false |
| elite18052021 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 3/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refbest_09082014 | 5/8 found, 1 false | 6/8 found, 0 false | 6/8 found, 0 false | 4/8 found, 0 false | 8/8 found, 0 false | 5/8 found, 0 false | 5/8 found, 0 false | 7/8 found, 0 false | 5/8 found, 0 false | 6/8 found, 0 false |
| refbest_12092010 | 0/0 found, 0 false | 0/0 found, 1 false | 0/0 found, 1 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 2 false | 0/0 found, 1 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| refbest_130321 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| refbest_150723 | 5/6 found, 0 false | 5/6 found, 0 false | 5/6 found, 0 false | 6/6 found, 0 false | 5/6 found, 0 false | 5/6 found, 0 false | 5/6 found, 0 false | 6/6 found, 0 false | 5/6 found, 0 false | 5/6 found, 0 false |
| refbest_230423 | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false |
| refbest_24082013 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 1 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refbest_30052010 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 1 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 1 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refbest_310819 | 4/4 found, 0 false | 4/4 found, 1 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refdom07012025 | 2/2 found, 0 false | 2/2 found, 1 false | 2/2 found, 1 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 1 false | 2/2 found, 1 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false |
| refdom12032026 | 6/6 found, 1 false | 5/6 found, 0 false | 5/6 found, 0 false | 4/6 found, 0 false | 4/6 found, 0 false | 6/6 found, 1 false | 5/6 found, 0 false | 4/6 found, 0 false | 4/6 found, 0 false | 4/6 found, 0 false |
| refmedie09062026 | 0/0 found, 2 false | 0/0 found, 2 false | 0/0 found, 2 false | 0/0 found, 0 false | 0/0 found, 1 false | 0/0 found, 1 false | 0/0 found, 1 false | 0/0 found, 1 false | 0/0 found, 1 false | 0/0 found, 1 false |
| refstaevne10092024 | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 2 false | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false |
| refstaevne24042025 | 3/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 1 false | 4/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 1 false | 4/5 found, 1 false | 4/5 found, 0 false |
| refudstyr16092025 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| rep2013 | 13/14 found, 1 false | 13/14 found, 1 false | 13/14 found, 1 false | 13/14 found, 0 false | 13/14 found, 1 false | 14/14 found, 1 false | 14/14 found, 0 false | 14/14 found, 1 false | 13/14 found, 0 false | 14/14 found, 1 false |
| rep2019 | 14/18 found, 1 false | 13/18 found, 1 false | 14/18 found, 1 false | 16/18 found, 0 false | 18/18 found, 2 false | 16/18 found, 1 false | 16/18 found, 1 false | 18/18 found, 0 false | 18/18 found, 0 false | 17/18 found, 0 false |

## Corrections applied

Changes on top of the judges' answers (eval/key/corrections.json), each checked against the minutes.

None.
