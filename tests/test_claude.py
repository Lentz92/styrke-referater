"""ask_claude and the Claude steps against the fake `claude` from conftest.py; no real Claude calls."""

import json
import re

import pytest

import analyze
from analyze import ClaudeError, ModelMismatch, RunBudget, ask_claude, run_parallel
from conftest import decision
from scrape import Doc

NOTHING_FOUND = {"moededato": None, "beslutninger": [], "regler": [], "udeladt": []}  # fits both steps


def _ask(model="claude-sonnet-5-5", **kwargs):
    return ask_claude("system", "prompt", {"type": "object"}, model=model, effort=None, timeout=30, **kwargs)


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path / "beslutninger")
    monkeypatch.setattr(analyze, "RULES_DIR", tmp_path / "regler")
    return tmp_path


def _docs(tmp_path, *names: str, sha: str = "sha") -> list[Doc]:
    docs = [Doc(name, "bestyrelse", "Referat", "2024", str(tmp_path / f"{name}.htm"), None, f"{sha}-{name}")
            for name in names]
    for doc in docs:
        (tmp_path / f"{doc.id}.htm").write_text("<p>Intet nyt.</p>")
    return docs


def _saved(data, doc: Doc) -> dict:
    return json.loads((data / "beslutninger" / f"{doc.id}.json").read_text())


# ---------------------------------------------------------------- usage and models

def test_usage_is_read_from_the_cli_result(fake_claude):
    fake_claude.plan("ok")
    output, usage = _ask()
    assert output == {"answer": "ok"}
    assert usage.models == ("claude-sonnet-5-5",) and usage.answered_by == "claude-sonnet-5-5"
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == \
        (2, 52, 967, 2514)
    assert (usage.cost_usd, usage.attempts) == (0.25, 1)


def test_an_alias_is_allowed_and_its_canonical_model_recorded(fake_claude):
    fake_claude.plan("ok")
    _, usage = _ask(model="sonnet")
    assert (usage.model, usage.answered_by) == ("sonnet", "claude-sonnet-5-5")


@pytest.mark.parametrize("step", ["helper-before", "helper-after"])
def test_with_a_helper_model_the_answer_comes_from_the_one_that_wrote_most(fake_claude, step):
    fake_claude.plan(step)
    _, usage = _ask(model="sonnet")
    assert len(usage.models) == 2
    assert usage.answered_by == "claude-sonnet-5-5"


def test_the_canonical_model_counts_not_the_usage_key_and_odd_entries_are_skipped(fake_claude):
    fake_claude.plan("keyed")
    _, usage = _ask()
    assert usage.models == ("claude-sonnet-5-5",)
    assert (usage.output_tokens, usage.cost_usd) == (52, 0.25)


def test_another_model_than_the_full_id_fails_without_retry_and_stops_the_run(fake_claude, caplog):
    fake_claude.plan("other")
    with pytest.raises(ModelMismatch, match="claude-sonnet-5-5.*claude-haiku-5-5"):
        _ask()
    assert fake_claude.invocations("call") == 1

    budget = RunBudget()
    first = run_parallel([1, 2, 3], lambda _: _ask(), workers=1, label="Extract", budget=budget)
    second = run_parallel([1, 2], lambda _: _ask(), workers=1, label="Consolidate", budget=budget)
    assert (first.failed, first.skipped, first.usage.cost_usd) == (1, 2, 0.25)
    assert (second.calls, second.skipped) == (0, 2)
    assert fake_claude.invocations("call") == 2
    assert "another model than requested" in caplog.text


# ---------------------------------------------------------------- retries, timeouts and limits

def test_a_failed_attempt_is_retried_and_its_cost_counted(fake_claude):
    fake_claude.plan("error", "ok")
    budget = RunBudget()
    output, usage = _ask(budget=budget)
    assert output == {"answer": "ok"}
    assert usage.attempts == 2 and fake_claude.invocations("call") == 2
    assert usage.cost_usd == pytest.approx(0.35) and budget.spent_usd == pytest.approx(0.35)


def test_a_call_that_times_out_fails(fake_claude):
    fake_claude.plan("sleep")
    with pytest.raises(ClaudeError, match="timeout"):
        ask_claude("system", "prompt", {}, model="claude-sonnet-5-5", effort=None, timeout=0.5, attempts=1)


def test_no_retry_once_the_cost_limit_is_reached(fake_claude):
    fake_claude.plan("error", "ok")
    with pytest.raises(ClaudeError, match="API Error: overloaded.*cost limit"):
        _ask(budget=RunBudget(max_cost_usd=0.1))
    assert fake_claude.invocations("call") == 1


def test_failed_call_not_retried_after_the_deadline_reports_its_error(fake_claude, caplog):
    fake_claude.plan("error")
    budget = RunBudget(minutes=-1)  # passed while the first attempt ran; that attempt still counts

    with pytest.raises(ClaudeError, match="API Error: overloaded.*time budget"):
        _ask(budget=budget)
    assert fake_claude.invocations("call") == 1

    step = run_parallel([1], lambda _: _ask(budget=budget), workers=1, label="Test")
    assert step.failed == 1 and "failed: API Error: overloaded" in caplog.text


def test_an_error_after_a_successful_call_keeps_its_cost(fake_claude, data, monkeypatch):
    def full_disk(path, value):
        raise OSError("No space left on device")

    monkeypatch.setattr(analyze, "_write_json", full_disk)
    fake_claude.answer(NOTHING_FOUND)
    step = analyze.extract(_docs(data, "a"), model="claude-sonnet-5-5", effort=None, workers=1)
    assert (step.calls, step.failed, step.usage.cost_usd) == (1, 1, 0.25)


