"""Consistency checks on decisions and rule histories. Problems are reported, never fixed here."""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal, get_args

import render
from analyze import PROPOSAL_EFFECT, Decision, has_slug, live_rules
from matching import FormerSlug, LiveRule, SlugRegistry, holder, same_title

# "stale": a rule left out; "effect": extraction and rule disagree; "date": a possible date trap;
# "identity": a decision id or rule slug that is missing, used twice or leads nowhere.
ProblemKind = Literal["stale", "effect", "date", "identity"]
PROBLEM_KINDS: tuple[str, ...] = get_args(ProblemKind)  # each is counted on the index page


@dataclass(frozen=True)
class Problem:
    kind: ProblemKind
    message: str


def find_problems(decisions: list[Decision], raw_rules: list[dict], retired_ids: Collection[str],
                  slugs: SlugRegistry) -> list[Problem]:
    by_ref = {d.ref: d for d in decisions}
    rules = render.build_rules(raw_rules, by_ref)
    return (identity_problems(decisions, retired_ids, raw_rules, slugs) + stale_versions(raw_rules, by_ref)
            + effect_mismatches(rules) + date_traps(rules))


def identity_problems(decisions: list[Decision], retired_ids: Collection[str], raw_rules: list[dict],
                      slugs: SlugRegistry) -> list[Problem]:
    """Decision ids and rule slugs that are missing, used twice, or lead nowhere or to the wrong rule.

    A version whose decision was retired is a stale rule (stale_versions); one whose ref was never a decision
    id is reported here. Rules without a slug are left out of the pages (render.build_rules). Former slugs are
    checked against their decisions and title directly, not through the resolver that set them
    (analyze.resolve_slugs), so a wrong pointer shows up even when the resolver is what went wrong.
    """
    problems = []
    live_ids = Counter(d.ref for d in decisions)
    problems += [_identity(f"decision id {ref} is used {n} times") for ref, n in sorted(live_ids.items()) if n > 1]
    problems += [_identity(f"decision id {ref} is both in use and retired")
                 for ref in sorted(set(live_ids) & set(retired_ids))]
    known = set(live_ids) | set(retired_ids)
    for raw in raw_rules:
        problems += [_identity(f"{raw['titel']}: version {v['ref']} refers to no decision") for v in raw["versioner"]
                     if v["ref"] not in known]
        if not has_slug(raw):
            problems.append(_identity(f"{raw['titel']} ({raw['kategori']}) has no slug and is not shown"))
    live_slugs = Counter(raw["slug"] for raw in raw_rules if has_slug(raw))
    problems += [_identity(f"slug {slug} is held by {n} rules") for slug, n in sorted(live_slugs.items()) if n > 1]
    problems += [_identity(f"alias {old} leads to {new}, which no rule holds")
                 for old, new in sorted(slugs.targets().items()) if new not in live_slugs]
    problems += [_identity(f"slug {slug} is held by a rule but also an alias or retired")
                 for slug in sorted(set(live_slugs) & slugs.taken())]
    problems += [_identity(f"slug {slug} is both an alias and retired")
                 for slug in sorted(set(slugs.aliases) & set(slugs.retired))]
    live = live_rules(raw_rules)
    for slug, former in sorted(slugs.former().items()):
        problems += _former_slug_problems(slug, former, live, set(live_ids))
    return problems


def _former_slug_problems(slug: str, former: FormerSlug, live: list[LiveRule], live_ids: set[str]) -> list[Problem]:
    """A former slug must lead to the live rule holding most of its decisions. When none holds any, an alias
    needs other evidence: its target is the only rule of its category with its title, or none of its decisions
    exists any more (they were extracted again under new ids), so the last known target is the best there is."""
    rule = holder(former.refs, live)
    if rule is not None:
        if former.to == rule.slug:
            return []
        return [_identity(f"former slug {slug} leads to {former.to or 'nothing'}, but {rule.slug} holds most of its "
                          f"decisions")]
    if former.to is None or not former.refs & live_ids:
        return []
    namesakes = same_title(former, live)
    if len(namesakes) == 1 and namesakes[0].slug == former.to:
        return []
    return [_identity(f"alias {slug} leads to {former.to}, which holds none of its decisions and is not the only "
                      f"rule of {former.category} titled like {former.title!r}")]


def _identity(message: str) -> Problem:
    return Problem("identity", message)


def stale_versions(raw_rules: list[dict], by_ref: dict[str, Decision]) -> list[Problem]:
    """One problem per rule that render leaves out because a decision behind it has changed or is
    gone since the rule was consolidated."""
    problems = []
    for raw in raw_rules:
        refs = render.stale_refs(raw, by_ref)
        if refs:
            reasons = ", ".join(f"{ref} {'has changed' if ref in by_ref else 'no longer exists'}" for ref in refs)
            problems.append(Problem("stale", f"{raw['titel']} is not shown until the category is consolidated "
                                             f"again: {reasons}"))
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
                problems.append(Problem("effect", f"{rule.titel}: the extraction of {d.ref} ({d.udfald}/"
                                                  f"{d.handling}) and the rule ({v.effekt}) disagree on the "
                                                  f"effect; check the minutes"))
    return problems


def date_traps(rules: list[render.Rule]) -> list[Problem]:
    """Version orders that may make Rule.in_force show the wrong version. Seasonal rules often trip
    these legitimately, so they are things to look at, not errors."""
    problems = []
    for rule in rules:
        content = [v for v in rule.versions if v.effekt in render.CONTENT_EFFECTS]
        for before, after in zip(content, content[1:]):
            if before.decision.gaelder_til and not after.decision.gaelder_til and after.effekt == "bekraeftet":
                problems.append(Problem("date", f"{rule.titel}: {after.decision.ref} confirms a rule with an "
                                                f"end date without having one itself, so it applies forever"))
        # in_force applies versions in effective order, so a newer decision that takes effect
        # before an older one is overridden by the older one.
        for i, newer in enumerate(content):
            older = next((v for v in content[i + 1:] if _is_full(newer.decision.dato) and _is_full(v.decision.dato)
                          and v.decision.dato < newer.decision.dato), None)
            if older:
                problems.append(Problem("date", f"{rule.titel}: {newer.decision.ref} was decided after "
                                                f"{older.decision.ref} but applies from before it"))
    return problems


def _is_full(value: str | None) -> bool:
    """A bare year can't be ordered against a date in the same year, so only full dates are compared."""
    return bool(value) and len(value) == 10
