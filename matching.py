"""Identity across Claude runs: which decision of a re-extraction is which earlier one, and which rule of a
re-consolidation keeps which earlier rule's slug.

Pure functions only: analyze.py reads the files and stores the result.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace

# A pair of decisions scoring at least this is the same decision; see _score().
#
# Measured on the two extractions of all 236 documents in the history (EXTRACT_VERSION 1 at commit 861adee and
# the current one; 959 and 1017 decisions). Pairs that share a quote score 1 or more. On text alone the
# best-scoring wrong pair scored 0.18 ("Headcoachens ansvar for adfærd og adgang" / "Akkreditering af
# assistentcoaches"), while reworded pairs from 0.25 up were the same decision (e.g. "Handlingsplan for akutte
# hændelser ved stævner", 0.29), so 0.25 leaves a margin.
MATCH_THRESHOLD = 0.25
# Text similarity a pair without a shared quote needs when both quotes were located, at different places. Claude
# does quote another sentence for the same decision, but two decisions worded alike at two places in a document
# are as often siblings: "Egenbetaling EM junior/subjunior" / "Egenbetaling EM Open" in elite10122017 score 0.46.
# On the two extractions this costs 23 of 900 matches (same decision, other sentence quoted) and removes all 6
# sibling pairs in one document that could otherwise swap ids.
LOCATED_APART_TEXT = 0.5

# A title of punctuation only would give an empty slug.
FALLBACK_SLUG = "regel"


# --------------------------------------------------------------------------- decisions

@dataclass(frozen=True)
class Candidate:
    """What the matcher compares of one extracted decision."""
    emne: str
    tekst: str
    udfald: str
    span: tuple[int, int] | None  # word offsets [start, end) of its quote in the document; None when not located


@dataclass(frozen=True)
class Match:
    old: int  # index into the old decisions
    new: int  # index into the new decisions
    score: float


def quote_span(pos: int | None, quote: str) -> tuple[int, int] | None:
    """The words a located quote covers: from its position, as many words as the quote has."""
    if pos is None:
        return None
    return pos, pos + max(len(_words(quote)), 1)


def quote_overlap(a: tuple[int, int] | None, b: tuple[int, int] | None) -> float:
    """Shared words over the shorter quote, so a quote cut a little shorter or longer, or one lying inside the
    other, still counts as the same passage; 0 when either quote was not located."""
    if a is None or b is None:
        return 0.0
    shared = min(a[1], b[1]) - max(a[0], b[0])
    return max(shared, 0) / min(a[1] - a[0], b[1] - b[0])


def match_decisions(old: Sequence[Candidate], new: Sequence[Candidate]) -> list[Match]:
    """Pair old and new decisions one to one, best score first (_score), leaving out pairs below MATCH_THRESHOLD.

    Greedy: the best-scoring pair is taken, its two decisions leave the pool, and so on. Equal scores go to the
    earlier old, then the earlier new decision, so the result is deterministic. Sorted by new decision.
    """
    old_grams = [_grams(c) for c in old]
    new_grams = [_grams(c) for c in new]
    pairs = []
    for i, o in enumerate(old):
        for j, n in enumerate(new):
            value = _score(o, n, _jaccard(old_grams[i], new_grams[j]))
            if value >= MATCH_THRESHOLD:
                pairs.append(Match(i, j, value))
    pairs.sort(key=lambda m: (-m.score, m.old, m.new))
    matches: list[Match] = []
    used_old: set[int] = set()
    used_new: set[int] = set()
    for m in pairs:
        if m.old not in used_old and m.new not in used_new:
            matches.append(m)
            used_old.add(m.old)
            used_new.add(m.new)
    return sorted(matches, key=lambda m: m.new)


def _score(old: Candidate, new: Candidate, text: float) -> float:
    """Quote overlap plus text similarity (`text`: the Jaccard similarity of the word trigrams of emne and tekst),
    each from 0 to 1.

    The quote says where in the document a decision stands and survives rewording; the text tells apart
    decisions that share one quote (a rule document listing four requirements under one heading). Without a
    shared quote (one was not located, or Claude quoted another sentence) the text alone must carry the match,
    and then the outcome must agree too: a rejected proposal and an adopted one can read alike. When both
    quotes were located, at different places, the text must reach LOCATED_APART_TEXT as well.
    """
    quote = quote_overlap(old.span, new.span)
    if quote > 0:
        return quote + text
    located_apart = old.span is not None and new.span is not None
    if old.udfald != new.udfald or (located_apart and text < LOCATED_APART_TEXT):
        return 0.0
    return text


def _grams(c: Candidate) -> frozenset[tuple[str, ...]]:
    words = _words(f"{c.emne} {c.tekst}")
    return frozenset(zip(words, words[1:], words[2:]))


def _jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


# --------------------------------------------------------------------------- rule slugs

def slugify(title: str) -> str:
    """A rule title as the website has always put it in URLs: lowercase, each run of characters other than
    letters, digits and _ one hyphen."""
    return re.sub(r"[^\w]+", "-", title.lower()).strip("-") or FALLBACK_SLUG


def unique_slug(title: str, taken: Collection[str]) -> str:
    """The title's slug, suffixed -2, -3 … until no live or former slug has it."""
    base = slugify(title)
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


