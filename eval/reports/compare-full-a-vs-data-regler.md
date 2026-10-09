# full-a against data-regler

- Grouping of decisions, B-cubed precision / recall / F1 (A against B): 94.4% / 93.9% / 94.1%
- In force at the year cutoffs, pooled: same decision 90.4%, same adopting decision 87.6%
- Same effect for decisions both put in a rule: 92.0% of 950
- Decisions in a rule in A only: 6; in B only: 12

## Held out: newest:20 (the newest documents, 87 decisions)

- Grouping of decisions, B-cubed precision / recall / F1 (A against B): 97.5% / 96.7% / 97.1%
- In force at the year cutoffs, pooled: same decision 83.3%, same adopting decision 77.6%
- Same effect for decisions both put in a rule: 94.2% of 86

## Held out: random:10 (late insertion: mostly older documents, 81 decisions)

- Grouping of decisions, B-cubed precision / recall / F1 (A against B): 94.6% / 92.8% / 93.7%
- In force at the year cutoffs, pooled: same decision 84.4%, same adopting decision 81.4%
- Same effect for decisions both put in a rule: 93.8% of 80

## Per year, all rules

| Year | Same decision in force | Same adopting decision |
|---|--:|--:|
| 2008 | 6/6 | 6/6 |
| 2009 | 15/15 | 14/16 |
| 2010 | 29/29 | 28/30 |
| 2011 | 49/50 | 48/51 |
| 2012 | 78/80 | 77/81 |
| 2013 | 102/110 | 101/111 |
| 2014 | 127/140 | 126/141 |
| 2015 | 175/193 | 175/193 |
| 2016 | 189/205 | 187/207 |
| 2017 | 199/219 | 195/223 |
| 2018 | 228/249 | 224/253 |
| 2019 | 245/269 | 241/273 |
| 2020 | 249/273 | 245/277 |
| 2021 | 261/283 | 257/287 |
| 2022 | 279/306 | 274/311 |
| 2023 | 289/322 | 285/326 |
| 2024 | 319/356 | 312/363 |
| 2025 | 341/389 | 334/396 |
| 2026 | 365/416 | 357/424 |
| 2027 | 366/415 | 359/422 |

## Against the rules key

| Measure | full-a | data-regler |
|---|--:|--:|
| Events found | 63.4% | 62.6% |
| Effects agree | 82.9% | 84.0% |
| Fragmented rules | 10 | 10 |
| Years, same event | 47.1% | 47.1% |
| Years, same content | 51.9% | 51.9% |

Per rule: events found of certain events, rules holding them, years with the same content in force of certain years (soft rules are left out of the totals above).

| Rule | full-a | data-regler |
|---|---|---|
| afholdelse-af-dommerprøver | 1/2 found, 1 rules, 3/4 years | 1/2 found, 1 rules, 1/4 years |
| afvikling-og-placering-af-sm-og-jm | 6/8 found, 1 rules, 8/12 years | 6/8 found, 1 rules, 8/12 years |
| b-dommerprøve | 2/4 found, 2 rules, 3/8 years | 3/4 found, 1 rules, 7/8 years |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | 5/13 found, 4 rules, 3/13 years | 5/13 found, 4 rules, 3/13 years |
| bruttolandshold-udtagelsesrunder-og-karenstid | 2/2 found, 1 rules, 3/4 years | 2/2 found, 1 rules, 3/4 years |
| coachlicens-og-ipf-træneruddannelse | 4/5 found, 1 rules, 2/3 years | 4/5 found, 1 rules, 2/3 years |
| dommeres-aktivitetskrav | 6/7 found, 2 rules, 13/13 years | 6/7 found, 2 rules, 12/13 years |
| dækning-af-udgifter-ved-internationale-masterstævner | 2/5 found, 4 rules, 6/9 years | 3/5 found, 3 rules, 8/9 years |
| godkendelse-af-logoer-på-løftertøj | 4/5 found, 2 rules, 8/8 years | 4/5 found, 2 rules, 8/8 years |
| krav-om-træning-i-egen-klub | 2/4 found, 1 rules, 1/10 years | 2/4 found, 1 rules, 1/10 years |
| licensgebyr | 6/11 found, 3 rules, 11/14 years | 6/11 found, 3 rules, 11/14 years |
| lån-af-dsf-s-stævneudstyr (soft) | 3/11 found, 3 rules, 3/15 years | 3/11 found, 3 rules, 3/15 years |
| medaljeceremoni-og-personligt-fremmøde | 6/6 found, 1 rules, 8/11 years | 6/6 found, 1 rules, 8/11 years |
| rejseplanlægning-og-booking | 5/8 found, 3 rules, 7/16 years | 4/8 found, 4 rules, 4/16 years |
| startgebyr-ved-stævner | 5/15 found, 3 rules, 0/18 years | 5/15 found, 3 rules, 0/18 years |
| stævner-hvor-kvalifikationskrav-kan-opnås | 6/6 found, 1 rules, 10/15 years | 6/6 found, 1 rules, 10/15 years |
| trænergebyr-og-trænerakkreditering-ved-em-vm | 5/7 found, 1 rules, 1/3 years | 5/7 found, 1 rules, 1/3 years |
| udtagelse-til-internationale-masterstævner | 4/6 found, 2 rules, 6/18 years | 4/6 found, 2 rules, 6/18 years |
| ventetid-fra-licens-til-stævnedeltagelse | 4/4 found, 1 rules, 13/13 years | 2/4 found, 3 rules, 13/13 years |
| årsafgift | 8/13 found, 4 rules, 1/14 years | 8/13 found, 4 rules, 1/14 years |
