# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "pymupdf>=1.24",
#   "snowballstemmer>=2.2",
# ]
# ///
"""Bring the DSF rule overview up to date.

    uv run update.py                  # download new minutes, analyse what changed, render regelsaet/
    uv run update.py --offline        # skip styrke.dk, use the files already in referater/
    uv run update.py --render-only    # only rebuild the Markdown and the website from data/

Steps: 1) scrape.py downloads new documents and writes data/manifest.json.
2) analyze.py asks Claude (via the `claude` CLI) to extract decisions from new or changed
documents and to consolidate them per category into rule histories; both are cached in data/.
A run that calls Claude adds a line with its tokens, cost and models to data/runs.jsonl.
3) checks.py checks the result, including what the analysis changed in the past; the outcome goes to
run-report.md and the GitHub step summary.
4) render.py writes regelsaet/<year>.md, regelsaet/regler/<area>.md and regelsaet/README.md.
5) website.py writes the website to _site/ (published on GitHub Pages by .github/workflows/pages.yml).

Exit codes, which the monthly workflow routes on: 0 the checks passed, publish; 3 the checks found errors, the
data is written but must be reviewed in a pull request; 1 the run failed (the checks passed, so its partial
results are kept). A render-only run reports what the checks find and exits 0 whatever they find.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path

import analyze
import checks
import incremental
import render
import scrape
import website
from analyze import StepSummary
from scrape import Doc

EFFORTS = ["low", "medium", "high", "xhigh", "max"]
EXTRACT_MODEL = "claude-opus-5-5"  # the pipeline's extraction model (--extract-model's default)
CONSOLIDATE_MODEL = "claude-opus-5-5"  # --consolidate-model's default
# How rule files are brought up to date: "incremental" (the default) files each new decision into its rule and leaves
# the others as they are (incremental.py); "full" consolidates each changed category anew (analyze.consolidate), which
# migrates the rules after a new extraction or consolidation prompt, or a new CONSOLIDATE_VERSION.
CONSOLIDATE_MODES = ("incremental", "full")
DEFAULT_CONSOLIDATE_MODE = "incremental"
# The workflow input (update.yml) that runs the consolidation in full, as GitHub shows it.
MODE_INPUT_LABEL = "Consolidate in full, to migrate a prompt or version change (--consolidate-mode full)"
# A migration (a full consolidation with --allow-rebuild) extracts every document again after a new extraction
# prompt or model (about 22 USD at list price with Opus) and consolidates every category anew (about 5): its limits
# let it finish in one run, and it runs offline, so no new minutes arrive meanwhile (the next run adds them).
MIGRATION_MAX_COST = 40
MIGRATION_TIME_BUDGET = 150
# The command that migrates with the default settings (Plan.command of a full run with --allow-rebuild).
MIGRATE = (f"uv run update.py --offline --consolidate-mode full --allow-rebuild --max-cost {MIGRATION_MAX_COST} "
           f"--time-budget {MIGRATION_TIME_BUDGET}")
RUNS_LOG = scrape.DATA_DIR / "runs.jsonl"
# More documents than this to extract means a new EXTRACT_VERSION or lost data, not new minutes.
REBUILD_SHARE = 0.10
# More categories than this to consolidate before any extraction means a new CONSOLIDATE_VERSION or similar.
REBUILD_CATEGORY_SHARE = 0.5
# evaluate.py's run log: what one extraction costs, by model and prompt, measured on the answer key's documents.
EVAL_RUNS_LOG = scrape.ROOT / "eval" / "runs.jsonl"
# A full consolidation's list-price cost with Opus (the README's figure), for a rebuild's estimate until
# data/runs.jsonl has a full run of its own.
FULL_CONSOLIDATION_USD = 5.0
# The label GitHub shows for the workflow input that passes --allow-rebuild (its description in update.yml).
REBUILD_INPUT_LABEL = "Allow a rebuild (--allow-rebuild)"
# A run's limits unless it sets others; the workflow sets none, so every run on GitHub keeps to these.
DEFAULT_MAX_COST = 15
DEFAULT_TIME_BUDGET = 75
# What the run found, for the pull request that reviews it (its body) and the GitHub step summary; not committed.
RUN_REPORT = scrape.ROOT / "run-report.md"
EXIT_FAILED = 1
EXIT_REVIEW = 3  # the checks found errors: the data is written, but must go to a pull request, not the website


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="skip the download from styrke.dk")
    parser.add_argument("--render-only", action="store_true", help="only build the Markdown and the website from data/")
    parser.add_argument("--only", metavar="REGEX",
                        help="only extract documents whose id matches, and only consolidate the categories their "
                             "decisions are in (for testing; skips the rebuild guard)")
    parser.add_argument("--extract-model", default=EXTRACT_MODEL,
                        help=f"model for extraction per document (default: {EXTRACT_MODEL})")
    parser.add_argument("--extract-prompt", choices=list(analyze.EXTRACT_PROMPTS),
                        help=f"extraction prompt (default: v{analyze.EXTRACT_VERSION}); another one extracts every "
                             f"document again, a migration (--consolidate-mode full --allow-rebuild)")
    parser.add_argument("--consolidate-model", default=CONSOLIDATE_MODEL,
                        help=f"model for consolidation (default: {CONSOLIDATE_MODEL})")
    parser.add_argument("--assign-model", default="claude-sonnet-5-5",
                        help="model for the votes on which rule a new decision belongs to, in incremental mode "
                             "(default: claude-sonnet-5-5); --consolidate-model breaks ties and updates the rules")
    parser.add_argument("--consolidate-mode", choices=CONSOLIDATE_MODES, default=DEFAULT_CONSOLIDATE_MODE,
                        help="incremental: file each new decision into its rule and leave every other rule as it is; "
                             "full: consolidate each category with changed decisions anew, to migrate a new prompt or "
                             f"CONSOLIDATE_VERSION (with --allow-rebuild) (default: {DEFAULT_CONSOLIDATE_MODE})")
    parser.add_argument("--extract-effort", choices=EFFORTS,
                        help="effort for extraction (default: Claude Code's own)")
    parser.add_argument("--consolidate-effort", choices=EFFORTS,
                        help="effort for consolidation (default: Claude Code's own)")
    parser.add_argument("--workers", type=int, default=4, help="parallel Claude calls (default: 4)")
    parser.add_argument("--time-budget", type=float, default=DEFAULT_TIME_BUDGET, metavar="MIN",
                        help=f"start no new Claude calls after this many minutes (default: {DEFAULT_TIME_BUDGET})")
    parser.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST, metavar="USD",
                        help=f"start no new Claude calls once the run has used this much at list price (default: "
                             f"{DEFAULT_MAX_COST})")
    parser.add_argument("--allow-rebuild", action="store_true",
                        help="let this run do the work the rebuild guard stops: many documents to extract, lost rule "
                             "files, many rules to update; with --consolidate-mode full, the migration after a new "
                             "prompt or version. Cut off, it is finished by running the same command again")
    args = parser.parse_args()
    budget = analyze.RunBudget(minutes=args.time_budget, max_cost_usd=args.max_cost)
    plan = Plan(None if args.extract_prompt == analyze.extract_prompt().name else args.extract_prompt,
                args.extract_model, args.extract_effort, args.consolidate_mode, args.consolidate_model,
                args.consolidate_effort)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    docs = scrape.load_manifest() if args.offline or args.render_only else scrape.sync()
    today = date.today()

    steps: dict[str, StepSummary] = {}
    before: checks.Snapshot | None = None  # what the year pages showed before the analysis
    failures: list[str] = []  # why the run failed, for the report
    stop: BaseException | None = None  # what the run ends with once everything is checked and written
    rebuild = False  # whether --allow-rebuild let the run do more than an ordinary month's work
    if not args.render_only:
        RUN_REPORT.unlink(missing_ok=True)  # the routing trusts a report only from the run that wrote it
        try:
            if not args.only:  # a test run on a few documents
                rebuild = check_rebuild(docs, allowed=args.allow_rebuild, plan=plan, max_cost=args.max_cost)
        except SystemExit as exc:  # nothing is analysed, but the data is checked and the pages written as usual
            failures.append(str(exc))
            stop = exc
        else:
            before = checks.snapshot(checks.Data.load(docs), today)
            stop = analyse(args, plan, docs, budget, steps, failures)
    failed = sum(step.failed + step.skipped for step in steps.values())
    if failed:
        failures.append(f"{failed} Claude calls failed or were skipped")
        # Partial results are cached and the pages are written; fail so CI reports it.
        stop = stop or SystemExit(f"{failed} Claude calls failed or were skipped; run again to include them.")
    # A rebuild cut off by its limits or by failed calls is finished by running it again (see _rerun).
    cut_off = plan if rebuild and stop is not None else None

    data, problems, history = None, [], None
    try:
        data = checks.Data.load(docs)
        problems += checks.find_problems(data)
        if before is not None:
            history = checks.check_history(before, checks.snapshot(data, today), data.slugs.targets())
            problems += history.problems()
    except Exception as exc:
        if before is None:  # nothing was analysed, so nothing can have changed the past
            raise
        # Unchecked history must not be published, so this is an error that sends the result to review.
        logging.exception("The checks failed")
        problems.append(checks.Problem("history", f"history could not be checked: {type(exc).__name__}: {exc}"))
    for problem in problems:
        level = logging.ERROR if problem.severity == "error" else logging.WARNING
        logging.log(level, "Check (%s): %s", problem.kind, problem.message)

    if data is not None:
        try:
            pages = render.render(docs, data.decisions, data.raw_rules,
                                  analyze.missing_extractions(docs, plan.prompt, model=plan.extract_model),
                                  Counter(problem.kind for problem in problems), today)
            logging.info("Wrote %d pages to %s", len(pages), render.OUT_DIR.relative_to(scrape.ROOT))
            site = website.build(docs, data.decisions, data.raw_rules, data.slugs.targets(), today)
            logging.info("Wrote the website to %s", site.relative_to(scrape.ROOT))
        except Exception as exc:
            failures.append(f"writing the pages failed: {type(exc).__name__}: {exc}")
            if stop is not None:
                logging.exception("Writing the pages failed")
            stop = stop or exc

    review = not args.render_only and bool(checks.errors(problems))
    if not args.render_only:
        code = EXIT_REVIEW if review else EXIT_FAILED if failures else 0
        write_report(code, steps, problems, history, failures, today, cut_off=cut_off)
    if review:
        if stop is not None and not isinstance(stop, SystemExit):
            logging.error("The run failed", exc_info=stop)
        logging.error("The checks found errors: the data is written, but it must be reviewed in a pull request "
                      "before it is published (see %s)", RUN_REPORT.name)
        raise SystemExit(EXIT_REVIEW)
    if stop is not None:
        raise stop


def analyse(args: argparse.Namespace, plan: Plan, docs: list[Doc], budget: analyze.RunBudget,
            steps: dict[str, StepSummary], failures: list[str]) -> Exception | None:
    """Extract and consolidate what changed with `plan`, filling in `steps`. An exception is returned, not raised:
    what was written before it is checked all the same."""
    targets = [d for d in docs if re.search(args.only, d.id)] if args.only else docs
    # A test run consolidates only the categories its documents had decisions in, or now have.
    selected = _categories_of(targets) if args.only else None
    try:
        # What the rule files reflect before the extraction, so a decision it only re-dates still reaches its rule.
        known = (incremental.known_inputs(analyze.load_decisions(docs), incremental.RuleBook.load(), docs)
                 if plan.mode == "incremental" else None)
        steps["extract"] = analyze.extract(targets, model=plan.extract_model, effort=plan.extract_effort,
                                           workers=args.workers, budget=budget, prompt=plan.prompt)
        if plan.mode == "incremental":
            settings = incremental.Settings(args.assign_model, plan.consolidate_model, plan.consolidate_effort,
                                            args.workers)
            steps["consolidate"] = incremental.consolidate(
                docs, analyze.load_decisions(docs), settings, budget,
                documents=None if not args.only else {d.id for d in targets}, known=known)
        else:
            steps["consolidate"] = analyze.consolidate(
                analyze.load_decisions(docs),
                {d.id: d.organ_label for d in docs},
                model=plan.consolidate_model,
                effort=plan.consolidate_effort,
                workers=args.workers,
                budget=budget,
                categories=None if selected is None else selected | _categories_of(targets),
            )
    except Exception as exc:
        logging.error("The analysis failed: %s; checking what it wrote before failing the run", exc)
        failures.append(f"the analysis stopped: {type(exc).__name__}: {exc}")
        return exc
    finally:
        record_run(steps, {"extract_prompt": plan.prompt_name, "consolidate_mode": plan.mode})
    return None


def _categories_of(docs: list[Doc]) -> set[str]:
    """The home categories (analyze.home_categories) of the documents' decisions."""
    return set(analyze.home_categories(analyze.load_decisions(docs), analyze.load_rules()).values())