# ---------------------------------------------------------------- CLI version and provenance

def test_an_unreadable_cli_version_does_not_stop_the_run(fake_claude, caplog):
    fake_claude.fail_version()
    assert analyze.cli_version() == "unknown"
    assert "Cannot read the Claude Code version" in caplog.text


def test_a_cli_version_that_is_not_utf8_is_still_read(fake_claude):
    fake_claude.garble_version()
    assert analyze.cli_version() == "2.1.294"


def test_no_cli_on_path_gives_an_unknown_version(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    analyze.cli_version.cache_clear()
    try:
        assert analyze.cli_version() == "unknown"
    finally:
        analyze.cli_version.cache_clear()


def test_steps_without_work_never_run_the_cli(fake_claude, data):
    (doc,) = _docs(data, "a")
    fake_claude.answer(NOTHING_FOUND)
    analyze.extract([doc], model="claude-sonnet-5-5", effort=None, workers=1)
    analyze.cli_version.cache_clear()
    before = fake_claude.invocations("version")

    analyze.extract([doc], model="claude-sonnet-5-5", effort=None, workers=1)
    analyze.consolidate([], {}, model="claude-opus-5-5", effort=None, workers=1)
    assert fake_claude.invocations("version") == before


def test_the_cli_version_is_read_once_for_two_steps_with_work(fake_claude, data):
    fake_claude.answer(NOTHING_FOUND)
    extracted = analyze.extract(_docs(data, "a", "b"), model="claude-sonnet-5-5", effort=None, workers=2)
    consolidated = analyze.consolidate([decision()], {"doc": "Bestyrelsen"}, model="claude-opus-5-5",
                                       effort=None, workers=1)
    assert (extracted.calls, consolidated.calls) == (2, 1)
    assert fake_claude.invocations("version") == 1
    rules = json.loads((data / "regler" / "okonomi.json").read_text())
    assert rules["provenance"]["model"] == "claude-opus-5-5" and rules["provenance"]["cli"] == "2.1.294"


def test_an_alias_extraction_records_the_canonical_model(fake_claude, data):
    (doc,) = _docs(data, "a")
    fake_claude.answer(NOTHING_FOUND)
    analyze.extract([doc], model="sonnet", effort="high", workers=1)
    saved = _saved(data, doc)
    assert saved["model"] == "sonnet"
    assert {k: saved["provenance"][k] for k in ("model", "cli", "effort")} == \
        {"model": "claude-sonnet-5-5", "cli": "2.1.294", "effort": "high"}


def test_the_prompt_fingerprint_follows_the_prompt(fake_claude, data, monkeypatch):
    fake_claude.answer(NOTHING_FOUND)
    original = analyze.EXTRACT_SYSTEM

    def fingerprints(sha: str) -> set[str]:
        docs = _docs(data, "a", "b", sha=sha)  # a new sha makes both documents need extraction again
        analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=2)
        return {_saved(data, doc)["provenance"]["prompt"] for doc in docs}

    (first,) = fingerprints("v1")
    monkeypatch.setattr(analyze, "EXTRACT_SYSTEM", original + "\nNew rule.")
    (changed,) = fingerprints("v2")
    monkeypatch.setattr(analyze, "EXTRACT_SYSTEM", original)
    (again,) = fingerprints("v3")

    assert re.fullmatch(r"[0-9a-f]{12}", first)
    assert changed != first and again == first


def test_v3_is_v2_plus_the_rule_to_split_fees_and_nothing_else():
    v2, v3 = analyze.EXTRACT_PROMPTS["v2"], analyze.EXTRACT_PROMPTS["v3"]
    assert v3.count(analyze.EXTRACT_SPLIT_RULE) == 1 and v3.replace(analyze.EXTRACT_SPLIT_RULE, "") == v2
    assert v3.index("Field rules:") < v3.index(analyze.EXTRACT_SPLIT_RULE) < v3.index("- tekst:")
    with pytest.raises(ValueError, match="no extraction prompt 'v9'"):
        analyze.extract_prompt("v9")


def test_the_pipeline_prompt_keeps_its_cache_and_another_prompt_extracts_again(fake_claude, data):
    fake_claude.answer(NOTHING_FOUND)
    pipeline = analyze.extract_prompt()
    other = next(name for name in analyze.EXTRACT_PROMPTS if name != pipeline.name)  # v3 until it is the default
    docs = _docs(data, "a", "b")
    analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=1)
    analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=1, prompt=pipeline.name)
    assert fake_claude.invocations("call") == 2  # the pipeline's prompt by name is the same prompt: cached
    assert analyze.missing_extractions(docs) == [] and analyze.missing_extractions(docs, other) == ["a", "b"]

    analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=1, prompt=other)
    systems = [argv[argv.index("--system-prompt") + 1] for argv in fake_claude.calls()]
    assert systems == [pipeline.system] * 2 + [analyze.EXTRACT_PROMPTS[other]] * 2
    hashes = {name: analyze.prompt_hash(analyze.EXTRACT_PROMPTS[name], analyze.EXTRACT_SCHEMA)
              for name in (pipeline.name, other)}
    assert hashes[pipeline.name] != hashes[other]
    for doc in docs:
        saved = _saved(data, doc)
        assert saved["version"] == analyze.extract_prompt(other).version
        assert saved["provenance"]["prompt"] == hashes[other]
    assert analyze.missing_extractions(docs, other) == [] and analyze.missing_extractions(docs) == ["a", "b"]
