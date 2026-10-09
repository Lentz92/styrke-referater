from collections import Counter
from dataclasses import replace
from datetime import date

from conftest import decision, rule

import render
from analyze import decision_hash
from checks import (
    DATA_KINDS,
    SEVERITY,
    Data,
    Problem,
    changed_decisions,
    check_history,
    date_traps,
    effect_mismatches,
    errors,
    identity_problems,
    snapshot,
    stale_versions,
    unassigned_decisions,
    unreflected,
)
from matching import FormerSlug, SlugRegistry
from render import build_rules


def _rule(slug, *versions):
    """A rule of okonomi titled after its slug; its versions as conftest.rule takes them."""
    return rule(slug.capitalize(), slug, *versions, kategori="okonomi")


def _rules(*versions):
    """One rule from (decision, effekt) versions, as render builds it."""
    return build_rules([_rule("regel", *versions)], {d.ref: d for d, _ in versions})


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


def test_every_problem_kind_of_the_data_is_counted_on_the_index_page():
    counts = Counter({kind: 101 + i for i, kind in enumerate(DATA_KINDS)})
    page = render.build_pages({}, [], [], [], counts, date(2026, 10, 8))["README.md"]
    assert all(f"{n}" in page for n in counts.values())


def test_each_identity_problem_is_reported():
    decisions = [decision(ref=ref) for ref in ("a#1", "a#1", "b#1", "c#1", "m#1", "u#1")]
    raw = [_rule("licensgebyr", "a#1", "x#9"), rule("Licens", "licensgebyr", "b#1", kategori="okonomi"),
           rule("Uden slug", None, "c#1", kategori="okonomi"), rule("Uden slug 2", "", "c#1", kategori="okonomi"),
           _rule("klubskifte", "r#1"), rule("Masterlicens", "masterlicens", "m#1", kategori="master")]
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
                "decision c#1 is in 2 rules",
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
    report = "Rapportering fra internationale mesterskaber"
    raw = [rule(report, "rapportering", "l#1", kategori="landshold"),
           rule(report, "rapportering-3", "o#9", kategori="organisation")]
    decisions = [decision(ref=ref) for ref in ("l#1", "o#9", "o#1")]  # o#1: a one-off now, in no rule

    def problems(to: str) -> list[str]:
        alias = FormerSlug("organisation", report, frozenset({"o#1"}), to)
        return [p.message for p in identity_problems(decisions, set(), raw, SlugRegistry(aliases={"r-2": alias}))]

    assert problems("rapportering-3") == []  # the only rule of that title in organisation
    wrong = ("alias r-2 leads to rapportering, which holds none of its decisions and is not the only rule of "
             "organisation titled like 'Rapportering fra internationale mesterskaber'")
    assert problems("rapportering") == [wrong]  # landshold's rule of that name is another rule


def test_consistent_ids_and_slugs_report_nothing():
    # A retired ref is a stale rule, not an identity problem; an alias whose decisions no longer exist keeps the
    # target it had; a former slug whose decisions are gone and that has no target leads nowhere.
    raw = [_rule("licensgebyr", "a#1", "r#1")]
    slugs = SlugRegistry(aliases={"gebyr": FormerSlug("okonomi", "Gebyr", frozenset({"a#1"}), "licensgebyr"),
                                  "licens": FormerSlug("okonomi", "Licens", frozenset({"b#8"}), "licensgebyr")},
                         retired={"x": FormerSlug("okonomi", "X", frozenset({"q#1"}))})
    assert identity_problems([decision(ref="a#1")], {"r#1"}, raw, slugs) == []


# ---------------------------------------------------------------- severity and unassigned decisions

def test_errors_block_publishing_and_warnings_are_reported_only():
    assert {kind for kind, severity in SEVERITY.items() if severity == "error"} == {"stale", "identity", "unassigned",
                                                                                   "history"}
    assert {kind for kind, severity in SEVERITY.items() if severity == "warning"} == {"effect", "date",
                                                                                     "history-text"}
    assert errors([Problem("date", "a"), Problem("stale", "b")]) == [Problem("stale", "b")]