# --------------------------------------------------------------------------- rebuild guard

@dataclass(frozen=True)
class Plan:
    """How a run does its Claude work: the extraction prompt (None: the pipeline's), model and effort, and the
    consolidation mode, model and effort (an effort None is Claude Code's own). The rebuild guard prices the work with
    it and names the command that does it."""
    prompt: str | None = None
    extract_model: str = EXTRACT_MODEL
    extract_effort: str | None = None
    mode: str = DEFAULT_CONSOLIDATE_MODE
    consolidate_model: str = CONSOLIDATE_MODEL
    consolidate_effort: str | None = None

    @property
    def prompt_name(self) -> str:
        return analyze.extract_prompt(self.prompt).name

    def command(self, *, allow_rebuild: bool = False) -> str:
        """The update.py command that does the work with this plan; settings at their default are left out. With
        --allow-rebuild in full mode it is a migration: offline, with the migration's limits (MIGRATE)."""
        default = Plan()
        migration = allow_rebuild and self.mode == "full"
        options = ["--offline"] if migration else []
        if self.mode != default.mode:
            options.append(f"--consolidate-mode {self.mode}")
        if self.prompt_name != default.prompt_name:
            options.append(f"--extract-prompt {self.prompt_name}")
        for name in ("extract_model", "extract_effort", "consolidate_model", "consolidate_effort"):
            if getattr(self, name) != getattr(default, name):
                options.append(f"--{name.replace('_', '-')} {getattr(self, name)}")
        if allow_rebuild:
            options.append("--allow-rebuild")
        if migration:
            options += [f"--max-cost {MIGRATION_MAX_COST}", f"--time-budget {MIGRATION_TIME_BUDGET}"]
        return " ".join(["uv run update.py", *options])

    def on_github(self) -> bool:
        """Whether the workflow can run it: its checkboxes pass only --allow-rebuild and --consolidate-mode full, so
        every other setting must be the default."""
        return replace(self, mode=DEFAULT_CONSOLIDATE_MODE).command() == "uv run update.py"


