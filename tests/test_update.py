import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from conftest import extracted, rule

import analyze
import update
from analyze import StepSummary, Usage

CATEGORIES = ["okonomi", "master", "dommere", "staevner"]
# The fake answers here are for full consolidation; incremental runs (the default) are tested in test_incremental.py.
FULL = ("--consolidate-mode", "full")
QUOTE = "Eventuelt intet"  # in every document's text (conftest.TEXT): quoted again, a decision keeps its id


def _extracted(world, *numbers: int) -> None:
    """Documents doc<i>, extracted, each with one decision in CATEGORIES[i % 4]."""
    for i in numbers:
        world.document(f"doc{i}", "2024", extracted(f"Regel doc{i}", kategori=CATEGORIES[i % 4], citat=QUOTE))


def _downloaded(world, n: int) -> None:
    """Documents doc0 to doc<n - 1>, not extracted yet."""
    for i in range(n):
        world.downloaded(f"doc{i}")


def _consolidated(world) -> None:
    """The rule files of a consolidation that left every decision out as a one-off."""
    for category in CATEGORIES:
        world.rules(category, udeladt=tuple(d.ref for d in world.decisions() if d.kategori == category))
    world.consolidated()


@pytest.fixture
def analysed(world):
    """20 documents, all extracted and consolidated, as after an ordinary run."""
    _extracted(world, *range(20))
    _consolidated(world)


# ---------------------------------------------------------------- rebuild guard

def _reasons(world, mode: str = "full") -> str:
    """Why the rebuild guard stops a run; these tests are about full consolidation unless they say otherwise."""
    return "; ".join(update.pending_work(world.docs, mode).reasons())


def test_a_few_new_documents_pass_the_rebuild_guard(world):
    _extracted(world, *range(18))
    _consolidated(world)
    world.downloaded("doc18")
    world.downloaded("doc19")
    assert _reasons(world) == ""  # 2 of 20 = 10 %


def test_a_replaced_file_passes_the_rebuild_guard(world, analysed):
    world.docs[0] = replace(world.docs[0], sha256="new")
    assert _reasons(world) == ""


def test_a_category_left_over_from_an_earlier_run_passes(world, analysed):
    world.document("doc0", "2024", extracted("Regel doc0", "Ændret regel"))
    assert _reasons(world) == ""  # 1 of 4 categories


def test_many_documents_to_extract_stop_the_run(world):
    _downloaded(world, 20)
    _extracted(world, *range(17))
    assert "3 of 20 documents" in _reasons(world)


def _extracted_with_v2(world) -> None:
    """The cached extractions as prompt v2 made them, before v3 became the pipeline's."""
    for doc in world.docs:
        analyze._write_json(analyze.DECISIONS_DIR / f"{doc.id}.json",
                            {**world.extraction(doc.id), "version": analyze.extract_prompt("v2").version})


def test_the_monthly_run_on_data_extracted_with_v2_refuses_before_any_call_and_names_the_migration(
        run, world, analysed, fake_claude):
    _extracted_with_v2(world)
    for options in ((), ("--allow-rebuild",)):  # incremental consolidation does not migrate, allowed or not
        with pytest.raises(SystemExit) as refused:
            run(*options)
        message = str(refused.value)
        assert (f"20 of 20 documents were extracted with another prompt or model than v{analyze.EXTRACT_VERSION} "
                f"and {update.EXTRACT_MODEL}, which only a full consolidation takes in") in message
        assert update.HOW_TO_PROCEED in message and update.MIGRATE in message
    assert fake_claude.invocations("call") == 0


def _reworded(world) -> list[dict]:
    """Fake answers, one per call in order: each document's decision comes back reworded, and each category's
    consolidation keeps its rules, one per decision (each category keeps the refs it was given)."""
    rules = [rule(f"Regel {doc.id}", None, f"{doc.id}#1") for doc in world.docs]
    return [{"moededato": None, "regler": rules, "udeladt": [],
             "beslutninger": [extracted(f"Regel {doc.id}, ny", kategori=CATEGORIES[i % 4], citat=QUOTE)]}
            for i, doc in enumerate(world.docs)]


