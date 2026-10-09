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
EXTRACT_MODEL = "claude-sonnet-5-5"  # the pipeline's extraction model (--extract-model's default)
CONSOLIDATE_MODEL = "claude-opus-5-5"
# How rule files are brought up to date: "full" consolidates each changed category anew (analyze.consolidate);
# "incremental" files each new decision into its rule and leaves the others as they are (incremental.py).
CONSOLIDATE_MODES = ("full", "incremental")
RUNS_LOG = scrape.DATA_DIR / "runs.jsonl"
# An approved rebuild that is not finished yet; plain runs continue it while it matches the prompt versions.
REBUILD_MARKER = scrape.DATA_DIR / "rebuild.json"
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
                             f"document again, which the rebuild guard stops until --allow-rebuild")
    parser.add_argument("--consolidate-model", default=CONSOLIDATE_MODEL,
                        help=f"model for consolidation (default: {CONSOLIDATE_MODEL})")
    parser.add_argument("--assign-model", default="claude-sonnet-5-5",
                        help="model for the votes on which rule a new decision belongs to, in incremental mode "
                             "(default: claude-sonnet-5-5); --consolidate-model breaks ties and updates the rules")
    parser.add_argument("--consolidate-mode", choices=CONSOLIDATE_MODES, default="full",
                        help="full: consolidate each category with changed decisions anew; incremental: file each "
                             "new decision into its rule and leave every other rule as it is (default: full)")
    parser.add_argument("--extract-effort", choices=EFFORTS, help="effort for extraction (default: Claude Code's own)")
    parser.add_argument("--consolidate-effort", choices=EFFORTS,
                        help="effort for consolidation (default: Claude Code's own)")
    parser.add_argument("--workers", type=int, default=4, help="parallel Claude calls (default: 4)")
    parser.add_argument("--time-budget", type=float, default=75, metavar="MIN",
                        help="start no new Claude calls after this many minutes (default: 75)")
    parser.add_argument("--max-cost", type=float, default=15, metavar="USD",
                        help="start no new Claude calls once the run has used this much at list price (default: 15)")
    parser.add_argument("--allow-rebuild", action="store_true",
                        help="allow work the rebuild guard stops, e.g. after a new prompt version")
    args = parser.parse_args()
    if args.consolidate_mode == "incremental" and analyze.extract_prompt(args.extract_prompt).version \
            != analyze.EXTRACT_VERSION:
        parser.error("another extraction prompt changes the decisions of every document: consolidate them with "
                     "--consolidate-mode full, which is cheaper and cleaner than updating every rule")
    budget = analyze.RunBudget(minutes=args.time_budget, max_cost_usd=args.max_cost)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    docs = scrape.load_manifest() if args.offline or args.render_only else scrape.sync()
    today = date.today()

    steps: dict[str, StepSummary] = {}
    before: checks.Snapshot | None = None  # what the year pages showed before the analysis
    failures: list[str] = []  # why the run failed, for the report
    stop: BaseException | None = None  # what the run ends with once everything is checked and written
    if not args.render_only:
        RUN_REPORT.unlink(missing_ok=True)  # the routing trusts a report only from the run that wrote it
        try:
            if not args.only:  # a test run on a few documents
                check_rebuild(docs, allowed=args.allow_rebuild, mode=args.consolidate_mode, plan=Plan.of(args))
        except SystemExit as exc:  # nothing is analysed, but the data is checked and the pages written as usual
            failures.append(str(exc))
            stop = exc
        else:
            before = checks.snapshot(checks.Data.load(docs), today)
            stop = analyse(args, docs, budget, steps, failures)
    failed = sum(step.failed + step.skipped for step in steps.values())
    if failed:
        failures.append(f"{failed} Claude calls failed or were skipped")
        # Partial results are cached and the pages are written; fail so CI reports it.
        stop = stop or SystemExit(f"{failed} Claude calls failed or were skipped; run again to include them.")

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
                                  analyze.missing_extractions(docs, args.extract_prompt),
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
        write_report(code, steps, problems, history, failures, today)
    if review:
        if stop is not None and not isinstance(stop, SystemExit):
            logging.error("The run failed", exc_info=stop)
        logging.error("The checks found errors: the data is written, but it must be reviewed in a pull request "
                      "before it is published (see %s)", RUN_REPORT.name)
        raise SystemExit(EXIT_REVIEW)
    if stop is not None:
        raise stop


