"""Consistency checks on decisions and rule histories. Problems are reported, never fixed here."""

from __future__ import annotations

from dataclasses import dataclass

import render
from analyze import PROPOSAL_EFFECT, Decision


@dataclass(frozen=True)
class Problem:
    kind: str  # "forældet" (a rule left out), "virkning" or "dato" (a possible date trap); counted on the index page
    message: str


def find_problems(decisions: list[Decision], raw_rules: list[dict]) -> list[Problem]:
    by_ref = {d.ref: d for d in decisions}
    rules = render.build_rules(raw_rules, by_ref)
    return stale_versions(raw_rules, by_ref) + effect_mismatches(rules) + date_traps(rules)


def stale_versions(raw_rules: list[dict], by_ref: dict[str, Decision]) -> list[Problem]:
    """One problem per rule that render leaves out because a decision behind it has changed or is
    gone since the rule was consolidated."""
    problems = []
    for raw in raw_rules:
        refs = render.stale_refs(raw, by_ref)
        if refs:
            reasons = ", ".join(f"{ref} {'er ændret' if ref in by_ref else 'findes ikke længere'}" for ref in refs)
            problems.append(Problem("forældet", f"{raw['titel']} vises ikke, før kategorien er konsolideret "
                                                f"igen: {reasons}"))
    return problems


def effect_mismatches(rules: list[render.Rule]) -> list[Problem]:
    """Versions where the extraction and the rule disagree about the effect.

    Either side can be wrong, but often it is the extraction's udfald: a board recommending a
    proposal to Repræsentantskabet is extracted as "vedtaget", while the rule shows a proposal.
    An abolition may legitimately show as "aendret": it can remove one part of a rule and keep the rest.
    """
    problems = []
    for rule in rules:
        for v in rule.versions:
            d = v.decision
            if d.udfald in PROPOSAL_EFFECT:
                wrong = v.effekt != PROPOSAL_EFFECT[d.udfald]
            elif d.handling == "ophaevelse":
                wrong = v.effekt not in ("ophaevet", "aendret")
            else:  # adopted: a content effect, or abolishing this rule while adopting something else
                wrong = v.effekt not in (*render.CONTENT_EFFECTS, "ophaevet")
            if wrong:
                problems.append(Problem("virkning", f"{rule.titel}: udtrækket af {d.ref} ({d.udfald}/"
                                                    f"{d.handling}) og reglen ({v.effekt}) er uenige om "
                                                    f"virkningen – tjek referatet"))
    return problems


def date_traps(rules: list[render.Rule]) -> list[Problem]:
    """Version orders that may make Rule.in_force show the wrong version. Seasonal rules often trip
    these legitimately, so they are things to look at, not errors."""
    problems = []
    for rule in rules:
        content = [v for v in rule.versions if v.effekt in render.CONTENT_EFFECTS]
        for before, after in zip(content, content[1:]):
            if before.decision.gaelder_til and not after.decision.gaelder_til and after.effekt == "bekraeftet":
                problems.append(Problem("dato", f"{rule.titel}: {after.decision.ref} bekræfter en regel med "
                                                f"slutdato uden selv at have en, så den gælder for altid"))
        # in_force applies versions in effective order, so a newer decision that takes effect
        # before an older one is overridden by the older one.
        for i, newer in enumerate(content):
            older = next((v for v in content[i + 1:] if _is_full(newer.decision.dato) and _is_full(v.decision.dato)
                          and v.decision.dato < newer.decision.dato), None)
            if older:
                problems.append(Problem("dato", f"{rule.titel}: {newer.decision.ref} er besluttet efter "
                                                f"{older.decision.ref}, men gælder fra før den"))
    return problems


def _is_full(value: str | None) -> bool:
    """A bare year can't be ordered against a date in the same year, so only full dates are compared."""
    return bool(value) and len(value) == 10
