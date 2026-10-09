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
# The extraction prompt the pipeline does not use.
OTHER_PROMPT = next(name for name in analyze.EXTRACT_PROMPTS if name != analyze.extract_prompt().name)
# The guard's tests are about full consolidation unless they say otherwise.
FULL = update.Plan(mode="full")


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path / "beslutninger")
    monkeypatch.setattr(analyze, "RULES_DIR", tmp_path / "regler")
    monkeypatch.setattr(update, "RUNS_LOG", tmp_path / "runs.jsonl")
    monkeypatch.setattr(update, "EVAL_RUNS_LOG", tmp_path / "eval-runs.jsonl")
    monkeypatch.setattr(update, "REBUILD_MARKER", tmp_path / "rebuild.json")
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
            {"doc_id": doc.id, "sha256": doc.sha256, "version": analyze.EXTRACT_VERSION, "model": "sonnet",
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


def test_the_approval_is_saved_before_any_claude_call(data):
    docs = _docs(data, 3)
    update.check_rebuild(docs, allowed=True, plan=FULL)
    marker = json.loads(update.REBUILD_MARKER.read_text())
    assert marker["documents"] == ["doc0", "doc1", "doc2"]


def test_new_minutes_may_arrive_while_an_approved_rebuild_is_unfinished(analysed, data, monkeypatch):
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", analyze.EXTRACT_VERSION + 1)
    update.check_rebuild(analysed, allowed=True, plan=FULL)  # all 20 documents to extract again
    update.check_rebuild(_docs(data, 22), allowed=False, plan=FULL)  # plus 2 new ones: ordinary on their own


def test_the_approval_covers_categories_that_approved_documents_move_into(analysed, monkeypatch):
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", analyze.EXTRACT_VERSION + 1)
    update.check_rebuild(analysed, allowed=True, plan=FULL)  # all 20 documents to extract again
    _extracted(analysed)  # the new prompt puts every decision in a category nobody approved
    for doc in analysed:
        _edit_decision(doc, kategori="andet")
    assert "no rule file in data/regler/: andet" in _reasons(analysed)
    update.check_rebuild(analysed, allowed=False, plan=FULL)


def test_an_approval_does_not_cover_lost_extractions(analysed, monkeypatch):
    monkeypatch.setattr(analyze, "CONSOLIDATE_VERSION", analyze.CONSOLIDATE_VERSION + 1)
    update.check_rebuild(analysed, allowed=True, plan=FULL)  # all 4 categories to consolidate again
    for path in analyze.DECISIONS_DIR.glob("*.json"):
        path.unlink()
    with pytest.raises(SystemExit, match="beyond the work approved in rebuild.json, 20 of 20 documents"):
        update.check_rebuild(analysed, allowed=False, plan=FULL)


def test_an_approval_of_one_category_does_not_cover_a_changed_consolidation_input(analysed, monkeypatch):
    (analyze.RULES_DIR / "master.json").unlink()
    update.check_rebuild(analysed, allowed=True, plan=FULL)
    assert json.loads(update.REBUILD_MARKER.read_text())["categories"] == ["master"]

    original = analyze._consolidation_input
    monkeypatch.setattr(analyze, "_consolidation_input", lambda d, organ: {**original(d, organ), "ny": 1})
    with pytest.raises(SystemExit, match="beyond the work approved in rebuild.json, 3 of 4 categories"):
        update.check_rebuild(analysed, allowed=False, plan=FULL)


def test_the_refusal_names_the_reason_and_the_github_checkbox(analysed):
    (analyze.RULES_DIR / "master.json").unlink()
    with pytest.raises(SystemExit) as refusal:
        update.check_rebuild(analysed, allowed=False, plan=FULL)
    assert "no rule file in data/regler/: master" in str(refusal.value)
    assert update.REBUILD_INPUT_LABEL in str(refusal.value)
    workflow = (Path(update.__file__).parent / ".github" / "workflows" / "update.yml").read_text()
    assert f'description: "{update.REBUILD_INPUT_LABEL}"' in workflow


def _log(path: Path, *lines: dict) -> None:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def _logged_step(calls: int, cost: float, model: str) -> dict:
    return {"calls": calls, "failed": 0, "skipped": 0, "cost_usd": cost, "models": [model]}


def test_a_rebuild_is_priced_from_measured_extractions_and_the_last_full_consolidation(analysed):
    opus = "claude-opus-5-5"
    _log(update.EVAL_RUNS_LOG,
         {"command": "extract", "model": opus, "steps": {"extract": _logged_step(10, 1.0, opus)}},  # v2: no prompt
         {"command": "extract", "model": opus, "prompt": OTHER_PROMPT,
          "steps": {"extract": _logged_step(4, 0.6, opus)}},
         {"command": "score", "runs": ["stored"]})
    _log(update.RUNS_LOG,
         {"time": "2026-11-01T06:00:00+00:00", "steps": {"consolidate": _logged_step(4, 2.0, opus)}},
         {"time": "2026-12-01T06:00:00+00:00", "consolidate_mode": "full",
          "steps": {"consolidate": _logged_step(1, 0.9, opus)}},  # one category: no full consolidation
         {"time": "2027-01-01T06:00:00+00:00", "consolidate_mode": "incremental",
          "steps": {"consolidate": _logged_step(30, 3.0, opus)}})
    work = update.pending_work(analysed, prompt=OTHER_PROMPT)  # all 20 documents, and their 4 categories

    estimate = update.estimate_cost(work, update.Plan(OTHER_PROMPT, opus, "full", opus))
    assert estimate.extract_usd == pytest.approx(20 * 0.15)  # the runs with this prompt only
    assert estimate.consolidate_usd == pytest.approx(4 * 0.5)  # the full consolidation of November
    assert estimate.total_usd == pytest.approx(5.0)
    assert "more than --max-cost 4" in estimate.describe(4.0) and "--max-cost" not in estimate.describe(15.0)

    sonnet = update.estimate_cost(work, update.Plan(OTHER_PROMPT, "claude-sonnet-5-5", "full", "claude-sonnet-5-5"))
    assert sonnet.extract_usd is None and sonnet.total_usd is None  # no Sonnet extraction measured
    assert sonnet.consolidate_usd == pytest.approx(update.FULL_CONSOLIDATION_USD)  # none logged: the README's
    assert "unknown" in sonnet.describe(15.0)


def _one_rule_each(docs: list[Doc]) -> dict:
    """A consolidation that gives each document's decision a rule of its own (filtered to each category's refs)."""
    return {"regler": [{"titel": f"Regel {doc.id}", "vigtig": True, "note": None,
                        "versioner": [{"ref": f"{doc.id}#1", "effekt": "indfoert", "tekst": None, "kort": "indført",
                                       "kort_regel": None}]} for doc in docs],
            "udeladt": []}


def test_a_migration_to_another_prompt_is_approved_continued_with_its_settings_and_reviewed_when_cut_off(
        run, data, fake_claude, monkeypatch, caplog):
    docs = _docs(data, 20)
    _extracted(docs)
    _consolidated(docs, monkeypatch, _one_rule_each(docs))
    opus = ("--extract-prompt", OTHER_PROMPT, "--extract-model", "claude-opus-5-5")
    resume = f"uv run update.py --extract-prompt {OTHER_PROMPT} --extract-model claude-opus-5-5"
    migrate = (f"uv run update.py --consolidate-mode full --extract-prompt {OTHER_PROMPT} --extract-model "
               f"claude-opus-5-5 --allow-rebuild")
    # Each document's decision comes back reworded, in order (one worker); each category keeps its rules.
    fake_claude.answers(*({"moededato": None, "beslutninger": [_raw(CATEGORIES[i % 4], f"Regel {doc.id}, ny")],
                           **_one_rule_each(docs)} for i, doc in enumerate(docs)))

    with pytest.raises(SystemExit, match=f"20 of 20 documents need extraction.*claude-opus-5-5.*`{migrate}`"):
        run(docs, *opus)
    assert fake_claude.invocations("call") == 0 and "Estimated cost at list price" in caplog.text

    # Cut off after 10 extractions (0.25 USD each): their rules are stale, so the result goes to review.
    assert _exit_code(run, docs, *opus, "--allow-rebuild", "--workers", "1", "--max-cost", "2.5") == \
        update.EXIT_REVIEW
    assert "- **stale**" in update.RUN_REPORT.read_text()
    marker = json.loads(update.REBUILD_MARKER.read_text())
    assert {k: marker[k] for k in ("extract_prompt", "extract_model", "mode", "consolidate_model",
                                   "consolidate_effort")} == {
        "extract_prompt": OTHER_PROMPT, "extract_model": "claude-opus-5-5", "mode": "full",
        "consolidate_model": "claude-opus-5-5", "consolidate_effort": None}
    assert len(marker["documents"]) == 10

    # A plain run would extract the first ten back with the default prompt, and one with the migration's prompt but
    # another model would mix two: each stops before any call and names the command that continues; the stale rules
    # still send the result to review.
    assert _exit_code(run, docs, mode=None) == update.EXIT_REVIEW
    assert re.search(f"holds an unfinished rebuild approved for extraction prompt {OTHER_PROMPT}: `{resume}` continues "
                     f"it", update.RUN_REPORT.read_text())
    assert _exit_code(run, docs, "--extract-prompt", OTHER_PROMPT, "--extract-model", "claude-sonnet-5-5") == \
        update.EXIT_REVIEW
    assert re.search(f"extracted with --extract-prompt {OTHER_PROMPT} --extract-model claude-opus-5-5 .*, not "
                     f"--extract-model claude-sonnet-5-5\\. Run `{resume}` without them", update.RUN_REPORT.read_text())
    assert fake_claude.invocations("call") == 10

    # Its prompt alone continues it, with the approved model and in full.
    run(docs, "--extract-prompt", OTHER_PROMPT, "--workers", "1", mode=None)
    chosen = analyze.extract_prompt(OTHER_PROMPT)
    systems = [argv[argv.index("--system-prompt") + 1] for argv in fake_claude.calls()]
    assert systems == [chosen.system] * 20 + [analyze.CONSOLIDATE_SYSTEM] * 4  # then every category in full
    for doc in docs:
        saved = json.loads((analyze.DECISIONS_DIR / f"{doc.id}.json").read_text())
        assert (saved["version"], saved["model"]) == (chosen.version, "claude-opus-5-5")
        assert [d["id"] for d in saved["beslutninger"]] == [f"{doc.id}#1"]  # carried over
        assert saved["provenance"]["prompt"] == analyze.prompt_hash(chosen.system, analyze.EXTRACT_SCHEMA)
    assert "**Ready to publish**" in update.RUN_REPORT.read_text() and not update.REBUILD_MARKER.exists()
    assert {k: _run_log()[-1][k] for k in ("extract_prompt", "consolidate_mode")} == \
        {"extract_prompt": OTHER_PROMPT, "consolidate_mode": "full"}

    # Until the migrated prompt is the default, a plain run would extract every document back, which incremental
    # consolidation refuses; once it is, a plain run has nothing to do.
    with pytest.raises(SystemExit, match=f"20 of 20 documents were extracted with another prompt than "
                                         f"{analyze.extract_prompt().name}.*Migrate with `{update.MIGRATE}`"):
        run(docs, mode=None)
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", chosen.version)
    monkeypatch.setattr(analyze, "EXTRACT_SYSTEM", chosen.system)
    run(docs, mode=None)
    assert fake_claude.invocations("call") == 24 and update.pending_work(docs).reasons() == []


def test_a_few_approved_documents_left_keep_the_approval_and_its_settings(analysed):
    plan = update.Plan(OTHER_PROMPT, "claude-opus-5-5", "full")
    update.check_rebuild(analysed, allowed=True, plan=plan)  # all 20 documents
    _extracted(analysed)  # 19 of them done with the other prompt: one left is ordinary, but keeps the approval
    for doc in analysed[1:]:
        _edit_version(doc, analyze.extract_prompt(OTHER_PROMPT).version)
    update.update_rebuild_marker(analysed, plan)
    marker = json.loads(update.REBUILD_MARKER.read_text())
    assert (marker["documents"], marker["extract_prompt"], marker["extract_model"]) == \
        (["doc0"], OTHER_PROMPT, "claude-opus-5-5")
    update.check_rebuild(analysed, allowed=False, plan=plan)


def _edit_version(doc: Doc, version: int) -> None:
    path = analyze.DECISIONS_DIR / f"{doc.id}.json"
    cached = json.loads(path.read_text())
    path.write_text(json.dumps({**cached, "version": version}))


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
    assert fake_claude.invocations("call") == 1 and not update.REBUILD_MARKER.exists()

    run(docs, "--allow-rebuild")
    assert fake_claude.invocations("call") == 3 and not update.REBUILD_MARKER.exists()
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


def test_an_interrupted_approved_rebuild_is_finished_by_plain_runs(run, data, fake_claude, monkeypatch):
    docs = _docs(data, 3)
    fake_claude.answer({"moededato": None, "beslutninger": []})
    fake_claude.plan("error")
    with pytest.raises(SystemExit, match="3 Claude calls failed"):
        run(docs, "--allow-rebuild")
    assert update.REBUILD_MARKER.exists() and _run_log()[0]["steps"]["extract"]["failed"] == 3

    # The approval holds for the prompt versions it was given for only.
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", analyze.EXTRACT_VERSION + 1)
    with pytest.raises(SystemExit, match="Stopped before any Claude call"):
        run(docs)
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", analyze.EXTRACT_VERSION - 1)

    fake_claude.plan("ok")
    run(docs)
    assert not update.REBUILD_MARKER.exists() and len(_run_log()) == 2


def test_an_ordinary_run_cut_off_with_most_categories_left_is_continued(run, data, fake_claude, monkeypatch):
    docs = _docs(data, 10)
    _extracted(docs[:9])
    _consolidated(docs[:9], monkeypatch)
    # A big meeting with decisions in three of the four categories, whose consolidation then fails.
    fake_claude.answer({"moededato": None, "beslutninger": [_raw(c, f"Ny regel {c}") for c in CATEGORIES[:3]],
                        "regler": [], "udeladt": [*(f"{doc.id}#1" for doc in docs), "doc9#2", "doc9#3"]})
    fake_claude.plan("ok", "error")
    with pytest.raises(SystemExit, match="failed"):
        run(docs)
    assert "3 of 4 categories" in _reasons(docs) and update.REBUILD_MARKER.exists()

    fake_claude.plan("ok")
    run(docs)
    assert not update.REBUILD_MARKER.exists()


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
    def crash(*args):
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
