import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

import analyze
import scrape
import update
from analyze import StepSummary, Usage
from scrape import Doc

CATEGORIES = ["okonomi", "master", "dommere", "staevner"]


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path / "beslutninger")
    monkeypatch.setattr(analyze, "RULES_DIR", tmp_path / "regler")
    monkeypatch.setattr(update, "RUNS_LOG", tmp_path / "runs.jsonl")
    monkeypatch.setattr(update, "RUN_REPORT", tmp_path / "run-report.md")
    analyze.DECISIONS_DIR.mkdir()
    analyze.RULES_DIR.mkdir()
    return tmp_path


def _raw(kategori: str, tekst: str) -> dict:
    return {"emne": tekst, "kategori": kategori, "udfald": "vedtaget", "forslagsstiller": None, "handling": "ny",
            "niveau": "staevneregel", "tekst": tekst, "citat": "intet", "side": None, "stemmer": None,
            "gaelder_fra": None, "gaelder_til": None}


def _docs(tmp_path, n: int) -> list[Doc]:
    docs = [Doc(f"doc{i}", "bestyrelse", "Referat", "2024", str(tmp_path / f"doc{i}.htm"), None, f"sha{i}")
            for i in range(n)]
    for doc in docs:
        (tmp_path / f"doc{doc.id[3:]}.htm").write_text("<p>Intet nyt.</p>")
    return docs


def _extracted(docs: list[Doc]) -> None:
    """Cached extractions, document i with one decision in CATEGORIES[i % 4]."""
    for i, doc in enumerate(docs):
        decision = {"id": f"{doc.id}#1", **_raw(CATEGORIES[i % len(CATEGORIES)], f"Regel {doc.id}"),
                    "citat_fundet": False, "citat_pos": None, "citat_side": None}
        (analyze.DECISIONS_DIR / f"{doc.id}.json").write_text(json.dumps(
            {"doc_id": doc.id, "sha256": doc.sha256, "version": analyze.EXTRACT_VERSION, "model": update.EXTRACT_MODEL,
             "provenance": {"model": update.EXTRACT_MODEL, "cli": "2.1.294", "prompt": "x", "effort": "default"},
             "moededato": None, "next_number": 2, "retired": [], "beslutninger": [decision]}))


def _consolidated(docs: list[Doc], monkeypatch, output: dict | None = None) -> None:
    """The rule files a completed consolidation leaves; Claude's `output`, else every decision left out as a
    one-off."""
    output = output or {"regler": [], "udeladt": [d.ref for d in analyze.load_decisions(docs)]}
    with monkeypatch.context() as m:
        m.setattr(analyze, "ask_claude", lambda *args, **kwargs: (output, Usage()))
        m.setattr(analyze, "cli_version", lambda: "2.1.294")
        analyze.consolidate(analyze.load_decisions(docs), {d.id: d.organ_label for d in docs}, model="opus",
                            effort=None, workers=1)


@pytest.fixture
def analysed(data, monkeypatch):
    """20 documents, all extracted and consolidated, as after an ordinary run."""
    docs = _docs(data, 20)
    _extracted(docs)
    _consolidated(docs, monkeypatch)
    return docs


# ---------------------------------------------------------------- rebuild guard

def _reasons(docs: list[Doc], mode: str = "full") -> str:
    """Why the rebuild guard stops a run; these tests are about full consolidation unless they say otherwise."""
    return "; ".join(update.pending_work(docs, mode).reasons())


def test_a_few_new_documents_pass_the_rebuild_guard(data, monkeypatch):
    docs = _docs(data, 20)
    _extracted(docs[:18])
    _consolidated(docs[:18], monkeypatch)
    assert _reasons(docs) == ""  # 2 of 20 = 10 %


def test_a_replaced_file_passes_the_rebuild_guard(analysed):
    replaced = Doc(**{**analysed[0].__dict__, "sha256": "new"})
    assert _reasons([replaced, *analysed[1:]]) == ""