@dataclass(frozen=True)
class RuleRefs:
    """A rule as slug carry-over sees it: its name (the slug of a previous rule, the title of a new one) and
    the ids of its decisions."""
    name: str
    refs: frozenset[str]


@dataclass(frozen=True)
class FormerSlug:
    """What a slug no rule holds any more last stood for, so its links can always be led to where that went."""
    category: str
    title: str
    refs: frozenset[str]
    to: str | None = None  # the live slug its links lead to; None while there is none (retired)


@dataclass(frozen=True)
class SlugPlan:
    slugs: tuple[str, ...]  # one per new rule, in order
    kept: int  # new rules that inherited a previous rule's slug
    revived: tuple[str, ...]  # former slugs a new rule took up again
    aliases: dict[str, str]  # previous slug -> slug of the new rule that holds most of its decisions (a merge)
    orphaned: tuple[str, ...]  # previous slugs none of whose decisions is in a new rule of the category


def carry_slugs(previous: Sequence[RuleRefs], new: Sequence[RuleRefs], taken: Collection[str],
                former: Mapping[str, FormerSlug] | None = None) -> SlugPlan:
    """Slugs for a category's new rules, inherited from its previous rules through shared decision ids.

    Each previous slug goes to the new rule holding most of its decisions (the first such rule on a tie), so
    in a split the largest part keeps it. When that rule already inherited a slug through a larger overlap
    (ties: the earlier previous rule), the slug becomes an alias of that one: a merge. A previous rule with
    none of its decisions left in the category is orphaned; its decisions may have moved to another category,
    which SlugRegistry.resolved finds once every category is consolidated.

    A new rule left without a slug first takes up a former slug (`former`, from data/slugs.json) when it holds
    more than half of that slug's decisions, so a rule that is merged away and split out again gets its own
    slug back instead of a new -2. Else it gets a new slug from its title, unique against `taken` (every live
    and former slug) and each other.
    """
    claims = []  # (overlap, previous index, new index)
    orphaned = []
    for i, p in enumerate(previous):
        overlaps = [len(p.refs & n.refs) for n in new]
        best = max(overlaps, default=0)
        if best == 0:
            orphaned.append(p.name)
        else:
            claims.append((best, i, overlaps.index(best)))
    inherited: dict[int, str] = {}  # new rule index -> previous slug
    aliases = {}
    for _, i, j in sorted(claims, key=lambda c: (-c[0], c[1])):
        if j in inherited:
            aliases[previous[i].name] = inherited[j]
        else:
            inherited[j] = previous[i].name
    in_use = set(taken) | set(inherited.values())
    revivable = dict(former or {})
    slugs, revived = [], []
    for j, n in enumerate(new):
        slug = inherited.get(j)
        if slug is None:
            slug = _revivable(n.refs, revivable)
            if slug is not None:
                del revivable[slug]
                revived.append(slug)
            else:
                slug = unique_slug(n.name, in_use)
            in_use.add(slug)
        slugs.append(slug)
    return SlugPlan(tuple(slugs), len(inherited), tuple(revived), aliases, tuple(orphaned))


def _revivable(refs: frozenset[str], former: Mapping[str, FormerSlug]) -> str | None:
    """The former slug most of whose decisions these are (more than half: no other rule can hold as many),
    the one sharing most of them on a tie, then the first by name."""
    candidates = [(len(refs & f.refs), slug) for slug, f in former.items() if 2 * len(refs & f.refs) > len(f.refs)]
    return min(candidates, key=lambda c: (-c[0], c[1]))[1] if candidates else None


