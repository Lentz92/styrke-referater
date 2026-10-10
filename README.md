# styrke-referater

Overview of the rules and agreements in Dansk Styrkeløft Forbund (DSF), extracted by Claude from the minutes and
rule documents published at <https://styrke.dk/?page=referater>. The minutes are always the authoritative source.

**Website: <https://lentz92.github.io/styrke-referater/>** – the rules in force today and what is on its way, with
search; open a rule for its text and its whole history, with each decision linked to the minutes. The same overview as
Markdown, with each year's rule set, starts at [regelsaet/README.md](regelsaet/README.md).

## Run it

Requires [uv](https://docs.astral.sh/uv/) and a logged-in Claude Code CLI (`claude`); no API key needed.

```bash
uv run -m styrke.update
```

downloads what is new on styrke.dk, has Claude extract and file its decisions, runs the checks and writes
`regelsaet/` and `_site/`. On GitHub the same runs on the 1st of every month: it publishes the website, or opens a
pull request when the checks find errors.

```bash
uv run pytest
```

runs the tests. The code is the package `styrke/`; its dependencies are declared in `pyproject.toml` and pinned in
`uv.lock`, which `uv run` installs into `.venv/`.

## Documentation

- [Running the pipeline](docs/operations.md): options, cost, rebuilds and migrations, the GitHub workflows,
  reviewing a pull request, and the audit.
- [How it works](docs/how-it-works.md): the steps, how decisions and rules keep their ids and links, what code
  checks, and the website's search.
- [Architecture](docs/architecture.md): drawings of how a run decides what is true, and the code in C4 levels 1 to 3.
- [Answer key and measurements](eval/README.md): how the pipeline's choices were measured, against an answer key
  judged once by Opus.

`docs/DSF_Generelt_Regelsaet.docx` and `docs/DSF_Verificeringsrapport.docx` are the earlier manual analysis (March
2026), kept for reference.