def test_a_cut_off_migration_is_finished_by_running_its_command_again_and_plain_runs_wait_for_it(
        run, world, fake_claude):
    _extracted(world, *range(20))
    for i, category in enumerate(CATEGORIES):
        world.rules(category, *(rule(f"Regel doc{n}", f"regel-doc{n}", f"doc{n}#1") for n in range(i, 20, 4)))
    world.consolidated()
    _extracted_with_v2(world)
    fake_claude.answers(*_reworded(world))
    migrate = update.MIGRATE.split()[3:]  # the options of the command the refusals name

    # The migration, cut off by its cost cap after 10 of 20 extractions (0.25 USD each): its rules are stale, so it
    # goes to review, and its report says to merge, then run the same command again.
    assert _exit_code(run, *migrate, "--max-cost", "2.5") == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "- **stale**" in report and "Merge the pull request, so what the run has paid for is kept" in report
    assert "**Unfinished rebuild**" in report and update.RERUN in report

    # A plain run (the monthly one) stops before any call and names that command.
    assert _exit_code(run) == update.EXIT_REVIEW and fake_claude.invocations("call") == 10
    report = update.RUN_REPORT.read_text()
    assert "which only a full consolidation takes in" in report and update.HOW_TO_PROCEED in report

    # The same command again does only the rest: the 10 documents left, then the 4 categories in full.
    run(*migrate)
    assert fake_claude.option("--system-prompt") == [analyze.EXTRACT_SYSTEM] * 20 + [analyze.CONSOLIDATE_SYSTEM] * 4
    for doc in world.docs:
        saved = world.extraction(doc.id)
        assert (saved["version"], saved["model"]) == (analyze.EXTRACT_VERSION, update.EXTRACT_MODEL)
        assert [d["id"] for d in saved["beslutninger"]] == [f"{doc.id}#1"]  # carried over
    assert "**Ready to publish**" in update.RUN_REPORT.read_text()
    # The run log records what was paid for: the pipeline's prompt, in full mode.
    assert {k: _run_log()[-1][k] for k in ("extract_prompt", "consolidate_mode")} == \
        {"extract_prompt": f"v{analyze.EXTRACT_VERSION}", "consolidate_mode": "full"}

    run()  # and a plain run then has nothing to do
    assert fake_claude.invocations("call") == 24


# ---------------------------------------------------------------- main()

def _run_log() -> list[dict]:
    return [json.loads(line) for line in update.RUNS_LOG.read_text().splitlines()] if update.RUNS_LOG.exists() else []


def test_only_and_allow_rebuild_pass_the_guard_and_plain_runs_do_not(run, world, fake_claude):
    _downloaded(world, 3)
    fake_claude.answer({"moededato": None, "beslutninger": []})

    with pytest.raises(SystemExit, match="Stopped before any Claude call.*--allow-rebuild"):
        run(*FULL)
    assert fake_claude.invocations("call") == 0

    run(*FULL, "--only", "^doc0$")
    assert fake_claude.invocations("call") == 1

    run(*FULL, "--allow-rebuild")
    assert fake_claude.invocations("call") == 3
    assert len(_run_log()) == 2


def test_only_consolidates_the_categories_of_the_selected_documents(run, world, analysed, fake_claude, monkeypatch):
    monkeypatch.setattr(analyze, "CONSOLIDATE_VERSION", analyze.CONSOLIDATE_VERSION + 1)  # all 4 categories due
    world.docs[1] = replace(world.docs[1], sha256="new")  # doc1: one decision in master
    fake_claude.answer({"moededato": None, "beslutninger": [extracted("Ny regel", kategori="dommere")], "regler": [],
                        "udeladt": [*(f"{doc.id}#1" for doc in world.docs), "doc1#2"]})

    run(*FULL, "--only", "^doc1$")

    assert fake_claude.invocations("call") == 3  # extract doc1, then master (before) and dommere (after)
    versions = {c: world.stored(c)["version"] for c in CATEGORIES}
    assert versions == {"okonomi": analyze.CONSOLIDATE_VERSION - 1, "master": analyze.CONSOLIDATE_VERSION,
                        "dommere": analyze.CONSOLIDATE_VERSION, "staevner": analyze.CONSOLIDATE_VERSION - 1}


def test_a_run_without_calls_writes_no_run_log_line(run, analysed, fake_claude):
    run(*FULL)
    assert _run_log() == [] and fake_claude.invocations("version") == 0


