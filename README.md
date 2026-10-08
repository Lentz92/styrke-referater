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

Requires [uv](https://docs.astral.sh/uv/) and a logged-in Claude Code CLI (`claude`). No API key needed.

```bash
uv run update.py
```

Only new or changed documents are sent to Claude, so a run with nothing new finishes in seconds.
A full rebuild of all ~240 documents costs about 12 USD at list price (about 7 for extraction, 5 for
consolidation), counted against the Claude subscription; a few new minutes cost well under 2.

| Option | Use |
|---|---|
| `--offline` | skip styrke.dk and use the files already in `referater/` |
| `--render-only` | only rebuild `regelsaet/` and `_site/` from `data/` (e.g. after editing `render.py`) |
| `--only REGEX` | only extract documents whose id matches (testing) |
| `--extract-model`, `--consolidate-model` | defaults `sonnet` and `opus` |
| `--workers N` | parallel Claude calls (default 4) |
| `--time-budget MIN` | start no new Claude calls after this many minutes (default 75); the rest runs next time |

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
`EXTRACT_VERSION` or `CONSOLIDATE_VERSION` so cached results are recomputed.

Files that disappear from styrke.dk stay in `referater/` and in the analysis, because they still
document the rules of their year; when their link stops working, they are cited without one. A file
that styrke.dk replaces under the same name (a different size) is downloaded again and re-analysed.

Code checks Claude's work where it can:

- Each quote is looked up in the document text. One that cannot be found verbatim is flagged ⚠; one
  found on another page than Claude cited gets the page where it actually stands.
- Decisions within a document are ordered by where their quote stands.
- Each rule version stores a fingerprint of the decision it was built from. If the document is
  re-analysed and the decision changes, the version is left out until its category is consolidated again.
- The effect of an undecided, rejected or withdrawn proposal follows from its outcome, whatever Claude says.
- `checks.py` reports versions whose effect contradicts the decision and possible date traps (a
  seasonal rule confirmed without an end date, a newer decision that takes effect before an older one).

`update.py` logs every finding, and `regelsaet/README.md` ends with the counts. The extraction is
automatic: the minutes are always the authoritative source.

### Tests

```bash
uv run --with-requirements tests/requirements.txt pytest
```

`.github/workflows/tests.yml` runs them, and a render-only build, on every pull request.

`DSF_Generelt_Regelsaet.docx` and `DSF_Verificeringsrapport.docx` are the earlier manual analysis
(March 2026), kept for reference.
