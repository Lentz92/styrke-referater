# Answer key and measurements

Two extraction runs agree on only ~90% of decisions, so comparing runs cannot tell better from different. Every
choice of the pipeline (model, prompt, consolidation mode) is therefore scored against an answer key that Opus judged
once. `evaluate.py` scores against it and runs the incremental consolidation's gates; everything it writes is under
`eval/`, and it never touches `data/` or `regelsaet/`.

| Path | What |
|---|---|
| `selection.json` | the 25 documents and 20 recurring rules the key covers, and the date it was judged as of |
| `key/decisions/<doc>.json` | each document's decisions |
| `key/rules/<slug>.json` | each rule's timeline and what was in force each year |
| `key/judges/` | every judge's answer |
| `key/corrections.json` | errors a check against the minutes found in the judges' answers |
| `runs/<run>/<doc>.json` | extraction runs scored against the key (`stored` is `data/beslutninger` as it was) |
| `reports/<name>.md` | the scores |
| `runs.jsonl` | one line per command: its settings, the Claude usage of each step, the scores |

## How the key was built

The key was built in October 2026 with commands that are no longer in `evaluate.py` (`select`, `key-decisions`,
`key-rules`, with `--rejudge` and `--rederive`); `git worktree add ../key-build 9f39874` checks them out with their
tests.

- `select` chose the recurring rules and the documents (`selection.json`).
- The decisions key clustered the decisions of five extraction runs per document (`stored sonnet-1 sonnet-2 haiku-1
  opus-1`). Two judges kept, rejected or merged each candidate and added what every run missed, by the extraction
  prompt's own criteria, with notes on granularity: one decision per rule a decision sets (a budget setting three
  fees is three decisions), while a table or list adopted as a whole for one rule stays one. That a decision exists
  and which fields it has were judged apart, and field accuracy is scored only on the fields the judges agreed on.
- The rules key gave two judges passages from all documents (around the rule's decisions, other decisions using its
  title's words, and its keywords, amounts first) and asked for the rule's true timeline and what was in force each
  year, or that it is unknown. The judges were told the selection's date, not the day they ran.
- Where two judges disagreed, a third answered blind, and two of three decided; what was still split is left out of
  the scores.

Errors a check against the minutes found in the judges' answers are in `key/corrections.json`, each with its reason
and evidence (document and quote). They are applied on top of the judges whenever a key is scored, and listed in the
reports. A rule marked `soft` in `selection.json` (loosely scoped, so which decisions are its events is arbitrary) is
reported but left out of the overall rules figures.

## What is scored

```bash
uv run evaluate.py extract --name stored     # today's extractions as a run; other names call Claude
uv run evaluate.py extract --name opus-v3-1 --model claude-opus-5-5 --prompt v3 --max-cost 5
uv run evaluate.py score --run stored --run opus-v3-1 --run opus-v3-2
uv run evaluate.py score-rules               # data/regler, or --rules-dir/--decisions-dir
uv run evaluate.py candidate-recall          # hide each decision from its rule: is the rule among the top K?
uv run evaluate.py replay --holdout newest:20,random:10 --seed 1 --name inc-1 --max-cost 20
uv run evaluate.py compare-rules eval/replays/inc-1/data/regler data/regler
```

- `score` matches each run's decisions to the decisions key (the matcher that carries decision ids over) and reports
  recall, precision (against the decisions the key certainly rejects), over-split (extra run decisions on a key
  decision already found), the share of found decisions with the key's coded fields (all four, and the three without
  handling, which often needs the rule's history), a paired bootstrap against the first run, and for a configuration
  run twice its stability: the share of the two runs' decisions matched one to one, per document and pooled. The key
  cannot show stability, and an unstable extraction rewrites rules every time a document is extracted again. With
  the `stored` run among them it also applies the extraction gate (below).
- `score-rules` takes, per key rule, the pipeline rule holding most of its certain events, and counts the events
  found there, elsewhere or missing, whether their effects agree, how many pipeline rules hold the events (more than
  one: fragmented), and for each year whether the version in force was adopted by the key's adopting decision (same
  content, the main measure) or is the key's event itself (same event). A key event quoting a whole budget line is
  mapped to its own fee's decision, which prompt v3 extracts apart.
- `candidate-recall` hides each decision of today's rules from its rule and ranks the candidates for it from the
  rest, as incremental consolidation does; it also ranks each as if its category were another one.