def test_a_category_left_over_from_an_earlier_run_passes(analysed):
    _edit_decision(analysed[0], tekst="Ændret regel")
    assert _reasons(analysed) == ""  # 1 of 4 categories


def test_many_documents_to_extract_stop_the_run(data):
    docs = _docs(data, 20)
    _extracted(docs[:17])
    assert "3 of 20 documents" in _reasons(docs)


def test_a_missing_rule_file_stops_the_run(analysed):
    (analyze.RULES_DIR / "master.json").unlink()
    assert "no rule file in data/regler/: master" in _reasons(analysed)


def test_a_new_consolidation_version_stops_the_run(analysed, monkeypatch):
    monkeypatch.setattr(analyze, "CONSOLIDATE_VERSION", analyze.CONSOLIDATE_VERSION + 1)
    assert "4 of 4 categories" in _reasons(analysed)


def _edit_decision(doc: Doc, **changes) -> None:
    path = analyze.DECISIONS_DIR / f"{doc.id}.json"
    cached = json.loads(path.read_text())
    cached["beslutninger"][0].update(changes)
    path.write_text(json.dumps(cached))


def test_the_workflow_job_outlasts_a_run_on_the_default_limits():
    workflow = (Path(update.__file__).parent / ".github" / "workflows" / "update.yml").read_text()
    job = workflow[workflow.index("\n  update:\n"):]
    timeout = int(re.search(r"timeout-minutes: (\d+)", job).group(1))
    assert "--time-budget" not in job and "--max-cost" not in job  # every run on GitHub keeps to the defaults
    # No call starts after the time budget, but one started just before it may take its whole timeout.
    assert timeout > update.DEFAULT_TIME_BUDGET + analyze.CONSOLIDATE_TIMEOUT / 60


def test_the_refusal_names_the_reason_and_the_workflow_checkboxes(analysed):
    (analyze.RULES_DIR / "master.json").unlink()
    with pytest.raises(SystemExit) as refusal:
        update.check_rebuild(analysed, allowed=False, mode="full")
    message = str(refusal.value)
    assert "no rule file in data/regler/: master" in message and update.HOW_TO_PROCEED in message
    workflow = (Path(update.__file__).parent / ".github" / "workflows" / "update.yml").read_text()
    inputs = workflow[workflow.index("workflow_dispatch:"):workflow.index("\npermissions:")]
    labels = re.findall(r'description: "(.*)"', inputs)  # 'Allow a rebuild …' and 'Consolidate in full …'
    assert len(labels) == 2 and all(f"'{label}'" in message for label in labels)


def _one_rule_each(docs: list[Doc]) -> dict:
    """A consolidation that gives each document's decision a rule of its own (filtered to each category's refs)."""
    return {"regler": [{"titel": f"Regel {doc.id}", "vigtig": True, "note": None,
                        "versioner": [{"ref": f"{doc.id}#1", "effekt": "indfoert", "tekst": None, "kort": "indført",
                                       "kort_regel": None}]} for doc in docs],
            "udeladt": []}


def _extracted_with_v2(docs: list[Doc]) -> None:
    """The cached extractions as prompt v2 made them, before v3 became the pipeline's."""
    for doc in docs:
        _edit_version(doc, analyze.extract_prompt("v2").version)


def test_the_monthly_run_on_data_extracted_with_v2_refuses_before_any_call_and_names_the_migration(
        run, analysed, fake_claude):
    _extracted_with_v2(analysed)
    for options in ((), ("--allow-rebuild",)):  # incremental consolidation does not migrate, allowed or not
        with pytest.raises(SystemExit) as refused:
            run(analysed, *options, mode=None)
        message = str(refused.value)
        assert (f"20 of 20 documents were extracted with another prompt or model than v{analyze.EXTRACT_VERSION} "
                f"and {update.EXTRACT_MODEL}, which only a full consolidation takes in") in message
        assert update.HOW_TO_PROCEED in message and update.MIGRATE in message
    assert fake_claude.invocations("call") == 0


