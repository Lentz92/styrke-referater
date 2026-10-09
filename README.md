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

Only new or changed documents are sent to Claude, so a run with nothing new finishes in seconds.
A full rebuild of all ~240 documents costs about 12 USD at list price (about 7 for extraction, 5 for
consolidation), counted against the Claude subscription; a few new minutes cost well under 2.

A run with more than an ordinary month's work stops before calling Claude and says why: more than 10% of
the documents to extract, a category with decisions but no file in `data/regler/`, or more than half the
categories to consolidate again before anything new is extracted (a new `CONSOLIDATE_VERSION`, for example).
If that work is intended, run `uv run update.py --allow-rebuild`. The approval is saved in
`data/rebuild.json` before any Claude call: the documents to extract, the categories to consolidate (plus
those the documents' decisions are in, since re-extracting them changes those categories), and the prompt
versions. When the time or cost limit or failed calls cut the work off, run `uv run update.py` again,
without the flag, until it ends without failures; the monthly run does the same. A later run goes ahead
while everything outside the approval would pass on its own (a few new minutes may arrive meanwhile);
anything more, or another prompt version, needs a new approval. The file is removed once the remaining work
is ordinary. A run that is cut off with much left in an ordinary month (consolidation failing for most
categories after a big meeting) leaves the same kind of approval for what is left.

| Option | Use |
|---|---|
| `--offline` | skip styrke.dk and use the files already in `referater/` |
| `--render-only` | only rebuild `regelsaet/` and `_site/` from `data/` (e.g. after editing `render.py`) |
| `--only REGEX` | only extract documents whose id matches, and only consolidate the categories their decisions are in, before or after extraction (testing); skips the rebuild check |
| `--extract-model`, `--consolidate-model` | defaults `claude-sonnet-5-5` and `claude-opus-5-5`; a full id fails the call if Claude Code answers with another model |
| `--consolidate-mode` | `full` (default): consolidate each category with changed decisions anew; `incremental`: file each new decision into its rule and leave every other rule as it is (see below) |
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
line to `data/runs.jsonl`, so compare cost and tokens at the next run that analyses documents. To approve
a rebuild on GitHub, tick "Allow a rebuild (--allow-rebuild)" under "Run workflow"; the following monthly
runs finish it if it is cut off. A rebuild that rewrites earlier years, or is cut off with rules left out, goes
to review: merge the pull request so the following runs continue from it.

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
(history). Each month with new minutes consolidates whole categories again, and Claude may rewrite rules that
no new decision touches. So for every year page and every rule (by slug; rules merged into one, through
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
`uv run update.py --render-only` and `uv run checks.py` there, before the next monthly run replaces it. Close
the pull request to discard the result, its line in `data/runs.jsonl` included; the next monthly run tries
again. GitHub does not run the tests on a pull request a workflow opened; close and reopen it to run them.

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
for a category when its decisions change.

With `--consolidate-mode incremental`, step 3 does not rewrite whole categories. It takes the decisions the rule
files do not reflect yet (new, changed or retired ids), one document at a time in date order (`incremental.py`).
Code ranks the 15 rules whose words are closest to each new decision (`candidates.py`: TF-IDF over Danish stems and
compound parts, as the website's search; the decision's category weighs in but filters nothing). Three Sonnet votes
put each decision in one of those rules, in a new rule or among the category's one-offs; two of three decide, else
Opus does, blind to the votes. Then one Opus call per touched rule gets its whole history, the new decisions and the
passage of the minutes around each quote, and writes the versions from the new decision on: earlier versions, and
every other rule, stay byte-identical (checked). A decision that belongs before a rule's newest version is placed
where it belongs and the versions after it are rewritten; a retired decision leaves its rule (an Opus call rewrites
what came after it), and a rule left empty is retired with its slug. A document is written only when all its calls
succeed; otherwise the next run tries it again. A category whose decisions are all reflected gets the `input_hash`
of its input, so `full` and the checks see it as consolidated. After editing a prompt in `analyze.py`, bump
`EXTRACT_VERSION` or `CONSOLIDATE_VERSION` so cached results are recomputed (a rebuild, see Update).
Each result records its `provenance`: the model that answered, the CLI version, a fingerprint of the prompt
and schema, and the effort. It is not part of the cache key, so changing the model alone reruns nothing.

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
group the decisions (B-cubed), what each shows in force every year and with which effect, and how each scores against
the rules key (it stops without the key unless `--no-key`): two `full` replays give the noise floor.

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

`DSF_Generelt_Regelsaet.docx` and `DSF_Verificeringsrapport.docx` are the earlier manual analysis
(March 2026), kept for reference.
