# Audit candidates

Rules that may be one rule: pairs whose TF-IDF vectors (candidates.py's profiles: title, latest text, kort_regel and the emne of each decision) have at least the threshold's cosine similarity, in any category. A propose call gets its category's rules and every rule of another category this similar to one of them. Recall on the answer key: for each key rule whose certain events are in more than one pipeline rule (fragmented), every pair of those rules. Flagged: the pair is at least that similar; In one call: flagged, or in one category (a call shows all of its category's rules). Soft rules are left out.

| Threshold | Flagged | In one call | Similar rules per rule | Other-category rules per call (mean, max) |
|--:|--:|--:|--:|--:|
| 0.05 | 97.0% | 97.0% | 75.3 | 326, 408 |
| 0.08 | 97.0% | 97.0% | 36.8 | 237, 338 |
| 0.1 | 90.9% | 97.0% | 24.2 | 185, 301 |
| 0.12 | 78.8% | 93.9% | 16.6 | 144, 258 |
| 0.15 | 69.7% | 90.9% | 9.5 | 92, 188 |
| 0.2 | 60.6% | 87.9% | 4.2 | 46, 105 |
| 0.25 | 51.5% | 84.8% | 2.0 | 23, 52 |

Chosen threshold: 0.1, the highest with at least 95% of the fragment pairs in one call.

## Fragment pairs: 33

| Key rule | Rule | Rule | Same category | Similarity |
|---|---|---|---|--:|
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | medaljer-til-stævner | bestilling-betaling-og-levering-af-medaljer-og-pokaler | no | 0.480 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | startgebyr-ved-stævner | invitation-tilmeldings-og-betalingsfrist-til-stævner | no | 0.297 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | bestilling-betaling-og-levering-af-medaljer-og-pokaler | invitation-tilmeldings-og-betalingsfrist-til-stævner | yes | 0.253 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | startgebyr-ved-stævner | bestilling-betaling-og-levering-af-medaljer-og-pokaler | no | 0.245 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | medaljer-til-stævner | startgebyr-ved-stævner | yes | 0.173 |
| bestilling-betaling-og-levering-af-medaljer-og-pokaler | medaljer-til-stævner | invitation-tilmeldings-og-betalingsfrist-til-stævner | no | 0.109 |
| dommeres-aktivitetskrav | dommeres-aktivitetskrav | nye-b-dommere-skal-dømme-inden-for-første-år | yes | 0.268 |
| dækning-af-udgifter-ved-internationale-masterstævner | dækning-af-udgifter-ved-internationale-masterstævner | klubbers-hæftelse-for-startgebyr-ved-udeblivelse | yes | 0.350 |
| dækning-af-udgifter-ved-internationale-masterstævner | ophold-på-stævnehotel-og-bindende-tilmelding | klubbers-hæftelse-for-startgebyr-ved-udeblivelse | no | 0.285 |
| dækning-af-udgifter-ved-internationale-masterstævner | ophold-på-stævnehotel-og-bindende-tilmelding | dækning-af-udgifter-ved-internationale-masterstævner | no | 0.265 |
| godkendelse-af-logoer-på-løftertøj | afgift-for-brug-af-logo | godkendelse-af-logoer-på-løftertøj | no | 0.383 |
| licensgebyr | licensgebyr | årsafgift | yes | 0.308 |
| licensgebyr | kontorhold-og-telefonpenge | årsafgift | yes | 0.307 |
| licensgebyr | kontorhold-og-telefonpenge | licensgebyr | yes | 0.106 |
| rejseplanlægning-og-booking | opholdslængde-ved-internationale-stævner | bestilling-af-rejser-til-internationale-stævner | no | 0.400 |
| rejseplanlægning-og-booking | opholdslængde-ved-internationale-stævner | rejseplanlægning-og-booking | yes | 0.205 |
| rejseplanlægning-og-booking | rejseplanlægning-og-booking | bestilling-af-rejser-til-internationale-stævner | no | 0.188 |
| rejseplanlægning-og-booking | egenbetaling-ved-internationale-mesterskaber | rejseplanlægning-og-booking | yes | 0.177 |
| rejseplanlægning-og-booking | egenbetaling-ved-internationale-mesterskaber | opholdslængde-ved-internationale-stævner | yes | 0.094 |
| rejseplanlægning-og-booking | egenbetaling-ved-internationale-mesterskaber | bestilling-af-rejser-til-internationale-stævner | no | 0.048 |
| startgebyr-ved-stævner | startgebyr-ved-stævner | årsafgift | yes | 0.364 |
| startgebyr-ved-stævner | kontorhold-og-telefonpenge | årsafgift | yes | 0.307 |
| startgebyr-ved-stævner | kontorhold-og-telefonpenge | startgebyr-ved-stævner | yes | 0.139 |
| udtagelse-til-internationale-masterstævner | dispensation-fra-landsholdskrav | udtagelse-til-internationale-masterstævner | no | 0.139 |
| ventetid-fra-licens-til-stævnedeltagelse | seksmånedersreglen-for-stævnedeltagelse | ventetid-fra-licens-til-stævnedeltagelse | yes | 0.247 |
| ventetid-fra-licens-til-stævnedeltagelse | karantæne-ved-klubløshed-udmeldelse-og-genindmeldelse | ventetid-fra-licens-til-stævnedeltagelse | yes | 0.110 |
| ventetid-fra-licens-til-stævnedeltagelse | karantæne-ved-klubløshed-udmeldelse-og-genindmeldelse | seksmånedersreglen-for-stævnedeltagelse | yes | 0.093 |
| årsafgift | årsafgift | årsafgift-for-nye-klubber | yes | 0.540 |
| årsafgift | betalingsdato-for-årsafgift | årsafgift-for-nye-klubber | yes | 0.480 |
| årsafgift | betalingsdato-for-årsafgift | årsafgift | yes | 0.469 |
| årsafgift | kontorhold-og-telefonpenge | årsafgift | yes | 0.307 |
| årsafgift | kontorhold-og-telefonpenge | årsafgift-for-nye-klubber | yes | 0.146 |
| årsafgift | betalingsdato-for-årsafgift | kontorhold-og-telefonpenge | yes | 0.117 |

Left out (soft): streaming-ved-danske-mesterskaber ~ stævneudstyrskufferter (0.045); streaming-ved-danske-mesterskaber ~ lån-af-dsf-s-stævneudstyr (0.223); stævneudstyrskufferter ~ lån-af-dsf-s-stævneudstyr (0.350).
