# Audit candidates

Rules that may be one rule: pairs whose TF-IDF vectors (candidates.py's profiles: title, latest text, kort_regel and the emne of each decision) have at least the threshold's cosine similarity, in any category. A propose call gets its category's rules and every rule of another category this similar to one of them. Recall on the answer key: for each key rule whose certain events are in more than one pipeline rule (fragmented), every pair of those rules. Flagged: the pair is at least that similar; In one call: flagged, or in one category (a call shows all of its category's rules). Soft rules are left out.

| Threshold | Flagged | In one call | Similar rules per rule | Other-category rules per call (mean, max) |
|--:|--:|--:|--:|--:|
| 0.05 | 96.7% | 100.0% | 96.9 | 386, 480 |
| 0.08 | 93.3% | 100.0% | 46.6 | 288, 420 |
| 0.1 | 90.0% | 100.0% | 30.7 | 229, 365 |
| 0.12 | 90.0% | 100.0% | 20.5 | 176, 311 |
| 0.15 | 83.3% | 96.7% | 11.7 | 116, 249 |
| 0.2 | 63.3% | 83.3% | 5.5 | 62, 148 |
| 0.25 | 56.7% | 83.3% | 2.8 | 35, 99 |

audit.SIMILARITY is 0.1: 100.0% of the fragment pairs in one call, at least the target of 95%. The highest threshold reaching the target is 0.15.

## Fragment pairs: 30

| Key rule | Rule | Rule | Same category | Similarity |
|---|---|---|---|--:|
| b-dommerprøve | b-dommerprøve | b-dommerprøve-2 | yes | 0.355 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | medaljer-til-stævner | bestilling-betaling-og-levering-af-medaljer-og-pokaler | no | 0.424 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | leverandør-af-medaljer | medaljer-til-stævner | no | 0.173 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | leverandør-af-medaljer | bestilling-betaling-og-levering-af-medaljer-og-pokaler | no | 0.169 |
| bruttolandshold-udtagelsesrunder-og-karenstid | bruttolandshold-udtagelsesrunder-og-karenstid | tid-i-bruttotruppen-før-international-deltagelse | yes | 0.000 |
| coachlicens-og-ipf-træneruddannelse | coachlicens-og-ipf-træneruddannelse | krav-til-coaches-ved-ipf-nominering | yes | 0.566 |
| dommeres-aktivitetskrav | dommeres-aktivitetskrav | nye-b-dommere-skal-dømme-inden-for-første-år | yes | 0.157 |
| dækning-af-udgifter-ved-internationale-masterstævner | dækning-af-udgifter-ved-internationale-masterstævner | klubbers-hæftelse-for-startgebyr-ved-udeblivelse | yes | 0.377 |
| dækning-af-udgifter-ved-internationale-masterstævner | hoteltilskud-til-ledere-ved-masterstævner | klubbers-hæftelse-for-startgebyr-ved-udeblivelse | yes | 0.133 |
| dækning-af-udgifter-ved-internationale-masterstævner | dækning-af-udgifter-ved-internationale-masterstævner | hoteltilskud-til-ledere-ved-masterstævner | yes | 0.087 |
| godkendelse-af-logoer-på-løftertøj | afgift-for-brug-af-logo | godkendelse-af-logoer-på-løftertøj | no | 0.325 |
| rejseplanlægning-og-booking | opholdslængde-ved-internationale-stævner | rejseplanlægning-og-booking | yes | 0.256 |
| startgebyr-ved-stævner | fordeling-af-startgebyr | startgebyr-ved-stævner | yes | 0.644 |
| stævner-hvor-kvalifikationskrav-kan-opnås | stævner-hvor-kvalifikationskrav-kan-opnås | udtagelse-til-em-vm-2021 | yes | 0.290 |
| trænergebyr-og-trænerakkreditering-ved-em-vm | trænergebyr-og-trænerakkreditering-ved-em-vm | trænergebyr-ved-ipf-mesterskaber | yes | 0.490 |
| trænergebyr-og-trænerakkreditering-ved-em-vm | trænergebyr-og-trænerakkreditering-ved-em-vm | trænerakkreditering-ved-em-og-vm | no | 0.468 |
| trænergebyr-og-trænerakkreditering-ved-em-vm | trænergebyr-ved-ipf-mesterskaber | trænerakkreditering-ved-em-og-vm | no | 0.324 |
| trænergebyr-og-trænerakkreditering-ved-em-vm | epf-s-medlemsafgift-for-nationer | trænergebyr-og-trænerakkreditering-ved-em-vm | yes | 0.191 |
| trænergebyr-og-trænerakkreditering-ved-em-vm | epf-s-medlemsafgift-for-nationer | trænerakkreditering-ved-em-og-vm | no | 0.125 |
| trænergebyr-og-trænerakkreditering-ved-em-vm | epf-s-medlemsafgift-for-nationer | trænergebyr-ved-ipf-mesterskaber | yes | 0.072 |
| udtagelse-til-internationale-masterstævner | kvalifikationsperiode-for-masterlandshold | udtagelse-til-internationale-masterstævner | yes | 0.260 |
| udtagelse-til-internationale-masterstævner | dispensation-fra-landsholdskrav | udtagelse-til-internationale-masterstævner | no | 0.191 |
| udtagelse-til-internationale-masterstævner | dispensation-fra-landsholdskrav | kvalifikationsperiode-for-masterlandshold | no | 0.152 |
| ventetid-fra-licens-til-stævnedeltagelse | ventetid-fra-licens-til-stævnedeltagelse | seksmånedersreglen-for-stævnedeltagelse | no | 0.299 |
| årsafgift | årsafgift | årsafgift-for-nye-klubber | yes | 0.692 |
| årsafgift | betalingsdato-for-årsafgift | årsafgift | yes | 0.612 |
| årsafgift | betalingsdato-for-årsafgift | årsafgift-for-nye-klubber | yes | 0.532 |
| årsafgift | opkrævning-af-licens | årsafgift-for-nye-klubber | yes | 0.259 |
| årsafgift | opkrævning-af-licens | årsafgift | yes | 0.211 |
| årsafgift | betalingsdato-for-årsafgift | opkrævning-af-licens | yes | 0.204 |

Left out (soft): udlån-af-forbundets-udstyr ~ ansvarsfordeling-mellem-dsf-og-arrangør-ved-dm (0.058); udlån-af-forbundets-udstyr ~ udlån-af-dommerlys (0.283); ansvarsfordeling-mellem-dsf-og-arrangør-ved-dm ~ udlån-af-dommerlys (0.175).