@dataclass(frozen=True)
class Work:
    """The Claude work a run would start with, as the rebuild guard judges it."""
    documents: frozenset[str]  # ids of the documents to extract
    categories: frozenset[str]  # categories to consolidate
    lost: frozenset[str]  # categories among them with decisions but no rule file
    outdated: frozenset[str]  # categories consolidated with another CONSOLIDATE_VERSION: only `full` migrates them
    sources: dict[str, frozenset[str]]  # category -> ids of the documents its cached decisions come from
    total_documents: int
    total_categories: int
    extract_version: int  # the version of the extraction prompt the documents are to be extracted with
    extract_model: str  # the model they are to be extracted by
    # Documents among those to extract whose cached extraction was made with another prompt or model: all their
    # decisions may change, which only a full consolidation should take in.
    superseded: frozenset[str]
    mode: str  # the consolidation mode the work is judged for

    def reasons(self) -> list[str]:
        """Why this is more than an ordinary month's work; empty when it is ordinary.

        In an ordinary month a few new or replaced documents are extracted, and only then are the categories
        they touch consolidated, so before extraction (almost) no category needs it. This catches:
        - more than REBUILD_SHARE of the documents to extract: a new EXTRACT_VERSION, or lost extractions;
        - a category with decisions but no rule file: lost rules;
        - more than REBUILD_CATEGORY_SHARE of the categories to consolidate: a new CONSOLIDATE_VERSION, a
          change to the consolidation input, edited decisions, or what a cut-off run left;
        - in incremental mode, a migration (see migration).
        """
        reasons = []
        if len(self.documents) > REBUILD_SHARE * self.total_documents:
            reasons.append(f"{len(self.documents)} of {self.total_documents} documents need extraction")
        if self.lost:
            lost = ", ".join(sorted(self.lost))
            reasons.append(f"categories with decisions but no rule file in data/regler/: {lost}")
        if len(self.categories) > REBUILD_CATEGORY_SHARE * self.total_categories:
            reasons.append(f"{len(self.categories)} of {self.total_categories} categories need consolidating again")
        if self.mode == "incremental":  # in full mode this is ordinary work to extract and consolidate
            reasons += self.migration()
        return reasons

    def migration(self) -> list[str]:
        """Why this work is a migration, which only a full consolidation does; empty when it is none: categories
        consolidated with another CONSOLIDATE_VERSION, or more than REBUILD_SHARE of the documents extracted with
        another prompt or model (a new extraction changes decisions throughout, which a full consolidation takes in
        more cheaply and cleanly than updating rule by rule)."""
        found = []
        if len(self.superseded) > REBUILD_SHARE * self.total_documents:
            found.append(f"{len(self.superseded)} of {self.total_documents} documents were extracted with another "
                         f"prompt or model than v{self.extract_version} and {self.extract_model}, which only a full "
                         f"consolidation takes in")
        if self.outdated:
            found.append(f"categories consolidated with another CONSOLIDATE_VERSION, which only a full "
                         f"consolidation migrates: {', '.join(sorted(self.outdated))}")
        return found

    def affected_categories(self) -> frozenset[str]:
        """The categories this work consolidates: those to consolidate now, and those the documents to extract have
        cached decisions in, which change once they are extracted."""
        return self.categories | {category for category, ids in self.sources.items() if ids & self.documents}