def _reworded(docs: list[Doc]) -> list[dict]:
    """Fake answers, one per call in order (one worker): each document's decision comes back reworded, and each
    category's consolidation keeps its rules."""
    rules = _one_rule_each(docs)
    return [{"moededato": None, "beslutninger": [_raw(CATEGORIES[i % 4], f"Regel {doc.id}, ny")], **rules}
            for i, doc in enumerate(docs)]


def test_a_cut_off_migration_is_finished_by_running_its_command_again_and_plain_runs_wait_for_it(
        run, data, fake_claude, monkeypatch):
    docs = _docs(data, 20)
    _extracted(docs)
    _consolidated(docs, monkeypatch, _one_rule_each(docs))
    _extracted_with_v2(docs)
    fake_claude.answers(*_reworded(docs))
    migrate = update.MIGRATE.split()[3:]  # the options of the command the refusals name

    # The migration, cut off by its cost cap after 10 of 20 extractions (0.25 USD each): its rules are stale, so it
    # goes to review, and its report says to merge, then run the same command again.
    assert _exit_code(run, docs, *migrate, "--workers", "1", "--max-cost", "2.5", mode=None) == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "- **stale**" in report and "Merge the pull request, so what the run has paid for is kept" in report
    assert "**Unfinished rebuild**" in report and update.RERUN in report

    # A plain run (the monthly one) stops before any call and names that command.
    assert _exit_code(run, docs, mode=None) == update.EXIT_REVIEW and fake_claude.invocations("call") == 10
    report = update.RUN_REPORT.read_text()
    assert "which only a full consolidation takes in" in report and update.HOW_TO_PROCEED in report

    # The same command again does only the rest: the 10 documents left, then the 4 categories in full.
    run(docs, *migrate, "--workers", "1", mode=None)
    systems = [argv[argv.index("--system-prompt") + 1] for argv in fake_claude.calls()]
    assert systems == [analyze.EXTRACT_SYSTEM] * 20 + [analyze.CONSOLIDATE_SYSTEM] * 4
    for doc in docs:
        saved = json.loads((analyze.DECISIONS_DIR / f"{doc.id}.json").read_text())
        assert (saved["version"], saved["model"]) == (analyze.EXTRACT_VERSION, update.EXTRACT_MODEL)
        assert [d["id"] for d in saved["beslutninger"]] == [f"{doc.id}#1"]  # carried over
    assert "**Ready to publish**" in update.RUN_REPORT.read_text()
    # The run log records what was paid for: the pipeline's prompt, in full mode.
    assert {k: _run_log()[-1][k] for k in ("extract_prompt", "consolidate_mode")} == \
        {"extract_prompt": f"v{analyze.EXTRACT_VERSION}", "consolidate_mode": "full"}

    run(docs, mode=None)  # and a plain run then has nothing to do
    assert fake_claude.invocations("call") == 24


def _edit_version(doc: Doc, version: int) -> None:
    """The cached extraction as made with the prompt of `version`."""
    path = analyze.DECISIONS_DIR / f"{doc.id}.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "version": version}))


def test_the_website_waits_for_queued_builds_and_skips_an_update_that_changed_nothing():
    workflow = (Path(update.__file__).parent / ".github" / "workflows" / "pages.yml").read_text()
    assert "cancel-in-progress: false" in workflow
    build = workflow[workflow.index("\n  build:"):workflow.index("\n  deploy:")]
    assert "needs: gate" in build and "if: needs.gate.outputs.changed == 'true'" in build


# ---------------------------------------------------------------- main()

