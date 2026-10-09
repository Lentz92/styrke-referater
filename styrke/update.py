"""Bring the DSF rule overview up to date.

    uv run -m styrke.update                  # download new minutes, analyse what changed, render regelsaet/
    uv run -m styrke.update --offline        # skip styrke.dk, use the files already in referater/
    uv run -m styrke.update --render-only    # only rebuild the Markdown and the website from data/

Steps: 1) styrke/scrape.py downloads new documents and writes data/manifest.json.
2) styrke/analyze.py asks Claude (through styrke/claude.py) to extract decisions from new or changed
documents and to consolidate them per category into rule histories; both are cached in data/.
A run that calls Claude adds a line with its tokens, cost and models to data/runs.jsonl.
3) styrke/checks.py checks the result, including what the analysis changed in the past; the outcome goes to
run-report.md and the GitHub step summary.
4) styrke/render.py writes regelsaet/<year>.md, regelsaet/regler/<area>.md and regelsaet/README.md.
5) styrke/website.py writes the website to _site/ (published on GitHub Pages by .github/workflows/pages.yml).

Exit codes, which the monthly workflow routes on: 0 the checks passed, publish; 3 the checks found errors, the
data is written but must be reviewed in a pull request; 1 the run failed (the checks passed, so its partial
results are kept). A render-only run reports what the checks find and exits 0 whatever they find.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from itertools import groupby

from styrke import analyze, checks, claude, incremental, render, scrape, website
from styrke.claude import StepSummary
from styrke.scrape import Doc

# The models the pipeline calls, each at Claude Code's default effort (effort None); it extracts with the prompt of
# analyze.EXTRACT_VERSION.
EXTRACT_MODEL = "claude-opus-5-5"
CONSOLIDATE_MODEL = "claude-opus-5-5"  # a full consolidation; in incremental mode, tie-breaks and rule updates
ASSIGN_MODEL = "claude-sonnet-5-5"  # incremental mode: the votes on which rule a new decision belongs to
# How rule files are brought up to date: "incremental" (the default) files each new decision into its rule and leaves
# the others as they are (styrke/incremental.py); "full" consolidates each changed category anew (analyze.consolidate),
# which migrates the rules after a new extraction or consolidation prompt, or a new CONSOLIDATE_VERSION.
CONSOLIDATE_MODES = ("incremental", "full")
DEFAULT_CONSOLIDATE_MODE = "incremental"
# A migration (a full consolidation with --allow-rebuild) extracts every document again after a new extraction
# prompt or model (16.74 USD at list price with Opus in October 2026) and consolidates every category anew (8.58):
# its limits let it finish in one run, and it runs offline, so no new minutes arrive meanwhile (the next run adds them).
MIGRATION_MAX_COST = 40
MIGRATION_TIME_BUDGET = 150
# The command that migrates: a full consolidation with --allow-rebuild, offline, with the migration's limits.
MIGRATE = (f"uv run -m styrke.update --offline --consolidate-mode full --allow-rebuild --max-cost {MIGRATION_MAX_COST} "
           f"--time-budget {MIGRATION_TIME_BUDGET}")
RUNS_LOG = scrape.DATA_DIR / "runs.jsonl"
# More documents than this to extract means a new EXTRACT_VERSION or lost data, not new minutes.
REBUILD_SHARE = 0.10
# More categories than this to consolidate before any extraction means a new CONSOLIDATE_VERSION or similar.
REBUILD_CATEGORY_SHARE = 0.5
# A run's limits unless it sets others; the workflow sets none, so every run on GitHub keeps to these.
DEFAULT_MAX_COST = 15
DEFAULT_TIME_BUDGET = 75
# How a rebuild that was cut off is finished: part of the guard's refusal and of a cut-off run's report. Extractions
# are cached by prompt and model, and a full consolidation skips each category consolidated with today's input.
RERUN = ("A rebuild cut off by its cost or time budget, or by failed calls, is finished by running the same command "
         "again (on GitHub, merge the review pull request of the cut-off run first, if it opened one, so what it paid "
         "for reaches main, then run the workflow again with the same boxes ticked): what was done is kept, so it does "
         "only the rest.")
# What the rebuild guard's refusal says after its reasons. The boxes are the workflow inputs as update.yml describes
# them; the workflow passes no limits, so every run on GitHub keeps to the default ones.
HOW_TO_PROCEED = (
    f"If this work is intended, run with --allow-rebuild (the run then stops at its --max-cost, {DEFAULT_MAX_COST} USD "
    f"by default); where a reason says only a full consolidation takes it in or migrates it, run `{MIGRATE}` instead "
    f"(it stops at {MIGRATION_MAX_COST} USD), after which plain runs go on incrementally. On GitHub: Actions > Update "
    f"rule overview > Run workflow with 'Allow a rebuild (--allow-rebuild)' ticked, and for a migration also "
    f"'Consolidate in full, to migrate a prompt or version change (--consolidate-mode full)'; each run there stops at "
    f"{DEFAULT_MAX_COST} USD or {DEFAULT_TIME_BUDGET} minutes, so a migration may take several. {RERUN}")
# What the run found, for the pull request that reviews it (its body) and the GitHub step summary; not committed.
RUN_REPORT = scrape.ROOT / "run-report.md"
EXIT_FAILED = 1
EXIT_REVIEW = 3  # the checks found errors: the data is written, but must go to a pull request, not the website


def main() -> None:
    parser = argparse.ArgumentParser(prog="uv run -m styrke.update", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--offline", action="store_true", help="skip the download from styrke.dk")
    parser.add_argument("--render-only", action="store_true", help="only build the Markdown and the website from data/")
    parser.add_argument("--only", metavar="REGEX",
                        help="only extract documents whose id matches, and only consolidate the categories their "
                             "decisions are in (for testing; skips the rebuild guard)")
    parser.add_argument("--consolidate-mode", choices=CONSOLIDATE_MODES, default=DEFAULT_CONSOLIDATE_MODE,
                        help="incremental: file each new decision into its rule and leave every other rule as it is; "
                             "full: consolidate each category with changed decisions anew, to migrate a new prompt or "
                             f"CONSOLIDATE_VERSION (with --allow-rebuild) (default: {DEFAULT_CONSOLIDATE_MODE})")
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
    budget = claude.RunBudget(minutes=args.time_budget, max_cost_usd=args.max_cost)

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
                rebuild = check_rebuild(docs, allowed=args.allow_rebuild, mode=args.consolidate_mode)
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
    # A rebuild cut off by its limits or by failed calls is finished by running it again (RERUN).
    cut_off = rebuild and stop is not None

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
                                  analyze.missing_extractions(docs, model=EXTRACT_MODEL),
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


def analyse(args: argparse.Namespace, docs: list[Doc], budget: claude.RunBudget, steps: dict[str, StepSummary],
            failures: list[str]) -> Exception | None:
    """Extract and consolidate what changed, in the run's --consolidate-mode, filling in `steps`. An exception is
    returned, not raised: what was written before it is checked all the same."""
    mode = args.consolidate_mode
    targets = [d for d in docs if re.search(args.only, d.id)] if args.only else docs
    # A test run consolidates only the categories its documents had decisions in, or now have.
    selected = _categories_of(targets) if args.only else None
    try:
        # What the rule files reflect before the extraction, so a decision it only re-dates still reaches its rule.
        known = (incremental.known_inputs(analyze.load_decisions(docs), incremental.RuleBook.load(), docs)
                 if mode == "incremental" else None)
        steps["extract"] = analyze.extract(targets, model=EXTRACT_MODEL, effort=None, workers=args.workers,
                                           budget=budget)
        if mode == "incremental":
            settings = incremental.Settings(ASSIGN_MODEL, CONSOLIDATE_MODEL, workers=args.workers)
            steps["consolidate"] = incremental.consolidate(
                docs, analyze.load_decisions(docs), settings, budget,
                documents=None if not args.only else {d.id for d in targets}, known=known)
        else:
            steps["consolidate"] = analyze.consolidate(
                analyze.load_decisions(docs),
                {d.id: d.organ_label for d in docs},
                model=CONSOLIDATE_MODEL,
                effort=None,
                workers=args.workers,
                budget=budget,
                categories=None if selected is None else selected | _categories_of(targets),
            )
    except Exception as exc:
        logging.error("The analysis failed: %s; checking what it wrote before failing the run", exc)
        failures.append(f"the analysis stopped: {type(exc).__name__}: {exc}")
        return exc
    finally:
        record_run(steps, {"extract_prompt": analyze.extract_prompt().name, "consolidate_mode": mode})
    return None


def _categories_of(docs: list[Doc]) -> set[str]:
    """The home categories (analyze.home_categories) of the documents' decisions."""
    return set(analyze.home_categories(analyze.load_decisions(docs), analyze.load_rules()).values())