def pending_work(docs: list[Doc], mode: str = DEFAULT_CONSOLIDATE_MODE, prompt: str | None = None,
                 model: str = EXTRACT_MODEL) -> Work:
    """The work before extraction with the extraction prompt named `prompt` (None: the pipeline's) and `model`, from
    data/ alone: no Claude call. The categories to consolidate are those whose input changed (full), or those the
    incremental work queue changes (incremental.queue_categories); in incremental mode, those consolidated with another
    CONSOLIDATE_VERSION, and documents extracted with another prompt or model, are a migration: only `full` does it."""
    decisions = analyze.load_decisions(docs)
    book = incremental.RuleBook.load()
    outdated = frozenset(category for category, stored in book.files.items()
                         if stored.get("version") != analyze.CONSOLIDATE_VERSION)
    if mode == "incremental":
        categories = incremental.queue_categories(incremental.work_queue(decisions, book, docs), decisions, book)
        lost = frozenset(category for category in categories if category not in book.files)
    else:
        todo = analyze.consolidation_todo(decisions, {d.id: d.organ_label for d in docs})
        categories = frozenset(job.category for job in todo)
        lost = frozenset(job.category for job in todo if job.rules_missing)
    sources: dict[str, set[str]] = {}
    home = analyze.home_categories(decisions, analyze.load_rules())
    for d in decisions:
        sources.setdefault(home[d.ref], set()).add(d.doc_id)
    return Work(
        documents=frozenset(analyze.missing_extractions(docs, prompt, model=model)),
        categories=categories,
        lost=lost,
        outdated=outdated,
        sources={category: frozenset(ids) for category, ids in sources.items()},
        total_documents=len(docs),
        total_categories=len(sources),
        extract_version=analyze.extract_prompt(prompt).version,
        extract_model=model,
        superseded=frozenset(analyze.superseded_extractions(docs, prompt, model=model)),
        mode=mode,
    )