def test_the_run_log_is_written_when_a_later_step_crashes(run, world, fake_claude, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("consolidation crashed")

    monkeypatch.setattr(analyze, "consolidate", crash)
    _downloaded(world, 3)
    fake_claude.answer({"moededato": None, "beslutninger": []})
    with pytest.raises(RuntimeError, match="consolidation crashed"):
        run(*FULL, "--allow-rebuild")
    (line,) = _run_log()
    assert line["steps"]["extract"]["calls"] == 3


def test_the_step_summary_is_written_before_rendering(run, world, fake_claude, monkeypatch):
    def crash(*args):
        raise RuntimeError("render crashed")

    summary = world.root / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(update.render, "render", crash)
    _downloaded(world, 3)
    fake_claude.answer({"moededato": None, "beslutninger": []})
    with pytest.raises(RuntimeError, match="render crashed"):
        run(*FULL, "--allow-rebuild")
    assert "| extract | 3 |" in summary.read_text()
    report = update.RUN_REPORT.read_text()
    assert "**The run failed**: writing the pages failed: RuntimeError: render crashed." in report
    assert report.startswith(update.CHECKS_PASSED)  # the data is fine; only the pages are not written


def test_skipped_calls_fail_the_run_whose_checks_pass_and_keep_its_results(run, world, fake_claude):
    _downloaded(world, 3)
    with pytest.raises(SystemExit, match="3 Claude calls failed or were skipped"):
        run(*FULL, "--allow-rebuild", "--max-cost", "0")
    assert fake_claude.invocations("call") == 0
    report = update.RUN_REPORT.read_text()
    assert report.startswith(update.CHECKS_PASSED) and "**The run failed**: 3 Claude calls failed or were skipped." \
        in report


def test_the_guard_refuses_a_missing_rule_file_before_any_call(run, analysed, fake_claude):
    (analyze.RULES_DIR / "master.json").unlink()
    with pytest.raises(SystemExit, match="no rule file in data/regler/: master") as refused:
        run(*FULL)
    assert update.HOW_TO_PROCEED in str(refused.value) and fake_claude.invocations("call") == 0
    report = update.RUN_REPORT.read_text()  # new downloads are kept: the data passes the checks
    assert report.startswith(update.CHECKS_PASSED) and "**The run failed**: Stopped before any Claude call" in report
    assert "only the rest. The checks found no errors" in report  # the refusal's own full stop is not doubled


# ---------------------------------------------------------------- outcome: exit code and run report

def _exit_code(run, *args) -> int:
    with pytest.raises(SystemExit) as stopped:
        run(*args)
    return stopped.value.code if isinstance(stopped.value.code, int) else 1


def test_a_run_whose_checks_pass_is_ready_to_publish(run, analysed):
    run(*FULL)
    assert "**Ready to publish**" in update.RUN_REPORT.read_text()


def test_a_decision_the_consolidation_drops_sends_the_run_to_review(run, world, analysed, fake_claude):
    world.downloaded("doc20")  # new; its decision lands in okonomi, which leaves it out without a word
    fake_claude.answer({"moededato": None, "beslutninger": [extracted("Ny regel")], "regler": [],
                        "udeladt": [f"doc{i}#1" for i in range(20)]})

    assert _exit_code(run, *FULL) == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "**Needs review**" in report and "## Errors: 1" in report
    assert report.startswith(update.CHECKS_FAILED) and "merging alone publishes nothing" in report
    assert "- **unassigned**: doc20#1 (okonomi: Ny regel) is in no rule" in report


GEBYR = rule("Gebyr", None, "old#1")  # old's decision as a rule of its own, as a consolidation answers it


def _gebyr(world) -> None:
    """old (2015), consolidated into Gebyr, and new (2024), not extracted yet."""
    world.document("old", "2015-03-01", extracted("Regel old"))
    world.downloaded("new", "2024-03-01")
    world.rules("okonomi", rule("Gebyr", "gebyr", "old#1"))
    world.consolidated()


@pytest.mark.parametrize("kept", [True, False])
def test_a_run_that_changes_what_applied_in_past_years_needs_review(run, world, fake_claude, kept):
    _gebyr(world)
    # new's decision lands in okonomi too, whose consolidation either keeps the rule or drops it as a one-off.
    fake_claude.answer({"moededato": None, "beslutninger": [extracted("Ny regel")],
                        "regler": [GEBYR] if kept else [], "udeladt": ["new#1"] if kept else ["old#1", "new#1"]})

    if kept:
        run(*FULL, "--allow-rebuild")
        assert "No rule in force there changed." in update.RUN_REPORT.read_text()
        return
    assert _exit_code(run, *FULL, "--allow-rebuild") == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "- **history**: Gebyr (gebyr): what was in force in 2015–2026 changed, although none of its decisions " \
           "did" in report
    assert "| Gebyr (`gebyr`) | 2015–2026 | old#1 | not in force | none changed |" in report
    assert "Merge the pull request to publish it" in report


def test_a_retry_of_a_failed_consolidation_does_not_change_the_past(run, world, fake_claude):
    _gebyr(world)
    changed = {"ref": "new#1", "effekt": "aendret", "tekst": None, "kort": "ændret", "kort_regel": None}
    fake_claude.answer({"moededato": None, "beslutninger": [extracted("Ny regel")],
                        "regler": [{**GEBYR, "versioner": [*GEBYR["versioner"], changed]}], "udeladt": []})

    fake_claude.plan("ok", "error")  # new is extracted, and the consolidation fails
    assert _exit_code(run, *FULL, "--allow-rebuild") == update.EXIT_FAILED
    fake_claude.plan("ok")
    run(*FULL, "--allow-rebuild")  # Gebyr takes up new#1 from 2024 on, which the first run did not get to


def test_history_that_cannot_be_checked_is_not_published(run, analysed, monkeypatch):
    def crash(*args):
        raise RuntimeError("snapshot broke")

    monkeypatch.setattr(update.checks, "check_history", crash)
    assert _exit_code(run, *FULL) == update.EXIT_REVIEW
    assert "- **history**: history could not be checked: RuntimeError: snapshot broke" in update.RUN_REPORT.read_text()


def test_the_outcome_is_reported_when_the_report_cannot_be_built(run, analysed, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("report broke")

    monkeypatch.setattr(update, "run_report", crash)
    run(*FULL)
    report = update.RUN_REPORT.read_text()
    assert report.startswith(update.CHECKS_PASSED) and "**Ready to publish**" in report


def test_a_run_that_fails_with_errors_still_goes_to_review(run, world, analysed, monkeypatch):
    analyze._write_json(analyze.RULES_DIR / "okonomi.json",
                        {**world.stored("okonomi"), "udeladt": []})  # its decisions are dropped, not one-offs

    def crash(*args, **kwargs):
        raise RuntimeError("consolidation crashed")

    monkeypatch.setattr(analyze, "consolidate", crash)
    assert _exit_code(run, *FULL) == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "The run also failed: the analysis stopped: RuntimeError: consolidation crashed." in report


def test_the_checks_command_fails_on_errors_only(world, analysed, monkeypatch, capsys):
    import checks

    monkeypatch.setattr(checks.scrape, "load_manifest", lambda: world.docs)
    checks.main()
    assert capsys.readouterr().out.startswith("Errors: 0\nWarnings: 0\n")

    analyze._write_json(analyze.RULES_DIR / "master.json", {**world.stored("master"), "udeladt": []})
    with pytest.raises(SystemExit) as failed:
        checks.main()
    assert failed.value.code == 1
    assert capsys.readouterr().out.startswith("Errors: 5 (unassigned 5)\n  unassigned: doc1#1 (master: Regel doc1)")

    world.document("doc1", "2024", extracted("Regel doc1", "Ændret regel", "master"))  # master is to be consolidated
    checks.main()  # again: not an error yet


# ---------------------------------------------------------------- run log and step summary

def _step(label: str, calls: int, cost: float) -> StepSummary:
    usage = Usage(model="claude-sonnet-5-5", output_by_model={"claude-sonnet-5-5": 52}, input_tokens=2,
                  cache_read_tokens=967, cache_write_tokens=2514, cost_usd=cost, attempts=calls)
    return StepSummary(label, calls=calls, failed=1, skipped=2, usage=usage, seconds=61.4)


def test_each_run_appends_one_line_to_the_run_log(tmp_path):
    path = tmp_path / "runs.jsonl"
    steps = {"extract": _step("Extract", 3, 0.123456), "consolidate": _step("Consolidate", 0, 0.0)}
    when = datetime(2026, 11, 1, 6, 0, tzinfo=timezone.utc)

    update.append_run_log(path, steps, "2.1.294", when)
    update.append_run_log(path, steps, "2.1.294", when)

    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0] == {
        "time": "2026-11-01T06:00:00+00:00",
        "cli": "2.1.294",
        "steps": {
            "extract": {"calls": 3, "failed": 1, "skipped": 2,
                        "tokens": {"input": 2, "output": 52, "cache_read": 967, "cache_write": 2514},
                        "cost_usd": 0.1235, "models": ["claude-sonnet-5-5"], "seconds": 61},
            "consolidate": {"calls": 0, "failed": 1, "skipped": 2,
                            "tokens": {"input": 2, "output": 52, "cache_read": 967, "cache_write": 2514},
                            "cost_usd": 0.0, "models": ["claude-sonnet-5-5"], "seconds": 61},
        },
    }


def test_a_run_log_that_cannot_be_written_only_warns(tmp_path, fake_claude, caplog, monkeypatch):
    monkeypatch.setattr(update, "RUNS_LOG", tmp_path / "missing-dir" / "runs.jsonl")
    update.record_run({"extract": _step("Extract", 1, 0.5)})
    assert "Could not write" in caplog.text


def test_step_summary_shows_the_numbers_and_the_check_results():
    text = update.step_summary({"extract": _step("Extract", 3, 1.5)}, Counter({"date": 13, "effect": 2}))
    for number in ("3483", "3481", "52", "1.50", "claude-sonnet-5-5", "date: 13", "effect: 2"):
        assert number in text
