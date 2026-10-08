import json
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
    monkeypatch.setattr(update, "REBUILD_MARKER", tmp_path / "rebuild.json")
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
        decision = {**_raw(CATEGORIES[i % len(CATEGORIES)], f"Regel {doc.id}"),
                    "citat_fundet": False, "citat_pos": None, "citat_side": None}
        (analyze.DECISIONS_DIR / f"{doc.id}.json").write_text(json.dumps(
            {"doc_id": doc.id, "sha256": doc.sha256, "version": analyze.EXTRACT_VERSION, "model": "sonnet",
             "moededato": None, "beslutninger": [decision]}))


def _consolidated(docs: list[Doc], monkeypatch) -> None:
    """The rule files a completed consolidation leaves."""
    with monkeypatch.context() as m:
        m.setattr(analyze, "ask_claude", lambda *args, **kwargs: ({"regler": [], "udeladt": []}, Usage()))
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

def _reasons(docs: list[Doc]) -> str:
    return "; ".join(update.pending_work(docs).reasons())


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
    update.check_rebuild(docs, allowed=True)
    marker = json.loads(update.REBUILD_MARKER.read_text())
    assert marker["documents"] == ["doc0", "doc1", "doc2"]


def test_new_minutes_may_arrive_while_an_approved_rebuild_is_unfinished(analysed, data, monkeypatch):
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", analyze.EXTRACT_VERSION + 1)
    update.check_rebuild(analysed, allowed=True)  # all 20 documents to extract again
    update.check_rebuild(_docs(data, 22), allowed=False)  # plus 2 new ones: ordinary on their own


def test_the_approval_covers_categories_that_approved_documents_move_into(analysed, monkeypatch):
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", analyze.EXTRACT_VERSION + 1)
    update.check_rebuild(analysed, allowed=True)  # all 20 documents to extract again
    _extracted(analysed)  # the new prompt puts every decision in a category nobody approved
    for doc in analysed:
        _edit_decision(doc, kategori="andet")
    assert "no rule file in data/regler/: andet" in _reasons(analysed)
    update.check_rebuild(analysed, allowed=False)


def test_an_approval_does_not_cover_lost_extractions(analysed, monkeypatch):
    monkeypatch.setattr(analyze, "CONSOLIDATE_VERSION", analyze.CONSOLIDATE_VERSION + 1)
    update.check_rebuild(analysed, allowed=True)  # all 4 categories to consolidate again
    for path in analyze.DECISIONS_DIR.glob("*.json"):
        path.unlink()
    with pytest.raises(SystemExit, match="beyond the work approved in rebuild.json, 20 of 20 documents"):
        update.check_rebuild(analysed, allowed=False)


def test_an_approval_of_one_category_does_not_cover_a_changed_consolidation_input(analysed, monkeypatch):
    (analyze.RULES_DIR / "master.json").unlink()
    update.check_rebuild(analysed, allowed=True)
    assert json.loads(update.REBUILD_MARKER.read_text())["categories"] == ["master"]

    original = analyze._consolidation_input
    monkeypatch.setattr(analyze, "_consolidation_input", lambda d, organ: {**original(d, organ), "ny": 1})
    with pytest.raises(SystemExit, match="beyond the work approved in rebuild.json, 3 of 4 categories"):
        update.check_rebuild(analysed, allowed=False)


def test_the_refusal_names_the_reason_and_the_github_checkbox(analysed):
    (analyze.RULES_DIR / "master.json").unlink()
    with pytest.raises(SystemExit) as refusal:
        update.check_rebuild(analysed, allowed=False)
    assert "no rule file in data/regler/: master" in str(refusal.value)
    assert update.REBUILD_INPUT_LABEL in str(refusal.value)
    workflow = (Path(update.__file__).parent / ".github" / "workflows" / "update.yml").read_text()
    assert f'description: "{update.REBUILD_INPUT_LABEL}"' in workflow


# ---------------------------------------------------------------- main()

@pytest.fixture
def run(data, fake_claude, monkeypatch):
    """update.main() on the documents passed to it, offline, with render and website stubbed out."""
    monkeypatch.setattr(update.render, "render", lambda *args: {})
    monkeypatch.setattr(update.website, "build", lambda *args: scrape.ROOT / "_site" / "index.html")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def main(docs: list[Doc], *args: str) -> None:
        monkeypatch.setattr(update.scrape, "load_manifest", lambda: docs)
        monkeypatch.setattr(sys, "argv", ["update.py", "--offline", *args])
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
                        "udeladt": []})

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


def test_skipped_calls_fail_the_run(run, data, fake_claude):
    with pytest.raises(SystemExit, match="3 Claude calls failed or were skipped"):
        run(_docs(data, 3), "--allow-rebuild", "--max-cost", "0")
    assert fake_claude.invocations("call") == 0


def test_the_guard_refuses_a_missing_rule_file_before_any_call(run, analysed, fake_claude):
    (analyze.RULES_DIR / "master.json").unlink()
    with pytest.raises(SystemExit, match="no rule file in data/regler/: master"):
        run(analysed)
    assert fake_claude.invocations("call") == 0


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
                        "regler": [], "udeladt": []})
    fake_claude.plan("ok", "error")
    with pytest.raises(SystemExit, match="failed"):
        run(docs)
    assert "3 of 4 categories" in _reasons(docs) and update.REBUILD_MARKER.exists()

    fake_claude.plan("ok")
    run(docs)
    assert not update.REBUILD_MARKER.exists()


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