def check_rebuild(docs: list[Doc], *, allowed: bool, plan: Plan, max_cost: float | None = None) -> bool:
    """Stop before any Claude call when the run would do more than an ordinary month's work without --allow-rebuild
    (`allowed`); return whether it goes ahead with such work (a rebuild).

    Nothing is saved between runs: a rebuild that is cut off is finished by running the same command again, which
    finds what is done current and does only the rest (_rerun), and until then a plain run stops here again.
    Incremental mode never does a migration (Work.migration), allowed or not. The work's estimated cost is logged
    first, with whether `max_cost` (the run's --max-cost) cuts the run off: for a migration refused in incremental
    mode, the cost of the full one.
    """
    work = pending_work(docs, plan.mode, plan.prompt, plan.extract_model)
    reasons = work.reasons()
    if not reasons:
        return False
    if plan.mode == "incremental" and work.migration():  # incremental mode cannot do this work, allowed or not
        migrate = replace(plan, mode="full")
        total = log_estimate(pending_work(docs, "full", plan.prompt, plan.extract_model), migrate, MIGRATION_MAX_COST)
        raise SystemExit(_migration_refusal(reasons, migrate, total))
    total = log_estimate(work, plan, max_cost)
    if not allowed:
        raise SystemExit(_guard_refusal(reasons, plan, total))
    logging.warning("Allowed by --allow-rebuild: %s", "; ".join(reasons))
    return True


# --------------------------------------------------------------------------- refusals and how to finish a rebuild

def _migration_refusal(reasons: list[str], migrate: Plan, total: float | None) -> str:
    """Work an incremental run cannot do: the command that migrates (`migrate`, the run's plan in full mode), with its
    estimated cost `total`, and how a migration that is cut off is finished."""
    within = ""
    if total is not None:
        limit = (f"within its --max-cost {MIGRATION_MAX_COST}" if total <= MIGRATION_MAX_COST else
                 f"more than its --max-cost {MIGRATION_MAX_COST}: running it again continues the rest")
        within = f" (estimated {total:.2f} USD at list price, {limit})"
    github = _github(full=True, total=total) if migrate.on_github() else ""
    return (f"Stopped before any Claude call: {'; '.join(reasons)}. Migrate with "
            f"`{migrate.command(allow_rebuild=True)}`{within}{github}. If it is cut off, {_rerun(migrate)}; once it is "
            f"finished, plain runs go on incrementally.")


def _guard_refusal(reasons: list[str], plan: Plan, total: float | None) -> str:
    """Why the guard stopped the run, with which extraction settings, and the exact command that does the work (and
    the workflow's checkboxes that do the same, when they can); `total` is the work's estimated cost."""
    full = plan.mode == "full"
    github = _github(full, total) if plan.on_github() else ""
    migrate = "" if full else (f" A new prompt or EXTRACT_VERSION/CONSOLIDATE_VERSION is migrated with `{MIGRATE}` "
                               f"(on GitHub, also tick '{MODE_INPUT_LABEL}'), and a migration that was cut off is "
                               f"finished by running that command again.")
    return (f"Stopped before any Claude call: {'; '.join(reasons)} (extracting with prompt {plan.prompt_name} and "
            f"{plan.extract_model}). If this work is intended, run `{plan.command(allow_rebuild=True)}`{github}. If "
            f"it is cut off, {_rerun(plan)}.{migrate}")


def _github(full: bool, total: float | None) -> str:
    """How to start the work on GitHub instead, where every run keeps to the default limits (the workflow passes
    none): work that costs more takes several runs."""
    more = (f": at an estimated {total:.2f} USD it takes several runs there, each with the same boxes ticked"
            if total is not None and total > DEFAULT_MAX_COST else "")
    return (f", or on GitHub: Actions > Update rule overview > Run workflow with {_boxes(full)} ticked, where each run "
            f"keeps to the default limits ({DEFAULT_MAX_COST} USD, {DEFAULT_TIME_BUDGET} minutes){more}")


def _rerun(plan: Plan) -> str:
    """How a rebuild with `plan` that was cut off is finished: by running it again, which finds what was done current
    (extractions are cached by prompt and model, and a full consolidation skips each category consolidated with
    today's input) and does only the rest."""
    github = (f"; on GitHub, merge the review pull request of the cut-off run first, if it opened one, so what it paid "
              f"for reaches main, then run the workflow again with {_boxes(plan.mode == 'full')} ticked"
              if plan.on_github() else "")
    return (f"run `{plan.command(allow_rebuild=True)}` again{github}: what was done is kept, so it does only the "
            f"rest")