# --------------------------------------------------------------------------- rebuild guard

@dataclass(frozen=True)
class Work:
    """The Claude work a run would start with, as the rebuild guard judges it."""
    documents: frozenset[str]  # ids of the documents to extract
    categories: frozenset[str]  # categories to consolidate (in incremental mode, to file decisions in)
    # Categories whose rules must be redone: in full mode those to consolidate; in incremental mode those of changed
    # and retired decisions, as filing the new decisions of documents already extracted is ordinary work.
    redone: frozenset[str]
    lost: frozenset[str]  # categories among them with decisions but no rule file
    outdated: frozenset[str]  # categories consolidated with another CONSOLIDATE_VERSION: only `full` migrates them
    total_documents: int
    total_categories: int  # categories with decisions
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
        - more than REBUILD_CATEGORY_SHARE of the categories to redo: a new CONSOLIDATE_VERSION, a change to the
          consolidation input, edited decisions, or what a cut-off full run left. New decisions waiting to be filed
          incrementally do not count, however many categories a cut-off month left them in: the next plain run
          files them;
        - in incremental mode, a migration (see migration).
        """
        reasons = []
        if len(self.documents) > REBUILD_SHARE * self.total_documents:
            reasons.append(f"{len(self.documents)} of {self.total_documents} documents need extraction")
        if self.lost:
            lost = ", ".join(sorted(self.lost))
            reasons.append(f"categories with decisions but no rule file in data/regler/: {lost}")
        if len(self.redone) > REBUILD_CATEGORY_SHARE * self.total_categories:
            reasons.append(f"{len(self.redone)} of {self.total_categories} categories need consolidating again")
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
                         f"prompt or model than v{analyze.EXTRACT_VERSION} and {EXTRACT_MODEL}, which only a full "
                         f"consolidation takes in")
        if self.outdated:
            found.append(f"categories consolidated with another CONSOLIDATE_VERSION, which only a full "
                         f"consolidation migrates: {', '.join(sorted(self.outdated))}")
        return found


def pending_work(docs: list[Doc], mode: str = DEFAULT_CONSOLIDATE_MODE) -> Work:
    """The work before extraction with the pipeline's extraction prompt and EXTRACT_MODEL, from data/ alone: no Claude
    call. The categories to consolidate are those whose input changed (full), or those the incremental work queue
    changes (incremental.queue_categories), of which those of changed and retired decisions are redone (Work.redone).
    In incremental mode, categories consolidated with another CONSOLIDATE_VERSION, and documents extracted with another
    prompt or model, are a migration: only `full` does it."""
    decisions = analyze.load_decisions(docs)
    book = incremental.RuleBook.load()
    outdated = frozenset(category for category, stored in book.files.items()
                         if stored.get("version") != analyze.CONSOLIDATE_VERSION)
    if mode == "incremental":
        queue = incremental.work_queue(decisions, book, docs)
        categories = incremental.queue_categories(queue, decisions, book)
        redone = incremental.queue_categories([replace(work, new=()) for work in queue], decisions, book)
        lost = frozenset(category for category in categories if category not in book.files)
    else:
        todo = analyze.consolidation_todo(decisions, {d.id: d.organ_label for d in docs})
        categories = redone = frozenset(job.category for job in todo)
        lost = frozenset(job.category for job in todo if job.rules_missing)
    home = analyze.home_categories(decisions, analyze.load_rules())
    return Work(
        documents=frozenset(analyze.missing_extractions(docs, model=EXTRACT_MODEL)),
        categories=categories,
        redone=redone,
        lost=lost,
        outdated=outdated,
        total_documents=len(docs),
        total_categories=len(set(home.values())),
        superseded=frozenset(analyze.superseded_extractions(docs, model=EXTRACT_MODEL)),
        mode=mode,
    )


def check_rebuild(docs: list[Doc], *, allowed: bool, mode: str) -> bool:
    """Stop before any Claude call when the run would do more than an ordinary month's work without --allow-rebuild
    (`allowed`); return whether it goes ahead with such work (a rebuild).

    Nothing is saved between runs: a rebuild that is cut off is finished by running the same command again, which
    finds what is done current and does only the rest (RERUN), and until then a plain run stops here again.
    Incremental mode never does a migration (Work.migration), allowed or not.
    """
    work = pending_work(docs, mode)
    reasons = work.reasons()
    if not reasons:
        return False
    if not allowed or (mode == "incremental" and work.migration()):  # incremental cannot migrate, allowed or not
        raise SystemExit(f"Stopped before any Claude call: {'; '.join(reasons)}. {HOW_TO_PROCEED}")
    logging.warning("Allowed by --allow-rebuild: %s", "; ".join(reasons))
    return True


# --------------------------------------------------------------------------- run log

def record_run(steps: dict[str, StepSummary], settings: dict | None = None) -> None:
    """Append the run's Claude usage to data/runs.jsonl (claude.append_run_log) after the CLI version and the run's
    `settings` (prompt, consolidation mode), which tell a full consolidation from an incremental one. A run without
    calls adds nothing: the log follows what runs cost, not how often they ran. A write error only warns, so it never
    hides the run's outcome."""
    if not any(step.calls for step in steps.values()):
        return
    try:
        header = {"cli": claude.cli_version(), **(settings or {})}
        claude.append_run_log(RUNS_LOG, datetime.now(timezone.utc), header, steps)
    except OSError as exc:
        logging.warning("Could not write %s: %s", RUNS_LOG, exc)