@dataclass(frozen=True)
class LiveRule:
    """A rule in data/regler/ as the slug history sees it."""
    slug: str
    category: str
    title: str
    refs: frozenset[str]


@dataclass(frozen=True)
class SlugRegistry:
    """data/slugs.json: every slug no rule holds any more. None of them is handed out again to another rule."""
    aliases: dict[str, FormerSlug] = field(default_factory=dict)  # each with `to`: links lead there
    retired: dict[str, FormerSlug] = field(default_factory=dict)  # each without: links lead nowhere for now

    @classmethod
    def of(cls, former: Mapping[str, FormerSlug]) -> SlugRegistry:
        return cls({slug: f for slug, f in former.items() if f.to},
                   {slug: f for slug, f in former.items() if not f.to})

    def former(self) -> dict[str, FormerSlug]:
        """Every former slug; one listed both as alias and retired (only by editing the file) counts as alias."""
        return {**self.retired, **self.aliases}

    def taken(self) -> set[str]:
        return set(self.aliases) | set(self.retired)

    def targets(self) -> dict[str, str]:
        """Former slug -> the live slug its links lead to."""
        return {slug: f.to for slug, f in self.aliases.items() if f.to}

    def with_former(self, former: Mapping[str, FormerSlug]) -> SlugRegistry:
        """With these slugs added (or replaced): orphans without `to`, merged rules with it."""
        return SlugRegistry.of({**self.former(), **former})

    def without(self, slugs: Collection[str]) -> SlugRegistry:
        """Without these slugs, which are live again."""
        return SlugRegistry.of({slug: f for slug, f in self.former().items() if slug not in slugs})

    def resolved(self, live: Sequence[LiveRule], live_ids: Collection[str]) -> SlugRegistry:
        """Every former slug led to its successor (see successor), or retired without one; slugs that are live
        again (a rule file that failed to write after its history did) leave the history.

        Run once all categories are consolidated: a decision can leave one category and arrive in another in the
        same run. It also moves an alias whose target was split later to where most of its decisions are now.
        """
        live_slugs = {rule.slug for rule in live}
        former = {slug: f for slug, f in self.former().items() if slug not in live_slugs}
        resolved: dict[str, str | None] = {}

        def lead(slug: str, seen: frozenset[str]) -> str | None:
            """Where `slug` leads now. A target that is itself a former slug now (merged away since) is replaced
            by where that one leads, so a kept link follows it and no alias points at another alias."""
            if slug not in resolved:
                f = former[slug]
                to = lead(f.to, seen | {slug}) if f.to in former and f.to not in seen else f.to
                resolved[slug] = successor(replace(f, to=to), live, live_ids)
            return resolved[slug]

        return SlugRegistry.of({slug: replace(f, to=lead(slug, frozenset())) for slug, f in former.items()})


def successor(former: FormerSlug, live: Sequence[LiveRule], live_ids: Collection[str]) -> str | None:
    """Where a former slug's links lead now, the first that applies:

    1. the live rule holding most of its decisions, in any category (they may have moved);
    2. its current target, while that is live and none of its decisions exists any more (extracted again under
       new ids, or their document is gone): nothing better is known, and the link keeps working;
    3. the one live rule of its own category with the same title as a slug, when there is exactly one; another
       category's rule of that name is another rule (two "Rapportering fra internationale mesterskaber" exist);
    4. none: retired, and tried again after every consolidation.

    Step 2 does not keep a target that holds none of the decisions while some of them still exist: they went to
    no rule (one-offs now), so nothing supports the pointer, and checks.py would report it.
    """
    rule = holder(former.refs, live)
    if rule is not None:
        return rule.slug
    if former.to in {r.slug for r in live} and not former.refs & set(live_ids):
        return former.to
    namesakes = same_title(former, live)
    return namesakes[0].slug if len(namesakes) == 1 else None


def same_title(former: FormerSlug, live: Sequence[LiveRule]) -> list[LiveRule]:
    """The live rules of the former slug's category whose title is the same as a slug."""
    return [r for r in live if r.category == former.category and slugify(r.title) == slugify(former.title)]


def holder(refs: frozenset[str], live: Sequence[LiveRule]) -> LiveRule | None:
    """The live rule holding most of these decisions, None when none holds any. On a tie the first in `live`,
    which is in category order and then Claude's order (analyze.live_rules)."""
    best, most = None, 0
    for rule in live:
        shared = len(refs & rule.refs)
        if shared > most:
            best, most = rule, shared
    return best
