from analyze import decision_hash
from checks import date_traps, effect_mismatches, stale_versions
from conftest import decision
from render import build_rules


def _rules(*pairs):
    """One rule from (decision, effekt) pairs."""
    raw = {"titel": "Regel", "kategori": "okonomi", "vigtig": True, "note": None,
           "versioner": [{"ref": d.ref, "effekt": e, "tekst": None, "kort": None, "kort_regel": None,
                          "dhash": decision_hash(d)} for d, e in pairs]}
    return build_rules([raw], {d.ref: d for d, _ in pairs})


def test_adopted_decision_shown_as_proposal_is_flagged():
    d = decision(ref="a#1", udfald="vedtaget", handling="aendring")
    assert [p.kind for p in effect_mismatches(_rules((d, "foreslaaet")))] == ["effect"]


def test_adopted_abolition_abolishes_the_rule_or_a_part_of_it():
    d = decision(ref="a#1", udfald="vedtaget", handling="ophaevelse")
    assert effect_mismatches(_rules((d, "ophaevet"))) == []
    assert effect_mismatches(_rules((d, "aendret"))) == []  # e.g. one requirement deleted, another kept
    assert [p.kind for p in effect_mismatches(_rules((d, "bekraeftet")))] == ["effect"]


def test_rejected_abolition_is_a_rejected_proposal():
    d = decision(ref="a#1", udfald="forkastet", handling="ophaevelse")
    assert effect_mismatches(_rules((d, "forkastet"))) == []


def test_confirmation_without_end_date_after_a_seasonal_rule():
    season = decision(ref="a#1", dato="2018-01-10", gaelder_til="2018-12-31")
    confirmed = decision(ref="b#1", dato="2018-06-01")
    assert [p.kind for p in date_traps(_rules((season, "indfoert"), (confirmed, "bekraeftet")))] == ["date"]


def test_newer_decision_taking_effect_before_an_older_one():
    older = decision(ref="a#1", dato="2024-03-01", gaelder_fra="2024-09-01")
    newer = decision(ref="b#1", dato="2024-06-01")
    problems = date_traps(_rules((older, "indfoert"), (newer, "aendret")))
    assert len(problems) == 1 and "b#1 was decided after a#1" in problems[0].message


def test_bare_years_are_not_compared_with_dates():
    # The year-only decision takes effect after the dated one, and "2015" < "2015-06-01" as text.
    year_only = decision(ref="a#1", dato="2015", gaelder_fra="2015-12-01")
    dated = decision(ref="b#1", dato="2015-06-01")
    assert date_traps(_rules((dated, "indfoert"), (year_only, "aendret"))) == []


def test_stale_rule_is_reported_once_with_each_reason():
    changed = decision(ref="a#1")
    raw = [{"titel": "Regel", "versioner": [{"ref": "a#1", "dhash": "000000000000"}, {"ref": "b#1", "dhash": "x"}]}]
    (problem,) = stale_versions(raw, {"a#1": changed})
    assert problem.kind == "stale"
    assert "a#1 has changed" in problem.message and "b#1 no longer exists" in problem.message
    raw[0]["versioner"] = [{"ref": "a#1", "dhash": decision_hash(changed)}]
    assert stale_versions(raw, {"a#1": changed}) == []


def test_every_problem_kind_is_counted_on_the_index_page():
    from collections import Counter
    from datetime import date

    import render
    from checks import PROBLEM_KINDS

    counts = Counter({kind: 101 + i for i, kind in enumerate(PROBLEM_KINDS)})
    page = render._index_page([2026], [], [], [], {}, [], counts, date(2026, 10, 8))
    assert all(f"{n}" in page for n in counts.values())
