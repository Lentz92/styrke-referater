import json

import pytest

import analyze
from analyze import decision_hash
from conftest import decision
from render import build_rules


def _version(d, effekt="indfoert", tekst=None, dhash=True):
    v = {"ref": d.ref, "effekt": effekt, "tekst": tekst, "kort": None, "kort_regel": None}
    return {**v, "dhash": decision_hash(d)} if dhash else v


def _rule(*versions, titel="Licensgebyr"):
    return {"titel": titel, "kategori": "okonomi", "vigtig": True, "note": None, "versioner": list(versions)}


def _built(decisions, *versions):
    return build_rules([_rule(*versions)], {d.ref: d for d in decisions})[0]


# ---------------------------------------------------------------- which version is in force

def test_abolished_rule_is_not_in_force():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2015-04-01", handling="ophaevelse")
    rule = _built([a, b], _version(a), _version(b, "ophaevet"))
    assert rule.in_force("2012-12-31").decision is a
    assert rule.in_force("2016-12-31") is None


def test_future_start_date_waits():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2024-03-24", gaelder_fra="2025-01-01")
    rule = _built([a, b], _version(a), _version(b, "aendret"))
    assert rule.in_force("2024-12-31").decision is a
    assert rule.in_force("2025-06-01").decision is b


def test_retroactive_decision_counts_only_once_decided():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2024-06-01", gaelder_fra="2024-01-01")
    rule = _built([a, b], _version(a), _version(b, "aendret"))
    assert rule.in_force("2024-03-01").decision is a
    assert rule.in_force("2024-12-31").decision is b


def test_end_date_expires_the_rule():
    a = decision(ref="a#1", dato="2023-01-10", gaelder_til="2023-12-31")
    rule = _built([a], _version(a))
    assert rule.in_force("2023-06-01").decision is a
    assert rule.in_force("2024-06-01") is None


def test_year_only_date_applies_from_the_start_of_the_year():
    a = decision(ref="a#1", dato="2015")
    rule = _built([a], _version(a))
    assert rule.in_force("2014-12-31") is None
    assert rule.in_force("2015-06-01").decision is a


@pytest.mark.parametrize("effekt", ["foreslaaet", "forkastet", "trukket"])
def test_proposals_do_not_change_the_rule(effekt):
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2020-04-01", udfald="forkastet")
    rule = _built([a, b], _version(a), _version(b, effekt))
    assert rule.in_force("2021-12-31").decision is a


# ---------------------------------------------------------------- order and stale versions

def test_same_day_versions_keep_claudes_order():
    # Claude's texts build on each other in the order it listed them, so that order must survive.
    first = decision(ref="m#10", rank=1)
    second = decision(ref="m#2", rank=0)
    rule = _built([first, second], _version(first, tekst="A"), _version(second, "aendret", tekst="A og B"))
    assert [v.text for v in rule.versions] == ["A", "A og B"]


def test_version_of_a_changed_decision_is_left_out():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2015-04-01")
    stale = {**_version(b, "aendret"), "dhash": "000000000000"}
    rule = _built([a, b], _version(a), stale)
    assert [v.decision for v in rule.versions] == [a]


def test_versions_without_fingerprint_are_accepted():
    a = decision(ref="a#1")
    assert len(_built([a], _version(a, dhash=False)).versions) == 1


# ---------------------------------------------------------------- consolidation output

def test_consolidation_fixes_proposal_effects_and_fingerprints(tmp_path, monkeypatch):
    adopted = decision(ref="a#1")
    rejected = decision(ref="b#1", udfald="forkastet", dato="2021-04-01")
    items = [analyze._consolidation_input(d, "Repræsentantskabet") for d in (adopted, rejected)]
    claude_output = {
        "regler": [_rule(_version(adopted, dhash=False), _version(rejected, "aendret", dhash=False),
                         {"ref": "x#9", "effekt": "indfoert", "tekst": None, "kort": "", "kort_regel": None})],
        "udeladt": [],
    }
    monkeypatch.setattr(analyze, "RULES_DIR", tmp_path)
    monkeypatch.setattr(analyze, "ask_claude", lambda *args, **kwargs: (claude_output, 0.0))

    analyze._consolidate_one("okonomi", items, "hash", {d.ref: decision_hash(d) for d in (adopted, rejected)},
                             model="opus", effort=None, deadline=None)

    (rule,) = json.loads((tmp_path / "okonomi.json").read_text())["regler"]
    assert [(v["ref"], v["effekt"]) for v in rule["versioner"]] == [("a#1", "indfoert"), ("b#1", "forkastet")]
    assert [v["dhash"] for v in rule["versioner"]] == [decision_hash(adopted), decision_hash(rejected)]