@pytest.fixture
def run(data, fake_claude, monkeypatch):
    """update.main() on the documents passed to it, offline, with render and website stubbed out."""
    monkeypatch.setattr(update.render, "render", lambda *args: {})
    monkeypatch.setattr(update.website, "build", lambda *args: scrape.ROOT / "_site" / "index.html")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def main(docs: list[Doc], *args: str, mode: str | None = "full") -> None:
        """A run with full consolidation, which the fake answers here are for; incremental runs (the default) are
        tested in test_incremental.py. `mode` None passes no --consolidate-mode, as a plain run."""
        monkeypatch.setattr(update.scrape, "load_manifest", lambda: docs)
        mode_args = ("--consolidate-mode", mode) if mode else ()
        monkeypatch.setattr(sys, "argv", ["update.py", "--offline", *mode_args, *args])
        update.main()

    return main


def _run_log() -> list[dict]:
    return [json.loads(line) for line in update.RUNS_LOG.read_text().splitlines()] if update.RUNS_LOG.exists() else []


def test_only_and_allow_rebuild_pass_the_guard_and_plain_runs_do_not(run, data, fake_claude):
    docs = _docs(data, 3)
    fake_claude.answer({"moededato": None, "beslutninger": []})

    with pytest.raises(SystemExit, match="Stopped before any Claude call.*--allow-rebuild"):
        run(docs)
    assert fake_claude.invocations("call") == 0

    run(docs, "--only", "^doc0$")
    assert fake_claude.invocations("call") == 1

    run(docs, "--allow-rebuild")
    assert fake_claude.invocations("call") == 3
    assert len(_run_log()) == 2


def test_only_consolidates_the_categories_of_the_selected_documents(run, analysed, fake_claude, monkeypatch):
    monkeypatch.setattr(analyze, "CONSOLIDATE_VERSION", analyze.CONSOLIDATE_VERSION + 1)  # all 4 categories due
    replaced = Doc(**{**analysed[1].__dict__, "sha256": "new"})  # doc1: one decision in master
    fake_claude.answer({"moededato": None, "beslutninger": [_raw("dommere", "Ny regel")], "regler": [],
                        "udeladt": [*(f"{doc.id}#1" for doc in analysed), "doc1#2"]})

    run([analysed[0], replaced, *analysed[2:]], "--only", "^doc1$")

    assert fake_claude.invocations("call") == 3  # extract doc1, then master (before) and dommere (after)
    versions = {c: json.loads((analyze.RULES_DIR / f"{c}.json").read_text())["version"] for c in CATEGORIES}
    assert versions == {"okonomi": analyze.CONSOLIDATE_VERSION - 1, "master": analyze.CONSOLIDATE_VERSION,
                        "dommere": analyze.CONSOLIDATE_VERSION, "staevner": analyze.CONSOLIDATE_VERSION - 1}


def test_a_run_without_calls_writes_no_run_log_line(run, analysed, fake_claude):
    run(analysed)
    assert _run_log() == [] and fake_claude.invocations("version") == 0


