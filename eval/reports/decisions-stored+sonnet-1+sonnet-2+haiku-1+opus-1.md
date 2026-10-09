# Decisions against the answer key

25 documents; the key has 193 certain decisions, 0 uncertain ones (not scored) and 26 certainly rejected candidates, judged from the runs haiku-1, opus-1, sonnet-1, sonnet-2, stored.

Recall: key decisions found. Precision: found / (found + run decisions the key rejects); it counts only run decisions in the key's pool, and the others are Unjudged. Over-split: extra run decisions on a key decision already found, per found decision. Fields: share of found decisions with the key's value, among those whose value the judges agreed on. Doc avg: averaged over documents.

| Run | Recall | Recall (doc avg) | Precision | Precision (doc avg) | Over-split | Kategori | Udfald | Handling | Niveau | All four | Found | False | Extra | Ignored | Unjudged |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| stored | 79.8% | 84.9% | 93.9% | 91.1% | 1.3% | 77.9% | 99.4% | 83.7% | 97.4% | 63.8% | 154 | 10 | 2 | 0 | 0 |
| sonnet-1 | 67.4% | 81.5% | 92.2% | 85.7% | 0.8% | 86.2% | 99.2% | 86.2% | 96.2% | 69.2% | 130 | 11 | 1 | 0 | 0 |
| sonnet-2 | 71.5% | 82.9% | 93.9% | 87.0% | 0.7% | 89.1% | 99.3% | 85.4% | 97.1% | 73.5% | 138 | 9 | 1 | 0 | 0 |
| haiku-1 | 91.7% | 90.4% | 92.7% | 94.6% | 1.7% | 57.1% | 100.0% | 81.9% | 91.0% | 44.6% | 177 | 14 | 3 | 0 | 0 |
| opus-1 | 91.2% | 92.5% | 96.7% | 92.5% | 1.7% | 96.0% | 100.0% | 97.7% | 98.9% | 93.7% | 176 | 6 | 3 | 0 | 0 |
| median (min–max) | 79.8% (67.4%–91.7%) | 84.9% (81.5%–92.5%) | 93.9% (92.2%–96.7%) | 91.1% (85.7%–94.6%) | 1.3% (0.7%–1.7%) | 86.2% (57.1%–96.0%) | 99.4% (99.2%–100.0%) | 85.4% (81.9%–97.7%) | 97.1% (91.0%–98.9%) | 69.2% (44.6%–93.7%) | | | | | |

## Paired bootstrap against stored

2000 resamples of the documents, seed 2026.

| Run | Metric | Difference | 95% interval | Resamples ahead |
|---|---|--:|--:|--:|
| sonnet-1 | recall | -12.4 | -23.8 to +0.0 | 2% |
| sonnet-1 | precision | -1.7 | -4.8 to +1.5 | 13% |
| sonnet-1 | over_split | -0.5 | -2.5 to +0.3 | 23% |
| sonnet-1 | field_all | +5.4 | -7.6 to +13.6 | 73% |
| sonnet-2 | recall | -8.3 | -14.6 to +1.4 | 4% |
| sonnet-2 | precision | -0.0 | -3.0 to +2.7 | 50% |
| sonnet-2 | over_split | -0.6 | -2.5 to +0.2 | 22% |
| sonnet-2 | field_all | +9.7 | -4.9 to +22.2 | 79% |
| haiku-1 | recall | +11.9 | +1.7 to +21.4 | 99% |
| haiku-1 | precision | -1.2 | -7.3 to +5.6 | 41% |
| haiku-1 | over_split | +0.4 | -1.8 to +3.0 | 60% |
| haiku-1 | field_all | -19.2 | -34.3 to -2.6 | 1% |
| opus-1 | recall | +11.4 | +5.0 to +17.3 | 100% |
| opus-1 | precision | +2.8 | -0.5 to +6.9 | 95% |
| opus-1 | over_split | +0.4 | -1.8 to +2.7 | 62% |
| opus-1 | field_all | +29.9 | +7.3 to +51.2 | 100% |

## Candidates only one run found

How many the judges kept; a judge favouring one model would keep its lone decisions more often.

- haiku-1: 18 of 28 kept
- opus-1: 8 of 11 kept
- sonnet-1: 1 of 2 kept
- stored: 1 of 3 kept

## Per document

| Document | stored | sonnet-1 | sonnet-2 | haiku-1 | opus-1 |
|---|--:|--:|--:|--:|--:|
| 1071kongres | 18/29 found, 2 false | 14/29 found, 2 false | 13/29 found, 1 false | 29/29 found, 9 false | 22/29 found, 0 false |
| 490 | 38/42 found, 0 false | 21/42 found, 0 false | 29/42 found, 0 false | 42/42 found, 0 false | 42/42 found, 0 false |
| CoachResponsibility | 9/14 found, 0 false | 7/14 found, 0 false | 5/14 found, 0 false | 12/14 found, 0 false | 13/14 found, 0 false |
| Tilmeldingsfrister2026 | 4/9 found, 0 false | 5/9 found, 0 false | 5/9 found, 0 false | 8/9 found, 0 false | 7/9 found, 0 false |
| elite09092017 | 8/10 found, 1 false | 6/10 found, 1 false | 8/10 found, 1 false | 8/10 found, 1 false | 8/10 found, 0 false |
| elite10122024 | 4/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 0 false | 5/5 found, 0 false | 5/5 found, 0 false |
| elite13022009 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| elite14122021 | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false | 2/2 found, 0 false |
| elite18052021 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 3/4 found, 0 false | 4/4 found, 0 false |
| refbest_09082014 | 5/8 found, 1 false | 6/8 found, 0 false | 6/8 found, 0 false | 4/8 found, 0 false | 8/8 found, 0 false |
| refbest_12092010 | 0/0 found, 0 false | 0/0 found, 1 false | 0/0 found, 1 false | 0/0 found, 0 false | 0/0 found, 0 false |
| refbest_130321 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| refbest_150723 | 5/6 found, 0 false | 5/6 found, 0 false | 5/6 found, 0 false | 6/6 found, 0 false | 5/6 found, 0 false |
| refbest_230423 | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false | 3/3 found, 0 false |
| refbest_24082013 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 1 false | 4/4 found, 0 false |
| refbest_30052010 | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 1 false |
| refbest_310819 | 4/4 found, 0 false | 4/4 found, 1 false | 4/4 found, 0 false | 4/4 found, 0 false | 4/4 found, 0 false |
| refdom07012025 | 2/2 found, 0 false | 2/2 found, 1 false | 2/2 found, 1 false | 2/2 found, 0 false | 2/2 found, 0 false |
| refdom12032026 | 6/6 found, 1 false | 5/6 found, 0 false | 5/6 found, 0 false | 4/6 found, 0 false | 4/6 found, 0 false |
| refmedie09062026 | 0/0 found, 2 false | 0/0 found, 2 false | 0/0 found, 2 false | 0/0 found, 0 false | 0/0 found, 1 false |
| refstaevne10092024 | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 1 false | 4/4 found, 2 false | 4/4 found, 1 false |
| refstaevne24042025 | 3/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 0 false | 4/5 found, 1 false | 4/5 found, 0 false |
| refudstyr16092025 | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false | 0/0 found, 0 false |
| rep2013 | 13/14 found, 1 false | 13/14 found, 1 false | 13/14 found, 1 false | 13/14 found, 0 false | 13/14 found, 1 false |
| rep2019 | 14/18 found, 1 false | 13/18 found, 1 false | 14/18 found, 1 false | 16/18 found, 0 false | 18/18 found, 2 false |
