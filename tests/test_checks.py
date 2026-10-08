from analyze import decision_hash
from checks import date_traps, effect_mismatches, stale_versions
from conftest import decision
from render import build_rules


def _rules(*pairs):
    """One rule from (decision, effekt) pairs."""
    raw = {"titel": "Regel", "slug": "regel", "kategori": "okonomi", "vigtig": True, "note": None,
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


def _identity_rule(titel, slug, *refs, kategori="okonomi"):
    return {"titel": titel, "kategori": kategori, "versioner": [{"ref": ref} for ref in refs],
            **({"slug": slug} if slug is not None else {})}


def test_each_identity_problem_is_reported():
    from checks import identity_problems
    from matching import FormerSlug, SlugRegistry

    decisions = [decision(ref=ref) for ref in ("a#1", "a#1", "b#1", "c#1", "m#1", "u#1")]
    raw = [_identity_rule("Licensgebyr", "licensgebyr", "a#1", "x#9"), _identity_rule("Licens", "licensgebyr", "b#1"),
           _identity_rule("Uden slug", None, "c#1"), _identity_rule("Uden slug 2", "", "c#1"),
           _identity_rule("Klubskifte", "klubskifte", "r#1"),
           _identity_rule("Masterlicens", "masterlicens", "m#1", kategori="master")]
    slugs = SlugRegistry(
        aliases={"gebyr": FormerSlug("okonomi", "Gebyr", frozenset({"a#1"}), "licensgebyr"),
                 "startgebyr": FormerSlug("okonomi", "Startgebyr", frozenset(), "findes-ikke"),
                 "rabat": FormerSlug("okonomi", "Rabat", frozenset(), "licensgebyr"),
                 "licens": FormerSlug("okonomi", "Licens", frozenset({"m#1"}), "licensgebyr"),
                 # u#1 still exists but no rule holds it, and masterlicens is not titled "Engangsgebyr"
                 "engangsgebyr": FormerSlug("master", "Engangsgebyr", frozenset({"u#1"}), "masterlicens")},
        retired={"klubskifte": FormerSlug("okonomi", "Klubskifte", frozenset()),
                 "rabat": FormerSlug("okonomi", "Rabat", frozenset()),
                 "masters": FormerSlug("master", "Masters", frozenset({"m#1"}))})

    messages = [p.message for p in identity_problems(decisions, {"b#1", "r#1"}, raw, slugs)]
    expected = ["decision id a#1 is used 2 times", "decision id b#1 is both in use and retired",
                "Licensgebyr: version x#9 refers to no decision", "Uden slug (okonomi) has no slug",
                "Uden slug 2 (okonomi) has no slug", "slug licensgebyr is held by 2 rules",
                "alias startgebyr leads to findes-ikke, which no rule holds",
                "slug klubskifte is held by a rule but also an alias or retired",
                "slug rabat is both an alias and retired",
                "former slug licens leads to licensgebyr, but masterlicens holds most of its decisions",
                "former slug masters leads to nothing, but masterlicens holds most of its decisions",
                "alias engangsgebyr leads to masterlicens, which holds none of its decisions"]
    assert len(messages) == len(expected)
    assert all(any(text in message for message in messages) for text in expected)


def test_an_alias_to_a_namesake_needs_it_to_be_the_only_one_in_its_category():
    from checks import identity_problems
    from matching import FormerSlug, SlugRegistry

    report = "Rapportering fra internationale mesterskaber"
    raw = [_identity_rule(report, "rapportering", "l#1", kategori="landshold"),
           _identity_rule(report, "rapportering-3", "o#9", kategori="organisation")]
    decisions = [decision(ref=ref) for ref in ("l#1", "o#9", "o#1")]  # o#1: a one-off now, in no rule

    def problems(to: str) -> list[str]:
        alias = FormerSlug("organisation", report, frozenset({"o#1"}), to)
        return [p.message for p in identity_problems(decisions, set(), raw, SlugRegistry(aliases={"r-2": alias}))]

    assert problems("rapportering-3") == []  # the only rule of that title in organisation
    wrong = ("alias r-2 leads to rapportering, which holds none of its decisions and is not the only rule of "
             "organisation titled like 'Rapportering fra internationale mesterskaber'")
    assert problems("rapportering") == [wrong]  # landshold's rule of that name is another rule


def test_consistent_ids_and_slugs_report_nothing():
    from checks import identity_problems
    from matching import FormerSlug, SlugRegistry

    # A retired ref is a stale rule, not an identity problem; an alias whose decisions no longer exist keeps the
    # target it had; a former slug whose decisions are gone and that has no target leads nowhere.
    raw = [_identity_rule("Licensgebyr", "licensgebyr", "a#1", "r#1")]
    slugs = SlugRegistry(aliases={"gebyr": FormerSlug("okonomi", "Gebyr", frozenset({"a#1"}), "licensgebyr"),
                                  "licens": FormerSlug("okonomi", "Licens", frozenset({"b#8"}), "licensgebyr")},
                         retired={"x": FormerSlug("okonomi", "X", frozenset({"q#1"}))})
    assert identity_problems([decision(ref="a#1")], {"r#1"}, raw, slugs) == []