def _boxes(full: bool) -> str:
    """The workflow's checkboxes that start a rebuild, and in full mode a migration."""
    return f"'{REBUILD_INPUT_LABEL}'" + (f" and '{MODE_INPUT_LABEL}'" if full else "")


# --------------------------------------------------------------------------- rebuild cost

@dataclass(frozen=True)
class CostEstimate:
    """A rebuild's list-price cost, estimated before its first call; a part is None when nothing measured it."""
    extract_usd: float | None
    extract_basis: str
    consolidate_usd: float | None
    consolidate_basis: str

    @property
    def total_usd(self) -> float | None:
        parts = (self.extract_usd, self.consolidate_usd)
        return None if None in parts else sum(parts)

    def describe(self, max_cost: float | None) -> str:
        """One line for the log, with what each part rests on and whether --max-cost will cut the run off."""
        def usd(value: float | None) -> str:
            return "unknown" if value is None else f"{value:.2f} USD"

        text = (f"Estimated cost at list price: extraction {usd(self.extract_usd)} ({self.extract_basis}); "
                f"consolidation {usd(self.consolidate_usd)} ({self.consolidate_basis}); total {usd(self.total_usd)}")
        if max_cost is not None and self.total_usd is not None and self.total_usd > max_cost:
            text += (f", more than --max-cost {max_cost:g}: this run stops there, and running it again continues "
                     f"the rest (or raise --max-cost)")
        return text


def estimate_cost(work: Work, plan: Plan) -> CostEstimate:
    """The work's cost: each document to extract at the mean measured cost of one extraction by the model
    (eval/runs.jsonl), and, in full mode, each category to consolidate at its share of the last full consolidation
    (data/runs.jsonl, else FULL_CONSOLIDATION_USD)."""
    name = plan.prompt_name
    documents = len(work.documents)
    measured = extraction_cost(plan.extract_model, name, EVAL_RUNS_LOG)
    if not documents:
        extract: tuple[float | None, str] = (0.0, "no documents to extract")
    elif measured is None:
        extract = (None, f"{documents} documents; eval/runs.jsonl has no extraction by {plan.extract_model}")
    else:
        per_document, calls, prompts = measured
        extract = (documents * per_document,
                   f"{documents} documents with prompt {name} × {per_document:.3f} USD, the mean of {calls} "
                   f"extractions by {plan.extract_model} with prompt {', '.join(prompts)} in eval/runs.jsonl")
    categories = len(work.affected_categories())
    if plan.mode != "full":
        consolidate: tuple[float | None, str] = (None, f"{categories} categories, incremental: not estimated")
    elif not categories:
        consolidate = (0.0, "no categories to consolidate")
    else:
        logged = full_consolidation_cost(plan.consolidate_model, work.total_categories, RUNS_LOG)
        if logged is None:
            per_category = FULL_CONSOLIDATION_USD / max(work.total_categories, 1)
            basis = (f"{categories} categories at the README's {FULL_CONSOLIDATION_USD:g} USD for a full "
                     f"consolidation with Opus; data/runs.jsonl has none by {plan.consolidate_model}")
        else:
            per_category, when = logged
            basis = f"{categories} categories × {per_category:.2f} USD, as in the full consolidation of {when}"
        consolidate = (categories * per_category, basis)
    return CostEstimate(*extract, *consolidate)


def log_estimate(work: Work, plan: Plan, max_cost: float | None) -> float | None:
    """Log the work's estimated cost, with whether `max_cost` cuts the run off, and return its total (None when
    unknown). An unreadable run log only warns: the estimate informs, it never stops a run."""
    try:
        estimate = estimate_cost(work, plan)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logging.warning("Could not estimate the cost of this work: %s: %s", type(exc).__name__, exc)
        return None
    logging.warning("%s", estimate.describe(max_cost))
    return estimate.total_usd


def _log_lines(path: Path) -> list[dict]:
    """The lines of a run log; none when it does not exist yet."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def extraction_cost(model: str, prompt: str, path: Path) -> tuple[float, int, tuple[str, ...]] | None:
    """Mean list-price cost of one extraction by `model` in evaluate.py's runs (eval/runs.jsonl), how many
    extractions that is the mean of, and with which prompts: those with `prompt` when there are any, since another
    prompt may ask for more, else all. Lines without a prompt are from before it was logged, when every run used v2."""
    runs = [(line.get("prompt", "v2"), line["steps"]["extract"]) for line in _log_lines(path)
            if line.get("command") == "extract" and line.get("model") == model and "extract" in line.get("steps", {})]
    chosen = [(name, step) for name, step in runs if name == prompt] or runs
    calls = sum(step["calls"] for _, step in chosen)
    if not calls:
        return None
    return sum(step["cost_usd"] for _, step in chosen) / calls, calls, tuple(sorted({name for name, _ in chosen}))


def full_consolidation_cost(model: str, categories: int, path: Path) -> tuple[float, str] | None:
    """Cost per category of the last full consolidation by `model` in data/runs.jsonl, and when it ran: the last run
    in full mode whose consolidation called Claude for at least `categories` categories. Lines without a mode are
    from before it was logged; the monthly runs then consolidated in full. Last by time, not by place in the file:
    a merge of two branches that both logged runs keeps both sides' lines (.gitattributes), not in time order."""
    for line in sorted(_log_lines(path), key=lambda line: line.get("time", ""), reverse=True):
        step = line.get("steps", {}).get("consolidate")
        if (step and line.get("consolidate_mode", "full") == "full" and model in step.get("models", ())
                and step["calls"] >= max(categories, 1)):
            return step["cost_usd"] / step["calls"], line["time"]
    return None