def step_summary(steps: dict[str, StepSummary], problem_counts: Counter[str]) -> str:
    """Markdown with the same numbers as data/runs.jsonl, plus the counts from styrke/checks.py."""
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
                 cut_off: bool = False) -> None:
    """Write run-report.md and, when running on GitHub, show it on the run page; a write error only warns.
    `cut_off`: whether the run was cut off in a rebuild."""
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
               cut_off: bool = False) -> str:
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


def _outcome(code: int, problems: list[checks.Problem], failures: list[str], cut_off: bool = False) -> str:
    """What the run's result is and what to do with it. Merging a pull request publishes it only when the website
    workflow's checks pass: they see every error but history, which only a run can compare. A rebuild that was cut off
    (`cut_off`) is finished by running it again (RERUN), from the merged pull request if the run opened one: closing it
    would throw away what the run has paid for."""
    failed = "; ".join(failure.rstrip(".") for failure in failures)  # each is followed by more text
    if code == 0:
        return "**Ready to publish**: the checks found no errors."
    if code == EXIT_FAILED:
        outcome = (f"**The run failed**: {failed}. The checks found no errors, so the results so far are kept"
                   + ("." if cut_off else "; the next run retries the rest."))
    else:
        blocking = sorted({problem.kind for problem in checks.errors(problems)} - {"history"})
        if blocking and cut_off:
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
                       "it" + (" (and what the run has paid for toward its rebuild)." if cut_off else "."))
        if failures:
            outcome += f"\n\nThe run also failed: {failed}."
    if cut_off:
        outcome += (f"\n\n**Unfinished rebuild**: the run was cut off before it finished the work --allow-rebuild let "
                    f"it do. {RERUN} Until then a plain run stops before any Claude call while more than an ordinary "
                    f"month's work is left.")
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
