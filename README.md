# styrke-referater

Overview of the rules and agreements in Dansk Styrkeløft Forbund (DSF), extracted from the
minutes and rule documents published at <https://styrke.dk/?page=referater>.

**Website: <https://lentz92.github.io/styrke-referater/>** – search the rules, pick a year to see what
applied then, and open a rule for its text and its history, with each decision linked to the minutes.

The same overview as Markdown, starting at [regelsaet/README.md](regelsaet/README.md):

- `regelsaet/<år>.md` – a short page per year: what was new that year, what is adopted but not yet
  in force, then one line per rule in force, grouped in seven areas, each with a link to the minutes.
- `regelsaet/regler/<område>.md` – one page per area (medlemskab, stævner, dommere, elite, master,
  antidoping, organisation) with every rule's full text, who adopted it with which votes, and its
  history.

## Update

Requires [uv](https://docs.astral.sh/uv/) and a logged-in Claude Code CLI (`claude`), preferably the version
pinned for the monthly run (below). No API key needed.

```bash
uv run update.py
```

Only new or changed documents are sent to Claude, so a run with nothing new finishes in seconds. Their
decisions are then filed into the rules they belong to, and every other rule is left as it is (incremental
consolidation, the default; see How it works).
Opus reads each document with extraction prompt v3 (see Extraction model and prompt): about 0.09 USD per document at
list price, the mean measured on the answer key's documents (the large congress documents cost more), counted against
the Claude subscription. A month with a few new minutes costs well under 2 USD, consolidation included; a full rebuild
of all ~240 documents about 27 (about 22 for extraction, 5 for consolidation).

A run with more than an ordinary month's work stops before calling Claude and says why: more than 10% of
the documents to extract, a category with decisions but no file in `data/regler/`, or more than half the
categories to consolidate again before anything new is extracted. Before it stops or goes ahead, it logs the work's
estimated cost at list price: each document at the mean cost of one extraction by the chosen model measured in
`eval/runs.jsonl` (with the chosen prompt when measured), in full mode each category at its share of the last full
consolidation in `data/runs.jsonl` (the 5 USD above until one is logged), and whether `--max-cost` will cut the run
off. If that work is intended, run `uv run update.py --allow-rebuild`. A new prompt or
`EXTRACT_VERSION`/`CONSOLIDATE_VERSION` is migrated with
`uv run update.py --offline --consolidate-mode full --allow-rebuild --max-cost 40 --time-budget 150`, which
consolidates the changed categories anew, offline so no new minutes arrive meanwhile (the next run adds them), with
limits a re-extraction of every document fits in; the incremental default cannot migrate rules made with another
`CONSOLIDATE_VERSION`, or more than 10% of the documents extracted with another prompt or model, and stops until that
is done, naming this command.
The approval is saved in `data/rebuild.json` before any Claude call: the documents to extract, the categories to
consolidate (plus those the documents' decisions are in, since re-extracting them changes those categories), the
prompt versions, the extraction prompt, model and effort, and the consolidation mode, model and effort. When the time
or cost limit or failed calls cut the work off, run `uv run update.py` again, without the flags, until it ends without
failures; the monthly run does the same. An approval holds for the extraction prompt it was given for only, so one
given with `--extract-prompt` for another prompt than the default is continued with that option, not by plain runs.
A run with its prompt and without `--consolidate-mode` continues an unfinished approval in the mode it was given for
(one saved before modes were recorded counts as full), and always with the extraction model and effort and the
consolidation model and effort it was approved with: a run asking for others is refused and told what to run,
including the command that approves the work anew with its settings (with another extraction model it would extract
again what the work extracted already). A cut-off full migration is
continued in full only for what it approved (the categories still on the old version, the approved ones and those of
approved documents); new minutes in other categories are filed incrementally in the same run once the migration is
finished, and wait until then, listed in the run report. Ticking "Allow a rebuild" while such a migration is open
approves nothing more. A later run goes ahead while everything outside the approval would pass on its own (a few new
minutes may arrive meanwhile); anything more, or another prompt version, needs a new approval. The file is removed
once the approved documents are extracted, the remaining work is ordinary and no category waits for a full
migration; the runs after that consolidate incrementally. A run that is cut off with much left in an ordinary month
(consolidation failing for most categories after a big meeting) leaves the same kind of approval for what is left.

| Option | Use |
|---|---|
| `--offline` | skip styrke.dk and use the files already in `referater/` |
| `--render-only` | only rebuild `regelsaet/` and `_site/` from `data/` (e.g. after editing `render.py`) |
| `--only REGEX` | only extract documents whose id matches, and only consolidate the categories their decisions are in, before or after extraction (testing); skips the rebuild check |
| `--extract-model`, `--consolidate-model` | both default to `claude-opus-5-5`, or to those the open approval in `data/rebuild.json` was given with; a full id fails the call if Claude Code answers with another model |
| `--extract-prompt` | the extraction prompt, `v3` (default) or `v2` (`analyze.EXTRACT_PROMPTS`, kept for evaluation); another than the default extracts every document again, a migration (see Extraction model and prompt) |
| `--consolidate-mode` | `incremental` (default): file each new decision into its rule and leave every other rule as it is; `full`: consolidate each category with changed decisions anew, to migrate a prompt or version change (with `--allow-rebuild`) |
| `--assign-model` | incremental mode: the model that votes on which rule a new decision belongs to (default `claude-sonnet-5-5`) |
| `--extract-effort`, `--consolidate-effort` | `low` … `max`; default is Claude Code's own |
| `--workers N` | parallel Claude calls (default 4) |
| `--time-budget MIN` | start no new Claude calls after this many minutes (default 75); the rest runs next time |
| `--max-cost USD` | start no new Claude calls once the run has used this much at list price (default 15) |
| `--allow-rebuild` | approve work the rebuild check stops (see above) |

To preview the website locally, open `_site/index.html` after a run, or build only the site with
`uv run website.py`.

### Search

The search runs in the browser (`website/search.js`): [MiniSearch](https://github.com/lucaong/minisearch)
ranks with BM25 and tolerates typos and word starts, a Danish Snowball stemmer (same output as Python's
`snowballstemmer`) matches word forms, compound words are also indexed by their parts
("licensgebyr" → "licens" + "gebyr"), and `website/synonyms.json` is a hand-written thesaurus: each group
lists words that should find each other (VM ↔ verdensmesterskab, kontingent ↔ gebyr ↔ pris …). Add a group
when a search misses something it should find, then push; the site rebuilds itself.
`website/vendor/minisearch.js` is MiniSearch 7.2.0 copied from npm.

Visits are counted without cookies by [GoatCounter](https://www.goatcounter.com/); the dashboard is
<https://lentz92.goatcounter.com> (log in as the account owner). Opened rules show up there as events
named `regel/<slug>`.

To share a year as Word: `pandoc regelsaet/2026.md -o DSF-regelsaet-2026.docx`.

### Monthly run on GitHub

`.github/workflows/update.yml` runs `update.py` at 06:00 UTC on the 1st of every month (or via
"Run workflow" in the Actions tab) and commits any changes to `main`, or to a pull request when the checks
find errors (see Review below). Claude runs on the Claude
subscription through the repository secret `CLAUDE_CODE_OAUTH_TOKEN`; the workflow refuses to run if
an `ANTHROPIC_API_KEY` is present, since that would be billed as API usage. The token is valid for one
year. To create or renew it:

```bash
claude setup-token
GH_TOKEN=$(gh auth token --user Lentz92) gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo Lentz92/styrke-referater
```

The workflow installs a pinned Claude Code version (`CLAUDE_CODE_VERSION` in `update.yml`, with auto-update
off) and stops if another one is installed: the version decides which model an alias means and what list
price it reports (2.1.289 got Haiku's price wrong by about 100×). Its smoke test also checks that a full
model id is answered by that model. To upgrade, change the version there and run the workflow by hand; it
fails if the new version is not installed or answers with another model. A run with nothing new writes no
line to `data/runs.jsonl`, so compare cost and tokens at the next run that analyses documents. The workflow passes
no extraction model or prompt, so it extracts with the defaults (Opus, prompt v3). To approve a rebuild on GitHub,
tick "Allow a rebuild (--allow-rebuild)" under "Run workflow", and to migrate a prompt or version change also
"Consolidate in full, to migrate a prompt or version change (--consolidate-mode full)": both ticked are the CI way to
run the migration. It runs with the default limits (15 USD, 75 minutes), so a re-extraction of every document (about
27 USD) is cut off; the following monthly runs (or "Run workflow" with nothing ticked) finish it in the mode and with
the settings it was approved with, then go on incrementally. A rebuild that rewrites earlier years, or is cut off with
rules left out, goes to review: merge the pull request so the following runs continue from it.

Each run that calls Claude adds a line to `data/runs.jsonl`: time, CLI version and, per step, calls,
failures, skipped calls, tokens, list-price cost, models and seconds. The Actions run page shows the run
report: the same numbers, every error and warning from the checks below, and what changed in earlier years.

`.github/workflows/pages.yml` then rebuilds the website from `main` and publishes it on GitHub Pages. It runs
after a successful monthly update that changed `main`, on pushes that change `data/`, `website/` or the
scripts, and via "Run workflow". It runs `uv run checks.py` first and publishes nothing when it finds an error,
so the site stays as it was. It does not call Claude.

#### Review

`update.py` ends with the checks and writes their outcome to `run-report.md` (not committed). Errors are
results the pipeline does not trust: a rule left out because a decision behind it changed (stale), a decision
the consolidation neither put in a rule nor left out as a one-off (unassigned), decision ids or slugs that are
missing, used twice or lead nowhere (identity), and a run that changes what applied in earlier years
(history). A run updates only the rules its new or changed decisions touch, but a late decision rewrites a rule from
where it belongs, and a full consolidation (a migration) rewrites whole categories, where Claude may change rules no
new decision touches. So for every year page and every rule (by slug; rules merged into one, through
`data/slugs.json`, count together), `update.py` compares the decision in force, the one that adopted its
content, and its wording before and after the analysis. A rule may change from the earliest date of its own
decisions that the run added, changed or removed, or that its last consolidation had not seen (a call that
failed); a difference in an earlier year is an error, or a warning when only the wording changed. A history
check that fails is an error too. The effect and date findings are warnings.

| `update.py` exits | The workflow |
|---|---|
| 0: no errors | commits to `main` and closes a review pull request left open; the website is rebuilt |
| 3: errors | force-pushes the result to the branch `auto/update` and opens the pull request "Monthly update needs review" with the run report as its body, or updates the open one; `main` and the website stay as they are |
| 1: the run failed | commits the partial results to `main` when its run report says the checks passed, so the next run continues from them, else does as for 3; the run is marked failed and the website is not rebuilt |

A result that cannot be pushed to `main` because it moved during the run goes to review as well. A run started
by hand on another branch uses `auto/update-<branch>` and a pull request into that branch.

There is never more than one review branch or pull request: every run starts from `main`, and a later one
replaces the branch and the report. To review, read the errors and the history table, and check the rules that
changed against the minutes (the diff of `regelsaet/` shows them as text). When the only errors are history,
merge to accept the result: the push to `main` rebuilds the website (history is checked only by `update.py`, so
merging accepts what changed there). Other errors must be fixed first, since the website workflow runs
`uv run checks.py` and publishes nothing while it finds one: push the fix to the branch after running
`uv run update.py --render-only` and `uv run checks.py` there, before the next monthly run replaces it. The
exception is approved work left unfinished in `data/rebuild.json` (the run report says so): a migration or rebuild
that is cut off leaves rules out (stale) until a later run finishes it, so merge the pull request, and the following
runs continue it from `main`; the website stays as it was until the checks pass, and closing would throw away what the
work has cost so far. Close the pull request to discard the result, its line in `data/runs.jsonl` included; the next
monthly run tries again. GitHub does not run the tests on a pull request a workflow opened; close and reopen it to
run them.

Opening the pull request needs "Allow GitHub Actions to create and approve pull requests" under Settings >
Actions > General > Workflow permissions. The routing is `.github/scripts/route-update.sh`.

## How it works

| Step | File | Output |
|---|---|---|
| 1. Download new documents, read sections and dates from the index page | `scrape.py` | `referater/<organ>/`, `data/manifest.json` |
| 2. Extract decisions per document, each with a verbatim quote and page | `analyze.py` | `data/beslutninger/<id>.json` |
| 3. Consolidate decisions per category into rule histories | `analyze.py` | `data/regler/<kategori>.json` |
| 4. Work out which version applied in each year and write Markdown | `render.py` | `regelsaet/` |
| 5. Embed the same rules and per-year state in one static page with search | `website.py`, `website/` | `_site/` |

`update.py` runs the steps in order. Step 2 reruns for a document when its file changes; step 3 reruns
for a category when its decisions change. A new extraction prompt goes into `analyze.EXTRACT_PROMPTS` as `v<n>`, and
an extraction records the n of the prompt it was made with as its version, so `--extract-prompt` (and
`evaluate.py extract --prompt`) can run it while the cache stays valid for the default; setting `EXTRACT_VERSION` to n
makes it the pipeline's. After editing the consolidation prompt, bump `CONSOLIDATE_VERSION`. Either way cached
results are recomputed (a rebuild, see Update).
Each result records its `provenance`: the model that answered, the CLI version, a fingerprint of the prompt
and schema, and the effort. Only the model is part of a cache key, the extraction's: an extraction is current only when
the model in its provenance is the extraction model asked for by full id (an alias counts for whichever model answers),
so a document extracted by another model, say in a test run with `--only` and `--extract-model`, is extracted again by
the next run.

By default (incremental consolidation), step 3 does not rewrite whole categories. It takes the decisions the rule
files do not reflect yet (new, changed or retired ids, or a decision whose date or organ changed), one document at a
time in date order (`incremental.py`). Code ranks the 15 rules whose words are closest to each new decision
(`candidates.py`: TF-IDF over Danish stems and compound parts, as the website's search; the decision's category weighs
in but filters nothing). Three Sonnet votes put each decision in one of those rules, in a new rule or among its
category's one-offs (what the extraction prompt lists as no decision); two of three decide, else Opus does, blind to
the votes, and also when a "new" rule would be named like a live one. Then one Opus call per touched rule gets its
whole history, the new decisions and the minutes from 100 words before each quote to its vote count (or 400 words
after), and writes the versions from the new decision on: earlier versions, and every other rule, stay byte-identical
(checked). A decision that belongs before a rule's newest version is placed where it belongs and the versions after it
are rewritten; a retired decision leaves its rule (an Opus call rewrites what came after it), and a rule left empty is
retired with its slug. When the call finds a new decision misfiled, the votes are asked once more without that rule;
misfiled again, it gets a rule of its own, listed in the run report for review.
A document is written only when all its calls succeed; otherwise the next run tries it again. Each rule file records a
fingerprint of every decision it reflects (`inputs`); a category whose decisions are all reflected gets the
`input_hash` of its input, so `full` and the checks see it as consolidated, and one that may have changed unseen is
left for `full`. A decision filed under another category's rule is consolidated with that category by `full` too, so
no decision lands in two rules (`checks.py` reports one that does).

The gate runs on today's data (`eval/reports/compare-*.md`, holding out the 20 newest and 10 random documents) chose
this default: against the answer key, incremental and full show the same content in force (51.5% vs 51.9% of years),
while two incremental runs agree on what was in force in 99.1% of years, two full ones in 87.6%.

Known limits: a document dated anew only on styrke.dk can leave a category for a full consolidation (the run report
says so), and a new prompt or `CONSOLIDATE_VERSION` takes a full consolidation (see Update). Until October 2026 the
extraction prompt was v2, under which a decision that set several rules at once (a budget setting several fees) was one
decision, held by one rule; v3, in force since, gives one decision per rule (see Extraction model and prompt).

Decisions and rules keep their identity when Claude redoes them, so links never break. Each decision has a
stored id (`<document>#<n>`). When a document is extracted again, each new decision is matched to a previous
one by where its quote stands and by its wording (`matching.py`) and keeps that id; the others get new
numbers, and previous decisions without a match are listed under `retired` in the file. A number is never
used twice. Each rule has a stored slug, its address on the website (`#regel/<slug>`) and in `regelsaet/`
(`regler/<område>.md#regel/<slug>`). When a category is consolidated again, a rule keeps the slug of the
previous rule it shares most decisions with, whatever its title. `data/slugs.json` keeps every slug no rule
holds any more, with the title and decisions it last stood for. After each consolidation run, each is led to
the rule, in any category, holding most of those decisions (or else one with the same title), and the website
opens that rule for it; a slug with neither is retired until one turns up. None is ever given to another rule.

Files that disappear from styrke.dk stay in `referater/` and in the analysis, because they still
document the rules of their year; when their link stops working, they are cited without one. A file
that styrke.dk replaces under the same name (a different size) is downloaded again and re-analysed.

Code checks Claude's work where it can:

- Each quote is looked up in the document text, allowing for line-break hyphenation and a page header
  inside it (at least 80% of its word triplets in place). One that cannot be found is flagged ⚠; one
  found only on another page than Claude cited gets the page where it actually stands.
- Decisions within a document are ordered by where their quote stands.
- Each rule version stores a fingerprint of the decision it was built from. If the document is
  re-analysed and the decision changes, the whole rule is left out until its category is consolidated
  again (leaving out only that version could bring back a repealed rule).
- The effect of an undecided, rejected or withdrawn proposal follows from its outcome, whatever Claude says.
- `checks.py` reports errors: rules left out as above, decisions in no rule that the consolidation did not
  leave out as one-offs, and decision ids or slugs that are missing, used twice or lead nowhere; and
  warnings: versions where the extraction and the rule disagree about the effect, and possible date traps (a
  seasonal rule confirmed without an end date, a newer decision that takes effect before an older one).
  `uv run checks.py` lists them and exits 1 on an error. `update.py` also checks what a run changed in earlier
  years (see Review).

`update.py` logs every finding, and `regelsaet/README.md` ends with the counts of those `data/` shows on its
own. The extraction is
automatic: the minutes are always the authoritative source.

### Tests

```bash
uv run --with-requirements tests/requirements.txt pytest
```

`.github/workflows/tests.yml` runs them, and a render-only build, on every pull request.

### Evaluation

Two extraction runs agree on only ~90% of decisions, so comparing runs cannot tell better from different.
`evaluate.py` builds an answer key once, judged by Opus, and scores any later change against it. Everything it
writes is under `eval/`; it never touches `data/` or `regelsaet/`.

```bash
uv run evaluate.py select                    # ~20 recurring rules and ~25 documents (eval/selection.json)
uv run evaluate.py extract --name stored     # today's extractions as a run; other names call Claude
uv run evaluate.py key-decisions --max-cost 10 --docs rep2013,elite18052021
uv run evaluate.py key-rules --max-cost 10 --rules licensgebyr,årsafgift
uv run evaluate.py score --run stored --run sonnet-1
uv run evaluate.py score-rules               # data/regler, or --rules-dir/--decisions-dir
```

The decisions key clusters the decisions of several runs per document (`--runs`, default `stored sonnet-1
sonnet-2 haiku-1 opus-1`); two judges keep, reject or merge each candidate and add what every run missed, by the
extraction prompt's own criteria. That a decision exists and which fields it has are judged apart, and field
accuracy is scored only on the fields the judges agree on. The rules key gives two judges passages from all
documents (around the rule's decisions, other decisions using its title's words, and its keywords, amounts first)
and asks for its true timeline and what was in force each year, or that it is unknown. Where two judges disagree, a
third answers blind, and two of three decide; what is still split is left out of the scores.

The incremental consolidation (above) has three gates of its own, which only read `data/`:

```bash
uv run evaluate.py candidate-recall          # hide each decision from its rule: is the rule among the top K?
uv run evaluate.py replay --holdout newest:20,random:10 --seed 1 --name inc-1 --max-cost 20
uv run evaluate.py compare-rules eval/replays/inc-1/data/regler data/regler
```

`replay` copies `data/` to `eval/replays/<name>/data`, removes the held-out documents' decisions from the rules there
(rules left empty go with their slugs), and consolidates them again on the copy (`--mode incremental`, or `full`, which
redoes their categories); a cut-off replay continues where it stopped. `compare-rules` reports how two rule directories
group the decisions (B-cubed), what each shows in force every year and with which effect, the same over each holdout
part of a replay (`random:` mostly tests decisions filed before a rule's newest version), and how each scores against
the rules key, per rule (it stops without the key unless `--no-key`): two `full` replays give the noise floor.
`candidate-recall` also ranks each hidden decision as if its category were another one.

Every command that calls Claude takes `--max-cost` and `--pilot N` or `--docs`/`--rules`, prints how many calls it
plans, and adds a line to `eval/runs.jsonl`; the scores go to `eval/reports/`. Every answer is kept, so a cut-off
build or a pilot is never paid twice, and the rules judges are told the selection's date, not today's. An answer
kept for other input (changed data, prompt, model or effort) stops the command until `--rejudge` (or `--force` for
extractions) says to pay for a new one. `--rederive` rebuilds the keys from the kept answers alone (the rules
keys on the passages they were judged on), never calling Claude, e.g. after a change to how answers are agreed.

Errors a check against the minutes finds in the judges' answers go to `eval/key/corrections.json`, each with its
reason and evidence (document and quote). They are applied on top of the judges whenever a key is written or
scored, and listed in the reports. A rule marked `soft` in `eval/selection.json` (loosely scoped, so which
decisions are its events is arbitrary) is reported but left out of the overall rules figures.

**Extraction model and prompt.** The pipeline extracts with Opus (`update.EXTRACT_MODEL`) and prompt v3
(`analyze.EXTRACT_VERSION`), chosen with the gate below in October 2026; until then it was Sonnet with prompt v2.
Measured on the decisions key (25 documents, 193 certain decisions;
`eval/reports/decisions-stored+sonnet-1+sonnet-2+haiku-1+opus-1.md`):

| Run | Recall | Precision | All four fields | Three (no handling) | Stability |
|---|--:|--:|--:|--:|--:|
| stored (`data/` then, Sonnet, v2) | 79.8% | 93.9% | 63.8% | 75.2% | – |
| fresh Sonnet, v2, two runs | 67.4% / 71.5% | 92.2% / 93.9% | 69.2% / 73.5% | 81.5% / 85.4% | 91.7% |
| Haiku, v2 | 91.7% | 92.7% | 44.6% | 54.2% | – |
| Opus, v2 | 91.2% | 96.7% | 93.7% | 95.4% | – |

Sonnet misses a fifth to a third of the decisions, mostly in the large congress documents. And v2 extracts a budget
line that sets or confirms several fees as one decision, which only one rule can hold, so the licence fee's yearly
confirmations end up in "Årsafgift": a main cause of the rules key's 51.9% years with the same content in force. Prompt
v3 (`analyze.EXTRACT_PROMPTS["v3"]`) is v2 plus one field rule (`analyze.EXTRACT_SPLIT_RULE`) that applies the key's
own granularity: one decision per rule, so a budget line setting or confirming different fees (licens, årsafgift,
startgebyr) gives one decision per fee, while the tiers of one fee and a list adopted as a whole for one rule (a
season's entry deadlines) stay one decision; a fee restated unchanged with the budget is handling bekraeftelse. The
key's judges keep v2's rules (`evaluate.KEY_PROMPT`), whose granularity notes already say this, so the key stays as
it is. Each candidate configuration is run twice (with v2 a run cost about 0.95 USD with
Sonnet and 2.35 with Opus):

```bash
uv run evaluate.py extract --name sonnet-v3-1 --model claude-sonnet-5-5 --prompt v3 --max-cost 3
uv run evaluate.py extract --name opus-v3-1 --model claude-opus-5-5 --prompt v3 --max-cost 5   # and -2 of each
uv run evaluate.py score --run stored --run sonnet-1 --run sonnet-2 --run sonnet-v3-1 --run sonnet-v3-2 \
    --run opus-v3-1 --run opus-v3-2
```

`score` gives each configuration scored with two runs (the same model, prompt fingerprint and effort in their
provenance) its stability: the share of the two runs' decisions matched one to one (the matcher that carries decision
ids over), per document and pooled. The key cannot show it, and an unstable extraction rewrites rules every time a
document is extracted again. The gate, in the report: a configuration passes when its worse run is at least the stored
run on recall and on the three fields, at most 2 points below it on precision and at most 2 points above it on
over-split (a prompt that splits more than the key asks for would otherwise pass on recall), and its two runs are at
least as stable as two runs of the pipeline's configuration (`evaluate.pipeline_configuration`: Sonnet with v2 when v3
was chosen, Opus with v3 since). The result
(`eval/reports/decisions-stored+sonnet-1+sonnet-2+sonnet-v3-1+sonnet-v3-2+opus-1+opus-v3-1+opus-v3-2.md`), each
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

The move is one migration, which extracts every document again with v3 and Opus (the defaults), carries the decision
ids over and consolidates every category in full:

```bash
uv run update.py --offline --consolidate-mode full --allow-rebuild --max-cost 40 --time-budget 150
```

It logs the estimate first (about 27 USD at list price: 236 documents at 0.093 USD, the mean of the Opus v3 runs in
`eval/runs.jsonl`, and 5 for the consolidation). Until it has run, the monthly run stops before any Claude call on the
data extracted with v2 and names this command (incremental consolidation refuses documents extracted with another
prompt: updating every rule one by one would cost more and leave the rules less clean). On GitHub, tick both boxes
under "Run workflow" (see Monthly run on GitHub). A run cut off before every category is consolidated leaves rules out
(stale), so its result goes to review; plain runs continue it with the approved settings until it ends without
failures.

A later configuration that passes the gate is migrated the same way before it becomes the default: run the command
above with `--extract-prompt v<n>` and `--extract-model <model>`, continue a cut-off run with
`uv run update.py --extract-prompt v<n>` (the approval supplies the model and the full mode; a plain run stops instead
of extracting everything back with the default prompt, and one with another model stops instead of mixing the two,
both naming the command that continues), then commit the data with `EXTRACT_VERSION` (and `EXTRACT_MODEL` in
`update.py`) changed. Or change those first and run the command as it stands, as was done for v3.

`DSF_Generelt_Regelsaet.docx` and `DSF_Verificeringsrapport.docx` are the earlier manual analysis
(March 2026), kept for reference.
