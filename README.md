# styrke-referater

Overview of the rules and agreements in Dansk Styrkeløft Forbund (DSF), extracted from the
minutes and rule documents published at <https://styrke.dk/?page=referater>.

**Read the result in [regelsaet/README.md](regelsaet/README.md):**

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
| `--render-only` | only rebuild `regelsaet/` from `data/` (e.g. after editing `render.py`) |
| `--only REGEX` | only extract documents whose id matches (testing) |
| `--extract-model`, `--consolidate-model` | defaults `sonnet` and `opus` |
| `--workers N` | parallel Claude calls (default 4) |

To share a year as Word: `pandoc regelsaet/2026.md -o DSF-regelsaet-2026.docx`.

## How it works

| Step | File | Output |
|---|---|---|
| 1. Download new documents, read sections and dates from the index page | `scrape.py` | `referater/<organ>/`, `data/manifest.json` |
| 2. Extract decisions per document, each with a verbatim quote and page | `analyze.py` | `data/beslutninger/<id>.json` |
| 3. Consolidate decisions per category into rule histories | `analyze.py` | `data/regler/<kategori>.json` |
| 4. Work out which version applied in each year and write Markdown | `render.py` | `regelsaet/` |

`update.py` runs the steps in order. Step 2 reruns for a document when its file changes; step 3 reruns
for a category when its decisions change. After editing a prompt in `analyze.py`, bump
`EXTRACT_VERSION` or `CONSOLIDATE_VERSION` so cached results are recomputed.

Files that disappear from styrke.dk stay in `referater/` and in the analysis, because they still
document the rules of their year.

Quotes that cannot be found verbatim in the document text are flagged ⚠, and
`regelsaet/README.md` ends with data-quality counts. The extraction is automatic: the minutes
are always the authoritative source.

`DSF_Generelt_Regelsaet.docx` and `DSF_Verificeringsrapport.docx` are the earlier manual analysis
(March 2026), kept for reference.