def analyse(args: argparse.Namespace, docs: list[Doc], budget: analyze.RunBudget, steps: dict[str, StepSummary],
            failures: list[str]) -> Exception | None:
    """Extract and consolidate what changed, filling in `steps`. An exception is returned, not raised: what was
    written before it is checked all the same."""
    targets = [d for d in docs if re.search(args.only, d.id)] if args.only else docs
    # A test run consolidates only the categories its documents had decisions in, or now have.
    selected = _categories_of(targets) if args.only else None
    try:
        # What the rule files reflect before the extraction, so a decision it only re-dates still reaches its rule.
        known = (incremental.known_inputs(analyze.load_decisions(docs), incremental.RuleBook.load(), docs)
                 if args.consolidate_mode == "incremental" else None)
        steps["extract"] = analyze.extract(targets, model=args.extract_model, effort=args.extract_effort,
                                           workers=args.workers, budget=budget, prompt=args.extract_prompt)
        if args.consolidate_mode == "incremental":
            settings = incremental.Settings(args.assign_model, args.consolidate_model, args.consolidate_effort,
                                            args.workers)
            steps["consolidate"] = incremental.consolidate(
                docs, analyze.load_decisions(docs), settings, budget,
                documents=None if not args.only else {d.id for d in targets}, known=known)
        else:
            steps["consolidate"] = analyze.consolidate(
                analyze.load_decisions(docs),
                {d.id: d.organ_label for d in docs},
                model=args.consolidate_model,
                effort=args.consolidate_effort,
                workers=args.workers,
                budget=budget,
                categories=None if selected is None else selected | _categories_of(targets),
            )
    except Exception as exc:
        logging.error("The analysis failed: %s; checking what it wrote before failing the run", exc)
        failures.append(f"the analysis stopped: {type(exc).__name__}: {exc}")
        return exc
    finally:
        record_run(steps, {"extract_prompt": analyze.extract_prompt(args.extract_prompt).name,
                           "consolidate_mode": args.consolidate_mode})
    if not args.only:
        update_rebuild_marker(docs, args.consolidate_mode, Plan.of(args))
    return None


def _categories_of(docs: list[Doc]) -> set[str]:
    """The home categories (analyze.home_categories) of the documents' decisions."""
    return set(analyze.home_categories(analyze.load_decisions(docs), analyze.load_rules()).values())


# --------------------------------------------------------------------------- rebuild guard

@dataclass(frozen=True)
class Work:
    """The Claude work a run would start with, as the rebuild guard judges it."""
    documents: frozenset[str]  # ids of the documents to extract
    categories: frozenset[str]  # categories to consolidate
    lost: frozenset[str]  # categories among them with decisions but no rule file
    sources: dict[str, frozenset[str]]  # category -> ids of the documents its cached decisions come from
    total_documents: int
    total_categories: int
    extract_version: int  # the version of the extraction prompt the documents are extracted with

    def reasons(self) -> list[str]:
        """Why this is more than an ordinary month's work; empty when it is ordinary.

        In an ordinary month a few new or replaced documents are extracted, and only then are the categories
        they touch consolidated, so before extraction (almost) no category needs it. This catches:
        - more than REBUILD_SHARE of the documents to extract: a new EXTRACT_VERSION, or lost extractions;
        - a category with decisions but no rule file: lost rules;
        - more than REBUILD_CATEGORY_SHARE of the categories to consolidate: a new CONSOLIDATE_VERSION, a
          change to the consolidation input, edited decisions, or what a cut-off run left.
        """
        reasons = []
        if len(self.documents) > REBUILD_SHARE * self.total_documents:
            reasons.append(f"{len(self.documents)} of {self.total_documents} documents need extraction")
        if self.lost:
            lost = ", ".join(sorted(self.lost))
            reasons.append(f"categories with decisions but no rule file in data/regler/: {lost}")
        if len(self.categories) > REBUILD_CATEGORY_SHARE * self.total_categories:
            reasons.append(f"{len(self.categories)} of {self.total_categories} categories need consolidating again")
        return reasons

    def approved_categories(self) -> frozenset[str]:
        """What an approval of this work lets later runs consolidate: the categories to consolidate now, and
        those the documents to extract have cached decisions in, which change once they are extracted."""
        return self.categories | {c for c, ids in self.sources.items() if ids & self.documents}

    def outside(self, approval: Approval) -> Work:
        """The part of this work an approval does not cover. Documents must be among the approved ones.
        A category is covered when it was approved, or when it now has decisions from an approved document:
        extracting those documents is approved, and their new decisions may land in any category."""
        covered = approval.categories | {c for c, ids in self.sources.items() if ids & approval.documents}
        return replace(self, documents=self.documents - approval.documents, categories=self.categories - covered,
                       lost=self.lost - covered)