- `replay` copies `data/` to `eval/replays/<name>/data`, removes the held-out documents' decisions from the rules
  there (rules left empty go with their slugs), and consolidates them again on the copy (`--mode incremental`, or
  `full`, which redoes their categories); a cut-off replay continues where it stopped. `compare-rules` reports how two
  rule directories group the decisions (B-cubed), what each shows in force every year and with which effect, the
  same over each holdout part of a replay (`random:` mostly tests decisions filed before a rule's newest version),
  and how each scores against the rules key (it stops without the key unless `--no-key`).

`extract` and `replay` call Claude: they take `--max-cost`, print how many calls they plan, add a line to
`runs.jsonl`, and exit 1 when a call failed or was skipped; run them again to continue. `extract` keeps each
document's answer, so a cut-off run or a pilot (`--pilot N`, `--docs`) is never paid twice; an extraction kept from
another file version, prompt or effort stops it until `--force`.

## Extraction model and prompt

The pipeline extracts with Opus (`update.EXTRACT_MODEL`) and prompt v3 (`analyze.EXTRACT_VERSION`), chosen with the
gate below in October 2026; until then it was Sonnet with prompt v2. Measured on the decisions key (25 documents,
193 certain decisions; `reports/decisions-stored+sonnet-1+sonnet-2+haiku-1+opus-1.md`):

| Run | Recall | Precision | All four fields | Three (no handling) | Stability |
|---|--:|--:|--:|--:|--:|
| stored (`data/` then, Sonnet, v2) | 79.8% | 93.9% | 63.8% | 75.2% | – |
| fresh Sonnet, v2, two runs | 67.4% / 71.5% | 92.2% / 93.9% | 69.2% / 73.5% | 81.5% / 85.4% | 91.7% |
| Haiku, v2 | 91.7% | 92.7% | 44.6% | 54.2% | – |
| Opus, v2 | 91.2% | 96.7% | 93.7% | 95.4% | – |

Sonnet misses a fifth to a third of the decisions, mostly in the large congress documents. And v2 extracts a budget
line that sets or confirms several fees as one decision, which only one rule can hold, so the licence fee's yearly
confirmations end up in "Årsafgift": a main cause of the rules key's low share of years with the same content in
force (46.6% with today's event mapping, see Migration). Prompt v3 (`analyze.EXTRACT_PROMPTS["v3"]`) is v2 plus one
field rule (`analyze.EXTRACT_SPLIT_RULE`) that applies the key's own granularity: one decision per rule, so a budget
line setting or confirming different fees (licens, årsafgift, startgebyr) gives one decision per fee, while the tiers
of one fee and a list adopted as a whole for one rule (a season's entry deadlines) stay one decision; a fee restated
unchanged with the budget is handling bekraeftelse. The key's judges had v2's rules, whose granularity notes already
said this, so the key stays as it is. Each candidate configuration was run twice (with v2 a run cost about 0.95 USD
with Sonnet and 2.35 with Opus):

```bash
uv run evaluate.py extract --name sonnet-v3-1 --model claude-sonnet-5-5 --prompt v3 --max-cost 3
uv run evaluate.py extract --name opus-v3-1 --model claude-opus-5-5 --prompt v3 --max-cost 5   # and -2 of each
uv run evaluate.py score --run stored --run sonnet-1 --run sonnet-2 --run sonnet-v3-1 --run sonnet-v3-2 \
    --run opus-v3-1 --run opus-v3-2
```

The gate: a configuration passes when its worse run is at least the stored run on recall and on the three fields, at
most 2 points below it on precision and at most 2 points above it on over-split (a prompt that splits more than the
key asks for would otherwise pass on recall), and its two runs are at least as stable as two runs of the pipeline's
configuration (`evaluate.pipeline_configuration`: Sonnet with v2 when v3 was chosen, Opus with v3 since). The result
(`reports/decisions-stored+sonnet-1+sonnet-2+sonnet-v3-1+sonnet-v3-2+opus-1+opus-v3-1+opus-v3-2.md`), each
configuration by its worse run:

| Configuration | Recall | Precision | Three fields | Over-split | Stability | Passes |
|---|--:|--:|--:|--:|--:|---|
| needs | ≥ 79.8% | ≥ 91.9% | ≥ 75.2% | ≤ 3.3% | ≥ 91.7% | |
| Sonnet, v2 (the pipeline then) | 67.4% | 92.2% | 81.5% | 0.8% | 91.7% | no: recall |
| Sonnet, v3 | 79.8% | 93.3% | 75.3% | 0.6% | 93.4% | yes, narrowly |
| Opus, v3 | 91.7% | 97.3% | 88.7% | 1.7% | 94.4% | yes |

Opus with v3 passes clearly: its worse run finds 91.7% of the key's decisions (the stored Sonnet extraction: 79.8%).
Sonnet with v3 only matches the stored run on recall and the three fields, so the pipeline moved to Opus, which costs
about 0.09 USD per document with v3 against Sonnet's 0.04.

## Migration to v3 with Opus

The move was one migration (`uv run update.py --offline --consolidate-mode full --allow-rebuild --max-cost 40
--time-budget 150`, with v3 and Opus as the defaults), which extracted every document again, carried the decision
ids over and consolidated every category in full. It logged its estimate first: about 27 USD at list price, 236
documents at 0.093 USD (the mean of the Opus v3 runs in `runs.jsonl`) and 5 for the consolidation.

It ran on 9 October 2026 for 25.32 USD at list price (extraction 16.74, consolidation 8.58). The full consolidation
left one decision in no rule, and the next plain run filed it for 0.11 USD; the run after that made no Claude call.
Of the 470 earlier rule slugs, 373 are unchanged, 51 lead to the rule that took over their decisions, and 46 are
retired: they held only decisions Opus no longer reads as decisions (reminders, items for information). Against the
key (`reports/decisions-migrated.md`, `reports/rules-premigration.md`, `reports/rules-migrated.md`):

| | Before (Sonnet, v2) | After (Opus, v3) |
|---|--:|--:|
| Decisions: recall / precision | 79.8% / 93.9% | 90.2% / 98.3% |
| Decisions: all four fields / three | 63.8% / 75.2% | 85.6% / 94.3% |
| Rules: years with the same content in force (of 206) | 96 (46.6%) | 106 (51.5%) |
| Rules: key events in their home rule / elsewhere / missing (of 131) | 81 / 28 / 22 | 82 / 32 / 17 |
| Rules: effects that agree | 68 of 81 | 70 of 81 |
| Rules: key rules spread over several pipeline rules (of 19) | 10 | 14 |

Both rule columns use the event mapping that gives a key event quoting a whole budget line to its own fee's decision
(the earlier figure of 51.9%, `reports/rules-regler.md`, came from the mapping before, which gave the licence fee's
events to the annual fee). Licensgebyr is now one rule holding ten of its eleven certain key events (the eleventh is
not extracted), where before its events were spread over three rules; more of the key's other events land in a rule
beside their own. Regrouping rules is the audit's work, not the extraction's.

## Incremental consolidation

Incremental consolidation offers each new decision the 15 rules whose words are closest to it
(`candidates.CANDIDATE_K`): the smallest K whose recall reaches 98% when each decision of the rules then is hidden
from its rule, 98.2% at 15 and 96.8% at 10 (`reports/candidate-recall.md`, run before the migration).

Replays on the data of then, holding out the 20 newest and 10 random documents, chose incremental consolidation as
the default (`reports/compare-*.md`, where `data-regler` is the full consolidation then in `data/regler`):

| Comparison | Grouping, B-cubed F1 | Same adopting decision in force | Years with the same content as the key |
|---|--:|--:|--:|
| incremental A against incremental B | 99.8% | 99.1% | 51.5% / 51.5% |
| full A against `data/regler` (full) | 94.1% | 87.6% | 51.9% / 51.9% |
| incremental A against `data/regler` | 97.7% | 97.1% | 51.5% / 51.9% |

Against the key, incremental and full show the same content in force (51.5% vs 51.9% of years, with the event mapping
of then), while two incremental runs agree on what was in force in 99.1% of years, two full ones in 87.6%.

## Audit

The audit's propose calls see a category's rules and every rule of another category at least `audit.SIMILARITY`
(0.1) similar to one of them. On the answer key that puts all the pairs of rules a key rule is spread over into one
call on the migrated data, 97% before the migration (`reports/audit-candidates.md`, `uv run audit.py candidates`).

The first audit (commit 9f39874) applied the 16 ops both Opus runs proposed: 8 merges, 4 splits and 4 category moves;
the 18 that only one run proposed are listed in `data/regler_ops.json` and were left out. It cost 14.12 USD at list
price (propose 13.21, rewrites 0.91). Against the answer key, without soft rules
(`uv run audit.py score --before 9f39874~1`):

| | Before | After |
|---|--:|--:|
| Key rules spread over several pipeline rules (of 19) | 14 | 13 |
| Key events in their home rule | 62.6% | 64.1% |
| Years with the same content in force | 51.5% | 51.5% |
| Missing key events | 17 | 17 |