def test_a_decision_in_no_rule_and_not_left_out_as_a_one_off_is_unassigned():
    in_rule, one_off, dropped = decision(ref="a#1"), decision(ref="b#1"), decision(ref="c#1")
    elsewhere = decision(ref="d#1")  # left out by another category's consolidation, which no longer has it
    waiting = decision(ref="e#1", kategori="master")  # master is to be consolidated again: not wrong, just missing
    raw = [{"titel": "Regel", "versioner": [{"ref": "a#1"}]}]
    one_offs = {"okonomi": {"b#1"}, "master": {"d#1"}}

    problems = unassigned_decisions([in_rule, one_off, dropped, elsewhere, waiting], raw, one_offs, {"master"})
    assert [(p.kind, p.message.split()[0]) for p in problems] == [("unassigned", "c#1"), ("unassigned", "d#1")]


# ---------------------------------------------------------------- history

TODAY = date(2026, 10, 8)
OLD = decision(ref="old#1", dato="2015-03-01")
NEW = decision(ref="new#1", dato="2024-03-01", emne="Ny", tekst="Licensgebyret er 300 kr.")


def _data(decisions, rules, one_offs=None, pending=()):
    return Data(list(decisions), rules, one_offs or {}, frozenset(pending), set(), SlugRegistry())


def _history(before_rules, after_rules, aliases=None, before=(OLD,), after=(OLD, NEW), **before_data):
    return check_history(snapshot(_data(before, before_rules, **before_data), TODAY),
                         snapshot(_data(after, after_rules), TODAY), aliases or {})


def test_history_before_the_rules_earliest_new_decision_must_not_change():
    check = _history([_rule("gebyr", (OLD, "indfoert"))], [_rule("gebyr", (NEW, "indfoert"))])

    assert {c.cutoff[:4] for c in check.changes} == {str(year) for year in range(2015, 2024)}
    assert {c.since for c in check.changes} == {"2024-03-01"}
    (problem,) = check.problems()
    assert problem.kind == "history"
    assert problem.message == ("Gebyr (gebyr): what was in force in 2015–2023 changed, although none of its "
                               "decisions before 2024-03-01 did")


def test_history_from_the_rules_earliest_new_decision_on_may_change():
    check = _history([_rule("gebyr", (OLD, "indfoert"))], [_rule("gebyr", (OLD, "indfoert"), (NEW, "aendret"))])
    assert check.changes == [] and check.problems() == []


def test_a_new_decision_in_one_rule_does_not_open_the_history_of_another():
    early = decision(ref="early#1", dato="2012-05-01", kategori="master", emne="Masterlicens")
    before = [_rule("gebyr", (OLD, "indfoert")), _rule("master", (early, "indfoert"))]
    changed = replace(early, tekst="Ændret")  # re-extracted: 2012 is open for its own rule only
    after = [_rule("gebyr", (OLD, "foreslaaet")), _rule("master", (changed, "indfoert"))]

    problems = _history(before, after, before=(OLD, early), after=(OLD, changed)).problems()
    assert [p.message.split(":")[0] for p in problems] == ["Gebyr (gebyr)"]
    assert "2015–2026 changed, although none of its decisions did" in problems[0].message


def test_an_undated_new_decision_opens_only_its_own_rules_history():
    undated = decision(ref="u#1", dato=None, emne="Udateret")
    before = [_rule("gebyr", (OLD, "indfoert"))]
    after = [_rule("gebyr", (OLD, "foreslaaet")), _rule("udateret", (undated, "indfoert"))]

    problems = _history(before, after, after=(OLD, undated)).problems()
    assert [p.message.split(":")[0] for p in problems] == ["Gebyr (gebyr)"]


def test_new_wording_for_the_same_decisions_is_a_warning():
    check = _history([_rule("gebyr", (OLD, "indfoert"))],
                     [_rule("gebyr", (OLD, "indfoert", {"tekst": "Ny formulering"}))])
    assert [(p.kind, "2015–2026" in p.message) for p in check.problems()] == [("history-text", True)]


