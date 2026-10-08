"""Consistency checks on decisions and rule histories. Problems are reported, never fixed here."""

from __future__ import annotations

from dataclasses import dataclass

import render
from analyze import PROPOSAL_EFFECT, Decision, version_matches


@dataclass(frozen=True)
class Problem:
    kind: str  # "forældet", "virkning" or "dato" (a possible date trap); counted in the data-quality section
    message: str


def find_problems(decisions: list[Decision], raw_rules: list[dict]) -> list[Problem]:
    by_ref = {d.ref: d for d in decisions}
    rules = render.build_rules(raw_rules, by_ref)
    return stale_versions(raw_rules, by_ref) + effect_mismatches(rules) + date_traps(rules)


def stale_versions(raw_rules: list[dict], by_ref: dict[str, Decision]) -> list[Problem]:
    """Versions whose decision changed after the rule was consolidated (render leaves them out)."""
    return [
        Problem("forældet", f"{raw['titel']}: {v['ref']} er ændret siden konsolideringen")
        for raw in raw_rules for v in raw["versioner"]
        if v["ref"] in by_ref and not version_matches(v, by_ref[v["ref"]])
    ]


def effect_mismatches(rules: list[render.Rule]) -> list[Problem]:
    """Versions whose effect contradicts the decision's outcome or action."""
    problems = []
    for rule in rules:
        for v in rule.versions:
            d = v.decision
            if d.udfald in PROPOSAL_EFFECT:
                wrong = v.effekt != PROPOSAL_EFFECT[d.udfald]
            elif d.handling == "ophaevelse":
                wrong = v.effekt != "ophaevet"
            else:  # adopted: a content effect, or abolishing this rule while adopting something else
                wrong = v.effekt not in (*render.CONTENT_EFFECTS, "ophaevet")
            if wrong:
                problems.append(Problem("virkning", f"{rule.titel}: {d.ref} er {d.udfald}/{d.handling}, "
                                                    f"men vises som {v.effekt}"))
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
