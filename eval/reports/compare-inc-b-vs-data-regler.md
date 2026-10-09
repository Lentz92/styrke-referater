# inc-b against data-regler

- Grouping of decisions, B-cubed precision / recall / F1 (A against B): 98.0% / 97.4% / 97.7%
- In force at the year cutoffs, pooled: same decision 96.5%, same adopting decision 96.5%
- Same effect for decisions both put in a rule: 97.8% of 962
- Decisions in a rule in A only: 3; in B only: 0

## Held out: newest:20 (the newest documents, 88 decisions)

- Grouping of decisions, B-cubed precision / recall / F1 (A against B): 96.1% / 91.7% / 93.9%
- In force at the year cutoffs, pooled: same decision 81.1%, same adopting decision 81.5%
- Same effect for decisions both put in a rule: 87.4% of 87

## Held out: random:10 (late insertion: mostly older documents, 83 decisions)

- Grouping of decisions, B-cubed precision / recall / F1 (A against B): 90.7% / 91.0% / 90.9%
- In force at the year cutoffs, pooled: same decision 78.2%, same adopting decision 74.1%
- Same effect for decisions both put in a rule: 90.1% of 81

## Per year, all rules

| Year | Same decision in force | Same adopting decision |
|---|--:|--:|
| 2008 | 6/6 | 6/6 |
| 2009 | 15/15 | 15/15 |
| 2010 | 27/29 | 27/29 |
| 2011 | 47/49 | 47/49 |
| 2012 | 77/80 | 77/80 |
| 2013 | 104/107 | 104/107 |
| 2014 | 131/134 | 131/134 |
| 2015 | 182/186 | 182/186 |
| 2016 | 195/199 | 195/199 |
| 2017 | 207/210 | 206/211 |
| 2018 | 235/240 | 235/240 |
| 2019 | 252/262 | 252/262 |
| 2020 | 256/267 | 256/267 |
| 2021 | 267/279 | 267/279 |
| 2022 | 288/299 | 288/299 |
| 2023 | 302/312 | 302/312 |
| 2024 | 335/345 | 335/345 |
| 2025 | 360/371 | 360/371 |
| 2026 | 379/400 | 379/400 |
| 2027 | 378/401 | 379/400 |

## Against the rules key

| Measure | inc-b | data-regler |
|---|--:|--:|
| Events found | 60.3% | 62.6% |
| Effects agree | 85.9% | 84.0% |
| Fragmented rules | 12 | 10 |
| Years, same event | 46.6% | 47.1% |
| Years, same content | 51.5% | 51.9% |

Per rule: events found of certain events, rules holding them, years with the same content in force of certain years (soft rules are left out of the totals above).

| Rule | inc-b | data-regler |
|---|---|---|
| afholdelse-af-dommerprøver | 1/2 found, 1 rules, 1/4 years | 1/2 found, 1 rules, 1/4 years |
| afvikling-og-placering-af-sm-og-jm | 6/8 found, 1 rules, 8/12 years | 6/8 found, 1 rules, 8/12 years |
| b-dommerprøve | 3/4 found, 1 rules, 7/8 years | 3/4 found, 1 rules, 7/8 years |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | 5/13 found, 4 rules, 3/13 years | 5/13 found, 4 rules, 3/13 years |
| bruttolandshold-udtagelsesrunder-og-karenstid | 2/2 found, 1 rules, 3/4 years | 2/2 found, 1 rules, 3/4 years |
| coachlicens-og-ipf-træneruddannelse | 4/5 found, 1 rules, 2/3 years | 4/5 found, 1 rules, 2/3 years |
| dommeres-aktivitetskrav | 6/7 found, 2 rules, 12/13 years | 6/7 found, 2 rules, 12/13 years |
| dækning-af-udgifter-ved-internationale-masterstævner | 3/5 found, 3 rules, 8/9 years | 3/5 found, 3 rules, 8/9 years |
| godkendelse-af-logoer-på-løftertøj | 3/5 found, 2 rules, 8/8 years | 4/5 found, 2 rules, 8/8 years |
| krav-om-træning-i-egen-klub | 2/4 found, 1 rules, 1/10 years | 2/4 found, 1 rules, 1/10 years |
| licensgebyr | 6/11 found, 3 rules, 11/14 years | 6/11 found, 3 rules, 11/14 years |
| lån-af-dsf-s-stævneudstyr (soft) | 3/11 found, 3 rules, 3/15 years | 3/11 found, 3 rules, 3/15 years |
| medaljeceremoni-og-personligt-fremmøde | 5/6 found, 2 rules, 8/11 years | 6/6 found, 1 rules, 8/11 years |
| rejseplanlægning-og-booking | 4/8 found, 4 rules, 4/16 years | 4/8 found, 4 rules, 4/16 years |
| startgebyr-ved-stævner | 5/15 found, 3 rules, 0/18 years | 5/15 found, 3 rules, 0/18 years |
| stævner-hvor-kvalifikationskrav-kan-opnås | 5/6 found, 2 rules, 9/15 years | 6/6 found, 1 rules, 10/15 years |
| trænergebyr-og-trænerakkreditering-ved-em-vm | 5/7 found, 1 rules, 1/3 years | 5/7 found, 1 rules, 1/3 years |
| udtagelse-til-internationale-masterstævner | 4/6 found, 2 rules, 6/18 years | 4/6 found, 2 rules, 6/18 years |
| ventetid-fra-licens-til-stævnedeltagelse | 2/4 found, 3 rules, 13/13 years | 2/4 found, 3 rules, 13/13 years |
| årsafgift | 8/13 found, 4 rules, 1/14 years | 8/13 found, 4 rules, 1/14 years |
