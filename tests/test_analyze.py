"""The Claude steps in styrke/analyze.py against the fake `claude` from conftest.py: what their results record and when
they are made again; no real Claude calls."""

import json
import re
from dataclasses import replace

import pytest
from conftest import decision

from styrke import analyze, claude

NOTHING_FOUND = {"moededato": None, "beslutninger": [], "regler": [], "udeladt": []}  # fits both steps


def test_an_error_after_a_successful_call_keeps_its_cost(fake_claude, world, monkeypatch):
    def full_disk(path, value):
        raise OSError("No space left on device")

    monkeypatch.setattr(analyze, "_write_json", full_disk)
    fake_claude.answer(NOTHING_FOUND)
    step = analyze.extract([world.downloaded("a")], model="claude-sonnet-5-5", effort=None, workers=1)
    assert (step.calls, step.failed, step.usage.cost_usd) == (1, 1, 0.25)


def test_steps_without_work_never_run_the_cli(fake_claude, world):
    doc = world.downloaded("a")
    fake_claude.answer(NOTHING_FOUND)
    analyze.extract([doc], model="claude-sonnet-5-5", effort=None, workers=1)
    claude.cli_version.cache_clear()
    before = fake_claude.invocations("version")

    analyze.extract([doc], model="claude-sonnet-5-5", effort=None, workers=1)
    analyze.consolidate([], {}, model="claude-opus-5-5", effort=None, workers=1)
    assert fake_claude.invocations("version") == before


def test_the_cli_version_is_read_once_for_two_steps_with_work(fake_claude, world):
    fake_claude.answer(NOTHING_FOUND)
    docs = [world.downloaded("a"), world.downloaded("b")]
    extracted = analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=2)
    consolidated = analyze.consolidate([decision()], {"doc": "Bestyrelsen"}, model="claude-opus-5-5",
                                       effort=None, workers=1)
    assert (extracted.calls, consolidated.calls) == (2, 1)
    assert fake_claude.invocations("version") == 1
    rules = world.stored("okonomi")
    assert rules["provenance"]["model"] == "claude-opus-5-5" and rules["provenance"]["cli"] == "2.1.294"


def test_an_alias_extraction_records_the_canonical_model(fake_claude, world):
    fake_claude.answer(NOTHING_FOUND)
    analyze.extract([world.downloaded("a")], model="sonnet", effort="high", workers=1)
    saved = world.extraction("a")
    assert saved["model"] == "sonnet"
    assert {k: saved["provenance"][k] for k in ("model", "cli", "effort")} == \
        {"model": "claude-sonnet-5-5", "cli": "2.1.294", "effort": "high"}


def test_the_prompt_fingerprint_follows_the_prompt(fake_claude, world, monkeypatch):
    fake_claude.answer(NOTHING_FOUND)
    original = analyze.EXTRACT_SYSTEM

    def fingerprints(sha: str) -> set[str]:
        docs = [replace(world.downloaded(name), sha256=sha) for name in "ab"]  # a new sha: extracted again
        analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=2)
        return {world.extraction(doc.id)["provenance"]["prompt"] for doc in docs}

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


def test_the_pipeline_prompt_keeps_its_cache_and_a_new_one_extracts_again(fake_claude, world, monkeypatch):
    fake_claude.answer(NOTHING_FOUND)
    pipeline = analyze.extract_prompt()
    other = analyze.extract_prompt(next(name for name in analyze.EXTRACT_PROMPTS if name != pipeline.name))
    docs = [world.downloaded("a"), world.downloaded("b")]
    for _ in range(2):
        analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=1)
    assert fake_claude.invocations("call") == 2  # the second run finds both current
    assert analyze.missing_extractions(docs, model="claude-sonnet-5-5") == []
    assert analyze.superseded_extractions(docs, model="claude-sonnet-5-5") == []

    # The pipeline moves to the other prompt (a new EXTRACT_VERSION): every document is extracted again with it.
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", other.version)
    monkeypatch.setattr(analyze, "EXTRACT_SYSTEM", other.system)
    assert analyze.missing_extractions(docs, model="claude-sonnet-5-5") == ["a", "b"]
    assert analyze.superseded_extractions(docs, model="claude-sonnet-5-5") == ["a", "b"]
    analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=1)
    assert fake_claude.option("--system-prompt") == [pipeline.system] * 2 + [other.system] * 2
    hashes = {prompt.name: claude.prompt_hash(prompt.system, analyze.EXTRACT_SCHEMA) for prompt in (pipeline, other)}
    assert hashes[pipeline.name] != hashes[other.name]
    for doc in docs:
        saved = world.extraction(doc.id)
        assert saved["version"] == other.version
        assert saved["provenance"]["prompt"] == hashes[other.name]
    assert analyze.missing_extractions(docs, model="claude-sonnet-5-5") == []

    # Back to the first prompt: the cache now holds the other prompt's extractions, so both are extracted again.
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", pipeline.version)
    monkeypatch.setattr(analyze, "EXTRACT_SYSTEM", pipeline.system)
    assert analyze.missing_extractions(docs, model="claude-sonnet-5-5") == ["a", "b"]


def test_an_extraction_by_another_model_than_the_one_asked_for_is_extracted_again(fake_claude, world):
    fake_claude.answer(NOTHING_FOUND)
    docs = [world.downloaded("a")]
    analyze.extract(docs, model="claude-sonnet-5-5", effort=None, workers=1)
    assert analyze.missing_extractions(docs, model="claude-sonnet-5-5") == []
    assert analyze.missing_extractions(docs, model="claude-opus-5-5") == ["a"]
    assert analyze.missing_extractions(docs, model="opus") == []  # an alias stands for whichever model answers
    for _ in range(2):
        analyze.extract(docs, model="claude-opus-5-5", effort=None, workers=1)
    assert fake_claude.invocations("call") == 2  # once with Sonnet, once with Opus: then it is current
    assert world.extraction("a")["provenance"]["model"] == "claude-opus-5-5"

    without = {k: v for k, v in world.extraction("a").items() if k != "provenance"}  # from before it was kept
    (analyze.DECISIONS_DIR / "a.json").write_text(json.dumps(without))
    assert analyze.missing_extractions(docs, model="claude-opus-5-5") == ["a"]
