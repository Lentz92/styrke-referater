# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "pymupdf>=1.24",
# ]
# ///
"""Consistency checks on decisions and rule histories. Problems are reported, never fixed here.

    uv run checks.py    # check data/, list errors and warnings; exit 1 when there is an error

Errors block publishing: update.py exits 3 so the monthly run sends its result to a pull request, and the
website workflow does not build from data/ with errors. Warnings are things to look at.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from itertools import groupby
from typing import Literal, get_args

import analyze
import render
import scrape
from analyze import PROPOSAL_EFFECT, Decision, decision_hash, has_slug, live_rules
from matching import FormerSlug, LiveRule, SlugRegistry, holder, same_title
from scrape import Doc

# Problems data/ shows on its own, each counted on the index page. "stale": a rule left out because a decision
# behind it changed; "identity": a decision id or rule slug that is missing, used twice or leads nowhere;
# "unassigned": a decision in no rule and not left out as a one-off; "effect": extraction and rule disagree;
# "date": a possible date trap.
DataKind = Literal["stale", "identity", "unassigned", "effect", "date"]
# Problems in what a run did to the past: what a year page showed changed although no decision before that year
# did ("history"), or only its wording changed ("history-text"). They compare the data before and after a run,
# so only update.py finds them; `uv run checks.py` and render-only runs cannot.
HistoryKind = Literal["history", "history-text"]
ProblemKind = Literal[DataKind, HistoryKind]
DATA_KINDS: tuple[DataKind, ...] = get_args(DataKind)
HISTORY_KINDS: tuple[HistoryKind, ...] = get_args(HistoryKind)
Severity = Literal["error", "warning"]
# Errors block publishing; warnings are reported only.
SEVERITY: dict[ProblemKind, Severity] = {
    "stale": "error", "identity": "error", "unassigned": "error", "history": "error",
    "effect": "warning", "date": "warning", "history-text": "warning",
}
assert set(SEVERITY) == set(get_args(ProblemKind)), "every problem kind needs a severity"


@dataclass(frozen=True)
class Problem:
    kind: ProblemKind
    message: str

    @property
    def severity(self) -> Severity:
        return SEVERITY[self.kind]


@dataclass(frozen=True)
class Data:
    """What the checks read from data/."""
    decisions: list[Decision]
    raw_rules: list[dict]
    one_offs: Mapping[str, Collection[str]]  # category -> refs its consolidation left out as one-offs (udeladt)
    pending: Collection[str]  # categories whose decisions changed since their rule file was written
    retired_ids: Collection[str]
    slugs: SlugRegistry

    @classmethod
    def load(cls, docs: list[Doc]) -> Data:
        decisions = analyze.load_decisions(docs)
        todo = analyze.consolidation_todo(decisions, {d.id: d.organ_label for d in docs})
        return cls(decisions, analyze.load_rules(), analyze.load_one_offs(), frozenset(job.category for job in todo),
                   analyze.load_retired_ids(docs), analyze.load_slugs())


def find_problems(data: Data) -> list[Problem]:
    """Every problem data/ shows on its own: all kinds but the history ones."""
    by_ref = {d.ref: d for d in data.decisions}
    rules = render.build_rules(data.raw_rules, by_ref)
    return (identity_problems(data.decisions, data.retired_ids, data.raw_rules, data.slugs)
            + stale_versions(data.raw_rules, by_ref)
            + unassigned_decisions(data.decisions, data.raw_rules, data.one_offs, data.pending)
            + effect_mismatches(rules) + date_traps(rules))


def errors(problems: Iterable[Problem]) -> list[Problem]:
    return [p for p in problems if p.severity == "error"]


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


def unassigned_decisions(decisions: list[Decision], raw_rules: list[dict], one_offs: Mapping[str, Collection[str]],
                         pending: Collection[str]) -> list[Problem]:
    """Decisions the pages leave out without a reason: in no rule, and not among the one-offs their category's
    consolidation left out (udeladt), although that consolidation saw them, so Claude dropped them (ikke_tildelt).

    A category whose decisions changed since its last consolidation (a call that failed or was skipped) is left
    to the next run: its new decisions are missing, not wrong, like those of a document whose extraction failed.
    """
    in_rules = {v["ref"] for raw in raw_rules for v in raw["versioner"]}
    return [Problem("unassigned", f"{d.ref} ({d.kategori}: {d.emne}) is in no rule and not left out as a one-off")
            for d in decisions
            if d.ref not in in_rules and d.ref not in one_offs.get(d.kategori, ()) and d.kategori not in pending]


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


# --------------------------------------------------------------------------- history

@dataclass(frozen=True)
class InForce:
    """A rule as a year page shows it: the version in force, the version that adopted its content
    (Rule.adopted; another one when the version in force confirms it), its text, and its short form (essence)."""
    ref: str
    adopted: str
    text: str
    essence: str

    @property
    def decisions(self) -> tuple[str, str]:
        return self.ref, self.adopted

    def label(self) -> str:
        return self.ref if self.adopted == self.ref else f"{self.ref} (confirms {self.adopted})"


@dataclass(frozen=True)
class Snapshot:
    """What the year pages show at one moment of a run, and the decisions behind it."""
    titles: Mapping[str, str]  # slug -> title of every rule shown
    refs: Mapping[str, frozenset[str]]  # slug -> the decisions in its versions
    in_force: Mapping[str, Mapping[str, InForce]]  # year cutoff -> slug -> rule in force; others are absent
    decisions: Mapping[str, tuple[str | None, str]]  # ref -> (date, fingerprint) of every decision
    unreflected: frozenset[str]  # decisions the rule files do not reflect yet (see unreflected)


def snapshot(data: Data, today: date) -> Snapshot:
    rules = render.build_rules(data.raw_rules, {d.ref: d for d in data.decisions})
    in_force = {}
    for year in render.covered_years(data.decisions, today):
        cutoff = render.year_cutoff(year, today)
        in_force[cutoff] = {
            rule.slug: InForce(v.decision.ref, rule.adopted(v, cutoff).decision.ref, v.text, v.essence)
            for rule in rules if (v := rule.in_force(cutoff)) is not None
        }
    return Snapshot({rule.slug: rule.titel for rule in rules},
                    {rule.slug: frozenset(v.decision.ref for v in rule.versions) for rule in rules}, in_force,
                    {d.ref: (d.dato, decision_hash(d)) for d in data.decisions}, unreflected(data))


def unreflected(data: Data) -> frozenset[str]:
    """Decisions the rule files do not reflect yet: no version was built from the decision as it is now (by its
    fingerprint), and it is not a one-off of a category whose consolidation saw it (udeladt keeps no fingerprint,
    so in a category to be consolidated again it proves nothing). A consolidation that failed or was skipped
    leaves them to the next run, which may change rules on their account."""
    built = {(v["ref"], v.get("dhash")) for raw in data.raw_rules for v in raw["versioner"]}
    return frozenset(d.ref for d in data.decisions
                     if (d.ref, decision_hash(d)) not in built
                     and (d.ref not in data.one_offs.get(d.kategori, ()) or d.kategori in data.pending))


@dataclass(frozen=True)
class HistoryChange:
    """What one rule showed at one cutoff, before and after a run. Rules merged into one count as one."""
    title: str
    slugs: str  # the rule's slug, or "a, b → c" when slugs became aliases of another rule
    cutoff: str
    since: str | None  # the earliest date of a changed decision of the rule; None when none changed
    before: tuple[InForce, ...]  # empty: not in force or not shown; several: rules merged into one since
    after: tuple[InForce, ...]

    @property
    def kind(self) -> HistoryKind:
        same = bool(self.before) and {f.decisions for f in self.before} == {f.decisions for f in self.after}
        return "history-text" if same else "history"


@dataclass(frozen=True)
class HistoryCheck:
    changes: list[HistoryChange]  # at cutoffs before the earliest changed decision of each rule

    def problems(self) -> list[Problem]:
        """One problem per rule and kind, naming the years."""
        cutoffs: dict[tuple[str, str, str | None, HistoryKind], list[str]] = {}
        for change in self.changes:
            cutoffs.setdefault((change.title, change.slugs, change.since, change.kind), []).append(change.cutoff)
        problems = []
        for (title, slugs, since, kind), found in cutoffs.items():
            if kind == "history":
                reason = f"none of its decisions before {since} did" if since else "none of its decisions did"
                what = f"what was in force in {years(found)} changed, although {reason}"
            else:
                what = f"the wording in force in {years(found)} changed, the decisions behind it did not"
            problems.append(Problem(kind, f"{title} ({slugs}): {what}"))
        return problems


def check_history(before: Snapshot, after: Snapshot, aliases: Mapping[str, str]) -> HistoryCheck:
    """What a run changed in the past: `before` is taken from data/ before the analysis, `after` once it is done,
    and `aliases` (old slug -> current slug) from data/slugs.json after it."""
    return HistoryCheck(history_changes(before, after, aliases, changed_decisions(before, after)))


def changed_decisions(before: Snapshot, after: Snapshot) -> dict[str, str]:
    """Ref -> date of every decision the run added, changed or removed (new and re-extracted documents), or that
    the rule files did not reflect before it. The date is the earlier one if it changed; an undated decision may
    apply at every cutoff, so it gets "", earlier than all."""
    changed = {}
    for ref in before.decisions.keys() | after.decisions.keys():
        was, now = before.decisions.get(ref), after.decisions.get(ref)
        if was != now or ref in before.unreflected:
            changed[ref] = min(entry[0] or "" for entry in (was, now) if entry is not None)
    return changed


def history_changes(before: Snapshot, after: Snapshot, aliases: Mapping[str, str],
                    changed: Mapping[str, str]) -> list[HistoryChange]:
    """Every rule whose state at a cutoff before its earliest changed decision differs between the snapshots.

    Rules are matched by permanent slug, and a slug that became an alias (`aliases`) counts with the rule it leads
    to: rules merged into one are compared, per cutoff, by what they showed between them. A rule that is gone or
    no longer shown is compared with nothing, and a new rule with nothing before it. A rule's decisions are those
    in its versions before or after the run; a cutoff on or after the earliest date of one in `changed` may
    differ, any earlier one may not (none may when no decision of the rule changed). A bare year counts from the
    start of the year ("2015-12-31" >= "2015"). Only cutoffs both snapshots cover are compared: a year only one
    covers has only rules holding a decision the run added or removed, dated that year or undated.
    """
    merged: dict[str, list[str]] = {}
    gone: list[str] = []
    for slug in before.titles:
        new = slug if slug in after.titles else aliases.get(slug)
        if new is None:
            gone.append(slug)
        else:
            merged.setdefault(new, []).append(slug)
    groups = ([(olds, new) for new, olds in merged.items()] + [([old], None) for old in gone]
              + [([], new) for new in after.titles if new not in merged])
    cutoffs = sorted(before.in_force.keys() & after.in_force.keys())
    changes = []
    for olds, new in groups:
        refs = frozenset().union(*(before.refs[old] for old in olds), after.refs.get(new, frozenset()))
        since = min((changed[ref] for ref in refs if ref in changed), default=None)
        title = after.titles[new] if new in after.titles else before.titles[olds[0]]
        slugs = _slugs(olds, new)
        for cutoff in cutoffs:
            if since is not None and cutoff >= since:
                continue
            shown = before.in_force[cutoff]
            was = tuple(sorted((shown[old] for old in olds if old in shown), key=lambda f: f.ref))
            now = (after.in_force[cutoff][new],) if new in after.in_force[cutoff] else ()
            if set(was) != set(now):
                changes.append(HistoryChange(title, slugs, cutoff, since, was, now))
    return changes


def _slugs(olds: list[str], new: str | None) -> str:
    """How a compared rule is named: its slug, or "a, b → c" when slugs became aliases of another rule."""
    if new is None:
        return olds[0]
    return new if olds in ([], [new]) else f"{', '.join(olds)} → {new}"


def years(cutoffs: Iterable[str]) -> str:
    """The years of the cutoffs, runs of consecutive years as ranges: "2015–2017, 2019"."""
    found = sorted({int(cutoff[:4]) for cutoff in cutoffs})
    runs = [[year for _, year in run] for _, run in groupby(enumerate(found), key=lambda item: item[1] - item[0])]
    return ", ".join(str(run[0]) if len(run) == 1 else f"{run[0]}–{run[-1]}" for run in runs)


# --------------------------------------------------------------------------- command line

def counts(problems: Iterable[Problem]) -> str:
    """"date 20, effect 4": the number of problems per kind."""
    found = Counter(p.kind for p in problems)
    return ", ".join(f"{kind} {found[kind]}" for kind in SEVERITY if found[kind])


def describe(problems: list[Problem]) -> str:
    """Errors, then warnings, each with its count per kind, as plain text."""
    lines = []
    for severity, heading in (("error", "Errors"), ("warning", "Warnings")):
        found = [p for p in problems if p.severity == severity]
        lines.append(f"{heading}: {len(found)}" + (f" ({counts(found)})" if found else ""))
        lines += [f"  {p.kind}: {p.message}" for p in found]
    lines.append(f"Not checked here: {', '.join(HISTORY_KINDS)} (update.py compares the data before and after a run)")
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    problems = find_problems(Data.load(scrape.load_manifest()))
    print(describe(problems))
    if errors(problems):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
