from dataclasses import replace

import pytest
from conftest import decision, rule

import analyze
from analyze import decision_hash
from render import build_rules


def _rule(*versions, titel="Licensgebyr"):
    """A rule of okonomi; its versions as conftest.rule takes them."""
    return rule(titel, titel.lower(), *versions, kategori="okonomi")


def _built(decisions, *versions):
    return build_rules([_rule(*versions)], {d.ref: d for d in decisions})[0]


# ---------------------------------------------------------------- which version is in force

def test_abolished_rule_is_not_in_force():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2015-04-01", handling="ophaevelse")
    rule = _built([a, b], a, (b, "ophaevet"))
    assert rule.in_force("2012-12-31").decision is a
    assert rule.in_force("2016-12-31") is None


def test_future_start_date_waits():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2024-03-24", gaelder_fra="2025-01-01")
    rule = _built([a, b], a, (b, "aendret"))
    assert rule.in_force("2024-12-31").decision is a
    assert rule.in_force("2025-06-01").decision is b


def test_retroactive_decision_counts_only_once_decided():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2024-06-01", gaelder_fra="2024-01-01")
    rule = _built([a, b], a, (b, "aendret"))
    assert rule.in_force("2024-03-01").decision is a
    assert rule.in_force("2024-12-31").decision is b


def test_end_date_expires_the_rule():
    a = decision(ref="a#1", dato="2023-01-10", gaelder_til="2023-12-31")
    rule = _built([a], a)
    assert rule.in_force("2023-06-01").decision is a
    assert rule.in_force("2024-06-01") is None


def test_a_confirmation_was_adopted_by_a_decision_made_by_the_cutoff():
    # A decision of August 2020 that applies from 2019 did not yet adopt what a December 2019 confirmation said.
    adopted = decision(ref="a#1", dato="2016-12-04", gaelder_fra="2018-01-01")
    late = decision(ref="b#1", dato="2020-08-16", gaelder_fra="2019-01-01")
    confirmed = decision(ref="c#1", dato="2019-12-07")
    rule = _built([adopted, late, confirmed], adopted, (late, "aendret"), (confirmed, "bekraeftet"))
    for cutoff, origin in (("2019-12-31", adopted), ("2020-12-31", late)):
        current = rule.in_force(cutoff)
        assert current.decision is confirmed and rule.adopted(current, cutoff).decision is origin


def test_year_only_date_applies_from_the_start_of_the_year():
    a = decision(ref="a#1", dato="2015")
    rule = _built([a], a)
    assert rule.in_force("2014-12-31") is None
    assert rule.in_force("2015-06-01").decision is a


@pytest.mark.parametrize("effekt", ["foreslaaet", "forkastet", "trukket"])
def test_proposals_do_not_change_the_rule(effekt):
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2020-04-01", udfald="forkastet")
    rule = _built([a, b], a, (b, effekt))
    assert rule.in_force("2021-12-31").decision is a


# ---------------------------------------------------------------- order and stale versions

def test_same_day_versions_keep_claudes_order():
    # Claude's texts build on each other in the order it listed them, so that order must survive.
    # Neither ref order ("m#10" < "m#2") nor reading order (rank) gives Claude's order here.
    first = decision(ref="m#2", rank=1)
    second = decision(ref="m#10", rank=0)
    rule = _built([first, second], (first, "indfoert", {"tekst": "A"}), (second, "aendret", {"tekst": "A og B"}))
    assert [v.text for v in rule.versions] == ["A", "A og B"]


def test_rule_with_a_changed_decision_is_left_out_whole():
    # Dropping only the changed abolition would show the repealed rule as in force.
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2015-04-01", handling="ophaevelse")
    stale = (b, "ophaevet", {"dhash": "000000000000"})
    assert build_rules([_rule(a, stale)], {d.ref: d for d in (a, b)}) == []


def test_rule_with_a_decision_that_is_gone_is_left_out_whole():
    a = decision(ref="a#1", dato="2010-04-01")
    b = decision(ref="b#1", dato="2015-04-01")
    assert build_rules([_rule(a, (b, "aendret"))], {a.ref: a}) == []


def test_other_rules_are_still_shown():
    a = decision(ref="a#1")
    b = decision(ref="b#1")
    raw = [_rule(a, titel="Licensgebyr"), _rule((b, "indfoert", {"dhash": "000000000000"}), titel="Andet")]
    assert [r.titel for r in build_rules(raw, {d.ref: d for d in (a, b)})] == ["Licensgebyr"]


def test_versions_without_fingerprint_are_left_out():
    a = decision(ref="a#1")
    assert build_rules([_rule(a.ref)], {a.ref: a}) == []


def test_new_quote_alone_keeps_the_rule():
    # The consolidation never saw the quote, so a re-extraction that only changes it must not hide the rule.
    a = decision(ref="a#1")
    assert len(_built([replace(a, citat="et andet citat")], a).versions) == 1
    assert build_rules([_rule(a)], {a.ref: replace(a, stemmer="31 for, 7 imod")}) == []


# ---------------------------------------------------------------- consolidation output

def test_consolidation_fixes_proposal_effects_and_fingerprints(world, fake_claude):
    adopted = decision(ref="a#1")
    rejected = decision(ref="b#1", udfald="forkastet", dato="2021-04-01")
    fake_claude.answer({"regler": [rule("Licensgebyr", None, "a#1", ("b#1", "aendret"), "x#9")], "udeladt": []})

    analyze.consolidate([adopted, rejected], {"doc": "Repræsentantskabet"}, model="claude-opus-5-5", effort=None,
                        workers=1)

    (consolidated,) = world.stored("okonomi")["regler"]
    assert [(v["ref"], v["effekt"]) for v in consolidated["versioner"]] == [("a#1", "indfoert"), ("b#1", "forkastet")]
    assert [v["dhash"] for v in consolidated["versioner"]] == [decision_hash(adopted), decision_hash(rejected)]