@dataclass(frozen=True)
class Approval:
    """Work approved with --allow-rebuild (or left by a run that was allowed to start it), for the prompt
    versions in force then. Saved in data/rebuild.json so later runs with the same settings (plain ones, the monthly
    one included, when they are the defaults) finish it."""
    documents: frozenset[str]
    categories: frozenset[str]
    extract_prompt: str  # the extraction prompt's name, e.g. "v3"
    extract_model: str | None  # None in an approval saved before the model was recorded


@dataclass(frozen=True)
class Plan:
    """What a run does its Claude work with: the extraction prompt (None: the pipeline's), the models, and its cost
    limit (--max-cost). The rebuild guard prices the work with it, and keeps an approved rebuild on one extraction
    model: the cache key leaves the model out, so a migration resumed on another model would mix two silently."""
    prompt: str | None = None
    extract_model: str = EXTRACT_MODEL
    consolidate_model: str = CONSOLIDATE_MODEL
    max_cost: float | None = None

    @classmethod
    def of(cls, args: argparse.Namespace) -> Plan:
        return cls(args.extract_prompt, args.extract_model, args.consolidate_model, args.max_cost)

    @property
    def prompt_name(self) -> str:
        return analyze.extract_prompt(self.prompt).name

    def command(self) -> str:
        return run_command(self.prompt_name, self.extract_model)


def run_command(prompt: str, extract_model: str) -> str:
    """The update.py command that extracts with this prompt and model; options at their default are left out."""
    options = [f"--extract-prompt {prompt}"] if prompt != f"v{analyze.EXTRACT_VERSION}" else []
    options += [f"--extract-model {extract_model}"] if extract_model != EXTRACT_MODEL else []
    return " ".join(["uv run update.py", *options])


def pending_work(docs: list[Doc], mode: str = "full", prompt: str | None = None) -> Work:
    """The work before extraction with the extraction prompt named `prompt` (None: the pipeline's), from data/
    alone: no Claude call. The categories to consolidate are those whose input changed (full), or those the
    incremental work queue changes (incremental.queue_categories)."""
    decisions = analyze.load_decisions(docs)
    if mode == "incremental":
        book = incremental.RuleBook.load()
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
        documents=frozenset(analyze.missing_extractions(docs, prompt)),
        categories=categories,
        lost=lost,
        sources={category: frozenset(ids) for category, ids in sources.items()},
        total_documents=len(docs),
        total_categories=len(sources),
        extract_version=analyze.extract_prompt(prompt).version,
    )


def check_rebuild(docs: list[Doc], *, allowed: bool, mode: str = "full", plan: Plan = Plan()) -> None:
    """Stop before any Claude call when the run would do more than an ordinary month's work unapproved.

    --allow-rebuild approves the work and saves it in data/rebuild.json before anything starts. A later run
    goes ahead while the part of its work outside that approval is ordinary: new minutes may arrive while a
    rebuild is unfinished, but other extra work (lost data, another prompt change) needs a new approval. The
    approval records the extraction prompt and model, and while documents it approved are left to extract, a run
    with another extraction model is refused unless it approves anew. The work's estimated cost is logged first,
    so it is known before it is approved.
    """
    work = pending_work(docs, mode, plan.prompt)
    approval = _load_approval(work.extract_version)
    switch = _model_switch(approval, work, plan)
    if switch and not allowed:
        raise SystemExit(f"Stopped before any Claude call: {switch}; or pass --allow-rebuild to extract the rest with "
                         f"{plan.extract_model}, mixing the two models.")
    if switch:
        logging.warning("Approving anew: %s", switch)
    reasons = work.reasons()
    if not reasons:
        return
    log_estimate(work, plan, mode)
    if allowed:
        _save_approval(work, plan)
        logging.warning("Approved, saved in %s: %s", REBUILD_MARKER.name, "; ".join(reasons))
        return
    if approval is not None:
        outside = work.outside(approval).reasons()
        if not outside:
            logging.warning("Continuing the work approved in %s: %s", REBUILD_MARKER.name, "; ".join(reasons))
            return
        reasons = [f"beyond the work approved in {REBUILD_MARKER.name}, {reason}" for reason in outside]
    raise SystemExit(_refusal(reasons, plan))