# --------------------------------------------------------------------------- run log

def record_run(steps: dict[str, StepSummary], settings: dict | None = None) -> None:
    """Append the run's Claude usage to data/runs.jsonl, with its `settings` (prompt, consolidation mode). A run
    without calls adds nothing: the log follows what runs cost, not how often they ran. A write error only warns,
    so it never hides the run's outcome."""
    if not any(step.calls for step in steps.values()):
        return
    try:
        append_run_log(RUNS_LOG, steps, analyze.cli_version(), datetime.now(timezone.utc), settings)
    except OSError as exc:
        logging.warning("Could not write %s: %s", RUNS_LOG, exc)


def append_run_log(path: Path, steps: dict[str, StepSummary], cli: str, now: datetime,
                   settings: dict | None = None) -> None:
    """Add one JSON line with what the run's Claude calls used, so cost and time can be followed over months; the
    settings tell a full consolidation (the rebuild estimate's measure) from an incremental one."""
    line = {"time": now.isoformat(timespec="seconds"), "cli": cli, **(settings or {}),
            "steps": {name: step_json(step) for name, step in steps.items()}}
    with path.open("a") as log:
        log.write(json.dumps(line, ensure_ascii=False) + "\n")


def step_json(step: StepSummary) -> dict:
    """One step's Claude usage as a line of a run log stores it (also evaluate.py's eval/runs.jsonl)."""
    usage = step.usage
    return {
        "calls": step.calls,
        "failed": step.failed,
        "skipped": step.skipped,
        "tokens": {"input": usage.input_tokens, "output": usage.output_tokens,
                   "cache_read": usage.cache_read_tokens, "cache_write": usage.cache_write_tokens},
        "cost_usd": round(usage.cost_usd, 4),
        "models": list(usage.models),
        "seconds": round(step.seconds),
    }


def step_summary(steps: dict[str, StepSummary], problem_counts: Counter[str]) -> str:
    """Markdown with the same numbers as data/runs.jsonl, plus the counts from checks.py."""
    rows = [
        "| Step | Calls | Failed | Skipped | Tokens in | of which cached | Tokens out | USD (list price) | Models |",
        "|---|--:|--:|--:|--:|--:|--:|--:|---|",
    ]
    for name, step in steps.items():
        u = step.usage
        rows.append(f"| {name} | {step.calls} | {step.failed} | {step.skipped} | {u.all_input_tokens} | "
                    f"{u.cache_read_tokens + u.cache_write_tokens} | {u.output_tokens} | {u.cost_usd:.2f} | "
                    f"{', '.join(u.models) or '–'} |")
    found = ", ".join(f"{kind}: {count}" for kind, count in sorted(problem_counts.items())) or "none"
    return "### Claude calls\n\n" + "\n".join(rows) + f"\n\nCheck findings: {found}\n"


# --------------------------------------------------------------------------- run report

# The first line of run-report.md, which .github/scripts/route-update.sh reads: a failed run's partial results go to
# main only when its report says the checks passed.
CHECKS_PASSED = "<!-- checks: passed -->"
CHECKS_FAILED = "<!-- checks: errors -->"


def write_report(code: int, steps: dict[str, StepSummary], problems: list[checks.Problem],
                 history: checks.HistoryCheck | None, failures: list[str], today: date, *,
                 cut_off: Plan | None = None) -> None:
    """Write run-report.md and, when running on GitHub, show it on the run page; a write error only warns.
    `cut_off`: the plan of the rebuild the run was cut off in, if it was."""
    try:
        report = run_report(code, steps, problems, history, failures, today, cut_off=cut_off)
    except Exception:  # the outcome must still reach the routing and the pull request
        logging.exception("Could not build the run report")
        report = "\n".join([_marker(code), f"# Rule overview update {today.isoformat()}", "",
                            _outcome(code, problems, failures, cut_off), "",
                            "The full report could not be built; the run's log has every finding.", ""])
    try:
        RUN_REPORT.write_text(report)
    except OSError as exc:
        logging.warning("Could not write %s: %s", RUN_REPORT, exc)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a") as summary:
            summary.write(report)
    except OSError as exc:
        logging.warning("Could not write the GitHub step summary: %s", exc)