def test_a_new_short_form_on_the_year_pages_is_a_warning():
    check = _history([_rule("gebyr", (OLD, "indfoert"))],
                     [_rule("gebyr", (OLD, "indfoert", {"kort_regel": "Licens 200 kr."}))])
    assert [p.kind for p in check.problems()] == ["history-text"]


def test_a_slug_that_became_an_alias_is_compared_with_the_rule_it_leads_to():
    before, after = [_rule("gebyr", (OLD, "indfoert"))], [_rule("licens", (OLD, "indfoert"))]
    assert _history(before, after, {"gebyr": "licens"}).problems() == []

    messages = [p.message for p in _history(before, after).problems()]  # without the alias: one gone, one new
    assert [m.split(":")[0] for m in messages] == ["Gebyr (gebyr)", "Licens (licens)"]


def test_rules_merged_into_one_are_compared_by_what_they_showed_between_them():
    repealed = decision(ref="old#2", dato="2018-03-01", handling="ophaevelse")
    later = decision(ref="later#1", dato="2019-03-01")
    before = [_rule("gebyr", (OLD, "indfoert"), (repealed, "ophaevet")), _rule("licens", (later, "indfoert"))]
    after = [_rule("licens", (OLD, "indfoert"), (repealed, "ophaevet"), (later, "indfoert"))]
    decisions = (OLD, repealed, later)

    assert _history(before, after, {"gebyr": "licens"}, before=decisions, after=decisions).problems() == []


def test_a_decision_the_rules_did_not_reflect_yet_counts_as_new():
    # A run whose consolidation failed left pending#1 in no rule; the next one adds it to the rule.
    pending = decision(ref="pending#1", dato="2024-03-01")
    before, after = [_rule("gebyr", (OLD, "indfoert"))], [_rule("gebyr", (OLD, "indfoert"), (pending, "aendret"))]
    decisions = (OLD, pending)

    assert _history(before, after, before=decisions, after=decisions).problems() == []
    # Left out as a one-off by a consolidation that saw it, it was reflected: the rule may not take it back silently.
    assert _history(before, after, before=decisions, after=decisions,
                    one_offs={"okonomi": {"pending#1"}}).problems() != []


def test_unreflected_decisions():
    built, changed, one_off, waiting, dropped = (decision(ref=f"{name}#1") for name in ("built", "changed",
                                                                                         "oneoff", "waiting", "drop"))
    rules = [_rule("gebyr", (built, "indfoert"), (replace(changed, tekst="Før"), "aendret"))]
    one_offs = {"okonomi": {"oneoff#1", "waiting#1"}}
    data = _data([built, changed, one_off, waiting, dropped], rules, one_offs)
    assert unreflected(data) == {"changed#1", "drop#1"}
    # In a category to be consolidated again, being left out as a one-off proves nothing.
    data = _data([built, changed, one_off, waiting, dropped], rules, one_offs, pending={"okonomi"})
    assert unreflected(data) == {"changed#1", "drop#1", "oneoff#1", "waiting#1"}


def test_a_run_that_changes_no_decision_must_not_change_any_year():
    check = _history([_rule("gebyr", (OLD, "indfoert"))], [_rule("gebyr", (OLD, "foreslaaet"))], after=(OLD,))
    assert {c.since for c in check.changes} == {None}
    (problem,) = check.problems()
    assert "2015–2026 changed, although none of its decisions did" in problem.message


def test_changed_decisions_include_removed_and_undated_ones():
    def of(*decisions):  # every decision left out as a one-off: reflected, so only differences count
        return snapshot(_data(decisions, [], {"okonomi": {d.ref for d in decisions}}), TODAY)

    assert changed_decisions(of(OLD, NEW), of(OLD, NEW)) == {}
    assert changed_decisions(of(OLD, NEW), of(NEW)) == {"old#1": "2015-03-01"}  # removed
    assert changed_decisions(of(OLD), of(replace(OLD, dato="2014-01-01"))) == {"old#1": "2014-01-01"}
    assert changed_decisions(of(NEW), of(NEW, replace(OLD, dato=None))) == {"old#1": ""}  # may apply always
