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
"Run workflow" in the Actions tab) and commits any changes to `main`. Claude runs on the Claude
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
runs finish it if it is cut off.

Each run that calls Claude adds a line to `data/runs.jsonl`: time, CLI version and, per step, calls,
failures, skipped calls, tokens, list-price cost, models and seconds. The Actions run page shows the same
numbers with the counts from the checks below.

`.github/workflows/pages.yml` then rebuilds the website and publishes it on GitHub Pages. It runs after
each monthly update, on pushes that change `data/`, `website/` or the scripts, and via "Run workflow".
It does not call Claude.

## How it works

| Step | File | Output |
|---|---|---|
| 1. Download new documents, read sections and dates from the index page | `scrape.py` | `referater/<organ>/`, `data/manifest.json` |
| 2. Extract decisions per document, each with a verbatim quote and page | `analyze.py` | `data/beslutninger/<id>.json` |
| 3. Consolidate decisions per category into rule histories | `analyze.py` | `data/regler/<kategori>.json` |
| 4. Work out which version applied in each year and write Markdown | `render.py` | `regelsaet/` |
| 5. Embed the same rules and per-year state in one static page with search | `website.py`, `website/` | `_site/` |

`update.py` runs the steps in order. Step 2 reruns for a document when its file changes; step 3 reruns
for a category when its decisions change. After editing a prompt in `analyze.py`, bump
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
- `checks.py` reports versions where the extraction and the rule disagree about the effect, possible
  date traps (a seasonal rule confirmed without an end date, a newer decision that takes effect before an
  older one), and decision ids or slugs that are missing, used twice or lead nowhere.

`update.py` logs every finding, and `regelsaet/README.md` ends with the counts. The extraction is
automatic: the minutes are always the authoritative source.

### Tests

```bash
uv run --with-requirements tests/requirements.txt pytest
```

`.github/workflows/tests.yml` runs them, and a render-only build, on every pull request.

`DSF_Generelt_Regelsaet.docx` and `DSF_Verificeringsrapport.docx` are the earlier manual analysis
(March 2026), kept for reference.
