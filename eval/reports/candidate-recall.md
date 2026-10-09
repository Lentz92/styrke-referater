# Candidate recall

Each decision of today's rules is hidden from its rule, and the candidates for it are ranked from the rest (candidates.py: TF-IDF over Danish stems, category boost 0.3). Recall@K: the share whose rule is among the K best. 296 decisions are their rule's only one and are left out (hidden, the right answer is a new rule).

| Category | Decisions | @3 | @5 | @8 | @10 | @15 |
|---|--:|--:|--:|--:|--:|--:|
| **all** | 666 | 88.9% | 93.8% | 96.2% | 96.8% | 98.2% |
| medlemskab | 25 | 96.0% | 96.0% | 96.0% | 96.0% | 100.0% |
| okonomi | 55 | 92.7% | 96.4% | 98.2% | 98.2% | 98.2% |
| antidoping | 19 | 94.7% | 100.0% | 100.0% | 100.0% | 100.0% |
| staevner | 193 | 88.6% | 93.8% | 97.9% | 99.0% | 99.0% |
| dommere | 78 | 89.7% | 94.9% | 98.7% | 98.7% | 98.7% |
| landshold | 159 | 82.4% | 89.3% | 90.6% | 91.8% | 96.2% |
| master | 49 | 95.9% | 95.9% | 100.0% | 100.0% | 100.0% |
| udstyr | 25 | 96.0% | 96.0% | 96.0% | 96.0% | 96.0% |
| organisation | 14 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| internationalt | 49 | 85.7% | 95.9% | 95.9% | 95.9% | 98.0% |

Chosen K: 15, the smallest with at least 98% overall.

## Ranked below 15

- elite14122021#1 in bruttolandshold-udtagelsesrunder-og-karenstid (landshold): rank 273
- elite12122023#5 in ledsagelse-af-løftere-under-mesterskab (landshold): rank 177
- elite25062024_2#6 in ledsagelse-af-løftere-under-mesterskab (landshold): rank 133
- elite19092023#4 in struktur-på-landsholdssamlinger (landshold): rank 55
- 921kongres#2 in skift-af-nationalt-tilhørsforhold (internationalt): rank 39
- elite15062019#1 in træning-i-hjemklub-for-bruttolandsholdsatleter (landshold): rank 39
- refbest_08022015#6 in plakater-bannere-og-bagtæppe-ved-dm (staevner): rank 36
- rep2026#9 in divisionsturnering-struktur-og-afvikling (staevner): rank 33
- refbest_091119#7 in vejen-til-international-dommer (dommere): rank 21
- rep2018#8 in udtagelseskriterier-til-bruttolandshold (landshold): rank 20
- rep2017#4 in medaljer-til-stævner (okonomi): rank 18
- rep2017#19 in knævarmere (udstyr): rank 17
