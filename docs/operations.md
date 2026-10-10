# Running the pipeline

How to run the monthly update locally and on GitHub, review what it sends to a pull request, and run the audit.

## Monthly update

Requires [uv](https://docs.astral.sh/uv/) and a logged-in Claude Code CLI (`claude`), preferably the version
pinned for the monthly run (see [Monthly run](#monthly-run)). No API key needed. Run every command from the
repository root: the package `styrke/` is found there.

```bash
uv run -m styrke.update
```

A run downloads what is new on styrke.dk, has Claude extract the decisions of new or changed documents (Opus with
extraction prompt v3), files each new decision into the rule it belongs to and leaves every other rule as it is
(incremental consolidation, see [How it works](how-it-works.md)), runs the checks (see [Review](#review)) and writes `regelsaet/` and `_site/`. A
run with nothing new finishes in seconds. Claude runs on the subscription; at list price an extraction costs about
0.07 USD per document (the migration in `data/runs.jsonl`: 16.74 USD for 236; the large congress documents cost
more), so a month with a few new minutes costs well under 2 USD, and extracting and consolidating all ~240 documents
again about 25 (the migration in October 2026: 16.74 for extraction, 8.58 for consolidation).

| Option | Use |
|---|---|
| `--offline` | skip styrke.dk and use the files already in `referater/` |
| `--render-only` | only rebuild `regelsaet/` and `_site/` from `data/` (e.g. after editing `styrke/render.py`) |
| `--only REGEX` | only extract documents whose id matches, and only consolidate the categories their decisions are in (testing); skips the rebuild guard |
| `--consolidate-mode` | `incremental` (default): file each new decision into its rule; `full`: consolidate each category with changed decisions anew, to migrate a prompt or version change |
| `--workers N` | parallel Claude calls (default 4) |
| `--time-budget MIN` | start no new Claude calls after this many minutes (default 75); the rest runs next time |
| `--max-cost USD` | start no new Claude calls once the run has used this much at list price (default 15) |
| `--allow-rebuild` | let this run do the work the rebuild guard stops |

The models, the prompt and the effort are not options: Opus extracts and consolidates and Sonnet votes
(`update.EXTRACT_MODEL`, `CONSOLIDATE_MODEL`, `ASSIGN_MODEL`, full ids, so a call answered by another model fails),
with extraction prompt v3 (`analyze.EXTRACT_VERSION`), at Claude Code's default effort.

To preview the website, open `_site/index.html` after a run, or build only the site with `uv run -m styrke.website`. To
share a year as Word: `pandoc regelsaet/2026.md -o DSF-regelsaet-2026.docx`.

### Rebuilds and migrations

A run with more than an ordinary month's work stops before calling Claude and says why: more than 10% of the
documents to extract, a category with decisions but no file in `data/regler/`, or more than half the categories
whose rules must be redone (new decisions waiting to be filed do not count). The refusal then says how to proceed: if
the work is intended, run with `--allow-rebuild`, which stops at `--max-cost` (15 USD by default), or, where a reason
says only a full consolidation takes the work in, the migration command below, which stops at 40 USD. It also names
the boxes to tick on GitHub and how to finish a run that is cut off.

A new extraction prompt or model, or a new `CONSOLIDATE_VERSION`, is migrated with

```bash
uv run -m styrke.update --offline --consolidate-mode full --allow-rebuild --max-cost 40 --time-budget 150
```

It extracts again every document extracted with another prompt or model (decision ids are carried over) and
consolidates each changed category anew, offline so no new minutes arrive meanwhile (the next run adds them), with
limits a re-extraction of every document fits in. Incremental mode cannot migrate rules made with another
`CONSOLIDATE_VERSION`, or more than 10% of the documents extracted with another prompt or model: until the migration
is done, a plain run stops before any Claude call and names this command. To migrate to another prompt or model,
change `analyze.EXTRACT_VERSION` or `update.EXTRACT_MODEL` (or, for the consolidation, `CONSOLIDATE_VERSION`) first
and run the command as it stands, as was done for v3.

`--allow-rebuild` applies only to the run it is given to. A rebuild or migration cut off by its time or cost limit, or
by failed calls, is finished by running the same command again: extractions made with its prompt and model are current,
and a full consolidation skips each category consolidated with today's input, so it does only the rest. Until then a
plain run stops before any Claude call while more than an ordinary month's work is left. The refusal and the cut-off
run's report say exactly what to run. A run killed outright (a cancelled or timed-out job, or the process crashing)
leaves every file in `data/` as it was or as written, never half-written, because each is written to a temporary file
next to it and then renamed over it; only the run log `data/runs.jsonl` is appended to, so it can end in a cut-off line,
which the audit's cost report skips with a warning (the next append starts on a new line). The next run, or the same command again, does what is left. A power cut or a crash
of the whole machine can still damage a file; restore it from git.

## On GitHub

### Monthly run

`.github/workflows/update.yml` runs `styrke/update.py` at 06:00 UTC on the 1st of every month (or via "Run workflow" in
the Actions tab) and commits any changes to `main`, or to a pull request when the checks find errors (see
[Review](#review)). Claude runs on the subscription through the repository secret `CLAUDE_CODE_OAUTH_TOKEN`; the
workflow refuses to run if an `ANTHROPIC_API_KEY` is present, since that would be billed as API usage. The token is
valid for one year. To create or renew it:

```bash
claude setup-token
GH_TOKEN=$(gh auth token --user Lentz92) gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo Lentz92/styrke-referater
```

The workflow installs a pinned Claude Code version, with auto-update off, and stops if another one is installed: the
version decides which model an alias means and what list price it reports (2.1.289 got Haiku's price wrong by about
100×). Its smoke test also checks that a full model id is answered by that model. Install and smoke test are the
composite action `.github/actions/claude-cli`, which the audit workflow uses too; the pin is its `version` input's
default. To upgrade, change the version there and run the workflow by hand; it fails if the new version is not
installed or answers with another model.

The workflow passes no limit, so every run has the defaults: 15 USD and 75 minutes, and incremental consolidation
unless asked otherwise (`styrke/update.py` has no model or prompt options). "Run workflow" has two boxes: "Allow a
rebuild (--allow-rebuild)" approves the work the rebuild guard stops, and with "Consolidate in full, to migrate a prompt
or version change (--consolidate-mode full)" ticked too it runs the migration. Within the default limits a
re-extraction of every document (16.74 USD in the October 2026 migration) takes two runs: after the first, merge its
review pull request if it opened one, so what it paid for reaches `main`, and run the workflow again with the same
boxes ticked.

Each run that calls Claude adds a line to `data/runs.jsonl`: time, CLI version and, per step, calls, failures,
skipped calls, tokens, list-price cost, models and seconds. A run with nothing new adds none, so compare cost and
tokens at the next run that analyses documents. The Actions run page shows the run report: the same numbers, every
error and warning from the checks, and what changed in earlier years.

`.github/workflows/pages.yml` rebuilds the website from `main` and publishes it on GitHub Pages: after a successful
monthly update that changed `main`, on pushes that change `data/`, `website/`, the code in `styrke/` or its
dependencies (`pyproject.toml`, `uv.lock`), and via "Run workflow". It runs `uv run -m styrke.checks` first and
publishes nothing when it finds an error, so the site stays as it was. It does not call Claude.
`.github/workflows/tests.yml` runs the tests and a render-only build on every pull request and push to `main`.

The workflows run every command with `uv run --locked`: the dependency versions pinned in `uv.lock`, and a lock out of
date with `pyproject.toml` fails the run instead of resolving anew. To upgrade them, run `uv lock --upgrade` and the
tests, and commit `uv.lock`.

### Review

`styrke/update.py` ends with the checks and writes their outcome to `run-report.md` (not committed). Errors are results
the pipeline does not trust: a rule left out because a decision behind it changed (stale), a decision the consolidation
neither put in a rule nor left out as a one-off (unassigned), decision ids or slugs that are missing, used twice or
lead nowhere (identity), and a run that changes what applied in earlier years (history). The effect and date
findings are warnings.

A run updates only the rules its new or changed decisions touch, but a late decision rewrites a rule from where it
belongs, and a full consolidation rewrites whole categories. So for every year page and every rule (rules merged into
one, through `data/slugs.json`, count together), `styrke/update.py` compares the decision in force, the one that adopted
its content, and its wording before and after the analysis. A rule may change from the earliest date of its own
decisions that the run added, changed or removed, or that its last consolidation had not seen (a call that failed); a
difference in an earlier year is an error, or a warning when only the wording changed. A history check that fails is
an error too.

| `styrke/update.py` exits | The workflow |
|---|---|
| 0: no errors | commits to `main` and closes a review pull request left open; the website is rebuilt |
| 3: errors | force-pushes the result to the branch `auto/update` and opens the pull request "Monthly update needs review" with the run report as its body, or updates the open one; `main` and the website stay as they are |
| 1: the run failed | commits the partial results to `main` when its run report says the checks passed, so the next run continues from them, else does as for 3; the run is marked failed and the website is not rebuilt |

A result that cannot be pushed to `main` because it moved during the run goes to review as well. A run started by
hand on another branch uses `auto/update-<branch>` and a pull request into that branch. There is never more than one
review branch or pull request: every run starts from `main`, and a later one replaces the branch and the report.

To review, read the errors and the history table, and check the rules that changed against the minutes (the diff of
`regelsaet/` shows them as text). When the only errors are history, merge to accept the result: the push to `main`
rebuilds the website (only `styrke/update.py` checks history, so merging accepts what changed there). Other errors must
be fixed first, since the website workflow publishes nothing while `uv run -m styrke.checks` finds one: push the fix to
the branch after running `uv run -m styrke.update --render-only` and `uv run -m styrke.checks` there, before the next
monthly run replaces it. A rebuild or migration that was cut off is the exception: it leaves rules out (stale) until it
is finished, so merge its pull request, which keeps what it paid for while the website stays as it was, and finish it as
the report says (see [Rebuilds and migrations](#rebuilds-and-migrations)). Close a pull request to discard its result,
its line in `data/runs.jsonl` included; the next monthly run tries again. GitHub does not run the tests on a pull
request a workflow opened; close and reopen it to run them.

Opening the pull request needs "Allow GitHub Actions to create and approve pull requests" under Settings > Actions >
General > Workflow permissions. The routing is `.github/scripts/route-update.sh`; the pull request plumbing it shares
with the audit's routing is in `.github/scripts/pr.sh`.

## Audit

Incremental consolidation files each new decision into a rule and never merges, splits or renames rules, so its
mistakes stay: one rule spread over several (a fee's yearly confirmations filed under another fee), one rule holding
decisions about different things, a title that no longer fits, a rule in the wrong category. The audit
(`styrke/audit.py`) fixes the structure:

```bash
uv run -m styrke.audit propose --max-cost 25     # Opus proposes ops, in two runs: data/regler_ops.json
uv run -m styrke.audit apply --max-cost 10       # the agreed ops applied to data/, the pages rebuilt
```

1. Code finds rules that may be one rule, in any category: rule against rule, with the TF-IDF profiles incremental
   consolidation ranks its candidates by (`styrke/candidates.py`), at a cosine similarity of at least
   `audit.SIMILARITY`.
2. Opus (`claude-opus-5-5`, effort high) reviews one category per call: its rules with every version, and in brief
   the similar rules of other categories. It answers with ops (merge, split, rename, move), each with its reason and
   the decisions it rests on. Two independent runs read the rules in different orders. Code rejects ops naming slugs
   or decisions the call was not shown, splits whose parts do not divide the rule's decisions exactly, and ops of one
   run that share a rule (only a rename and a move may go together). An op is agreed when both runs propose it; where
   their titles differ, and for every rename, a third Opus call chooses between them or keeps the old title.
3. `data/regler_ops.json` lists every op with its proposals (run, reason, cited decisions), whether it is agreed, the
   answers code rejected, and after apply what each op did. Every Claude answer is kept in `data/audit/` with a
   fingerprint of what it was asked, so a cut-off or repeated command never pays for the same question twice.
4. `apply` applies the agreed ops in code, only to the rules they were proposed for (it stops when `data/regler/`
   changed since). A merge keeps the slug of the rule with most versions and leaves the other slugs in
   `data/slugs.json` as aliases, so their links lead to it; a split keeps the slug on its largest part and gives the
   others new slugs; a move changes the rule's file, a rename only its title. Each merged or split rule then gets one
   Opus call that rewrites its history's texts (incremental's update call, told why: `audit.AUDIT_UPDATE_SYSTEM`);
   code checks that only the texts changed. `apply` builds and checks the whole result in a copy first, and writes
   `data/` only when it has no check errors, leaves `styrke/update.py` nothing to do and every old slug still leads to a
   rule. It then replaces `data/slugs.json`, the rule files and the ops file in the order a consolidation writes
   them, each whole (a temporary file renamed over it).

Run it on settled data, e.g. quarterly or after a migration: it stops while `styrke/update.py` has decisions to file or
a category to consolidate, and a later full consolidation (a migration) regroups categories anew, undoing it. On
GitHub: Actions > "Audit rules" > Run workflow, optionally naming categories and the cost limit (for `propose` and
`apply` each, default 25 USD). It never publishes: an audit changes what earlier years show by design, so its result
always goes to a pull request on `auto/audit-<date>` (`.github/scripts/route-audit.sh`), whose body
(`audit-report.md`, not committed) lists the applied ops with their reasons, the ops only one run proposed, the
answers code rejected, what each year shows before and after for every rule that changed, the answer key's scores
before and after, and the cost. To review, read the applied ops and check the merged and split rules against the
minutes (the diff of `regelsaet/` shows them); merge to publish, or close to discard. Locally, the same commands leave
the changes in the working tree. To score the rules against the answer key before and after, run `uv run -m
styrke.evaluate score-rules` on `data/` and on a copy of an earlier revision's (`git archive REV data/regler
data/beslutninger | tar -x -C DIR`, then `--rules-dir DIR/data/regler --decisions-dir DIR/data/beslutninger`), each with
its own `--report` name; across an audit, which leaves the decisions as they are, `--rules-dir` alone does.

An audit can be cut off: by its cost limit, by failed calls, or by its time budget (`--time-budget`, 75 minutes by
default; in the workflow `propose` and `apply` share it, so the result reaches its pull request within the job's 120
minutes). Then the pull request is titled "(unfinished)" and holds what the audit paid for: the answers kept in
`data/audit/`, the ops file and `data/runs.jsonl`, but no change to `data/regler/`, `data/slugs.json` or
`regelsaet/`. Run the workflow on that pull request's branch to finish it: kept answers are not paid again. For the
same reason a new audit refuses to start while another audit's branch is on GitHub: finish that audit, or merge or
close its pull request and delete its branch.

An open audit pull request conflicts with the monthly update in `data/regler/`, so merge or close it before the 1st:
the workflow refuses to start an audit in the last two days of a month, and while an update runs or is queued. A
monthly run that did not happen (or ran into a conflict) is started by hand with "Run workflow" in `update.yml`.

Cost at list price: on the migrated data (555 rules) `propose` is 24 calls of up to 66K tokens, estimated at
about 11 USD before the first call (the first audit's cost 13.21); each merged or split rule's rewrite about 0.05 USD
(the first audit's 17 rewrites: 0.91 in `data/runs.jsonl`); the title choice a few cents. Each
`propose` and `apply` that calls Claude adds a line to `data/runs.jsonl` (with the audit's id), and the pull request
shows the audit's whole cost from those lines, failed attempts and rejected answers included. Both workflows append
to that file, so git merges it by keeping both sides' lines (`.gitattributes`); what reads it orders the lines by
time. The first audit and its scores are in [eval/README.md](../eval/README.md).
