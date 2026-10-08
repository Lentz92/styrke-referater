from analyze import decision_hash
from checks import date_traps, effect_mismatches, stale_versions
from conftest import decision
from render import build_rules


def _rules(*pairs):
    """One rule from (decision, effekt) pairs."""
    raw = {"titel": "Regel", "kategori": "okonomi", "vigtig": True, "note": None,
           "versioner": [{"ref": d.ref, "effekt": e, "tekst": None, "kort": None, "kort_regel": None} for d, e in pairs]}
    return build_rules([raw], {d.ref: d for d, _ in pairs})


def test_adopted_decision_shown_as_proposal_is_flagged():
    d = decision(ref="a#1", udfald="vedtaget", handling="aendring")
    assert [p.kind for p in effect_mismatches(_rules((d, "foreslaaet")))] == ["virkning"]


def test_adopted_abolition_must_abolish():
    d = decision(ref="a#1", udfald="vedtaget", handling="ophaevelse")
    assert len(effect_mismatches(_rules((d, "aendret")))) == 1
    assert effect_mismatches(_rules((d, "ophaevet"))) == []


def test_rejected_abolition_is_a_rejected_proposal():
    d = decision(ref="a#1", udfald="forkastet", handling="ophaevelse")
    assert effect_mismatches(_rules((d, "forkastet"))) == []


def test_confirmation_without_end_date_after_a_seasonal_rule():
    season = decision(ref="a#1", dato="2018-01-10", gaelder_til="2018-12-31")
    confirmed = decision(ref="b#1", dato="2018-06-01")
    assert [p.kind for p in date_traps(_rules((season, "indfoert"), (confirmed, "bekraeftet")))] == ["dato"]


def test_newer_decision_taking_effect_before_an_older_one():
    older = decision(ref="a#1", dato="2024-03-01", gaelder_fra="2024-09-01")
    newer = decision(ref="b#1", dato="2024-06-01")
    problems = date_traps(_rules((older, "indfoert"), (newer, "aendret")))
    assert len(problems) == 1 and "b#1 er besluttet efter a#1" in problems[0].message


def test_bare_years_are_not_compared_with_dates():
    year_only = decision(ref="a#1", dato="2015")
    dated = decision(ref="b#1", dato="2015-06-01")
    assert date_traps(_rules((year_only, "indfoert"), (dated, "aendret"))) == []


def test_stale_version_is_reported():
    d = decision(ref="a#1")
    raw = [{"titel": "Regel", "versioner": [{"ref": "a#1", "dhash": "000000000000"}]}]
    assert [p.kind for p in stale_versions(raw, {"a#1": d})] == ["forældet"]
    raw[0]["versioner"][0]["dhash"] = decision_hash(d)
    assert stale_versions(raw, {"a#1": d}) == []
