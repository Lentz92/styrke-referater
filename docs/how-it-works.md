# How it works

[architecture.md](architecture.md) draws all of this: how a run decides what is true, and the code in C4
levels 1 to 3.

| Step | File | Output |
|---|---|---|
| 1. Download new documents, read sections and dates from the index page | `styrke/scrape.py` | `referater/<organ>/`, `data/manifest.json` |
| 2. Extract decisions per document, each with a verbatim quote and page | `styrke/analyze.py` | `data/beslutninger/<id>.json` |
| 3. File decisions into rule histories | `styrke/incremental.py` (`styrke/analyze.py` in full mode) | `data/regler/<kategori>.json` |
| 4. Work out which version applied in each year and write Markdown | `styrke/render.py` | `regelsaet/` |
| 5. Embed the same rules and what is in force today in one static page with search | `styrke/website.py`, `website/` | `_site/` |

`styrke/update.py` runs the steps in order. Steps 2 and 3 ask Claude through `styrke/claude.py`, which runs `claude -p`
on the subscription, checks that the model asked for answered, keeps the run's cost and time limits and records what
each call used. Step 2 reruns for a document when its file changes, or when it was extracted with another prompt or
model; step 3 takes the decisions the rule files do not reflect yet. A new extraction prompt goes into
`analyze.EXTRACT_PROMPTS` as `v<n>`, where `uv run -m styrke.evaluate extract --prompt v<n>` can measure it against the
answer key; an extraction records the n of the prompt it was made with, so setting `EXTRACT_VERSION` to n makes it the
pipeline's and every document due for extraction with it. After editing the consolidation prompt, bump
`CONSOLIDATE_VERSION`. Either way a migration follows (see [Rebuilds and
migrations](operations.md#rebuilds-and-migrations)). Each result records its `provenance`: the model that answered, the
CLI version, a fingerprint of the prompt and schema, and the effort. Only the extraction's model is part of a cache key:
an extraction is current only when the model in its provenance is the extraction model asked for by full id (an alias
counts for whichever model answers).

Incremental consolidation (`styrke/incremental.py`) takes the decisions the rule files do not reflect yet (new, changed
or retired, or re-dated), one document at a time in date order. Code ranks the 15 rules whose words are closest to each
new decision (`styrke/candidates.py`: TF-IDF over Danish stems and compound parts, as the website's search). Three
Sonnet votes put each decision in one of those rules, in a new rule or among its category's one-offs; two of three
decide, else Opus does. Then one Opus call per touched rule, with the minutes around each new quote, rewrites its
versions from the new decision on: earlier versions, and every other rule, stay byte-identical (checked). A decision the
call finds misfiled is voted on again without that rule, and misfiled twice gets a rule of its own, listed in the run
report. A document is written only when all its calls succeed; otherwise the next run tries it again. Known limit: a
document dated anew only on styrke.dk can leave a category for a full consolidation (the run report says so).

Decisions and rules keep their identity when Claude redoes them, so links never break. When a document is extracted
again, a new decision that matches a previous one by where its quote stands and by its wording (`styrke/matching.py`)
keeps its id (`<document>#<n>`); previous decisions without a match are listed under `retired`, and a number is never
used twice. A rule's slug is its address on the website (`#regel/<slug>`) and in `regelsaet/`
(`regler/<område>.md#regel/<slug>`) and stays whatever its title (a full consolidation gives each rule the slug of
the previous rule it shares most decisions with). `data/slugs.json` keeps every slug no rule holds any more, with the
title and decisions it last stood for, and leads it to the rule now holding most of those decisions (or else one with
the same title), which the website then opens; a slug with neither is retired until one turns up. None is ever given
to another rule.

Files that disappear from styrke.dk stay in `referater/` and in the analysis, because they still document the rules of
their year; when their link stops working, they are cited without one. A file that styrke.dk replaces under the same
name (a different size) is downloaded again and re-analysed.

Code checks Claude's work where it can:

- Each quote is looked up in the document text, allowing for line-break hyphenation and a page header inside it (at
  least 80% of its word triplets in place). One that cannot be found is flagged ⚠; one found only on another page
  than Claude cited gets the page where it actually stands.
- Decisions within a document are ordered by where their quote stands.
- Each rule version stores a fingerprint of the decision it was built from. If the document is re-analysed and the
  decision changes, the whole rule is left out until its category is consolidated again (leaving out only that
  version could bring back a repealed rule).
- The effect of an undecided, rejected or withdrawn proposal follows from its outcome, whatever Claude says.
- `uv run -m styrke.checks` lists the errors and warnings `data/` shows on its own (see [Review](operations.md#review))
  and exits 1 on an error; `regelsaet/README.md` ends with their counts. The extraction is automatic: the minutes are
  always the authoritative source.

## The Markdown pages

The Markdown overview starts at `regelsaet/README.md`:

- `regelsaet/<år>.md` – a short page per year: what was new that year, what is adopted but not yet
  in force, then one line per rule in force, grouped in seven areas, each with a link to the minutes.
- `regelsaet/regler/<område>.md` – one page per area (medlemskab, stævner, dommere, elite, master,
  antidoping, organisation) with every rule's full text, who adopted it with which votes, and its
  history.

## Website

The page shows the rules as of the day it was built (the date in its footer): the latest meetings' decisions, the
rules in force per area, what is adopted but not yet in force ("På vej"), and per rule its text, where it stands and its
whole history, each decision linked to the minutes. What applied in an earlier year is on that year's Markdown page.

The search runs in the browser (`website/search.js`): [MiniSearch](https://github.com/lucaong/minisearch) ranks with
BM25 and tolerates typos and word starts, a Danish Snowball stemmer (same output as Python's `snowballstemmer`)
matches word forms, compound words are also indexed by their parts ("licensgebyr" → "licens" + "gebyr"), and
`website/synonyms.json` is a hand-written thesaurus: each group lists words that should find each other (VM ↔
verdensmesterskab, kontingent ↔ gebyr ↔ pris …). Add a group when a search misses something it should find, then
push; the site rebuilds itself. `website/vendor/minisearch.js` is MiniSearch 7.2.0 copied from npm.

Visits are counted without cookies by [GoatCounter](https://www.goatcounter.com/); the dashboard is
<https://lentz92.goatcounter.com> (log in as the account owner). Opened rules show up there as events named
`regel/<slug>`.