def _model_switch(approval: Approval | None, work: Work, plan: Plan) -> str | None:
    """Why this run would mix extraction models in an approved rebuild, or None: documents the approval covers are
    still to extract, and it was given for another model."""
    if approval is None or approval.extract_model in (None, plan.extract_model):
        return None
    left = approval.documents & work.documents
    if not left:
        return None
    return (f"the rebuild approved in {REBUILD_MARKER.name} extracts with {approval.extract_model}, not "
            f"{plan.extract_model}, and {len(left)} of its documents are left; continue it with "
            f"`{run_command(approval.extract_prompt, approval.extract_model)}`")


def _refusal(reasons: list[str], plan: Plan) -> str:
    """Why the guard stopped the run, with which extraction settings, the exact commands that approve the work and
    continue it, and the one that continues a rebuild approved for other settings."""
    stopped = (f"Stopped before any Claude call: {'; '.join(reasons)} (extracting with prompt {plan.prompt_name} and "
               f"{plan.extract_model}).")
    command = plan.command()
    if command == run_command(f"v{analyze.EXTRACT_VERSION}", EXTRACT_MODEL):
        how = (f" If this work is intended, run `{command} --allow-rebuild`, or on GitHub: Actions > Update rule "
               f"overview > Run workflow with '{REBUILD_INPUT_LABEL}' ticked. The approval is saved in "
               f"data/rebuild.json, and plain runs, the monthly one included, continue the work if it is cut off.")
    else:
        how = (f" If this work is intended, run `{command} --allow-rebuild`. The approval is saved in "
               f"data/rebuild.json, and `{command}` continues the work if it is cut off; plain runs, the monthly one "
               f"included, cannot, as they use the defaults.")
    return stopped + how + _other_rebuild(plan)


def _other_rebuild(plan: Plan) -> str:
    """A sentence naming the command that continues a rebuild approved for other extraction settings, if any."""
    marker = _read_marker()
    if marker is None:
        return ""
    approval = _approval_of(marker)
    command = run_command(approval.extract_prompt, approval.extract_model or EXTRACT_MODEL)
    if command == plan.command():
        return ""
    return f" {REBUILD_MARKER.name} holds an unfinished rebuild approved for other extraction settings: `{command}` " \
           f"continues it."


def update_rebuild_marker(docs: list[Doc], mode: str = "full", plan: Plan = Plan()) -> None:
    """After the analysis: approve what is left while it is more than ordinary or approved documents are left to
    extract, else remove the approval.

    Leftovers of an ordinary run count too (consolidation failing for most categories after a big meeting):
    the run was allowed to start that work, so the following runs may finish it. The new approval covers
    only what is left. It is kept until the approved documents are extracted, however few are left, so the runs
    that finish them keep to its extraction model.
    """
    work = pending_work(docs, mode, plan.prompt)
    reasons = work.reasons()
    approval = _load_approval(work.extract_version)
    left = approval.documents & work.documents if approval is not None else frozenset()
    if reasons or left:
        _save_approval(work, plan)
        logging.warning("Unfinished: %s. `%s` continues it (approval in %s)",
                        "; ".join(reasons) or f"{len(left)} approved documents to extract", plan.command(),
                        REBUILD_MARKER.name)
    elif REBUILD_MARKER.exists():
        REBUILD_MARKER.unlink(missing_ok=True)
        logging.info("Approved work finished; removed %s", REBUILD_MARKER.name)