def run_report(code: int, steps: dict[str, StepSummary], problems: list[checks.Problem],
               history: checks.HistoryCheck | None, failures: list[str], today: date, *,
               cut_off: Plan | None = None) -> str:
    """Markdown for the pull request that reviews a run, and for its run page: the outcome, the Claude calls,
    every error and warning, what a step asks to have looked at (its notes), and what the run would change in earlier
    years."""
    lines = [_marker(code), f"# Rule overview update {today.isoformat()}", "",
             _outcome(code, problems, failures, cut_off), ""]
    if steps:
        lines.append(step_summary(steps, Counter(problem.kind for problem in problems)))
    for severity, heading in (("error", "Errors"), ("warning", "Warnings")):
        found = [problem for problem in problems if problem.severity == severity]
        lines += [f"## {heading}: {len(found)}", ""]
        if found:
            lines += [f"- **{problem.kind}**: {problem.message}" for problem in found] + [""]
    for step in steps.values():
        for heading, notes in step.notes.items():
            lines += [f"## {heading}: {len(notes)}", "", *(f"- {note}" for note in notes), ""]
    if history is not None:
        lines += _history_section(history)
    return "\n".join(lines).rstrip() + "\n"


def _marker(code: int) -> str:
    return CHECKS_FAILED if code == EXIT_REVIEW else CHECKS_PASSED


def _outcome(code: int, problems: list[checks.Problem], failures: list[str], cut_off: Plan | None = None) -> str:
    """What the run's result is and what to do with it. Merging a pull request publishes it only when the website
    workflow's checks pass: they see every error but history, which only a run can compare. A rebuild that was cut off
    (`cut_off`, its plan) is finished by running it again (_rerun), from the merged pull request if the run opened one:
    closing it would throw away what the run has paid for."""
    failed = "; ".join(failures)
    if code == 0:
        return "**Ready to publish**: the checks found no errors."
    if code == EXIT_FAILED:
        outcome = (f"**The run failed**: {failed}. The checks found no errors, so the results so far are kept"
                   + ("; the next run retries the rest." if cut_off is None else "."))
    else:
        blocking = sorted({problem.kind for problem in checks.errors(problems)} - {"history"})
        if blocking and cut_off is not None:
            outcome = (f"**Needs review**: the checks found errors in the data ({', '.join(blocking)}), as a migration "
                       f"or rebuild that is cut off leaves rules out until it is finished. Merge the pull request, so "
                       f"what the run has paid for is kept; the website workflow publishes nothing while the checks "
                       f"find errors, so the site stays as it was until then. Closing it discards that work.")
        elif blocking:
            outcome = (f"**Needs review**: the checks found errors in the data ({', '.join(blocking)}), which must be "
                       f"fixed before it can be published: the website workflow refuses data with these errors, so "
                       f"merging alone publishes nothing. Fix data/ on this branch, or close the pull request.")
        else:
            outcome = ("**Needs review**: this update changes what applied in earlier years, or that could not be "
                       "checked (see History and Errors). Merge the pull request to publish it, or close it to discard "
                       "it" + (" (and what the run has paid for toward its rebuild)." if cut_off is not None else "."))
        if failures:
            outcome += f"\n\nThe run also failed: {failed}."
    if cut_off is not None:
        outcome += (f"\n\n**Unfinished rebuild**: the run was cut off before it finished the work --allow-rebuild let "
                    f"it do. To finish it, {_rerun(cut_off)}. Until then a plain run stops before any Claude call "
                    f"while more than an ordinary month's work is left.")
    return outcome


def _history_section(history: checks.HistoryCheck) -> list[str]:
    """A table of what would change in earlier years: per rule and run of years, the decisions in force before
    and after (and the one each confirms)."""
    lines = ["## History", "",
             "Each rule should show what it showed before in the years before its earliest decision this run "
             "added, changed or removed, or that its consolidation had not seen yet.", ""]
    changes = sorted((c for c in history.changes if c.kind == "history"),
                     key=lambda c: (c.title.lower(), c.slugs, c.cutoff))
    if not changes:
        return lines + ["No rule in force there changed.", ""]
    lines += ["| Rule | Years | Before | After | Its decisions changed from |", "|---|---|---|---|---|"]
    by_run = groupby(changes, key=lambda c: (c.title, c.slugs, _state(c.before), _state(c.after), c.since))
    for (title, slugs, was, now, since), run in by_run:
        lines.append(f"| {_cell(title)} (`{slugs}`) | {checks.years(c.cutoff for c in run)} | {_cell(was)} | "
                     f"{_cell(now)} | {since or 'none changed'} |")
    return lines + [""]


def _state(rules: tuple[checks.InForce, ...]) -> str:
    return " + ".join(rule.label() for rule in rules) or "not in force"


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


if __name__ == "__main__":
    main()
