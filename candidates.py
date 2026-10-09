"""Candidate rules for a decision: every live rule ranked by TF-IDF over Danish-stemmed words, without Claude.

Words are split, stemmed and compound-split the way the website's search does (website/search.js): lowercase runs of
letters and digits, Snowball's Danish stop words left out, the Danish Snowball stemmer (Python's snowballstemmer gives
the same stems as search.js), and a compound word also counts as its parts ("licensgebyr" -> "licens" + "gebyr").
A rule is described by its title, its latest text and kort_regel, and the emne of its decisions; a decision by its emne
and tekst. The decision's category boosts the rules of that category but filters nothing: extraction categories are
noisy, and a rule's decisions may come from more than one.

Pure functions only: incremental.py builds the profiles from the rule files, evaluate.py measures the recall.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache

import snowballstemmer

# How many ranked rules the assignment offers per decision: the smallest K whose recall reaches 98 % when each
# decision of today's rules is hidden from its rule (`evaluate.py candidate-recall`: 98.2 % at 15, 96.9 % at 10).
CANDIDATE_K = 15
# The score of a rule in the decision's own category is multiplied by 1 + CATEGORY_BOOST. Today's rules never mix
# categories (each was consolidated per category), so the recall gate rewards any boost; this one is kept moderate so
# a clearly closer rule of another category still ranks first.
CATEGORY_BOOST = 0.3
# A rule's title and a decision's emne name the rule; they count this many times their other words.
TITLE_WEIGHT = 2
EMNE_WEIGHT = 2

# Snowball's Danish stop word list minus "uden" (meaningful in rules: "uden krav"), as in website/search.js.
STOPWORDS = frozenset((
    "og i jeg det at en den til er som på de med han af for ikke der var mig sig men et har om vi min havde ham hun "
    "nu over da fra du ud sin dem os op man hans hvor eller hvad skal selv her alle vil blev kunne ind når være dog "
    "noget ville jo deres efter ned skulle denne end dette mit også under have dig anden hende mine alt meget sit "
    "sine vor mod disse hvis din nogle hos blive mange ad bliver hendes været thi jer sådan").split())

_STEMMER = snowballstemmer.stemmer("danish")
_WORD = re.compile(r"\w+")


@lru_cache(maxsize=None)
def stem(word: str) -> str:
    """The Danish Snowball stem of a lowercase word."""
    return _STEMMER.stemWord(word)


def words(text: str | None) -> list[str]:
    """The text's lowercase words of at least two letters that are not stop words (search.js's words())."""
    return [w for w in _WORD.findall((text or "").lower()) if len(w) >= 2 and w not in STOPWORDS]


def compound_parts(word: str, vocabulary: Collection[str]) -> list[str]:
    """The parts of a compound word, split as the website's search does (website/search.js): two words of the
    vocabulary, the second at least four letters, the first perhaps joined by -s- or -e- ("landsholdsdragt").
    Every split found counts, so a word may give more than two parts."""
    found = []
    for i in range(3, len(word) - 3):
        left, right = word[:i], word[i:]
        if right not in vocabulary:
            continue
        if left in vocabulary:
            found += [left, right]
        elif left[-1] in "se" and left[:-1] in vocabulary:
            found += [left[:-1], right]
    return found


def vocabulary(texts: Iterable[str | None]) -> frozenset[str]:
    """The words compound parts may be: those of at least three letters that occur on their own in the texts."""
    return frozenset(w for text in texts for w in words(text) if len(w) >= 3)


def terms(text: str | None, vocab: Collection[str]) -> list[str]:
    """The index terms of a text: the stem of each word, and the stems of its compound parts."""
    found = []
    for w in words(text):
        found.append(stem(w))
        found += [stem(part) for part in compound_parts(w, vocab)]
    return found


@dataclass(frozen=True)
class RuleProfile:
    """What the ranking reads of a rule."""
    slug: str
    category: str
    title: str
    text: str  # the latest text of the rule (or of its latest decision when it never applied)
    kort_regel: str | None
    emner: tuple[str, ...]  # the emne of each of its decisions that still exists

    def texts(self) -> list[str | None]:
        return [self.title, self.text, self.kort_regel, *self.emner]

    def terms(self, vocab: Collection[str]) -> Counter[str]:
        found = Counter(terms(self.title, vocab) * TITLE_WEIGHT)
        for text in (self.text, self.kort_regel, *self.emner):
            found.update(terms(text, vocab))
        return found


@dataclass(frozen=True)
class Query:
    """What the ranking reads of a decision."""
    emne: str
    tekst: str
    category: str

    def terms(self, vocab: Collection[str]) -> Counter[str]:
        return Counter(terms(self.emne, vocab) * EMNE_WEIGHT + terms(self.tekst, vocab))


class CandidateIndex:
    """TF-IDF vectors of the rules: sublinear term frequency (1 + log tf) times smoothed idf, normalised to length 1.

    Built from term counts, so evaluate.py can swap one rule's counts per hidden decision without re-reading the
    others; the vocabulary for compound parts is the profiles' own words."""

    def __init__(self, counts: Mapping[str, Counter[str]], categories: Mapping[str, str], vocab: Collection[str]):
        self.categories = dict(categories)
        self.vocab = vocab
        n = len(counts)
        df = Counter(term for found in counts.values() for term in found)
        self.idf = {term: math.log((n + 1) / (k + 1)) + 1 for term, k in df.items()}
        self.vectors = {slug: _unit({t: (1 + math.log(k)) * self.idf[t] for t, k in found.items()})
                        for slug, found in counts.items()}
        # Each slug's position in the order given: ties keep it, so the ranking is deterministic.
        self.order = {slug: i for i, slug in enumerate(counts)}

    @classmethod
    def of(cls, profiles: Sequence[RuleProfile]) -> CandidateIndex:
        vocab = vocabulary(text for p in profiles for text in p.texts())
        return cls({p.slug: p.terms(vocab) for p in profiles}, {p.slug: p.category for p in profiles}, vocab)

    def rank(self, query: Query) -> list[tuple[str, float]]:
        """Every rule with its score, best first: cosine similarity, boosted in the query's category."""
        found = query.terms(self.vocab)
        # Terms no rule has carry no idf and cannot match; leaving them out keeps the scores comparable.
        q = _unit({t: (1 + math.log(k)) * self.idf[t] for t, k in found.items() if t in self.idf})
        scored = []
        for slug, vector in self.vectors.items():
            score = sum(w * vector.get(t, 0.0) for t, w in q.items())
            if self.categories[slug] == query.category:
                score *= 1 + CATEGORY_BOOST
            scored.append((slug, score))
        return sorted(scored, key=lambda item: (-item[1], self.order[item[0]]))

    def top(self, query: Query, k: int = CANDIDATE_K) -> list[str]:
        """The slugs of the k best rules."""
        return [slug for slug, _ in self.rank(query)[:k]]

    def similar(self, slug: str) -> list[tuple[str, float]]:
        """Every other rule with the cosine similarity of its vector to this rule's, best first; no category boost
        (audit.py looks for one rule spread over several, in any category)."""
        own = self.vectors[slug]
        scored = [(other, sum(w * vector.get(t, 0.0) for t, w in own.items()))
                  for other, vector in self.vectors.items() if other != slug]
        return sorted(scored, key=lambda item: (-item[1], self.order[item[0]]))


def _unit(vector: dict[str, float]) -> dict[str, float]:
    norm = math.sqrt(sum(w * w for w in vector.values()))
    return {t: w / norm for t, w in vector.items()} if norm else {}