def test_the_run_log_is_written_when_a_later_step_crashes(run, data, fake_claude, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("consolidation crashed")

    monkeypatch.setattr(analyze, "consolidate", crash)
    fake_claude.answer({"moededato": None, "beslutninger": []})
    with pytest.raises(RuntimeError, match="consolidation crashed"):
        run(_docs(data, 3), "--allow-rebuild")
    (line,) = _run_log()
    assert line["steps"]["extract"]["calls"] == 3


def test_the_step_summary_is_written_before_rendering(run, data, fake_claude, monkeypatch):
    def crash(*args):
        raise RuntimeError("render crashed")

    summary = data / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(update.render, "render", crash)
    fake_claude.answer({"moededato": None, "beslutninger": []})
    with pytest.raises(RuntimeError, match="render crashed"):
        run(_docs(data, 3), "--allow-rebuild")
    assert "| extract | 3 |" in summary.read_text()
    report = update.RUN_REPORT.read_text()
    assert "**The run failed**: writing the pages failed: RuntimeError: render crashed." in report
    assert report.startswith(update.CHECKS_PASSED)  # the data is fine; only the pages are not written


def test_skipped_calls_fail_the_run(run, data, fake_claude):
    with pytest.raises(SystemExit, match="3 Claude calls failed or were skipped"):
        run(_docs(data, 3), "--allow-rebuild", "--max-cost", "0")
    assert fake_claude.invocations("call") == 0


def test_the_guard_refuses_a_missing_rule_file_before_any_call(run, analysed, fake_claude):
    (analyze.RULES_DIR / "master.json").unlink()
    with pytest.raises(SystemExit, match="no rule file in data/regler/: master"):
        run(analysed)
    assert fake_claude.invocations("call") == 0
    report = update.RUN_REPORT.read_text()  # new downloads are kept: the data passes the checks
    assert report.startswith(update.CHECKS_PASSED) and "**The run failed**: Stopped before any Claude call" in report


# ---------------------------------------------------------------- outcome: exit code and run report

def _exit_code(run, *args, **options) -> int:
    with pytest.raises(SystemExit) as stopped:
        run(*args, **options)
    return stopped.value.code if isinstance(stopped.value.code, int) else 1


def test_a_run_whose_checks_pass_is_ready_to_publish(run, analysed):
    run(analysed)
    assert "**Ready to publish**" in update.RUN_REPORT.read_text()


def test_a_decision_the_consolidation_drops_sends_the_run_to_review(run, analysed, data, fake_claude):
    docs = _docs(data, 21)  # doc20 is new; its decision lands in okonomi, which leaves it out without a word
    fake_claude.answer({"moededato": None, "beslutninger": [_raw("okonomi", "Ny regel")], "regler": [],
                        "udeladt": [f"{doc.id}#1" for doc in analysed]})

    assert _exit_code(run, docs) == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "**Needs review**" in report and "## Errors: 1" in report
    assert report.startswith(update.CHECKS_FAILED) and "merging alone publishes nothing" in report
    assert "- **unassigned**: doc20#1 (okonomi: Ny regel) is in no rule" in report


def _dated_docs(data, **dates: str) -> list[Doc]:
    docs = [Doc(id_, "bestyrelse", "Referat", dato, str(data / f"{id_}.htm"), None, f"sha-{id_}")
            for id_, dato in dates.items()]
    for doc in docs:
        Path(doc.path).write_text("<p>Intet nyt.</p>")
    return docs


GEBYR = {"titel": "Gebyr", "vigtig": True, "note": None,
         "versioner": [{"ref": "old#1", "effekt": "indfoert", "tekst": None, "kort": "indført", "kort_regel": None}]}


@pytest.mark.parametrize("kept", [True, False])
def test_a_run_that_changes_what_applied_in_past_years_needs_review(run, data, fake_claude, monkeypatch, kept):
    docs = _dated_docs(data, old="2015-03-01", new="2024-03-01")
    old = docs[0]
    _extracted([old])
    _consolidated([old], monkeypatch, {"regler": [GEBYR], "udeladt": []})
    # new's decision lands in okonomi too, whose consolidation either keeps the rule or drops it as a one-off.
    fake_claude.answer({"moededato": None, "beslutninger": [_raw("okonomi", "Ny regel")],
                        "regler": [GEBYR] if kept else [], "udeladt": ["new#1"] if kept else ["old#1", "new#1"]})

    if kept:
        run(docs, "--allow-rebuild")
        assert "No rule in force there changed." in update.RUN_REPORT.read_text()
        return
    assert _exit_code(run, docs, "--allow-rebuild") == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "- **history**: Gebyr (gebyr): what was in force in 2015–2026 changed, although none of its decisions " \
           "did" in report
    assert "| Gebyr (`gebyr`) | 2015–2026 | old#1 | not in force | none changed |" in report
    assert "Merge the pull request to publish it" in report


def test_a_retry_of_a_failed_consolidation_does_not_change_the_past(run, data, fake_claude, monkeypatch):
    docs = _dated_docs(data, old="2015-03-01", new="2024-03-01")
    _extracted(docs[:1])
    _consolidated(docs[:1], monkeypatch, {"regler": [GEBYR], "udeladt": []})
    changed = {"ref": "new#1", "effekt": "aendret", "tekst": None, "kort": "ændret", "kort_regel": None}
    fake_claude.answer({"moededato": None, "beslutninger": [_raw("okonomi", "Ny regel")],
                        "regler": [{**GEBYR, "versioner": [*GEBYR["versioner"], changed]}], "udeladt": []})

    fake_claude.plan("ok", "error")  # new is extracted, and the consolidation fails
    assert _exit_code(run, docs, "--allow-rebuild") == update.EXIT_FAILED
    fake_claude.plan("ok")
    run(docs, "--allow-rebuild")  # Gebyr takes up new#1 from 2024 on, which the first run did not get to


def test_history_that_cannot_be_checked_is_not_published(run, analysed, monkeypatch):
    def crash(*args):
        raise RuntimeError("snapshot broke")

    monkeypatch.setattr(update.checks, "check_history", crash)
    assert _exit_code(run, analysed) == update.EXIT_REVIEW
    assert "- **history**: history could not be checked: RuntimeError: snapshot broke" in update.RUN_REPORT.read_text()


def test_the_outcome_is_reported_when_the_report_cannot_be_built(run, analysed, monkeypatch):
    def crash(*args, **kwargs):
        raise RuntimeError("report broke")

    monkeypatch.setattr(update, "run_report", crash)
    run(analysed)
    report = update.RUN_REPORT.read_text()
    assert report.startswith(update.CHECKS_PASSED) and "**Ready to publish**" in report


def test_a_run_that_fails_with_errors_still_goes_to_review(run, analysed, monkeypatch):
    path = analyze.RULES_DIR / "okonomi.json"
    rules = json.loads(path.read_text())
    path.write_text(json.dumps({**rules, "udeladt": []}))  # its decisions are dropped, not one-offs

    def crash(*args, **kwargs):
        raise RuntimeError("consolidation crashed")

    monkeypatch.setattr(analyze, "consolidate", crash)
    assert _exit_code(run, analysed) == update.EXIT_REVIEW
    report = update.RUN_REPORT.read_text()
    assert "The run also failed: the analysis stopped: RuntimeError: consolidation crashed." in report


def test_a_failed_run_whose_checks_pass_keeps_its_results(run, data):
    assert _exit_code(run, _docs(data, 3), "--allow-rebuild", "--max-cost", "0") == update.EXIT_FAILED
    assert "**The run failed**: 3 Claude calls failed or were skipped." in update.RUN_REPORT.read_text()


def test_the_checks_command_fails_on_errors_only(analysed, monkeypatch, capsys):
    import checks

    monkeypatch.setattr(checks.scrape, "load_manifest", lambda: analysed)
    checks.main()
    assert capsys.readouterr().out.startswith("Errors: 0\nWarnings: 0\n")

    path = analyze.RULES_DIR / "master.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "udeladt": []}))
    with pytest.raises(SystemExit) as failed:
        checks.main()
    assert failed.value.code == 1
    assert capsys.readouterr().out.startswith("Errors: 5 (unassigned 5)\n  unassigned: doc1#1 (master: Regel doc1)")

    _edit_decision(analysed[1], tekst="Ændret regel")  # master is to be consolidated again: not an error yet
    checks.main()


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


def test_a_run_log_that_cannot_be_written_only_warns(data, caplog, monkeypatch):
    monkeypatch.setattr(update, "RUNS_LOG", data / "missing-dir" / "runs.jsonl")
    monkeypatch.setattr(analyze, "cli_version", lambda: "2.1.294")
    update.record_run({"extract": _step("Extract", 1, 0.5)})
    assert "Could not write" in caplog.text


def test_step_summary_shows_the_numbers_and_the_check_results():
    text = update.step_summary({"extract": _step("Extract", 3, 1.5)}, Counter({"date": 13, "effect": 2}))
    for number in ("3483", "3481", "52", "1.50", "claude-sonnet-5-5", "date: 13", "effect: 2"):
        assert number in text