def _save_approval(work: Work, plan: Plan) -> None:
    marker = {
        "extract_version": work.extract_version,
        "extract_prompt": plan.prompt_name,
        "extract_model": plan.extract_model,
        "consolidate_version": analyze.CONSOLIDATE_VERSION,
        "documents": sorted(work.documents),
        "categories": sorted(work.approved_categories()),
        "reasons": work.reasons(),
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        REBUILD_MARKER.write_text(json.dumps(marker, ensure_ascii=False, indent=1) + "\n")
    except OSError as exc:
        logging.warning("Could not save the approval in %s: %s", REBUILD_MARKER, exc)


def _read_marker() -> dict | None:
    try:
        marker = json.loads(REBUILD_MARKER.read_text())
    except (OSError, ValueError):
        return None
    return marker if isinstance(marker, dict) else None


def _approval_of(marker: dict) -> Approval:
    return Approval(frozenset(marker.get("documents") or ()), frozenset(marker.get("categories") or ()),
                    marker.get("extract_prompt") or f"v{marker.get('extract_version')}", marker.get("extract_model"))


def _load_approval(extract_version: int) -> Approval | None:
    """The saved approval, if it was given for the prompt versions this run uses."""
    marker = _read_marker()
    if marker is None or marker.get("extract_version") != extract_version \
            or marker.get("consolidate_version") != analyze.CONSOLIDATE_VERSION:
        return None
    return _approval_of(marker)


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


def estimate_cost(work: Work, plan: Plan, mode: str = "full") -> CostEstimate:
    """The work's cost: each document to extract at the mean measured cost of one extraction by the model
    (eval/runs.jsonl), and each category to consolidate at its share of the last full consolidation
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
    categories = len(work.approved_categories())
    if mode != "full":
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


def log_estimate(work: Work, plan: Plan, mode: str) -> None:
    """Log the work's estimated cost. An unreadable run log only warns: the estimate informs, it never stops a run."""
    try:
        text = estimate_cost(work, plan, mode).describe(plan.max_cost)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        logging.warning("Could not estimate the cost of this work: %s: %s", type(exc).__name__, exc)
        return
    logging.warning("%s", text)


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
    from before it was logged; the monthly runs then consolidated in full."""
    for line in reversed(_log_lines(path)):
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
                 history: checks.HistoryCheck | None, failures: list[str], today: date) -> None:
    """Write run-report.md and, when running on GitHub, show it on the run page; a write error only warns."""
    try:
        report = run_report(code, steps, problems, history, failures, today)
    except Exception:  # the outcome must still reach the routing and the pull request
        logging.exception("Could not build the run report")
        report = "\n".join([_marker(code), f"# Rule overview update {today.isoformat()}", "",
                            _outcome(code, problems, failures), "", "The full report could not be built; the "
                            "run's log has every finding.", ""])
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
               history: checks.HistoryCheck | None, failures: list[str], today: date) -> str:
    """Markdown for the pull request that reviews a run, and for its run page: the outcome, the Claude calls,
    every error and warning, and what the run would change in earlier years."""
    lines = [_marker(code), f"# Rule overview update {today.isoformat()}", "", _outcome(code, problems, failures),
             ""]
    if steps:
        lines.append(step_summary(steps, Counter(problem.kind for problem in problems)))
    for severity, heading in (("error", "Errors"), ("warning", "Warnings")):
        found = [problem for problem in problems if problem.severity == severity]
        lines += [f"## {heading}: {len(found)}", ""]
        if found:
            lines += [f"- **{problem.kind}**: {problem.message}" for problem in found] + [""]
    if history is not None:
        lines += _history_section(history)
    return "\n".join(lines).rstrip() + "\n"


def _marker(code: int) -> str:
    return CHECKS_FAILED if code == EXIT_REVIEW else CHECKS_PASSED


def _outcome(code: int, problems: list[checks.Problem], failures: list[str]) -> str:
    """What the run's result is and what to do with it. Merging a pull request publishes it only when the website
    workflow's checks pass: they see every error but history, which only a run can compare."""
    failed = "; ".join(failures)
    if code == 0:
        return "**Ready to publish**: the checks found no errors."
    if code == EXIT_FAILED:
        return (f"**The run failed**: {failed}. The checks found no errors, so the results so far are kept; the "
                f"next run retries the rest.")
    blocking = sorted({problem.kind for problem in checks.errors(problems)} - {"history"})
    if blocking:
        outcome = (f"**Needs review**: the checks found errors in the data ({', '.join(blocking)}), which must be "
                   f"fixed before it can be published: the website workflow refuses data with these errors, so "
                   f"merging alone publishes nothing. Fix data/ on this branch, or close the pull request.")
    else:
        outcome = ("**Needs review**: this update changes what applied in earlier years, or that could not be "
                   "checked (see History and Errors). Merge the pull request to publish it, or close it to discard it.")
    return outcome + (f"\n\nThe run also failed: {failed}." if failures else "")


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
