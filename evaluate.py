# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "pymupdf>=1.24",
#   "snowballstemmer>=2.2",
# ]
# ///
"""An answer key for the extraction and the rules, judged by Opus, and a scorer against it.

    uv run evaluate.py select                       # choose the rules and documents (no Claude)
    uv run evaluate.py extract --name stored        # today's extractions from data/ as a run (no Claude)
    uv run evaluate.py extract --name sonnet-1 --model claude-sonnet-5-5 --max-cost 5
    uv run evaluate.py key-decisions --max-cost 40  # Part B key: Opus judges the candidates per document
    uv run evaluate.py key-rules --max-cost 30      # Part A key: Opus writes each rule's timeline
    uv run evaluate.py score --run stored --run sonnet-1    # Part B scores (no Claude)
    uv run evaluate.py score-rules                  # Part A scores of data/regler (no Claude)
    uv run evaluate.py candidate-recall             # incremental consolidation: candidate ranking recall (no Claude)
    uv run evaluate.py replay --holdout newest:20,random:10 --seed 1 --name inc-1 --max-cost 20
    uv run evaluate.py compare-rules eval/replays/inc-1/data/regler data/regler    # (no Claude)

Two Sonnet runs agree on only ~90% of decisions, so comparing runs cannot tell better from different. The key is
built once and kept in the repo; every later change (models, prompts, consolidation) is scored against it.

Part B: per selected document, the decisions of several extraction runs are clustered into candidates, and two
Opus judges independently keep, reject or merge each one and add what all runs missed. Part A: per selected
recurring rule, passages from all documents go to two Opus judges, who write the rule's true timeline and what was
in force each year. Where the two disagree, a third judge answers blind, and two of three decide.

Everything is written under eval/: selection.json, runs/<run>/<doc>.json, key/decisions/<doc>.json,
key/rules/<slug>.json, key/judges/ (every judge answer: a cut-off or piloted build resumes without paying twice),
reports/<name>.md, replays/<name>/ and runs.jsonl. Nothing is written to data/ or regelsaet/: a replay
withholds documents from a copy of data/ in eval/replays/<name>/data and consolidates them again there. Every
command that calls Claude takes --max-cost and --pilot N or --docs/--rules (a replay: --holdout), prints how many
calls it plans, logs each call's usage and adds a line to eval/runs.jsonl; it exits 1 when a call failed or was
skipped. It never asks again silently: an answer stored for other input is an error until --rejudge (or --force for
extractions) says to pay for a new one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import random
import re
import shutil
import statistics
from bisect import bisect_left
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, timezone
from itertools import combinations
from pathlib import Path

import analyze
import candidates
import incremental
import matching
import render
import scrape
import update
from analyze import EFFEKTER, Decision, DocWords, RunBudget, StepSummary, Usage, word_spans
from candidates import compound_parts
from matching import Candidate
from scrape import Doc

EVAL_DIR = scrape.ROOT / "eval"
SYNONYMS = scrape.ROOT / "website" / "synonyms.json"

# Selection. Fixed, so `select` gives the same selection from the same data.
SEED = 2026
TARGET_RULES = 20
MAX_RULES_PER_CATEGORY = 3
MIN_VERSIONS = 4
MIN_YEARS = 3
REQUIRED_RULES = ("licensgebyr",)
# Selected rules whose scope is loose, so which decisions are "its" events is arbitrary: scored and reported, but left
# out of the overall rules metrics (selection.json marks them "soft").
SOFT_RULES = {"lån-af-dsf-s-stævneudstyr": "loosely scoped, mostly board action items, so its missing events are "
                                           "arbitrary"}
TARGET_DOCUMENTS = 25
# rep2013 sets the yearly fees in a budget ("Licens: kr. 200,- (Uændret)"), which the extraction must split.
REQUIRED_DOCUMENTS = ("490", "1071kongres", "rep2013")
ERAS = (("2008–12", 2012), ("2013–17", 2017), ("2018–22", 2022), ("2023–26", 2026))  # (label, last year)
SIZES = ("small", "medium", "large")  # word-count terciles
# The effects that are versions of a rule; proposals (foreslaaet, forkastet, trukket) change nothing.
RULE_EFFECTS = ("indfoert", "aendret", "bekraeftet", "ophaevet")

# The extraction runs whose decisions are the candidates of the decisions key; "stored" is data/beslutninger.
STORED_RUN = "stored"
CANDIDATE_RUNS = (STORED_RUN, "sonnet-1", "sonnet-2", "haiku-1", "opus-1")
JUDGE_MODEL = "claude-opus-5-5"
JUDGE_EFFORT = "max"
# Seconds one judge attempt may take, and attempts per call. A timed-out attempt reports no cost, so the budget
# does not see it: a call that keeps timing out is given up early rather than retried at length.
JUDGE_TIMEOUT = 1200
JUDGE_ATTEMPTS = 2
# Calls already running finish after the cost limit is reached, so a run can overshoot it by one call per worker;
# below this limit the default is one worker.
SMALL_BUDGET_USD = 10.0

# Passages for the rules key: words around a quote or keyword hit, up to PASSAGE_BUDGET words per rule, each
# keyword passage at most MAX_PASSAGE_WORDS long; keywords found in more than MAX_KEYWORD_SHARE of the documents say
# little.
PASSAGE_CONTEXT = 80
PASSAGE_BUDGET = 25_000
MAX_PASSAGE_WORDS = 400
MIN_PASSAGE_WORDS = 15  # a piece of a keyword window, trimmed around decision passages, shorter than this is left out
MERGE_GAP = PASSAGE_CONTEXT // 2  # decision windows closer than this join into one passage
# A passage with a proposal but no outcome after it is extended forward to the outcome within this many words, and
# one with a proposal or an outcome back to the heading of its agenda item within this many.
EXTEND_FORWARD = 150
EXTEND_BACK = 100
OUTCOME_WORDS = frozenset({"vedtaget", "vedtages", "vedtoges", "godkendt", "godkendes", "forkastet", "forkastes",
                           "nedstemt", "trukket", "enstemmigt", "enstemmig", "afvist", "afvises"})
# The first words of a line that starts an agenda item or a proposal: "Forslag 5 fra bestyrelsen", "6. Indkomne
# forslag", "c) …", "Ad 3:".
HEADING = re.compile(r"(?:(?:Ændrings)?[Ff]orslag|FORSLAG|Punkt|Pkt|Ad|Indkomne)\b|\d{1,2}[.):]\s|[a-zA-Z][.)]\s")
MAX_KEYWORD_SHARE = 0.25
# A synonym's hits count this much of the rule's own words': "kontingent" is weaker evidence for licensgebyr.
SYNONYM_WEIGHT = 0.5
# A token of at least this length also matches longer words that start with it (licensgebyr -> licensgebyret).
PREFIX_LENGTH = 4
# A keyword hit with a number this close (words after, words before) may state an amount, which the judges need.
AMOUNT_WINDOW = (12, 4)

BOOTSTRAP_SAMPLES = 2000
# The candidate-recall gate: recall@K for these K, and the recall the chosen K must reach.
RECALL_KS = (3, 5, 8, 10, 15)
RECALL_TARGET = 0.98
# For the call estimate printed before a command starts: Danish text runs about this many characters per token.
CHARS_PER_TOKEN = 3.2

# The fields a key decision has, and those whose agreement is judged and scored.
JUDGED_FIELDS = ("emne", "kategori", "udfald", "handling", "niveau", "tekst", "citat", "side", "gaelder_fra",
                 "gaelder_til")
CODED_FIELDS = ("kategori", "udfald", "handling", "niveau")
# The coded fields one document decides: handling (new, change, confirmation) often needs the rule's history, so
# field accuracy is also given without it.
FIRM_FIELDS = ("kategori", "udfald", "niveau")
# The outcome a decision with this effect has, so a key event can be compared with extracted decisions.
EFFECT_OUTCOME = {"foreslaaet": "ikke_afgjort", "forkastet": "forkastet", "trukket": "trukket"}
JUDGES = (1, 2, 3)

log = logging.getLogger("evaluate")


# --------------------------------------------------------------------------- files

def write_text(path: Path, text: str) -> None:
    """Write under eval/ only: the evaluation must never touch data/ or regelsaet/."""
    if not path.resolve().is_relative_to(EVAL_DIR.resolve()):
        raise ValueError(f"{path} is outside {EVAL_DIR}; evaluate.py writes nowhere else")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_json(path: Path, value: object) -> None:
    write_text(path, json.dumps(value, ensure_ascii=False, indent=1) + "\n")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def selection_path() -> Path:
    return EVAL_DIR / "selection.json"


def run_dir(run: str) -> Path:
    return EVAL_DIR / "runs" / run


def run_path(run: str, doc_id: str) -> Path:
    return run_dir(run) / f"{doc_id}.json"


def key_dir(kind: str) -> Path:
    """eval/key/decisions or eval/key/rules."""
    return EVAL_DIR / "key" / kind


def judge_path(kind: str, item: str, number: int) -> Path:
    return EVAL_DIR / "key" / "judges" / kind / item / f"{number}.json"


def report_path(name: str) -> Path:
    return EVAL_DIR / "reports" / f"{name}.md"


def runs_log_path() -> Path:
    return EVAL_DIR / "runs.jsonl"


def today() -> date:
    """The date a new selection is made; tests replace it."""
    return date.today()


class Texts:
    """Each document's text, words and word positions, read once per command (reading PDFs is the slow part)."""

    def __init__(self) -> None:
        self._text: dict[str, str] = {}
        self._words: dict[str, DocWords] = {}
        self._spans: dict[str, list[tuple[int, int]]] = {}

    def text(self, doc: Doc) -> str:
        if doc.id not in self._text:
            self._text[doc.id] = analyze.document_text(doc)
        return self._text[doc.id]

    def words(self, doc: Doc) -> DocWords:
        if doc.id not in self._words:
            self._words[doc.id] = DocWords.of(self.text(doc))
        return self._words[doc.id]

    def spans(self, doc: Doc) -> list[tuple[int, int]]:
        """Character span of each word of `words(doc)`, so a word offset leads back to the text."""
        if doc.id not in self._spans:
            spans = word_spans(self.text(doc))
            if len(spans) != len(self.words(doc).words):
                raise ValueError(f"{doc.id}: {len(spans)} word positions for {len(self.words(doc).words)} words")
            self._spans[doc.id] = spans
        return self._spans[doc.id]

    def excerpt(self, doc: Doc, start: int, end: int) -> str:
        """The text of words [start, end) as it stands in the document."""
        spans = self.spans(doc)
        return self.text(doc)[spans[start][0]:spans[end - 1][1]]


_WORD = re.compile(r"\w+")


def tokens(text: str) -> list[str]:
    """Words as DocWords has them: lowercase runs of letters and digits."""
    return _WORD.findall(text.lower())


_MONTHS = "januar|februar|marts|april|maj|juni|juli|august|september|oktober|november|december"
# Dates and years, however written: "1.1.2015", "01-01-2015", "2015-01-01", "1. januar 2015", "2015"; a year-like
# number next to "kr" or ",-" is an amount.
_DATE = re.compile(rf"\b\d{{1,2}}\.\s*(?:{_MONTHS})\b(?:\s+\d{{4}})?"
                   rf"|\b\d{{4}}-\d{{1,2}}-\d{{1,2}}\b"
                   rf"|\b\d{{1,2}}[./-]\d{{1,2}}[./-]\d{{2,4}}\b"
                   rf"|(?<!kr\. )(?<!kr )\b(?:19|20)\d{{2}}\b(?!\s*(?:kr|,-))", re.IGNORECASE)


def numbers(text: str | None) -> tuple[str, ...]:
    """The amounts and other numbers of a text, sorted, as judges must agree on them: dates and years are left out
    (they are written in many ways and belong to the years), "1.000" is 1000 and "250,00" is 250."""
    plain = re.sub(r"(?<=\d)\.(?=\d{3}\b)", "", _DATE.sub(" ", text or ""))
    return tuple(sorted(re.sub(r",0+$", "", n) for n in re.findall(r"\d+(?:,\d+)?", plain)))


# --------------------------------------------------------------------------- selection

@dataclass(frozen=True)
class RuleChoice:
    slug: str
    kategori: str
    titel: str
    versions: int  # versions that introduce, change, confirm or abolish it; proposals are not versions
    years: int  # distinct years of those versions
    vigtig: bool  # a rule clubs, athletes, coaches or referees need to know, as the consolidation judged it
    parallel: bool  # changed more than once in one document: parallel sub-rules (one per championship, say)
    docs: frozenset[str]  # documents holding its decisions

    @property
    def rank(self) -> tuple:
        """Most distinct years first, then most versions; the slug settles ties."""
        return -self.years, -self.versions, self.slug

    @property
    def recurring(self) -> bool:
        """Selectable for Part A: it recurs, matters, and has one version in force at a time, which the key's one
        event per year can describe."""
        return self.versions >= MIN_VERSIONS and self.years >= MIN_YEARS and self.vigtig and not self.parallel


def rule_choices(raw_rules: list[dict], by_ref: Mapping[str, Decision]) -> list[RuleChoice]:
    """Every rule with its real versions, distinct years and documents, ranked."""
    choices = []
    for raw in raw_rules:
        real = [(v, by_ref[v["ref"]]) for v in raw["versioner"] if v["ref"] in by_ref and v["effekt"] in RULE_EFFECTS]
        changes = Counter(d.doc_id for v, d in real if v["effekt"] in ("indfoert", "aendret"))
        choices.append(RuleChoice(
            raw["slug"], raw["kategori"], raw["titel"], len(real), len({d.dato[:4] for _, d in real if d.dato}),
            bool(raw.get("vigtig", True)), max(changes.values(), default=0) > 1,
            frozenset(by_ref[v["ref"]].doc_id for v in raw["versioner"] if v["ref"] in by_ref)))
    return sorted(choices, key=lambda r: r.rank)


def select_rules(choices: list[RuleChoice]) -> list[tuple[RuleChoice, str]]:
    """About TARGET_RULES recurring rules, with why each was chosen.

    Recurring (RuleChoice.recurring): at least MIN_VERSIONS real versions over at least MIN_YEARS distinct years,
    important, and one version in force at a time. Each category offers its best-ranked MAX_RULES_PER_CATEGORY, and
    the rounds go over the categories in turn (the first round takes each category's best, ordered by rank, and so
    on), so no category dominates. REQUIRED_RULES come first, recurring or not: the owner checks them by hand.
    """
    missing = sorted(set(REQUIRED_RULES) - {c.slug for c in choices})
    if missing:
        raise SystemExit(f"Required rules not in data/regler: {', '.join(missing)}")

    def order(rule: RuleChoice) -> tuple:
        return rule.slug not in REQUIRED_RULES, rule.rank

    offered: dict[str, list[RuleChoice]] = defaultdict(list)
    qualifying = Counter(c.kategori for c in choices if c.recurring)
    for rule in sorted((c for c in choices if c.recurring or c.slug in REQUIRED_RULES), key=order):
        if len(offered[rule.kategori]) < MAX_RULES_PER_CATEGORY:
            offered[rule.kategori].append(rule)
    chosen: list[tuple[RuleChoice, str]] = []
    for round_ in range(MAX_RULES_PER_CATEGORY):
        for rule in sorted((rules[round_] for rules in offered.values() if len(rules) > round_), key=order):
            if len(chosen) >= TARGET_RULES and rule.slug not in REQUIRED_RULES:
                continue
            reason = (f"{rule.versions} versions over {rule.years} years; no. {round_ + 1} of "
                      f"{qualifying[rule.kategori]} recurring rules in {rule.kategori}")
            chosen.append((rule, f"required; {reason}" if rule.slug in REQUIRED_RULES else reason))
    return chosen


@dataclass(frozen=True)
class DocChoice:
    id: str
    organ: str
    date: str | None
    era: str
    words: int
    size: str
    rules: tuple[str, ...]  # selected rules with a decision in the document
    decisions: int = 0  # decisions extracted from it today


def era_of(day: str | None) -> str:
    """The era of a date; years before 2008 count to the first, after 2026 (next year's deadlines) to the last."""
    if not day:
        return "unknown"
    year = int(day[:4])
    return next((label for label, last in ERAS if year <= last), ERAS[-1][0])


def size_cuts(word_counts: Sequence[int]) -> tuple[int, int]:
    """The word counts where the middle and the top tercile start."""
    ordered = sorted(word_counts)
    return ordered[len(ordered) // 3], ordered[2 * len(ordered) // 3]


def size_of(words: int, cuts: tuple[int, int]) -> str:
    return SIZES[0] if words < cuts[0] else SIZES[1] if words < cuts[1] else SIZES[2]


def organ_quotas(counts: Mapping[str, int], total: int) -> dict[str, int]:
    """Documents per organ: its share of `total` by its share of the corpus (largest remainder), at least one.

    Organs whose share is below one get one each, and the rest is shared out again among the others."""
    quotas: dict[str, int] = {}
    rest, seats = dict(counts), total
    while rest:
        small = sorted(o for o, n in rest.items() if seats * n / sum(rest.values()) < 1)
        if not small:
            break
        for organ in small:
            quotas[organ] = 1
            del rest[organ]
        seats -= len(small)
    if rest:
        shares = {o: max(seats, 0) * n / sum(rest.values()) for o, n in rest.items()}
        floors = {o: math.floor(s) for o, s in shares.items()}
        left = max(seats, 0) - sum(floors.values())
        for organ in sorted(rest, key=lambda o: (floors[o] - shares[o], o))[:left]:
            floors[organ] += 1
        quotas |= floors
    return quotas


def _draw(seed: int, item: str) -> str:
    """A seeded, stable random order: the same on every machine and Python version."""
    return hashlib.sha256(f"{seed}:{item}".encode()).hexdigest()


def select_documents(infos: list[DocChoice], rule_order: Sequence[str] = (),
                     seed: int = SEED) -> list[tuple[DocChoice, str]]:
    """About TARGET_DOCUMENTS documents, stratified by organ, era and size, with why each was chosen.

    REQUIRED_DOCUMENTS first. Each organ gets organ_quotas() documents (at least one). The organs then pick in turn,
    one document a round: the stratum (era, size) furthest below its share of the corpus so far, counting the eras
    overall and within the organ, and the sizes overall. Within the stratum, a document holding decisions of
    selected rules no chosen document holds yet comes first, then any document behind a selected rule, then a
    seeded draw. Last, every selected rule (`rule_order`) still without a document behind it gets the one that
    covers most such rules, the shortest on a tie: the scorer can then compare the decisions behind every rule.
    """
    by_id = {d.id: d for d in infos}
    missing = [doc_id for doc_id in REQUIRED_DOCUMENTS if doc_id not in by_id]
    if missing:
        raise SystemExit(f"Required documents not in the manifest: {', '.join(missing)}")
    quotas = organ_quotas(Counter(d.organ for d in infos), TARGET_DOCUMENTS)
    for organ, n in Counter(by_id[doc_id].organ for doc_id in REQUIRED_DOCUMENTS).items():
        quotas[organ] = max(quotas[organ], n)

    def shares(key: Callable[[DocChoice], str], docs: Iterable[DocChoice]) -> dict[str, float]:
        found = Counter(key(d) for d in docs)
        return {value: n / sum(found.values()) for value, n in found.items()}

    era_share, size_share = shares(lambda d: d.era, infos), shares(lambda d: d.size, infos)
    organ_era_share = {o: shares(lambda d: d.era, [d for d in infos if d.organ == o]) for o in quotas}
    chosen: list[tuple[DocChoice, str]] = []
    eras, sizes, organ_eras, organs = Counter(), Counter(), Counter(), Counter()

    def uncovered() -> set[str]:
        return set(rule_order) - {slug for d, _ in chosen for slug in d.rules}

    def add(doc: DocChoice, reason: str) -> None:
        if doc.rules:
            reason += f"; holds decisions of {', '.join(doc.rules)}"
        chosen.append((doc, reason))
        eras[doc.era] += 1
        sizes[doc.size] += 1
        organ_eras[doc.organ, doc.era] += 1
        organs[doc.organ] += 1

    def deficit(doc: DocChoice) -> float:
        n, k = len(chosen) + 1, organs[doc.organ] + 1
        return (era_share[doc.era] * n - eras[doc.era] + size_share[doc.size] * n - sizes[doc.size]
                + organ_era_share[doc.organ][doc.era] * k - organ_eras[doc.organ, doc.era])

    for doc_id in REQUIRED_DOCUMENTS:
        add(by_id[doc_id], f"required; {by_id[doc_id].organ}, {by_id[doc_id].era}, {by_id[doc_id].size}")
    while open_organs := [o for o in sorted(quotas) if organs[o] < quotas[o]]:
        for organ in open_organs:
            taken = {d.id for d, _ in chosen}
            pool = [d for d in infos if d.organ == organ and d.id not in taken]
            if not pool:
                quotas[organ] = organs[organ]
                continue
            new = uncovered()
            best = min(pool, key=lambda d: (-round(deficit(d), 9), -len(new & set(d.rules)), not d.rules,
                                            _draw(seed, d.id)))
            add(best, f"{organ} {organs[organ] + 1} of {quotas[organ]}; stratum {best.era}, {best.size} was "
                      f"furthest below its share")
    while new := uncovered():
        taken = {d.id for d, _ in chosen}
        pool = [d for d in infos if new & set(d.rules) and d.id not in taken]
        if not pool:
            break
        best = min(pool, key=lambda d: (-len(new & set(d.rules)), d.words, _draw(seed, d.id)))
        add(best, f"covers {', '.join(r for r in rule_order if r in new & set(best.rules))}, which no other "
                  f"selected document held; {best.organ}, {best.era}, {best.size}")
    return chosen


def build_selection(docs: list[Doc], decisions: list[Decision], raw_rules: list[dict], word_counts: Mapping[str, int],
                    seed: int = SEED) -> dict:
    """The selection as eval/selection.json stores it, without its date; deterministic for the same data."""
    by_ref = {d.ref: d for d in decisions}
    rules = select_rules(rule_choices(raw_rules, by_ref))
    cuts = size_cuts(list(word_counts.values()))
    rules_in: dict[str, list[str]] = defaultdict(list)
    for rule, _ in rules:
        for doc_id in sorted(rule.docs):
            rules_in[doc_id].append(rule.slug)
    extracted = Counter(d.doc_id for d in decisions)
    infos = [DocChoice(doc.id, doc.organ, doc.date, era_of(doc.date), word_counts[doc.id],
                       size_of(word_counts[doc.id], cuts), tuple(rules_in[doc.id]), extracted[doc.id]) for doc in docs]
    documents = select_documents(infos, [r.slug for r, _ in rules], seed)
    biggest = sorted((d for d, _ in documents), key=lambda d: -d.decisions)[:2]
    total = sum(d.decisions for d, _ in documents)
    return {
        "seed": seed,
        "size_cuts": list(cuts),
        "rules": [{"slug": r.slug, "kategori": r.kategori, "titel": r.titel, "versions": r.versions,
                   "years": r.years, "reason": reason,
                   **({"soft": True, "soft_reason": SOFT_RULES[r.slug]} if r.slug in SOFT_RULES else {})}
                  for r, reason in rules],
        "documents": [{"id": d.id, "organ": d.organ, "date": d.date, "era": d.era, "words": d.words, "size": d.size,
                       "decisions": d.decisions, "rules": list(d.rules), "reason": reason} for d, reason in documents],
        # Pooled scores lean on the documents with most decisions; the report gives per-document averages too.
        "summary": {"decisions": total, "two_largest": [d.id for d in biggest],
                    "two_largest_share": round(sum(d.decisions for d in biggest) / total, 3) if total else None,
                    "without_decisions": [d.id for d, _ in documents if not d.decisions]},
    }


def load_selection() -> dict:
    path = selection_path()
    if not path.exists():
        raise SystemExit(f"{path} is missing; run `uv run evaluate.py select` first.")
    return read_json(path)


def chosen_items(order: Sequence[str], pilot: int | None, named: Sequence[str] | None, kind: str) -> list[str]:
    """The items a command works on, in selection order: all, the first `pilot`, or those `named`, which must be
    selected. Answers are kept per item, so a pilot's are reused by the full build."""
    if named:
        unknown = [item for item in named if item not in order]
        if unknown:
            raise SystemExit(f"Not in eval/selection.json ({kind}): {', '.join(unknown)}")
        return [item for item in order if item in named]
    return list(order)[:pilot]


def selected_docs(selection: dict, pilot: int | None = None, named: Sequence[str] | None = None) -> list[Doc]:
    docs = {doc.id: doc for doc in scrape.load_manifest()}
    chosen = chosen_items([entry["id"] for entry in selection["documents"]], pilot, named, "documents")
    missing = [doc_id for doc_id in chosen if doc_id not in docs]
    if missing:
        raise SystemExit(f"Selected documents not in the manifest: {', '.join(missing)}")
    return [docs[doc_id] for doc_id in chosen]


def selection_date(selection: dict) -> date:
    """The date the selection was made: "today" for the rules judges, so a build resumed later asks the same."""
    if "as_of" not in selection:
        raise SystemExit("eval/selection.json has no as_of date; run `uv run evaluate.py select --force`")
    return date.fromisoformat(selection["as_of"])


# --------------------------------------------------------------------------- usage and the run log

def usage_json(usage: Usage) -> dict:
    return {"input": usage.input_tokens, "cache_read": usage.cache_read_tokens,
            "cache_write": usage.cache_write_tokens, "output": usage.output_tokens,
            "cost_usd": round(usage.cost_usd, 4), "seconds": round(usage.duration_s), "attempts": usage.attempts,
            "models": list(usage.models)}


def describe_usage(usage: Usage) -> str:
    return (f"{usage.all_input_tokens:,} tokens in, {usage.output_tokens:,} out, {usage.cost_usd:.2f} USD, "
            f"{usage.duration_s:.0f} s")


def estimate_tokens(system: str, prompt: str, schema: dict) -> int:
    return round((len(system) + len(prompt) + len(json.dumps(schema))) / CHARS_PER_TOKEN)


def print_plan(label: str, calls: int, input_tokens: int, model: str, effort: str | None, note: str = "") -> None:
    print(f"{label}: {calls} Claude calls to {model} (effort {effort or 'default'}), about "
          f"{input_tokens / 1000:.0f}K input tokens{note}", flush=True)


def log_run(command: str, details: dict, steps: Mapping[str, StepSummary] | None = None) -> None:
    """Append one line to eval/runs.jsonl: the command, its settings and, per step, what its Claude calls used
    (as data/runs.jsonl has it) or the scores it computed."""
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "command": command, **details}
    if steps:
        line["cli"] = analyze.cli_version()
        line["steps"] = {name: update.step_json(step) for name, step in steps.items()}
    path = runs_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:  # under eval/, like write_text
        out.write(json.dumps(line, ensure_ascii=False) + "\n")


def finish(steps: Mapping[str, StepSummary], unfinished: str = "") -> None:
    """Fail the command when a call failed or was skipped, or work is left: run it again to resume."""
    failed = sum(step.failed + step.skipped for step in steps.values())
    spent = sum(step.usage.cost_usd for step in steps.values())
    print(f"Claude calls used {spent:.2f} USD at list price", flush=True)
    problems = [f"{failed} Claude calls failed or were skipped"] if failed else []
    problems += [unfinished] if unfinished else []
    if problems:
        raise SystemExit(f"{'; '.join(problems)}; run the command again to continue (answers so far are kept).")


def workers_for(args: argparse.Namespace) -> int:
    """--workers, else 1 under SMALL_BUDGET_USD (each worker's running call may overshoot the limit), else 4."""
    if args.workers is not None:
        return args.workers
    return 1 if args.max_cost is not None and args.max_cost < SMALL_BUDGET_USD else 4


# --------------------------------------------------------------------------- extraction runs

def check_run_name(name: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
        raise SystemExit(f"Run name {name!r}: use lowercase letters, digits, '.', '_' and '-'")
    return name


def stored_record(doc: Doc) -> dict:
    """The pseudo-run "stored" for a document: today's extraction in data/beslutninger, as it is."""
    source = analyze.DECISIONS_DIR / f"{doc.id}.json"
    if not source.exists():
        raise SystemExit(f"{doc.id} has no extraction in {analyze.DECISIONS_DIR}; run update.py first")
    cached = read_json(source)
    if cached["sha256"] != doc.sha256:
        raise SystemExit(f"{doc.id}: the extraction in {analyze.DECISIONS_DIR} is of another version of the file")
    return {"doc_id": doc.id, "sha256": doc.sha256, "run": STORED_RUN, "model": cached["model"],
            "provenance": cached.get("provenance"), "usage": None, "moededato": cached["moededato"],
            "beslutninger": cached["beslutninger"]}


def copy_stored(docs: list[Doc], force: bool) -> list[str]:
    """Copy the stored extractions; returns the documents copied. Replacing a different copy changes the decisions
    judges' input, and every judge of that document would be asked again, so it needs `force`."""
    records = {doc.id: stored_record(doc) for doc in docs}
    differ = [doc_id for doc_id, record in records.items()
              if run_path(STORED_RUN, doc_id).exists() and read_json(run_path(STORED_RUN, doc_id)) != record]
    if differ and not force:
        raise SystemExit(f"The stored run already has other copies of {', '.join(differ)} (data/ changed since); "
                         f"pass --force to replace them, after which their decisions judges must answer again")
    copied = [doc_id for doc_id, record in records.items()
              if not run_path(STORED_RUN, doc_id).exists() or doc_id in differ]
    for doc_id in copied:
        write_json(run_path(STORED_RUN, doc_id), records[doc_id])
    return copied


def run_state(run: str, doc: Doc, model: str, effort: str | None) -> str:
    """"missing", "current" (this file version, today's prompt and this effort) or "stale". One made with another
    model would mix two configurations under one name, so it stops the command."""
    path = run_path(run, doc.id)
    if not path.exists():
        return "missing"
    stored = read_json(path)
    if stored["model"] != model:
        raise SystemExit(f"Run {run} was extracted with {stored['model']}, not {model}; use another --name")
    provenance = stored.get("provenance") or {}
    current = (stored["sha256"] == doc.sha256
               and provenance.get("prompt") == analyze.prompt_hash(analyze.EXTRACT_SYSTEM, analyze.EXTRACT_SCHEMA)
               and provenance.get("effort") == (effort or "default"))
    return "current" if current else "stale"


def extract_into_run(doc: Doc, run: str, *, model: str, effort: str | None, cli: str,
                     budget: RunBudget) -> tuple[str, Usage]:
    """One extraction with the pipeline's prompt (analyze.run_extraction), written to eval/runs/<run>/."""
    extraction, usage = analyze.run_extraction(doc, model=model, effort=effort, budget=budget)
    with analyze.usage_kept(usage):
        write_json(run_path(run, doc.id), {
            "doc_id": doc.id, "sha256": doc.sha256, "run": run, "model": model,
            "provenance": analyze.provenance(usage, cli, analyze.EXTRACT_SYSTEM, analyze.EXTRACT_SCHEMA, effort),
            "usage": usage_json(usage), "moededato": extraction.moededato, "beslutninger": extraction.decisions,
        })
    missing = sum(not d["citat_fundet"] for d in extraction.decisions)
    message = f"{doc.id}: {len(extraction.decisions)} decisions, {missing} quotes not found; {describe_usage(usage)}"
    return message, usage


def load_run(run: str, doc: Doc) -> list[dict]:
    """A run's decisions for a document, checked to be of the current file."""
    path = run_path(run, doc.id)
    if not path.exists():
        raise SystemExit(f"Run {run} has no extraction of {doc.id}; run `evaluate.py extract --name {run}` first")
    stored = read_json(path)
    if stored["sha256"] != doc.sha256:
        raise SystemExit(f"Run {run} extracted another version of {doc.id}; extract it again")
    return stored["beslutninger"]


# --------------------------------------------------------------------------- candidates

def decision_candidate(d: dict) -> Candidate:
    """What the matcher compares of an extracted or judged decision (its quote located as citat_pos)."""
    return Candidate(d["emne"] or "", d["tekst"] or "", d["udfald"] or "",
                     matching.quote_span(d.get("citat_pos"), d["citat"] or ""))


def cluster(groups: Sequence[Sequence[Candidate]]) -> list[dict[int, int]]:
    """Cluster the items of several runs: each cluster maps a group (run) to one of its items.

    Every pair of groups is matched with matching.match_decisions, and the matched pairs are joined best score
    first, like single linkage, except that two clusters never join when both hold an item of the same group: one
    run's two decisions are two decisions. Clusters are in the order of their first item (group, index).
    """
    where = {(g, i): (g, i) for g, items in enumerate(groups) for i in range(len(items))}
    clusters = {key: {key[0]: key[1]} for key in where}
    pairs = [(m.score, a, m.old, b, m.new) for a, b in combinations(range(len(groups)), 2)
             for m in matching.match_decisions(groups[a], groups[b])]
    for _, a, i, b, j in sorted(pairs, key=lambda p: (-p[0], p[1:])):
        first, second = where[a, i], where[b, j]
        if first == second or clusters[first].keys() & clusters[second].keys():
            continue
        keep, gone = min(first, second), max(first, second)
        clusters[keep].update(clusters.pop(gone))
        for g, idx in clusters[keep].items():
            where[g, idx] = keep
    return [clusters[root] for root in sorted(clusters)]


@dataclass(frozen=True)
class CandidateCluster:
    """Decisions of several runs that look like one decision, numbered in reading order."""
    number: int
    members: tuple[tuple[str, int, dict], ...]  # (run, index in the run, decision), in run order


def candidate_clusters(runs: Mapping[str, list[dict]]) -> list[CandidateCluster]:
    """The candidates of a document: its runs' decisions clustered, in order of their earliest located quote
    (clusters without one last)."""
    names = list(runs)
    found = cluster([[decision_candidate(d) for d in runs[name]] for name in names])

    def start(members: dict[int, int]) -> tuple:
        located = [runs[names[g]][i].get("citat_pos") for g, i in members.items()]
        return min((p for p in located if p is not None), default=math.inf), min(members.items())

    return [CandidateCluster(n, tuple((names[g], i, runs[names[g]][i]) for g, i in sorted(members.items())))
            for n, members in enumerate(sorted(found, key=start), start=1)]


def candidate_view(c: CandidateCluster, judge: int = 1) -> dict:
    """A candidate as one judge sees it: one run's emne, tekst and quote (another run's for each judge, so the
    judges' answers depend less on one wording), every value the runs gave for the coded fields and dates, and up to
    three other quotes. Which runs found it is left out, so no model's name or majority sways the judge."""
    shown = c.members[(judge - 1) % len(c.members)][2]

    def values(key: str) -> list:
        return list(dict.fromkeys(d[key] for _, _, d in c.members))

    others = list(dict.fromkeys(d["citat"] for _, _, d in c.members if d["citat"] != shown["citat"]))
    return {"candidate": c.number, "emne": shown["emne"], "tekst": shown["tekst"], "citat": shown["citat"],
            "citat_fundet": shown["citat_fundet"], "side": shown.get("citat_side") or shown["side"],
            **{key: values(key) for key in (*CODED_FIELDS, "gaelder_fra", "gaelder_til")},
            "andre_citater": others[:3]}


def rotated(items: Sequence, judge: int) -> list:
    """The items starting a third further on for each judge, so the judges do not read them in the same order."""
    k = (judge - 1) * len(items) // len(JUDGES)
    return [*items[k:], *items[:k]]


# --------------------------------------------------------------------------- judge prompts

def _extraction_rules() -> tuple[str, str]:
    """EXTRACT_SYSTEM's criteria for what counts as a decision, and its field rules, verbatim: the key judges
    decisions by the rules the extraction is given. The first paragraph (the extractor's role) is left out."""
    body = analyze.EXTRACT_SYSTEM.split("\n\n", 1)[1]
    criteria, heading, field_rules = body.partition("Field rules:")
    if not criteria.startswith("Extract every decision") or not heading:
        raise ValueError("EXTRACT_SYSTEM no longer has the sections the judge prompts quote")
    return criteria.strip(), (heading + field_rules).strip()


DECISION_CRITERIA, FIELD_RULES = _extraction_rules()

GRANULARITY = """\
Granularity, which extractions apply unevenly:
- One decision per rule a decision sets, changes or confirms. A budget that sets three fees is three \
decisions, one per fee; a fee restated unchanged with a budget ("uændret") is a decision too, with \
handling bekraeftelse.
- A table or list adopted as a whole is one decision per rule it sets, not one per line: the \
qualification totals for one championship and category (every weight class in the table) are one \
decision. A package of IPF or EPF rule changes adopted in one vote is one decision, unless the \
document gives each change its own vote or outcome; then each is one.
- A proposal and its adoption (or rejection) at the same meeting are one decision with the final \
outcome: udfald and handling say what the meeting ended with, and citat shows that outcome.
- The same rule stated twice in one document (an agenda item and the decision on it, a summary and \
the full text) is one decision."""

DECISIONS_JUDGE_SYSTEM = f"""\
You build an answer key: the decisions one document of Dansk Styrkeløft Forbund (DSF), the Danish \
powerlifting federation, really contains. The document is minutes ("referat") or a rule document. \
Several automatic extractions have read it, and their decisions are grouped into numbered candidates, \
listed in no particular order: each candidate is the decisions the extractions gave for what looks \
like one decision, shown with one extraction's emne, tekst and citat (citat_fundet false: that quote is \
not in the document), every value the extractions gave for the coded fields and dates, and other \
quotes they used. The key measures how well future extractions find the right decisions with the right \
fields, so it must follow the document; a candidate is only a hint.

A decision counts when it meets the rules the extractions were given:

<extraction_rules>
{DECISION_CRITERIA}

{FIELD_RULES}
</extraction_rules>

{GRANULARITY}

Give a verdict for every candidate, by its number:
- keep: the candidate is a decision by these rules. Fill in all its fields (those of the output \
schema) from the document, correcting whatever the candidate got wrong. citat is copied verbatim \
from the document.
- reject: it is not a decision by these rules (an action item, a discussion, an appointment, a \
one-off event, a budget approved as such, or something the document does not say). Leave the fields \
null.
- duplicate: by the granularity rules it is the same decision as another candidate, which you keep; \
give that candidate's number in duplicate_of and leave the fields null. Of candidates describing the \
same decision, keep the one whose citat states the outcome (the vote or the adoption); if both or \
neither do, keep the one whose citat comes first in the document. When a candidate mixes two \
decisions, keep it for the one its citat shows and add the other under missing.
reason: one short Danish sentence for reject and duplicate, else null.

Then list under missing every decision by these rules that no kept candidate covers, with all fields \
and a verbatim citat. Read the whole document for these: extractions miss decisions."""

RULES_JUDGE_SYSTEM = f"""\
You build an answer key for the history of one recurring rule of Dansk Styrkeløft Forbund (DSF), the \
Danish powerlifting federation: which decisions introduced, changed, confirmed or abolished it, and \
what was in force in each year. The key checks an automatic overview of DSF's rules that shows, for \
every year, the version of each rule in force then.

The input names the rule: its title and the topics (emne) its decisions were filed under, and today's \
date. Then follow numbered passages from DSF's minutes and rule documents in chronological order, each \
with its document id, meeting date, organ and page. They were found by searching for the rule's \
decisions, other decisions using the words of its title, and its keywords, so many are about other \
rules: use only what concerns this rule. A passage is a window of text around a hit and may start or \
end mid-sentence.

What counts as a decision follows the rules DSF's decisions are extracted by:

<extraction_rules>
{DECISION_CRITERIA}
</extraction_rules>

events: every decision about this rule, in chronological order, one event per decision:
- passage: the id of the passage it stands in, e.g. "P4".
- quote: copied verbatim from that passage (at most about 300 characters), showing the decision. It \
is checked against the passage: a quote from elsewhere makes the event void.
- value_after: for indfoert, aendret and bekraeftet, the rule's content in force after the event: its \
amounts, limits and conditions in at most 12 Danish words (e.g. "300 kr. pr. løfter pr. år"), without \
dates (when it applies from belongs to years); null for the other effects.
- effect: indfoert (the rule first appears), aendret (its content changes), bekraeftet (repeated, \
confirmed or clarified without changing content, including a rule document listing it as in force), \
ophaevet (abolished), foreslaaet (a proposal presented or discussed without a decision yet), \
forkastet (a proposal voted down), trukket (the proposer withdrew it). Decide between aendret and \
bekraeftet by comparing value_after with the content in force just before the event, not by the \
document's wording: the same content is bekraeftet even when the document says "hævet" or "ændret", \
other content is aendret even when it says "uændret". The earliest event in the passages is indfoert, \
even when it says the rule was raised or changed (the earlier content is not in the passages), with the \
value it states as value_after.
A proposal and its adoption at the same meeting are one event with the final outcome. A decision that \
also changes other rules is one event here. A passage that only mentions the rule (a report, a \
reminder, a discussion) is not an event.

years: every year from the year of the first event to the current year, each with a state:
- event: the rule applied at the end of the year (for the current year: today) as an event set it; \
give the event's number, counting your events from 1. That is the latest indfoert, aendret or \
bekraeftet event decided by then and in effect, unless a later ophaevet event abolished the rule or it \
applied only until a date that has passed. Proposals never change what is in force. A board or \
committee decision cannot abolish a rule adopted by Repræsentantskabet.
- none: the rule did not apply that year.
- unknown: the passages do not show what applied. A fee of 150 kr. set in 2010 and stated as \
"200 kr. (uændret)" in 2013 leaves 2011 and 2012 unknown: it changed to 200 kr. in one of them.

Never invent events, and never guess a year: unknown is a valid answer."""


def _nullable(schema: dict) -> dict:
    """The same field, or null: a verdict other than keep has no fields."""
    types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
    result = {**schema, "type": [*types, "null"] if "null" not in types else types}
    if "enum" in schema:
        result["enum"] = [*schema["enum"], None]
    return result


_EXTRACTED = analyze.EXTRACT_SCHEMA["properties"]["beslutninger"]["items"]["properties"]
_JUDGED = {name: _EXTRACTED[name] for name in JUDGED_FIELDS}

DECISIONS_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "candidate": {"type": "integer"},
                    "verdict": {"type": "string", "enum": ["keep", "reject", "duplicate"]},
                    "duplicate_of": {"type": ["integer", "null"]},
                    "reason": {"type": ["string", "null"]},
                    **{name: _nullable(spec) for name, spec in _JUDGED.items()},
                },
                "required": ["candidate", "verdict", "duplicate_of", "reason", *JUDGED_FIELDS],
                "additionalProperties": False,
            },
        },
        "missing": {
            "type": "array",
            "items": {"type": "object", "properties": _JUDGED, "required": list(JUDGED_FIELDS),
                      "additionalProperties": False},
        },
    },
    "required": ["candidates", "missing"],
    "additionalProperties": False,
}

YEAR_STATES = ("event", "none", "unknown")

RULES_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "passage": {"type": "string"},
                    "quote": {"type": "string"},
                    "value_after": {"type": ["string", "null"]},
                    "effect": {"type": "string", "enum": EFFEKTER},
                },
                "required": ["passage", "quote", "value_after", "effect"],
                "additionalProperties": False,
            },
        },
        "years": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"year": {"type": "integer"},
                               "state": {"type": "string", "enum": list(YEAR_STATES)},
                               "event": {"type": ["integer", "null"]}},
                "required": ["year", "state", "event"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["events", "years"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- judge calls

@dataclass(frozen=True)
class JudgeCall:
    """One judge's call. Judges 1 and 2 answer every item; judge 3 only items they disagree on. All three answer
    blind: none sees another's answer."""
    kind: str  # "decisions" or "rules"
    item: str  # document id or rule slug
    number: int
    system: str
    prompt: str
    schema: dict

    def fingerprint(self, model: str, effort: str | None) -> str:
        """Everything the answer depends on; a stored answer is reused only for the same."""
        text = json.dumps([self.system, self.prompt, self.schema, model, effort], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    @property
    def path(self) -> Path:
        return judge_path(self.kind, self.item, self.number)


@dataclass(frozen=True)
class Judging:
    """The judge model and effort of a key command, its budget, and whether answers to other input may be
    replaced."""
    model: str
    effort: str | None
    budget: RunBudget
    workers: int
    rejudge: bool = False
    offline: bool = False  # --rederive: only stored answers; a missing one is an error, never a call


def stored_answer(call: JudgeCall, judging: Judging) -> dict | None:
    """The judge's stored answer to exactly this call, or None."""
    if not call.path.exists():
        return None
    stored = read_json(call.path)
    return stored["output"] if stored["fingerprint"] == call.fingerprint(judging.model, judging.effort) else None


def stored_judges(calls: Iterable[JudgeCall]) -> list[dict]:
    """Provenance and usage of the stored answers, for the key file."""
    found = []
    for call in calls:
        stored = read_json(call.path)
        found.append({"judge": call.number, "provenance": stored["provenance"], "usage": stored["usage"]})
    return found


def ask_judge(call: JudgeCall, judging: Judging, cli: str) -> tuple[str, Usage]:
    try:
        output, usage = analyze.ask_claude(call.system, call.prompt, call.schema, model=judging.model,
                                           effort=judging.effort, timeout=JUDGE_TIMEOUT, budget=judging.budget,
                                           attempts=JUDGE_ATTEMPTS)
    except analyze.ClaudeError as exc:
        if not str(exc).startswith("timeout"):
            raise
        message = (f"{call.item} judge {call.number} timed out: no answer within {JUDGE_TIMEOUT // 60} minutes in "
                   f"any of {JUDGE_ATTEMPTS} attempts ({exc}); timed-out attempts report no cost, so the cost limit "
                   f"does not count them")
        raise analyze.ClaudeError(message, exc.usage) from exc
    with analyze.usage_kept(usage):
        write_json(call.path, {
            "fingerprint": call.fingerprint(judging.model, judging.effort),
            "provenance": analyze.provenance(usage, cli, call.system, call.schema, judging.effort),
            "usage": usage_json(usage), "output": output,
        })
    return f"{call.item} judge {call.number}: {describe_usage(usage)}", usage


def run_judges(label: str, calls: list[JudgeCall], judging: Judging, note: str = "") -> StepSummary | None:
    """Ask the judges whose answer is not stored yet; None when there is nothing to ask.

    An answer stored for other input (data, candidates, prompt, model or effort changed) stops the command before
    any call: asking again pays for it, so only --rejudge does."""
    changed = [f"{c.item} judge {c.number}" for c in calls if c.path.exists()
               and read_json(c.path)["fingerprint"] != c.fingerprint(judging.model, judging.effort)]
    if changed and not judging.rejudge:
        raise SystemExit(f"Stored judge answers were given for other input (data, candidates, prompt, model or "
                         f"effort): {', '.join(changed)}. Pass --rejudge to ask them again (paid), or restore the "
                         f"input.")
    todo = [call for call in calls if stored_answer(call, judging) is None]
    if todo and judging.offline:
        missing = ", ".join(f"{c.item} judge {c.number}" for c in todo)
        raise SystemExit(f"Rederiving needs answers that are not stored: {missing}; run without --rederive to ask "
                         f"for them (paid)")
    reused = len(calls) - len(todo)
    tokens_in = sum(estimate_tokens(c.system, c.prompt, c.schema) for c in todo)
    print_plan(label, len(todo), tokens_in, judging.model, judging.effort,
               (f"; {reused} answers stored already" if reused else "") + note)
    if not todo:
        return None
    cli = analyze.cli_version()
    return analyze.run_parallel(todo, lambda call: ask_judge(call, judging, cli), judging.workers, label,
                                judging.budget)


# --------------------------------------------------------------------------- agreement

@dataclass(frozen=True)
class Resolution:
    status: str  # "certain", "uncertain", "open" (judges 1 and 2 disagree, judge 3 has not answered) or "dropped"
    choice: object = None  # the agreed answer when certain


def resolve(answers: Sequence[object], same: Callable[[object, object], bool]) -> Resolution:
    """Two of three decide: judges 1 and 2 agreeing is certain; otherwise judge 3, answering blind, decides when it
    agrees with one of them (that judge's answer is kept), and leaves it uncertain when it agrees with neither."""
    first, second = answers[:2]
    if same(first, second):
        return Resolution("certain", first)
    if len(answers) < 3:
        return Resolution("open")
    for earlier in (first, second):
        if same(answers[2], earlier):
            return Resolution("certain", earlier)
    return Resolution("uncertain")


@dataclass(frozen=True)
class Fields:
    """The coded fields of a decision several judges described."""
    values: dict[str, object]  # each field's majority value (the first judge's where there is none)
    uncertain: tuple[str, ...]  # fields no two judges agree on; field accuracy skips them
    open: bool  # some field is uncertain while a third judge could still settle it


def resolve_fields(decisions: Sequence[dict], complete: bool) -> Fields:
    """Each coded field's value where at least two judges agree. Whether the decision exists is decided apart:
    handling, say, often cannot be told from one document, and that must not make the decision itself uncertain."""
    values, uncertain = {}, []
    for name in CODED_FIELDS:
        value, votes = Counter(d[name] for d in decisions).most_common(1)[0]
        values[name] = value
        if votes < 2:
            uncertain.append(name)
    return Fields(values, tuple(uncertain), bool(uncertain) and not complete)


@dataclass(frozen=True)
class Verdict:
    """One judge's answer for one candidate."""
    kind: str  # keep, reject, duplicate; "none" when the judge gave no answer for it
    duplicate_of: int | None = None
    reason: str | None = None
    decision: dict | None = None  # for keep: the judged fields, the quote located (citat_pos)

    def summary(self) -> dict:
        found = {"verdict": self.kind}
        if self.duplicate_of is not None:
            found["duplicate_of"] = self.duplicate_of
        if self.reason:
            found["reason"] = self.reason
        if self.decision is not None:
            found |= {name: self.decision[name] for name in CODED_FIELDS} | {"citat": self.decision["citat"]}
        return found


def judged_decision(raw: dict, words: DocWords) -> dict:
    """A judged decision's fields with its quote located in the document."""
    found = {name: raw.get(name) for name in JUDGED_FIELDS}
    return found | analyze.quote_fields(found["citat"] or "", words, found["side"])


def verdicts_of(output: dict, numbers_: Sequence[int], words: DocWords) -> dict[int, Verdict]:
    """Each candidate's verdict in a judge's answer; the first answer for a number counts, unknown numbers and a
    keep without fields are no answer."""
    given: dict[int, Verdict] = {}
    for item in output["candidates"]:
        n = item["candidate"]
        if n in given or n not in numbers_:
            continue
        kind = item["verdict"]
        if kind == "keep" and any(item.get(name) is None for name in (*CODED_FIELDS, "citat")):
            continue
        given[n] = Verdict(kind, item["duplicate_of"] if kind == "duplicate" else None, item.get("reason"),
                           judged_decision(item, words) if kind == "keep" else None)
    return {n: given.get(n, Verdict("none")) for n in numbers_}


def same_kind(a: Verdict, b: Verdict) -> bool:
    """Two verdicts agree on whether the candidate is a decision: both keep it, both reject it, or both call it a
    duplicate of the same candidate. Its fields are judged apart (resolve_fields)."""
    return a.kind == b.kind != "none" and (a.kind != "duplicate" or a.duplicate_of == b.duplicate_of)


@dataclass(frozen=True)
class CandidateVerdict:
    status: str  # whether the candidate's verdict is certain (Resolution.status)
    kind: str | None = None  # keep, reject or duplicate when certain
    duplicate_of: int | None = None
    reason: str | None = None
    decision: dict | None = None  # keep: one judge's decision with the majority coded fields
    fields: Fields | None = None
    quotes: tuple[dict, ...] = ()  # keep: the other judges' located quotes, so their wording finds it too


def resolve_candidate(verdicts: Sequence[Verdict]) -> CandidateVerdict:
    """A candidate kept by two judges is a certain decision whatever fields they gave; the fields are certain where
    two agree. The decision's quote and text are those of the first judge that kept it with a located quote."""
    r = resolve(verdicts, same_kind)
    if r.status != "certain":
        return CandidateVerdict(r.status)
    chosen: Verdict = r.choice
    if chosen.kind != "keep":
        return CandidateVerdict("certain", chosen.kind, chosen.duplicate_of, chosen.reason)
    keepers = [v.decision for v in verdicts if v.kind == "keep"]
    fields_ = resolve_fields(keepers, complete=len(verdicts) == len(JUDGES))
    shown = next((d for d in keepers if d["citat_fundet"]), keepers[0])
    quotes = tuple({"citat": d["citat"], "citat_pos": d["citat_pos"]} for d in keepers
                   if d is not shown and d["citat_pos"] is not None)
    return CandidateVerdict("certain", "keep", decision={**shown, **fields_.values}, fields=fields_, quotes=quotes)


@dataclass(frozen=True)
class AddedDecision:
    """A decision the judges added under missing."""
    status: str  # certain (two or more judges added it), open (one of judges 1 and 2), or uncertain (one of three)
    decision: dict
    fields: Fields
    judges: tuple[int, ...]


def resolve_missing(lists: Sequence[list[dict]], kept: Sequence[Candidate] = ()) -> list[AddedDecision]:
    """The decisions the judges added, clustered across judges (cluster()).

    An added decision that duplicates a kept candidate (scores at least matching.MATCH_THRESHOLD against one of its
    decisions) is left out: the candidate has it. Added by two or more judges, it is certain, with the fields two
    agree on; added by one, it is open while judge 3 has not answered, then uncertain (kept, but not scored).
    """
    complete = len(lists) == len(JUDGES)
    lists = [[d for d in found if not any(matching.score(k, decision_candidate(d)) >= matching.MATCH_THRESHOLD
                                          for k in kept)] for found in lists]
    added = []
    for members in cluster([[decision_candidate(d) for d in found] for found in lists]):
        decisions = [lists[g][i] for g, i in sorted(members.items())]
        shown = next((d for d in decisions if d["citat_fundet"]), decisions[0])
        fields_ = resolve_fields(decisions, complete)
        status = "certain" if len(decisions) > 1 else "uncertain" if complete else "open"
        added.append(AddedDecision(status, {**shown, **fields_.values}, fields_,
                                   tuple(sorted(g + 1 for g in members))))
    return added


# --------------------------------------------------------------------------- the decisions key (Part B)

@dataclass(frozen=True)
class DecisionsTask:
    doc: Doc
    runs: tuple[str, ...]
    clusters: list[CandidateCluster]
    words: DocWords
    document: str  # the document as the extraction sees it (analyze.document_prompt)

    def prompt(self, judge: int) -> str:
        """The document and the candidates, in another order and with another run's wording for each judge."""
        shown = "\n".join(json.dumps(candidate_view(c, judge), ensure_ascii=False)
                          for c in rotated(self.clusters, judge))
        return f"{self.document}\n\n<candidates>\n{shown}\n</candidates>"

    def call(self, judge: int) -> JudgeCall:
        return JudgeCall("decisions", self.doc.id, judge, DECISIONS_JUDGE_SYSTEM, self.prompt(judge),
                         DECISIONS_JUDGE_SCHEMA)

    def verdicts(self, answers: Sequence[dict]) -> list[dict[int, Verdict]]:
        return [verdicts_of(answer, [c.number for c in self.clusters], self.words) for answer in answers]

    def resolved(self, answers: Sequence[dict]) -> tuple[dict[int, CandidateVerdict], list[AddedDecision]]:
        verdicts = self.verdicts(answers)
        candidates = {c.number: resolve_candidate([v[c.number] for v in verdicts]) for c in self.clusters}
        kept = [probe for c in self.clusters if candidates[c.number].kind == "keep"
                for probe in [decision_candidate(d) for _, _, d in c.members]
                + [decision_candidate(candidates[c.number].decision)]]
        added = resolve_missing([[judged_decision(d, self.words) for d in a["missing"]] for a in answers], kept)
        return candidates, added

    def needs_tiebreak(self, answers: Sequence[dict]) -> bool:
        candidates, added = self.resolved(answers)
        return (any(r.status == "open" or (r.fields is not None and r.fields.open) for r in candidates.values())
                or any(a.status == "open" or a.fields.open for a in added))


def decisions_task(doc: Doc, runs: Sequence[str], texts: Texts) -> DecisionsTask:
    clusters = candidate_clusters({run: load_run(run, doc) for run in runs})
    return DecisionsTask(doc, tuple(runs), clusters, texts.words(doc), analyze.document_prompt(doc, texts.text(doc)))


def _span(d: dict) -> tuple[int, int] | None:
    return matching.quote_span(d.get("citat_pos"), d.get("citat") or "")


def decisions_key(task: DecisionsTask, answers: Sequence[dict]) -> dict:
    """The key for one document from its judges' answers (two, or three when they disagreed).

    Each candidate gets a role for scoring: "decision" (certainly kept, or certainly a duplicate of a kept one),
    "reject" (certainly not a decision) or "ignored" (uncertain). Kept candidates and added decisions are the key's
    decisions, K<candidate> and M<n>, each listing the coded fields the judges did not agree on.
    """
    verdicts = task.verdicts(answers)
    resolved, added = task.resolved(answers)
    kept = {n for n, r in resolved.items() if r.kind == "keep"}
    joined: dict[int, list[int]] = defaultdict(list)
    candidates = []
    for c in task.clusters:
        r = resolved[c.number]
        role, key = "ignored", None
        if r.kind == "keep":
            role, key = "decision", f"K{c.number}"
        elif r.kind == "duplicate" and r.duplicate_of in kept:
            role, key = "decision", f"K{r.duplicate_of}"
            joined[r.duplicate_of].append(c.number)
        elif r.kind == "reject":
            role = "reject"
        candidates.append({
            "number": c.number, "status": r.status, "role": role, "key": key, "verdict": r.kind,
            "reason": r.reason,
            "members": [{"run": run, "index": i, "emne": d["emne"], "tekst": d["tekst"], "udfald": d["udfald"],
                         "span": _span(d)} for run, i, d in c.members],
            "verdicts": [v[c.number].summary() for v in verdicts],
        })
    decisions = [{"id": f"K{n}", "status": "certain", "candidates": [n, *joined[n]], **resolved[n].decision,
                  "uncertain_fields": list(resolved[n].fields.uncertain), "quotes": list(resolved[n].quotes)}
                 for n in sorted(kept)]
    decisions += [{"id": f"M{k}", "status": a.status, "candidates": [], **a.decision,
                   "uncertain_fields": list(a.fields.uncertain), "quotes": [], "judges": list(a.judges)}
                  for k, a in enumerate(added, start=1)]
    decisions.sort(key=lambda d: (d.get("citat_pos") is None, d.get("citat_pos") or 0, d["id"]))
    return {"doc_id": task.doc.id, "sha256": task.doc.sha256, "runs": list(task.runs), "decisions": decisions,
            "candidates": candidates}


def solo_candidates(keys: Iterable[dict]) -> dict[str, tuple[int, int]]:
    """Run -> (kept, total) among the candidates only that run found: a judge favouring one model's wording would
    keep that model's lone decisions more often."""
    found: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for key in keys:
        for c in key["candidates"]:
            runs = {m["run"] for m in c["members"]}
            if len(runs) == 1:
                (run,) = runs
                found[run][0] += c["role"] == "decision"
                found[run][1] += 1
    return {run: (kept, n) for run, (kept, n) in sorted(found.items())}


# --------------------------------------------------------------------------- passages (Part A)

@dataclass(frozen=True)
class Passage:
    id: str
    doc: Doc
    page: int | None
    start: int  # word offsets [start, end) in the document
    end: int
    source: str  # "rule" (around the rule's own decisions), "related" (other decisions with its title words), "keyword"
    weight: float  # keyword density: the weight and rarity of its keyword hits per 100 words

    @property
    def words(self) -> int:
        return self.end - self.start

    def entry(self) -> dict:
        return {"id": self.id, "doc": self.doc.id, "date": self.doc.date, "page": self.page, "start": self.start,
                "end": self.end, "source": self.source}


def load_synonyms() -> list[list[tuple[str, ...]]]:
    """website/synonyms.json: groups of words (or phrases) that should find each other, as token tuples."""
    return [[tuple(tokens(entry)) for entry in group] for group in read_json(SYNONYMS)["groups"]]


def _token_matches(word: str, token: str) -> bool:
    return word == token or (len(token) >= PREFIX_LENGTH and word.startswith(token))


def _phrase_at(words: list[str], i: int, phrase: tuple[str, ...]) -> bool:
    return i + len(phrase) <= len(words) and all(_token_matches(words[i + k], t) for k, t in enumerate(phrase))


class KeywordIndex:
    """Where each word occurs in the corpus, to find a rule's keywords and how distinctive they are."""

    def __init__(self, docs: list[Doc], texts: Texts) -> None:
        self.docs = {doc.id: doc for doc in docs}
        self.texts = texts
        postings: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for doc in docs:
            for i, word in enumerate(texts.words(doc).words):
                postings[word][doc.id].append(i)
        self._postings = postings
        self._vocabulary = sorted(postings)
        self._hits: dict[tuple[str, ...], dict[str, list[int]]] = {}

    def _matching(self, token: str) -> list[str]:
        """The corpus words a token matches (_token_matches): itself, and when long enough every word it starts."""
        if len(token) < PREFIX_LENGTH:
            return [token] if token in self._postings else []
        start, end = (bisect_left(self._vocabulary, bound) for bound in (token, token + "\U0010ffff"))
        return self._vocabulary[start:end]

    def hits(self, phrase: tuple[str, ...]) -> dict[str, list[int]]:
        """Document id -> word offsets where the phrase starts."""
        if phrase not in self._hits:
            found: dict[str, list[int]] = defaultdict(list)
            for word in self._matching(phrase[0]):
                for doc_id, starts in self._postings[word].items():
                    words = self.texts.words(self.docs[doc_id]).words
                    found[doc_id] += [i for i in starts if _phrase_at(words, i, phrase)]
            self._hits[phrase] = {doc_id: sorted(starts) for doc_id, starts in found.items() if starts}
        return self._hits[phrase]

    def share(self, phrase: tuple[str, ...]) -> float:
        """The share of the documents the phrase occurs in."""
        return len(self.hits(phrase)) / len(self.docs)


def decision_vocabulary(decisions: Iterable[Decision]) -> frozenset[str]:
    """Words of at least three letters in the decisions' emne and tekst: the parts compound words are split into
    (candidates.compound_parts). Not the documents' words, whose line-break hyphenation leaves fragments ("tagelse")."""
    return frozenset(w for d in decisions for w in tokens(f"{d.emne} {d.tekst}") if len(w) >= 3)


def _distinctive(words: Iterable[str], index: KeywordIndex) -> list[tuple[str, ...]]:
    """The words in at most MAX_KEYWORD_SHARE of the documents (at least the rarest one), rarest first."""
    ranked = sorted({(w,) for w in words}, key=lambda p: (index.share(p), p))
    distinctive = [p for p in ranked if 0 < index.share(p) <= MAX_KEYWORD_SHARE]
    return distinctive or [p for p in ranked if index.share(p) > 0][:1]


def title_words(titel: str, index: KeywordIndex, vocabulary: Collection[str]) -> list[tuple[str, ...]]:
    """The title's distinctive words and compound parts ("licensgebyr": licensgebyr, licens, gebyr)."""
    words = {w for w in tokens(titel) if len(w) > 1}
    return _distinctive(words | {part for w in words for part in compound_parts(w, vocabulary)}, index)


def rule_keywords(titel: str, emner: Iterable[str], groups: list[list[tuple[str, ...]]],
                  index: KeywordIndex, vocabulary: Collection[str]) -> dict[tuple[str, ...], float]:
    """The rule's distinctive words with their weight: those of its title and its decisions' emne and their compound
    parts weigh 1, the other words of every synonym group one of them belongs to SYNONYM_WEIGHT. Only words in at
    most MAX_KEYWORD_SHARE of the documents are kept (at least the rarest one), rarest first. Single letters say
    nothing."""
    words = {w for w in tokens(" ".join([titel, *emner])) if len(w) > 1}
    words |= {part for w in words for part in compound_parts(w, vocabulary)}
    weights = {(w,): 1.0 for w in words}
    for group in groups:
        if any(all(any(_token_matches(w, t) for w in words) for t in phrase) for phrase in group):
            weights |= {phrase: weights.get(phrase, SYNONYM_WEIGHT) for phrase in group}
    ranked = sorted(weights, key=lambda p: (index.share(p), p))
    distinctive = [p for p in ranked if 0 < index.share(p) <= MAX_KEYWORD_SHARE]
    return {p: weights[p] for p in distinctive or [p for p in ranked if index.share(p) > 0][:1]}


def related_decisions(decisions: Iterable[Decision], words: Sequence[tuple[str, ...]],
                      own: Collection[str]) -> list[Decision]:
    """Decisions of other rules (or of none) whose emne or tekst uses one of the title's words: where the pipeline
    split a rule (a budget's fee lines in another rule), the evidence lies behind them."""
    return [d for d in decisions if d.ref not in own
            and any(_token_matches(t, w[0]) for t in tokens(f"{d.emne} {d.tekst}") for w in words)]


def _merge(windows: list[tuple[int, int]], gap: int = 0) -> list[tuple[int, int]]:
    """Overlapping windows, and those fewer than `gap` words apart, as one."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _subtract(window: tuple[int, int], taken: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """The parts of a window outside the taken spans: a keyword window overlapping a decision passage is trimmed,
    not dropped."""
    pieces, start = [], window[0]
    for s, e in sorted(taken):
        if e <= start or s >= window[1]:
            continue
        if s > start:
            pieces.append((start, s))
        start = max(start, e)
    if start < window[1]:
        pieces.append((start, window[1]))
    return pieces


def _split(start: int, end: int) -> list[tuple[int, int]]:
    """A window cut into pieces of at most MAX_PASSAGE_WORDS."""
    return [(s, min(s + MAX_PASSAGE_WORDS, end)) for s in range(start, end, MAX_PASSAGE_WORDS)]


@dataclass(frozen=True)
class Gathering:
    passages: list[Passage]  # P1… in chronological order
    unread_amounts: list[dict]  # amount_hits that no passage holds


def gather_passages(own: list[Decision], related: list[Decision], keywords: Mapping[tuple[str, ...], float],
                    title: Sequence[tuple[str, ...]], index: KeywordIndex, docs: Mapping[str, Doc]) -> Gathering:
    """The passages a rules judge reads, up to PASSAGE_BUDGET words.

    First the windows (PASSAGE_CONTEXT words either side) around the quotes of the rule's own decisions, always;
    then those around related decisions, then keyword windows holding one of the title's words next to a number
    (an amount, which a timeline of values needs), then the other keyword windows. Each group goes by keyword
    density, every document's best before any document's second, so the passages span the years. Overlapping
    windows merge; a keyword window is trimmed where it overlaps a decision window and cut to MAX_PASSAGE_WORDS.
    The amounts no passage holds are reported (unread_amounts): what the judges did not see.
    """
    texts = index.texts
    weights = {p: factor * math.log(1 / index.share(p)) for p, factor in keywords.items()}
    hits: dict[str, list[tuple[int, tuple[str, ...]]]] = defaultdict(list)
    for phrase in keywords:
        for doc_id, starts in index.hits(phrase).items():
            hits[doc_id] += [(i, phrase) for i in starts]

    def weight(doc_id: str, start: int, end: int) -> float:
        return 100 * sum(weights[p] for i, p in hits[doc_id] if start <= i < end) / max(end - start, 1)

    def windows(decisions: list[Decision]) -> dict[str, list[tuple[int, int]]]:
        found: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for d in decisions:
            words = texts.words(docs[d.doc_id])
            pos = analyze.locate_quote(d.citat, words, d.side)
            if pos is not None:
                end = matching.quote_span(pos, d.citat)[1]
                found[d.doc_id].append((max(pos - PASSAGE_CONTEXT, 0), min(end + PASSAGE_CONTEXT, len(words.words))))
        return found

    def passage(doc_id: str, start: int, end: int, source: str) -> Passage:
        return Passage("", docs[doc_id], texts.words(docs[doc_id]).pages[start], start, end, source,
                       weight(doc_id, start, end))

    own_windows, related_windows = windows(own), windows(related)
    # Decision windows a few words apart join: the words between them often finish a sentence one of them starts.
    decision_spans = {doc_id: _merge(own_windows.get(doc_id, []) + related_windows.get(doc_id, []), MERGE_GAP)
                      for doc_id in own_windows.keys() | related_windows.keys()}
    owned = {doc_id: _merge(spans) for doc_id, spans in own_windows.items()}
    chosen, related_found, keyword_found = [], [], []
    for doc_id, spans in decision_spans.items():
        for s, e in spans:
            is_own = any(a < e and s < b for a, b in owned.get(doc_id, []))
            (chosen if is_own else related_found).append(passage(doc_id, s, e, "rule" if is_own else "related"))
    for doc_id, doc_hits in hits.items():
        size = len(texts.words(docs[doc_id]).words)
        around = _merge([(max(i - PASSAGE_CONTEXT, 0), min(i + len(p) + PASSAGE_CONTEXT, size)) for i, p in doc_hits])
        for window in around:
            for start, end in _subtract(window, decision_spans.get(doc_id, [])):
                keyword_found += [passage(doc_id, s, e, "keyword") for s, e in _split(start, end)
                                  if e - s >= MIN_PASSAGE_WORDS or (start, end) == window]
    amounts = amount_hits(title, index, docs)

    def has_amount(p: Passage) -> bool:
        return any(p.start <= hit["pos"] < p.end for hit in amounts.get(p.doc.id, []))

    used = sum(p.words for p in chosen)
    ranked = (_spread(related_found) + _spread([p for p in keyword_found if has_amount(p)])
              + _spread([p for p in keyword_found if not has_amount(p)]))
    for p in ranked:
        if used + p.words <= PASSAGE_BUDGET:
            chosen.append(p)
            used += p.words
    # Passages that touch are one stretch of text: joined first, so a proposal split between two is extended whole.
    chosen = _join_passages([_extended(p, texts) for p in _join_passages(chosen, texts)], texts)
    chosen.sort(key=lambda p: (p.doc.date or "", p.doc.id, p.start))
    numbered = [Passage(f"P{n}", p.doc, p.page, p.start, p.end, p.source, p.weight)
                for n, p in enumerate(chosen, start=1)]
    return Gathering(numbered, [hit for doc_id, found in sorted(amounts.items()) for hit in found
                                if not any(p.doc.id == doc_id and p.start <= hit["pos"] and hit["end"] <= p.end
                                           for p in numbered)])


def _is_proposal(word: str) -> bool:
    return "forslag" in word or word.startswith(("foresl", "indstill"))


def _is_outcome(words: Sequence[str], k: int) -> bool:
    """An outcome word, or "imod" in a vote count ("31 for, 7 imod")."""
    return words[k] in OUTCOME_WORDS or (words[k] == "imod" and any(w.isdigit() for w in words[max(k - 4, 0):k]))


def _extended(p: Passage, texts: Texts) -> Passage:
    """The passage with the whole proposal it holds: forward to the outcome when it has a proposal and no outcome
    after it (within EXTEND_FORWARD words, to the end of the outcome's line), and back to the heading of the agenda
    item when it has a proposal or an outcome but no heading before it (within EXTEND_BACK words). A window around a
    quote often stops before "Forslag 5 … blev vedtaget enstemmigt", or starts in the middle of the proposal."""
    words, spans, text = texts.words(p.doc).words, texts.spans(p.doc), texts.text(p.doc)

    def line_start(k: int) -> bool:
        return k == 0 or "\n" in text[spans[k - 1][1]:spans[k][0]]

    def heading(k: int) -> bool:
        """A line that opens an agenda item; "Forslag 5 … blev vedtaget" closes one."""
        if not line_start(k) or HEADING.match(text, spans[k][0]) is None:
            return False
        line_end = next((j for j in range(k + 1, min(k + 30, len(words))) if line_start(j)), min(k + 30, len(words)))
        return not any(_is_outcome(words, j) for j in range(k, line_end))

    start, end = p.start, p.end
    proposals = [k for k in range(start, end) if _is_proposal(words[k])]
    outcomes = [k for k in range(start, end) if _is_outcome(words, k)]
    if proposals and not any(k > proposals[-1] for k in outcomes):
        found = next((k for k in range(end, min(end + EXTEND_FORWARD, len(words))) if _is_outcome(words, k)), None)
        if found is not None:
            end = next((k for k in range(found + 1, min(found + 20, len(words))) if line_start(k)),
                       min(found + 20, len(words)))
    markers = proposals + outcomes
    if markers and not any(heading(k) for k in range(start, min(markers) + 1)):
        start = next((k for k in range(start - 1, max(start - EXTEND_BACK, 0) - 1, -1) if heading(k)), start)
    if (start, end) == (p.start, p.end):
        return p
    return replace(p, start=start, end=end, page=texts.words(p.doc).pages[start])


_SOURCE_ORDER = ("rule", "related", "keyword")


def _join_passages(passages: list[Passage], texts: Texts) -> list[Passage]:
    """Passages of one document that overlap or touch, as one; it keeps the strongest source."""
    joined: list[Passage] = []
    for p in sorted(passages, key=lambda p: (p.doc.id, p.start)):
        last = joined[-1] if joined else None
        if last is not None and last.doc.id == p.doc.id and p.start <= last.end:
            source = min(last.source, p.source, key=_SOURCE_ORDER.index)
            joined[-1] = replace(last, end=max(last.end, p.end), source=source, weight=max(last.weight, p.weight))
        else:
            joined.append(p)
    return joined


def _spread(passages: list[Passage]) -> list[Passage]:
    """By keyword density, every document's best passage before any document's second best, and so on."""
    ranked = sorted(passages, key=lambda p: (-p.weight, p.doc.date or "", p.doc.id, p.start))
    nth: Counter[str] = Counter()
    rounds = []
    for p in ranked:
        rounds.append((nth[p.doc.id], p))
        nth[p.doc.id] += 1
    return [p for _, p in sorted(rounds, key=lambda item: item[0])]


def _is_amount(word: str) -> bool:
    """A number that is not a year: "200", "000" of "1.000", "84" of a weight class."""
    return word.isdigit() and not (len(word) == 4 and 1990 <= int(word) <= 2039)


def amount_hits(title: Sequence[tuple[str, ...]], index: KeywordIndex,
                docs: Mapping[str, Doc]) -> dict[str, list[dict]]:
    """Document id -> the hits of the title's words (title_words) with a number within AMOUNT_WINDOW: {"doc", "pos",
    "end", "text"}, where [pos, end) holds the hit and its numbers. The title's words, not every keyword: "start" or
    "fordeling" next to a number is seldom this rule."""
    after, before = AMOUNT_WINDOW
    found: dict[str, list[dict]] = defaultdict(list)
    for phrase in title:
        for doc_id, starts in index.hits(phrase).items():
            words = index.texts.words(docs[doc_id]).words
            for i in starts:
                near = range(max(i - before, 0), min(i + len(phrase) + after, len(words)))
                amounts = [k for k in near if _is_amount(words[k])]
                if amounts:
                    text = index.texts.excerpt(docs[doc_id], near.start, near.stop).replace("\n", " ")
                    found[doc_id].append({"doc": doc_id, "pos": min(i, amounts[0]),
                                          "end": max(i + len(phrase), amounts[-1] + 1), "text": text})
    return {doc_id: sorted(hits, key=lambda hit: hit["pos"]) for doc_id, hits in found.items()}


@dataclass(frozen=True)
class RuleTask:
    slug: str
    kategori: str
    titel: str
    emner: tuple[str, ...]
    keywords: tuple[str, ...]
    passages: list[Passage]
    unread: int  # amounts no passage holds (Gathering.unread_amounts)
    as_of: date
    prompt: str  # the rule and its passages
    texts: Texts = field(repr=False, compare=False)  # to check the judges' quotes against the passages
    # Document id -> the quote spans of the extracted decisions (data/beslutninger), to tell judges' events apart
    extracted: Mapping[str, Sequence[tuple[int, int]]] = field(default_factory=dict, repr=False, compare=False)
    unread_amounts: tuple[dict, ...] = ()  # the unread amounts themselves, when the passages were gathered now

    def call(self, judge: int) -> JudgeCall:
        return JudgeCall("rules", self.slug, judge, RULES_JUDGE_SYSTEM, self.prompt, RULES_JUDGE_SCHEMA)


def rule_prompt(titel: str, emner: Sequence[str], passages: Sequence[Passage], as_of: date, texts: Texts) -> str:
    blocks = "\n\n".join(f"[{p.id}] {p.doc.id} · {p.doc.date or 'ukendt dato'} · {p.doc.organ_label} · "
                         f"side {p.page or '?'}\n{texts.excerpt(p.doc, p.start, p.end)}" for p in passages)
    return (f"Rule: {titel}\nFiled as: {'; '.join(emner)}\nToday: {as_of.isoformat()}\n\n"
            f"<passages>\n{blocks}\n</passages>")


def _emner(raw: dict, decisions: Sequence[Decision]) -> tuple[str, ...]:
    by_ref = {d.ref: d for d in decisions}
    return tuple(dict.fromkeys(by_ref[v["ref"]].emne for v in raw["versioner"] if v["ref"] in by_ref))


def rule_task(choice: dict, raw: dict, decisions: list[Decision], docs: Mapping[str, Doc], index: KeywordIndex,
              groups: list[list[tuple[str, ...]]], as_of: date,
              extracted: Mapping[str, Sequence[tuple[int, int]]] | None = None) -> RuleTask:
    """The rule with passages gathered from the documents now."""
    by_ref = {d.ref: d for d in decisions}
    own = [by_ref[v["ref"]] for v in raw["versioner"] if v["ref"] in by_ref]
    emner = _emner(raw, decisions)
    vocabulary = decision_vocabulary(decisions)
    keywords = rule_keywords(raw["titel"], emner, groups, index, vocabulary)
    title = title_words(raw["titel"], index, vocabulary)
    related = related_decisions(decisions, title, {d.ref for d in own})
    gathering = gather_passages(own, related, keywords, title, index, docs)
    prompt = rule_prompt(raw["titel"], emner, gathering.passages, as_of, index.texts)
    return RuleTask(choice["slug"], raw["kategori"], raw["titel"], emner, tuple(" ".join(k) for k in keywords),
                    gathering.passages, len(gathering.unread_amounts), as_of, prompt, index.texts, extracted or {},
                    tuple(gathering.unread_amounts))


def stored_rule_task(key: dict, raw: dict, decisions: list[Decision], docs: Mapping[str, Doc], texts: Texts,
                     as_of: date, extracted: Mapping[str, Sequence[tuple[int, int]]] | None = None) -> RuleTask:
    """The rule with the passages its key was judged on (key["passages"]): the same prompt, so the stored answers
    still fit (key-rules --rederive), however the gathering has changed since."""
    passages = [Passage(e["id"], docs[e["doc"]], e["page"], e["start"], e["end"], e["source"], 0.0)
                for e in key["passages"]]
    emner = _emner(raw, decisions)
    return RuleTask(key["slug"], raw["kategori"], raw["titel"], emner, tuple(key["keywords"]), passages,
                    key["unread_amounts"], as_of, rule_prompt(raw["titel"], emner, passages, as_of, texts), texts,
                    extracted or {})


def extracted_spans(decisions: Iterable[Decision], docs: Mapping[str, Doc],
                    texts: Texts) -> dict[str, list[tuple[int, int]]]:
    """Document id -> where the quotes of its extracted decisions stand."""
    found: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for d in decisions:
        if d.doc_id in docs:
            pos = analyze.locate_quote(d.citat, texts.words(docs[d.doc_id]), d.side)
            if pos is not None:
                found[d.doc_id].append(matching.quote_span(pos, d.citat))
    return found


# --------------------------------------------------------------------------- the rules key (Part A)

@dataclass(frozen=True)
class Event:
    """One event of a judge's timeline, its quote located inside its passage."""
    passage: Passage
    effect: str
    quote: str
    value_after: str | None
    pos: int  # word offset of the quote in the document

    @property
    def span(self) -> tuple[int, int] | None:
        return matching.quote_span(self.pos, self.quote)

    @property
    def content(self) -> tuple[str, tuple[str, ...]]:
        """What judges must agree on for the same event: its effect and the numbers of its value after."""
        return self.effect, numbers(self.value_after)

    def candidate(self, titel: str) -> Candidate:
        return Candidate(titel, self.value_after or self.quote, EFFECT_OUTCOME.get(self.effect, "vedtaget"),
                         self.span)


def events_of(answer: dict, passages: Mapping[str, Passage], texts: Texts) -> list[Event | None]:
    """A timeline's events. One whose passage is unknown, or whose quote is not inside that passage, is void
    (None): the judge did not read it there."""
    events: list[Event | None] = []
    for raw in answer["events"]:
        p = passages.get(raw["passage"])
        pos = None
        if p is not None:
            pos = analyze.locate_quote(raw["quote"], DocWords.of(texts.excerpt(p.doc, p.start, p.end)))
        events.append(None if pos is None else Event(p, raw["effect"], raw["quote"], raw["value_after"],
                                                     p.start + pos))
    return events


def adopting(run: list[Event | None], index: int) -> int | None:
    """The event of a judge's timeline that set the content in force with event `index`, like render.Rule.adopted:
    a confirmation leads back to the latest introduction or change before it, unless an abolition came between.
    None when that event is void."""
    if run[index].effect != "bekraeftet":
        return index
    for i in range(index - 1, -1, -1):
        e = run[i]
        if e is None:
            return None  # a void event may have been the change
        if e.effect in ("indfoert", "aendret"):
            return i
        if e.effect == "ophaevet":
            return index
    return index


def year_answers(answer: dict, run: list[Event | None], judge: int,
                 cluster_of: Mapping[tuple[int, int], int]) -> dict[int, tuple[int | str, int | None]]:
    """Year -> (the cluster of the event that set the content in force, the cluster of the event in force), or
    ("none", None), ("unknown", None), or ("?", None) for an answer that names no event the judge could place.
    Years before the judge's first listed year are "none" (the rule did not exist yet); a year skipped later is
    "?"."""
    found: dict[int, tuple[int | str, int | None]] = {}
    for entry in answer["years"]:
        n = entry["event"]
        if entry["state"] != "event":
            found[entry["year"]] = (entry["state"], None)
        elif n is not None and 1 <= n <= len(run) and run[n - 1] is not None \
                and (origin := adopting(run, n - 1)) is not None:
            found[entry["year"]] = (cluster_of[judge, origin], cluster_of[judge, n - 1])
        else:
            found[entry["year"]] = ("?", None)
    return found


@dataclass(frozen=True)
class RuleTimeline:
    """The judges' timelines of one rule, their events clustered across judges (by document and quote)."""
    task: RuleTask
    runs: list[list[Event | None]]  # per judge
    clusters: list[dict[int, int]]  # per event: judge -> index in that judge's events
    events: list[Resolution]  # per cluster; choice: (judge, index)
    years: dict[int, Resolution]  # choice: (adopting cluster, in-force cluster), ("none", None) or ("unknown", None)

    @classmethod
    def of(cls, task: RuleTask, answers: Sequence[dict]) -> RuleTimeline:
        passages = {p.id: p for p in task.passages}
        runs = [events_of(answer, passages, task.texts) for answer in answers]
        clusters = event_clusters(runs, task.titel, task.extracted)
        events = [resolve_event(c, runs) for c in clusters]
        cluster_of = {(g, i): k for k, c in enumerate(clusters) for g, i in c.items()}
        answered = [year_answers(answer, run, g, cluster_of) for g, (answer, run) in enumerate(zip(answers, runs))]
        firsts = [min(found) for found in answered if found]
        years = {}
        # Judges agree on a year when the same event set the content in force: one may list a confirmation the
        # other skipped. Which event was in force is kept from the judge whose answer counts (Resolution).
        for year in range(min(firsts), task.as_of.year + 1) if firsts else ():
            said = [found.get(year, ("none" if not found or year < min(found) else "?", None)) for found in answered]
            r = resolve(said, lambda a, b: a[0] == b[0] != "?")
            if r.status == "certain" and isinstance(r.choice[0], int) and events[r.choice[0]].status != "certain":
                r = Resolution("uncertain")  # the content comes from an event the judges do not agree on
            years[year] = r
        return cls(task, runs, clusters, events, years)

    @property
    def open(self) -> bool:
        return any(r.status == "open" for r in [*self.events, *self.years.values()])


def same_decision(a: Event, b: Event, extracted: Sequence[tuple[int, int]]) -> bool:
    """Two judges' events in one document are one decision, even when cited from different passages: their quotes
    overlap in the document, both stand inside one extracted decision's quote, or they give the rule the same
    amounts with the same effect (a meeting does not set one rule to the same amounts twice; a budget line and the
    proposal it refers to are one decision)."""
    if matching.quote_overlap(a.span, b.span) > 0:
        return True
    if any(_inside(a.span, span) and _inside(b.span, span) for span in extracted):
        return True
    return bool(a.content[1]) and a.content == b.content


def _inside(span: tuple[int, int] | None, outer: tuple[int, int]) -> bool:
    return span is not None and outer[0] <= span[0] and span[1] <= outer[1]


def event_clusters(runs: Sequence[list[Event | None]], titel: str,
                   extracted: Mapping[str, Sequence[tuple[int, int]]] | None = None) -> list[dict[int, int]]:
    """Events of several judges clustered per document (cluster()), in chronological order. Clusters of one
    document whose events are the same decision (same_decision) are then joined, never two events of one judge."""
    doc_ids = sorted({e.passage.doc.id for run in runs for e in run if e is not None})
    found = []
    for doc_id in doc_ids:
        index = [[i for i, e in enumerate(run) if e is not None and e.passage.doc.id == doc_id] for run in runs]
        clusters = [{g: index[g][k] for g, k in members.items()}
                    for members in cluster([[run[i].candidate(titel) for i in idx] for run, idx in zip(runs, index)])]
        found += _join_events(clusters, runs, (extracted or {}).get(doc_id, ()))

    def order(members: dict[int, int]) -> tuple:
        g, i = min(members.items())
        e = runs[g][i]
        return e.passage.doc.date or "", e.passage.doc.id, e.pos, g, i

    return sorted(found, key=order)


def _join_events(clusters: list[dict[int, int]], runs: Sequence[list[Event | None]],
                 extracted: Sequence[tuple[int, int]]) -> list[dict[int, int]]:
    joined = [dict(c) for c in clusters]
    while pair := next(((x, y) for x, y in combinations(range(len(joined)), 2)
                        if not joined[x].keys() & joined[y].keys()
                        and any(same_decision(runs[g][i], runs[h][j], extracted)
                                for g, i in joined[x].items() for h, j in joined[y].items())), None):
        x, y = pair
        joined[x].update(joined.pop(y))
    return joined


def resolve_event(members: dict[int, int], runs: Sequence[list[Event | None]]) -> Resolution:
    """Judges 1 and 2 having the event with the same effect and the same numbers in value_after is certain.
    Otherwise, once judge 3 has answered, two of three decide its content, or that there is no such event
    ("dropped"); else uncertain."""
    contents = [runs[g][members[g]].content if g in members else None for g in range(len(runs))]
    if contents[0] is not None and contents[0] == contents[1]:
        return Resolution("certain", (0, members[0]))
    if len(runs) < len(JUDGES):
        return Resolution("open")
    content, votes = Counter(contents).most_common(1)[0]
    if votes < 2:
        return Resolution("uncertain")
    if content is None:
        return Resolution("dropped")
    g = contents.index(content)
    return Resolution("certain", (g, members[g]))


def rules_key(task: RuleTask, answers: Sequence[dict]) -> dict:
    """The key for one rule: its events (E1… in chronological order) and, per year, what was in force: the event
    that set the content (adopted) and the event in force (event: the same, or a later confirmation), none, or
    unknown. A year is certain only when the judges agree on its adopting event and on that event's content."""
    timeline = RuleTimeline.of(task, answers)
    referenced = {k for r in timeline.years.values() if r.choice for k in r.choice if isinstance(k, int)}
    ids: dict[int, str] = {}
    events = []
    for k, (members, r) in enumerate(zip(timeline.clusters, timeline.events)):
        if r.status == "dropped" and k not in referenced:
            continue
        g, i = r.choice if r.status == "certain" else min(members.items())
        e = timeline.runs[g][i]
        ids[k] = f"E{len(events) + 1}"
        events.append({"id": ids[k], "status": "certain" if r.status == "certain" else "uncertain",
                       "passage": e.passage.id, "doc": e.passage.doc.id, "date": e.passage.doc.date,
                       "page": e.passage.page, "effect": e.effect, "quote": e.quote, "value_after": e.value_after,
                       "span": e.span, "judges": sorted(g + 1 for g in members)})
    by_id = {entry["id"]: entry for entry in events}
    years = []
    for year, r in timeline.years.items():
        certain = r.status == "certain"
        adopted, in_force = r.choice if certain else (None, None)
        state = "event" if isinstance(adopted, int) else adopted
        adopted_id = ids.get(adopted) if state == "event" else None
        years.append({"year": year, "status": "certain" if certain else "uncertain", "state": state,
                      "adopted": adopted_id, "event": ids.get(in_force) if state == "event" else None,
                      "value": by_id[adopted_id]["value_after"] if adopted_id else None})
    return {"slug": task.slug, "kategori": task.kategori, "titel": task.titel, "as_of": task.as_of.isoformat(),
            "keywords": list(task.keywords), "passages": [p.entry() for p in task.passages],
            "unread_amounts": task.unread, "events": events, "years": years}


# --------------------------------------------------------------------------- corrections

# What a correction may change, per kind. Corrections are kept in eval/key/corrections.json, each with its reason and
# evidence from the documents, and applied on top of the judges whenever a key is written or scored.
CORRECTABLE = {
    "rule-event": frozenset({"status", "effect_status", "effect", "value_after"}),
    "rule-year": frozenset({"status", "state", "adopted", "event", "value"}),
    "decision": frozenset({"status", "uncertain_fields", *CODED_FIELDS}),
}


def corrections_path() -> Path:
    return EVAL_DIR / "key" / "corrections.json"


def load_corrections() -> list[dict]:
    """eval/key/corrections.json: [{"kind", "target", "change", "reason", "evidence": [{"doc", "quote"}]}]. A rule
    event is targeted by {"rule", "doc", "quote"}, rule years by {"rule", "years"}, a key decision by {"doc", "id"}
    or {"doc", "quote"}: quotes, not event numbers, which change when a key is judged again. Checked, so a
    correction the code cannot apply fails instead of being ignored."""
    path = corrections_path()
    if not path.exists():
        return []
    corrections = read_json(path)
    for n, c in enumerate(corrections, start=1):
        allowed = CORRECTABLE.get(c.get("kind"))
        if allowed is None or not c.get("reason") or not c.get("change") or set(c["change"]) - allowed:
            raise SystemExit(f"{path}: correction {n} needs a kind ({', '.join(CORRECTABLE)}), a reason and a change "
                             f"of {', '.join(sorted(allowed or ()))}")
    return corrections


def _quoted(quote: str | None, target: str) -> bool:
    """The target names this quote: one holds the other, ignoring case, punctuation and spacing."""
    a, b = " ".join(tokens(quote or "")), " ".join(tokens(target))
    return bool(a and b) and (b in a or a in b)


def correct_rule_key(key: dict, corrections: Sequence[dict]) -> dict:
    """The rule key with the corrections of its rule applied, each listed under "corrections" with what it matched
    (nothing: the key has changed since, and the reports say so). Applying them again changes nothing."""
    key = json.loads(json.dumps(key))
    applied = []
    for c in corrections:
        target = c["target"]
        if c["kind"] not in ("rule-event", "rule-year") or target.get("rule") != key["slug"]:
            continue
        if c["kind"] == "rule-event":
            items = [e for e in key["events"] if e["doc"] == target["doc"] and _quoted(e["quote"], target["quote"])]
            matched = [e["id"] for e in items]
        else:
            items = [y for y in key["years"] if y["year"] in target["years"]]
            matched = [y["year"] for y in items]
        for item in items:
            item.update(c["change"])
        applied.append({**c, "matched": matched})
    key["corrections"] = applied
    return key


def correct_decisions_key(key: dict, corrections: Sequence[dict]) -> dict:
    """The decisions key with the corrections of its document applied (see correct_rule_key)."""
    key = json.loads(json.dumps(key))
    applied = []
    for c in corrections:
        target = c["target"]
        if c["kind"] != "decision" or target.get("doc") != key["doc_id"]:
            continue
        items = [d for d in key["decisions"]
                 if d["id"] == target.get("id") or ("quote" in target and _quoted(d["citat"], target["quote"]))]
        for item in items:
            item.update(c["change"])
        applied.append({**c, "matched": [d["id"] for d in items]})
    key["corrections"] = applied
    return key


def corrections_section(applied: Iterable[dict]) -> list[str]:
    """Markdown listing the corrections a report's keys carry, and those that matched nothing."""
    lines = ["## Corrections applied", "",
             "Changes on top of the judges' answers (eval/key/corrections.json), each checked against the minutes.", ""]
    for c in applied:
        target = ", ".join(f"{k} {v}" for k, v in c["target"].items() if k != "quote")
        if "quote" in c["target"]:
            target += f' "{c["target"]["quote"]}"'
        matched = ", ".join(map(str, c["matched"])) or "NOTHING: the key has changed, check the correction"
        evidence = "; ".join(f'{e["doc"]}: "{e["quote"]}"' for e in c.get("evidence", []))
        lines.append(f"- {c['kind']} {target} ({matched}): {json.dumps(c['change'], ensure_ascii=False)}. "
                     f"{c['reason']}" + (f" Evidence: {evidence}." if evidence else ""))
    return lines + ([] if lines[-1] else ["None."])


# --------------------------------------------------------------------------- scoring decisions (Part B)

@dataclass(frozen=True)
class DocScore:
    """One run on one document against the key."""
    key_decisions: int  # certain decisions in the key
    found: int  # key decisions the run has
    false: int  # run decisions the key certainly rejects
    extra: int  # run decisions beyond the first on a key decision (over-split)
    split: int  # key decisions with more than one run decision
    ignored: int  # run decisions on something the key is uncertain about
    unjudged: int  # run decisions on nothing in the key's pool (no candidate run found them, no judge added them)
    correct: Mapping[str, int] = field(default_factory=dict)  # coded field (or "all") -> right among `judged`
    judged: Mapping[str, int] = field(default_factory=dict)  # coded field (or "all") -> found decisions it is certain

    def __add__(self, other: DocScore) -> DocScore:
        counts = {f.name: getattr(self, f.name) + getattr(other, f.name) for f in fields(self)
                  if f.name not in ("correct", "judged")}
        return DocScore(**counts, correct=dict(Counter(self.correct) + Counter(other.correct)),
                        judged=dict(Counter(self.judged) + Counter(other.judged)))


ZERO = DocScore(0, 0, 0, 0, 0, 0, 0)


@dataclass(frozen=True)
class KeyItem:
    role: str  # "decision", "reject" or "ignored"
    name: str
    decision: dict | None = None  # the judged fields of a certain decision


def key_probes(key: dict) -> list[tuple[KeyItem, Candidate]]:
    """What run decisions are matched against: each certain key decision by its judged fields, every judge's quote
    and every candidate decision behind it (its own and its duplicates', so another run's wording still finds it),
    each certain reject by its candidate decisions, and each uncertain item likewise (matches there are ignored)."""
    probes: list[tuple[KeyItem, Candidate]] = []
    members = {c["number"]: c["members"] for c in key["candidates"]}

    def from_member(m: dict) -> Candidate:
        return Candidate(m["emne"], m["tekst"], m["udfald"], tuple(m["span"]) if m["span"] else None)

    for d in key["decisions"]:
        item = KeyItem("decision", d["id"], d) if d["status"] == "certain" else KeyItem("ignored", d["id"])
        probes.append((item, decision_candidate(d)))
        probes += [(item, decision_candidate({**d, **quote})) for quote in d.get("quotes", [])]
        probes += [(item, from_member(m)) for n in d["candidates"] for m in members[n]]
    for c in key["candidates"]:
        if c["role"] != "decision":
            item = KeyItem(c["role"], f"C{c['number']}")
            probes += [(item, from_member(m)) for m in c["members"]]
    return probes


def score_document(key: dict, decisions: list[dict]) -> DocScore:
    """Match a run's decisions to the key (matching.match_decisions, one run decision per probe) and count.

    A key decision matched by several run decisions is found once, by the best match; the others are over-split.
    Field accuracy counts only the fields the judges agreed on.
    """
    probes = key_probes(key)
    matches = matching.match_decisions([c for _, c in probes], [decision_candidate(d) for d in decisions])
    hits: dict[str, list[tuple[float, int]]] = defaultdict(list)
    items: dict[str, KeyItem] = {}
    for m in matches:
        item = probes[m.old][0]
        items[item.name] = item
        hits[item.name].append((m.score, m.new))
    found = false = extra = split = ignored = 0
    correct: Counter[str] = Counter()
    judged: Counter[str] = Counter()
    for name, matched in hits.items():
        item = items[name]
        if item.role == "decision":
            best = decisions[min(matched, key=lambda h: (-h[0], h[1]))[1]]
            found += 1
            extra += len(matched) - 1
            split += len(matched) > 1
            certain = [f for f in CODED_FIELDS if f not in item.decision.get("uncertain_fields", ())]
            for f in certain:
                judged[f] += 1
                correct[f] += best[f] == item.decision[f]
            if len(certain) == len(CODED_FIELDS):
                judged["all"] += 1
                correct["all"] += all(best[f] == item.decision[f] for f in CODED_FIELDS)
            if set(FIRM_FIELDS) <= set(certain):
                judged["three"] += 1
                correct["three"] += all(best[f] == item.decision[f] for f in FIRM_FIELDS)
        elif item.role == "reject":
            false += len(matched)
        else:
            ignored += len(matched)
    key_decisions = sum(d["status"] == "certain" for d in key["decisions"])
    return DocScore(key_decisions, found, false, extra, split, ignored, len(decisions) - len(matches),
                    dict(correct), dict(judged))


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


METRICS: dict[str, Callable[[DocScore], float | None]] = {
    "recall": lambda s: _ratio(s.found, s.key_decisions),
    "precision": lambda s: _ratio(s.found, s.found + s.false),
    "over_split": lambda s: _ratio(s.extra, s.found),
    **{f"field_{f}": (lambda s, f=f: _ratio(s.correct.get(f, 0), s.judged.get(f, 0)))
       for f in (*CODED_FIELDS, "all", "three")},
}


def total(scores: Iterable[DocScore]) -> DocScore:
    return sum(scores, ZERO)


def per_document(scores: Sequence[DocScore], metric: Callable[[DocScore], float | None]) -> float | None:
    """The metric averaged over documents, each counting once: pooled figures lean on the largest documents."""
    values = [v for s in scores if (v := metric(s)) is not None]
    return statistics.mean(values) if values else None


def bootstrap(a: Sequence[DocScore], b: Sequence[DocScore], metric: Callable[[DocScore], float | None],
              samples: int = BOOTSTRAP_SAMPLES, seed: int = SEED) -> dict | None:
    """Paired bootstrap over documents of metric(a) - metric(b): each resample takes the same documents for both
    runs. The observed difference, a 95% interval and the share of resamples where a is ahead; seeded."""
    observed_a, observed_b = metric(total(a)), metric(total(b))
    if observed_a is None or observed_b is None:
        return None
    rng = random.Random(seed)
    diffs = []
    for _ in range(samples):
        picks = [rng.randrange(len(a)) for _ in range(len(a))]
        ma, mb = metric(total(a[i] for i in picks)), metric(total(b[i] for i in picks))
        if ma is not None and mb is not None:
            diffs.append(ma - mb)
    if not diffs:
        return None
    diffs.sort()
    return {"difference": observed_a - observed_b, "low": diffs[int(0.025 * (len(diffs) - 1))],
            "high": diffs[int(0.975 * (len(diffs) - 1))], "ahead": sum(d > 0 for d in diffs) / len(diffs)}


def _pct(value: float | None) -> str:
    return "–" if value is None else f"{100 * value:.1f}%"


def decisions_report(scores: Mapping[str, list[DocScore]], doc_ids: list[str], keys: Mapping[str, dict],
                     seed: int, warnings: Sequence[str] = ()) -> str:
    """Markdown: per run the metrics pooled and averaged per document, median and spread over runs, a paired
    bootstrap of each run against the first, and how often the judges kept decisions only one run found."""
    certain = sum(d["status"] == "certain" for k in keys.values() for d in k["decisions"])
    uncertain = sum(d["status"] != "certain" for k in keys.values() for d in k["decisions"])
    rejects = sum(c["role"] == "reject" for k in keys.values() for c in k["candidates"])
    pool = sorted({run for k in keys.values() for run in k["runs"]})
    outside = [run for run in scores if run not in pool]
    lines = ["# Decisions against the answer key", "",
             f"{len(doc_ids)} documents; the key has {certain} certain decisions, {uncertain} uncertain ones (not "
             f"scored) and {rejects} certainly rejected candidates, judged from the runs {', '.join(pool)}.", "",
             "Recall: key decisions found. Precision: found / (found + run decisions the key rejects); it counts "
             "only run decisions in the key's pool, and the others are Unjudged. Over-split: extra run decisions on "
             "a key decision already found, per found decision. Fields: share of found decisions with the key's "
             "value, among those whose value the judges agreed on; handling often needs the rule's history, so the "
             "three fields without it are given too. Doc avg: averaged over documents.", ""]
    if outside:
        lines += [f"Not candidate runs: {', '.join(outside)}. The judges never saw their decisions, so those no "
                  f"candidate run had are unjudged, and their precision covers only the rest.", ""]
    lines += [f"Warning: {w}" for w in warnings] + ([""] if warnings else [])
    lines += ["| Run | Recall | Recall (doc avg) | Precision | Precision (doc avg) | Over-split | Kategori | Udfald | "
              "Handling | Niveau | All four | Three (no handling) | Found | False | Extra | Ignored | Unjudged |",
              "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    totals = {run: total(found) for run, found in scores.items()}
    for run, s in totals.items():
        values = [_pct(METRICS["recall"](s)), _pct(per_document(scores[run], METRICS["recall"])),
                  _pct(METRICS["precision"](s)), _pct(per_document(scores[run], METRICS["precision"]))]
        values += [_pct(METRICS[m](s)) for m in METRICS if m not in ("recall", "precision")]
        lines.append(f"| {run} | {' | '.join(values)} | {s.found} | {s.false} | {s.extra} | {s.ignored} | "
                     f"{s.unjudged} |")
    if len(totals) > 1:
        cells = []
        for m in ("recall", "recall_doc", "precision", "precision_doc", *list(METRICS)[2:]):
            if m.endswith("_doc"):
                values = [v for run in scores if (v := per_document(scores[run], METRICS[m[:-4]])) is not None]
            else:
                values = [v for s in totals.values() if (v := METRICS[m](s)) is not None]
            cells.append(f"{_pct(statistics.median(values))} ({_pct(min(values))}–{_pct(max(values))})"
                         if values else "–")
        lines.append(f"| median (min–max) | {' | '.join(cells)} | | | | | |")
        base, *others = scores
        lines += ["", f"## Paired bootstrap against {base}", "",
                  f"{BOOTSTRAP_SAMPLES} resamples of the documents, seed {seed}.", "",
                  "| Run | Metric | Difference | 95% interval | Resamples ahead |", "|---|---|--:|--:|--:|"]
        for run in others:
            for m in ("recall", "precision", "over_split", "field_all", "field_three"):
                b = bootstrap(scores[run], scores[base], METRICS[m], seed=seed)
                if b:
                    lines.append(f"| {run} | {m} | {100 * b['difference']:+.1f} | {100 * b['low']:+.1f} to "
                                 f"{100 * b['high']:+.1f} | {100 * b['ahead']:.0f}% |")
    solo = solo_candidates(keys.values())
    lines += ["", "## Candidates only one run found", "",
              "How many the judges kept; a judge favouring one model would keep its lone decisions more often.", ""]
    lines += [f"- {run}: {kept} of {n} kept" for run, (kept, n) in solo.items()] or ["None."]
    lines += ["", "## Per document", "", "| Document | " + " | ".join(scores) + " |",
              "|---|" + "--:|" * len(scores)]
    for k, doc_id in enumerate(doc_ids):
        cells = [f"{s[k].found}/{s[k].key_decisions} found, {s[k].false} false" for s in scores.values()]
        lines.append(f"| {doc_id} | {' | '.join(cells)} |")
    lines += ["", *corrections_section(c for key in keys.values() for c in key.get("corrections", []))]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- scoring rules (Part A)

@dataclass(frozen=True)
class RuleScore:
    slug: str
    home: str | None  # the pipeline rule holding most of the key's certain events
    events: int  # certain key events
    found: int  # in the home rule
    elsewhere: int  # in another pipeline rule
    missing: int  # not extracted, or in no rule
    extra: int  # versions of the home rule that are no key event
    effects: int  # found events whose version has the key's effect, among those whose effect is certain
    effect_events: int  # found events whose effect is certain (not corrected to uncertain)
    rules: int  # pipeline rules holding certain key events: more than one is fragmentation
    years: int  # certain years with a known state
    same_event: int  # years the pipeline shows the key's event in force
    same_content: int  # years the pipeline's version in force was adopted by the key's adopting event
    disagreements: tuple[str, ...]  # years where the content differs, for the report


def map_events(key: dict, decisions_by_doc: Mapping[str, list[Decision]], docs: Mapping[str, Doc],
               texts: Texts) -> dict[str, str]:
    """Key event id -> the pipeline decision it is, matched (match_decisions) among the decisions of its document."""
    mapped: dict[str, str] = {}
    by_doc: dict[str, list[dict]] = defaultdict(list)
    for e in key["events"]:
        by_doc[e["doc"]].append(e)
    for doc_id, events in by_doc.items():
        pipeline = decisions_by_doc.get(doc_id, [])
        if not pipeline or doc_id not in docs:
            continue
        words = texts.words(docs[doc_id])
        found = [Candidate(d.emne, d.tekst, d.udfald, matching.quote_span(analyze.locate_quote(d.citat, words, d.side),
                                                                          d.citat)) for d in pipeline]
        wanted = [Candidate(key["titel"], e["value_after"] or e["quote"], EFFECT_OUTCOME.get(e["effect"], "vedtaget"),
                            tuple(e["span"]) if e["span"] else None) for e in events]
        for m in matching.match_decisions(wanted, found):
            mapped[events[m.old]["id"]] = pipeline[m.new].ref
    return mapped


def score_rule(key: dict, decisions: list[Decision], raw_rules: list[dict], docs: Mapping[str, Doc],
               texts: Texts) -> RuleScore:
    """Compare a pipeline's rules with one rule of the key. The key's rule is the pipeline rule holding most of its
    certain events (whatever its slug, so another consolidation can be scored). Per certain year with a known
    state, the pipeline's version in force (render.Rule.in_force at the year cutoff) is compared with the key's: by
    the decision that adopted its content (Same content, the main measure) and, stricter, by the decision itself
    (Same event, which also needs every confirmation)."""
    decisions_by_doc: dict[str, list[Decision]] = defaultdict(list)
    for d in decisions:
        decisions_by_doc[d.doc_id].append(d)
    mapped = map_events(key, decisions_by_doc, docs, texts)
    live = analyze.live_rules(raw_rules)
    certain = [e for e in key["events"] if e["status"] == "certain"]
    home = matching.holder(frozenset(mapped[e["id"]] for e in certain if e["id"] in mapped), live)
    rule_of = {ref: rule.slug for rule in live for ref in rule.refs}
    found = [e for e in certain if home and mapped.get(e["id"]) in home.refs]
    elsewhere = [e for e in certain if e not in found and rule_of.get(mapped.get(e["id"], "")) is not None]
    raw_home = next((raw for raw in raw_rules if home and raw.get("slug") == home.slug), None)
    effects = {v["ref"]: v["effekt"] for v in raw_home["versioner"]} if raw_home else {}
    holders = {rule_of[mapped[e["id"]]] for e in certain if mapped.get(e["id"]) in rule_of}
    key_refs = set(mapped.values())

    by_ref = {d.ref: d for d in decisions}
    built = render.build_rules([raw_home], by_ref) if raw_home else []
    rule = built[0] if built else None  # None: no such rule, or not shown (stale)
    as_of = date.fromisoformat(key["as_of"])
    years = [y for y in key["years"] if y["status"] == "certain" and y["state"] in ("event", "none")]
    same_event = same_content = 0
    disagreements = []
    for y in years:
        cutoff = render.year_cutoff(y["year"], as_of)
        shown = rule.in_force(cutoff) if rule else None
        if y["adopted"] is None:
            agree = content = shown is None
        else:
            agree = shown is not None and shown.decision.ref == mapped.get(y["event"])
            content = shown is not None and rule.adopted(shown, cutoff).decision.ref == mapped.get(y["adopted"])
        same_event += agree
        same_content += content
        if not content:
            said = f"{y['adopted']} ({y['value'] or 'no value'})" if y["adopted"] else "none (not in force)"
            disagreements.append(f"{y['year']}: key {said}, pipeline {shown.decision.ref if shown else 'none'}")
    effect_known = [e for e in found if e.get("effect_status", "certain") == "certain"]
    return RuleScore(key["slug"], home.slug if home else None, len(certain), len(found), len(elsewhere),
                     len(certain) - len(found) - len(elsewhere), sum(1 for ref in effects if ref not in key_refs),
                     sum(effects.get(mapped[e["id"]]) == e["effect"] for e in effect_known), len(effect_known),
                     len(holders), len(years), same_event, same_content, tuple(disagreements))


def rules_report(scores: list[RuleScore], rules_dir: Path, soft: Mapping[str, str] | None = None,
                 corrections: Iterable[dict] = ()) -> str:
    """Markdown: per rule the events, fragmentation and years; the totals and the summary leave out the soft rules
    (`soft`: slug -> why), whose events depend on how the rule is scoped."""
    soft = soft or {}
    lines = ["# Rules against the answer key", "", f"Pipeline rules from {rules_dir}.", "",
             "Found: certain key events in the pipeline rule holding most of them (Home); Elsewhere: in another "
             "rule; Missing: not extracted or in no rule; Extra: versions of the home rule that are no key event; "
             "Rules: pipeline rules holding key events (more than one: fragmented). Per year with a known state: "
             "the version in force is the key's event (Same event), or was adopted by the key's adopting event "
             "(Same content). Years the key leaves unknown or uncertain are not scored.", "",
             "| Rule | Home | Events | Found | Elsewhere | Missing | Extra | Effects agree | Rules | Years | "
             "Same event | Same content |", "|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    for s in scores:
        name = f"{s.slug} (soft)" if s.slug in soft else s.slug
        lines.append(f"| {name} | {s.home or '–'} | {s.events} | {s.found} | {s.elsewhere} | {s.missing} | "
                     f"{s.extra} | {s.effects} | {s.rules} | {s.years} | {s.same_event} | {s.same_content} |")
    gated = [s for s in scores if s.slug not in soft]
    t = rules_summary(gated)
    lines += [f"| **all** | | {t['events']} | {t['found']} | {t['elsewhere']} | {t['missing']} | {t['extra']} | "
              f"{t['effects']} | | {t['years']} | {t['same_event']} | {t['same_content']} |", "",
              f"Events found: {_pct(t['found_share'])}; effects agree: {_pct(t['effect_share'])}; fragmented rules: "
              f"{t['fragmented']} of {len(gated)}; years with the same content in force: {_pct(t['content_share'])} "
              f"(the main measure), with the same event: {_pct(t['event_share'])}.", ""]
    if soft:
        lines += ["Soft rules are reported but left out of the totals and the summary: "
                  + "; ".join(f"{slug}: {why}" for slug, why in soft.items()) + ".", ""]
    lines += ["## Years where the content differs", ""]
    lines += [f"- {s.slug} {d}" for s in scores for d in s.disagreements] or ["None."]
    lines += ["", *corrections_section(corrections)]
    return "\n".join(lines) + "\n"


def rules_summary(scores: list[RuleScore]) -> dict:
    t = {name: sum(getattr(s, name) for s in scores)
         for name in ("events", "found", "elsewhere", "missing", "extra", "effects", "effect_events", "years",
                      "same_event", "same_content")}
    return t | {"found_share": _ratio(t["found"], t["events"]),
                "effect_share": _ratio(t["effects"], t["effect_events"]),
                "fragmented": sum(s.rules > 1 for s in scores), "event_share": _ratio(t["same_event"], t["years"]),
                "content_share": _ratio(t["same_content"], t["years"])}


# --------------------------------------------------------------------------- incremental consolidation: gates

@dataclass(frozen=True)
class Hidden:
    """One decision hidden from its rule, and where its rule ranked among the candidates built from the rest: with
    the decision's own category, and with it relabelled to another category (an extraction that got it wrong)."""
    ref: str
    slug: str
    category: str
    rank: int  # 1 = the best candidate
    relabelled: int | None = None


def relabel(category: str) -> str:
    """The category a hidden decision is relabelled to: the next one in CATEGORIES (the first after the last)."""
    order = list(analyze.CATEGORIES)
    return order[(order.index(category) + 1) % len(order)]


def hide_one_ranks(raw_rules: list[dict], decisions: list[Decision]) -> list[Hidden]:
    """For each decision of a rule with other decisions too, the rank of its rule among all live rules when it is
    hidden from that rule: the ranking a new decision of that rule would get. A rule's only decision is left out:
    hidden, the right answer is a new rule, which no ranking offers.

    The vocabulary for compound words is that of all rules, the hidden decision's emne included; it only decides
    which words are split, so the measure is a little optimistic there."""
    by_ref = {d.ref: d for d in decisions}
    profiles = {raw["slug"]: incremental.rule_profile(raw["kategori"], raw, by_ref) for raw in raw_rules
                if analyze.has_slug(raw)}
    vocab = candidates.vocabulary(text for p in profiles.values() for text in p.texts())
    counts = {slug: p.terms(vocab) for slug, p in profiles.items()}
    categories = {slug: p.category for slug, p in profiles.items()}
    found = []
    for raw in raw_rules:
        live = [v for v in raw["versioner"] if v["ref"] in by_ref]
        if raw.get("slug") not in profiles or len(live) < 2:
            continue
        for v in live:
            rest = {**raw, "versioner": [w for w in raw["versioner"] if w is not v]}
            index = candidates.CandidateIndex({**counts, raw["slug"]: incremental.rule_profile(
                raw["kategori"], rest, by_ref).terms(vocab)}, categories, vocab)
            q = incremental.query(by_ref[v["ref"]])
            ranks = [[slug for slug, _ in index.rank(each)].index(raw["slug"]) + 1
                     for each in (q, replace(q, category=relabel(q.category)))]
            found.append(Hidden(v["ref"], raw["slug"], raw["kategori"], *ranks))
    return found


def recall_at(hidden: Sequence[Hidden], k: int, relabelled: bool = False) -> float | None:
    ranks = [h.relabelled if relabelled else h.rank for h in hidden]
    return _ratio(sum(rank is not None and rank <= k for rank in ranks), len(hidden))


def choose_k(hidden: Sequence[Hidden]) -> int | None:
    """The smallest K of RECALL_KS whose recall reaches RECALL_TARGET, None when none does."""
    return next((k for k in RECALL_KS if (recall_at(hidden, k) or 0) >= RECALL_TARGET), None)


def recall_report(hidden: Sequence[Hidden], single: int) -> str:
    chosen = choose_k(hidden)
    k = chosen or RECALL_KS[-1]
    header = " | ".join(f"@{n}" for n in RECALL_KS)
    lines = ["# Candidate recall", "",
             "Each decision of today's rules is hidden from its rule, and the candidates for it are ranked from the "
             f"rest (candidates.py: TF-IDF over Danish stems, category boost {candidates.CATEGORY_BOOST}). Recall@K: "
             f"the share whose rule is among the K best. {single} decisions are their rule's only one and are left out "
             "(hidden, the right answer is a new rule). Relabelled: the same, with the decision's category changed to "
             "the next one (an extraction that got the category wrong); today's rules never mix categories, so this "
             "is how much the category boost carries.", "",
             f"| Category | Decisions | {header} | @{k} relabelled |", f"|---|--:|{'--:|' * (len(RECALL_KS) + 1)}"]
    groups = [("**all**", list(hidden))] + [(c, [h for h in hidden if h.category == c]) for c in analyze.CATEGORIES]
    for name, group in groups:
        if group:
            cells = " | ".join(_pct(recall_at(group, n)) for n in RECALL_KS)
            lines.append(f"| {name} | {len(group)} | {cells} | {_pct(recall_at(group, k, relabelled=True))} |")
    verdict = (f"Chosen K: {chosen}, the smallest with at least {RECALL_TARGET:.0%} overall." if chosen else
               f"No K of {', '.join(map(str, RECALL_KS))} reaches {RECALL_TARGET:.0%}.")
    if chosen and chosen != candidates.CANDIDATE_K:
        verdict += f" candidates.CANDIDATE_K is {candidates.CANDIDATE_K}: set it to {chosen}."
    misses = sorted((h for h in hidden if h.rank > k), key=lambda h: (-h.rank, h.ref))
    lines += ["", verdict, "", f"## Ranked below {k}", ""]
    lines += [f"- {h.ref} in {h.slug} ({h.category}): rank {h.rank}" for h in misses] or ["None."]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Holdout:
    """The documents a replay withholds, per part of its spec: "newest:20" tests filing new minutes, "random:10" mostly
    older documents, which land before rules' newest versions (late insertion)."""
    spec: str  # e.g. "newest:20,random:10"
    seed: int
    parts: tuple[tuple[str, tuple[str, ...]], ...]  # (part, its documents), in the spec's order

    @property
    def documents(self) -> tuple[str, ...]:
        return tuple(doc_id for _, docs in self.parts for doc_id in docs)


def holdout_parts(spec: str, decisions: Sequence[Decision], seed: int) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """The documents each part of a holdout spec names, among those with decisions: "newest:N" the N newest (by meeting
    date, then id), "random:N" N drawn with the seed from those not chosen yet; parts are taken in order."""
    dates: dict[str, str] = {}
    for d in decisions:
        dates[d.doc_id] = max(dates.get(d.doc_id, ""), d.dato or "")
    newest_first = sorted(dates, key=lambda doc_id: (dates[doc_id], doc_id), reverse=True)
    chosen: list[str] = []
    parts = []
    for part in filter(None, spec.split(",")):
        kind, _, count = part.partition(":")
        if kind not in ("newest", "random") or not count.isdigit():
            raise SystemExit(f"--holdout {spec!r}: give parts like newest:20 or random:10, separated by commas")
        rest = [doc_id for doc_id in newest_first if doc_id not in chosen]
        n = min(int(count), len(rest))
        picked = rest[:n] if kind == "newest" else sorted(random.Random(f"{seed}:{part}").sample(sorted(rest), n))
        chosen += picked
        parts.append((part, tuple(picked)))
    return tuple(parts)


def withhold(files: Mapping[str, dict], registry: matching.SlugRegistry,
             refs: Collection[str]) -> tuple[dict[str, dict], matching.SlugRegistry]:
    """The rule files and slug history as if the decisions `refs` had never been consolidated: their versions,
    one-off and unassigned entries gone, rules left without versions gone with their slugs, and former slugs that
    stood for nothing but those decisions forgotten. Later versions keep their texts: a replay that files the
    decisions again rewrites them from where they belong."""
    out = {}
    for category, stored in files.items():
        rules = []
        for rule in stored["regler"]:
            versions = [v for v in rule["versioner"] if v["ref"] not in refs]
            if versions:
                rules.append({**rule, "versioner": versions})
        out[category] = {**stored, "regler": rules,
                         **{key: [ref for ref in stored.get(key, []) if ref not in refs]
                            for key in ("udeladt", "ikke_tildelt")}}
    kept = {slug: f for slug, f in registry.former().items() if not f.refs or not f.refs <= set(refs)}
    return out, matching.SlugRegistry.of(kept)


def replay_dir(name: str) -> Path:
    return EVAL_DIR / "replays" / name


@contextmanager
def data_paths(root: Path) -> Iterator[None]:
    """Point analyze's decisions, rules and slug history at a copy of data/ under eval/ while a replay runs: the
    consolidation writes where these lead, and must never write data/."""
    if not root.resolve().is_relative_to(EVAL_DIR.resolve()):
        raise ValueError(f"{root} is outside {EVAL_DIR}; a replay runs on a copy there")
    saved = analyze.DECISIONS_DIR, analyze.RULES_DIR, analyze.SLUGS_PATH
    analyze.DECISIONS_DIR, analyze.RULES_DIR, analyze.SLUGS_PATH = (root / "beslutninger", root / "regler",
                                                                    root / "slugs.json")
    try:
        yield
    finally:
        analyze.DECISIONS_DIR, analyze.RULES_DIR, analyze.SLUGS_PATH = saved


def prepare_replay(name: str, holdout: Holdout, mode: str, force: bool) -> Path:
    """eval/replays/<name>/data: a copy of data/'s decisions, rules and slug history without the held-out documents'
    decisions in the rules. A copy made for the same holdout and mode is kept, so a cut-off replay continues; another
    one needs --force. For `full`, the categories of the held-out decisions get no input_hash, so they are
    consolidated anew, as a monthly full run would."""
    root = replay_dir(name)
    meta_path = root / "holdout.json"
    meta = {"spec": holdout.spec, "seed": holdout.seed, "mode": mode, "documents": list(holdout.documents),
            "parts": {part: list(docs) for part, docs in holdout.parts}}
    if meta_path.exists():
        stored = read_json(meta_path)
        if {k: stored.get(k) for k in meta} == meta:
            print(f"Continuing the replay in {root}", flush=True)
            return root / "data"
        if not force:
            raise SystemExit(f"{root} holds a replay of another holdout or mode; pass --force to replace it")
    data = root / "data"
    if root.exists():
        shutil.rmtree(root)
    decisions = analyze.load_decisions(scrape.load_manifest())
    held = {d.ref for d in decisions if d.doc_id in holdout.documents}
    files = {path.stem: read_json(path) for path in sorted(analyze.RULES_DIR.glob("*.json"))}
    files, registry = withhold(files, analyze.load_slugs(), held)
    if mode == "full":
        stale = {d.kategori for d in decisions if d.ref in held}
        files = {c: {**stored, "input_hash": None} if c in stale else stored for c, stored in files.items()}
    for path in sorted(analyze.DECISIONS_DIR.glob("*.json")):
        write_text(data / "beslutninger" / path.name, path.read_text())
    for category, stored in files.items():
        write_text(data / "regler" / f"{category}.json", json.dumps(stored, ensure_ascii=False, indent=1) + "\n")
    with data_paths(data):
        analyze._save_slugs(registry)
    write_json(meta_path, {**meta, "withheld_decisions": len(held), "time": datetime.now(timezone.utc)
                           .isoformat(timespec="seconds")})
    return data


def side_decisions(rules_dir: Path, decisions_dir: Path | None) -> Path:
    """The decisions a rules directory was built from: --decisions-dir, else the beslutninger/ next to it (a replay's
    copy, or data/), else data/beslutninger."""
    if decisions_dir is not None:
        return decisions_dir
    sibling = rules_dir.parent / "beslutninger"
    return sibling if sibling.is_dir() else analyze.DECISIONS_DIR


def clusters_of(raw_rules: list[dict], live: Collection[str]) -> dict[str, frozenset[str]]:
    """Each current decision in a rule -> the current decisions of that rule (itself included)."""
    found = {}
    for raw in raw_rules:
        refs = frozenset(v["ref"] for v in raw["versioner"] if v["ref"] in live)
        for ref in refs:
            found.setdefault(ref, refs)
    return found


def bcubed(a: Mapping[str, frozenset[str]], b: Mapping[str, frozenset[str]],
           items: Iterable[str]) -> tuple[float | None, float | None, float | None]:
    """B-cubed precision, recall and F1 of grouping A against grouping B over the items: per item, the share of its
    A-cluster that shares its B-cluster (precision) and the reverse (recall). A decision in no rule is a cluster of
    its own."""
    precision, recall = [], []
    for ref in items:
        ca, cb = a.get(ref, frozenset({ref})), b.get(ref, frozenset({ref}))
        precision.append(len(ca & cb) / len(ca))
        recall.append(len(ca & cb) / len(cb))
    if not precision:
        return None, None, None
    p, r = statistics.fmean(precision), statistics.fmean(recall)
    return p, r, 2 * p * r / (p + r) if p + r else 0.0


def in_force(rules: list[render.Rule], cutoffs: Sequence[str]) -> dict[str, tuple[frozenset[str], frozenset[str]]]:
    """Per cutoff, the decisions in force (one per rule in force) and the decisions that adopted their content."""
    found = {}
    for cutoff in cutoffs:
        shown = [(rule, v) for rule in rules if (v := rule.in_force(cutoff)) is not None]
        found[cutoff] = (frozenset(v.decision.ref for _, v in shown),
                         frozenset(rule.adopted(v, cutoff).decision.ref for rule, v in shown))
    return found


def jaccard(a: frozenset[str], b: frozenset[str]) -> tuple[int, int]:
    """(shared, either): pooled over years, their sums give the agreement."""
    return len(a & b), len(a | b)


@dataclass(frozen=True)
class Agreement:
    """How far two consolidations agree, over all decisions or those of some documents."""
    decisions: int  # in a rule on either side
    bcubed: tuple[float | None, float | None, float | None]  # A against B
    # Per cutoff: ((same decision in force, decisions in force on either side), (same adopting decision, either)),
    # counting the rules that hold one of the decisions.
    years: Mapping[str, tuple[tuple[int, int], tuple[int, int]]]
    effects: tuple[int, int]  # (same effect, decisions in a rule on both sides)

    def shares(self) -> dict[str, float | None]:
        event = [sum(y[0][i] for y in self.years.values()) for i in (0, 1)]
        content = [sum(y[1][i] for y in self.years.values()) for i in (0, 1)]
        return {"bcubed_precision": self.bcubed[0], "bcubed_recall": self.bcubed[1], "bcubed_f1": self.bcubed[2],
                "event_agreement": _ratio(*event), "content_agreement": _ratio(*content),
                "effect_agreement": _ratio(*self.effects)}


@dataclass(frozen=True)
class RulesComparison:
    overall: Agreement
    parts: Mapping[str, Agreement]  # per holdout part, over its documents' decisions
    only_a: int  # decisions in a rule in A, in none in B
    only_b: int


def compare_rules(raw_a: list[dict], raw_b: list[dict], decisions_a: list[Decision], decisions_b: list[Decision],
                  as_of: date, parts: Mapping[str, Collection[str]] | None = None) -> RulesComparison:
    """How far two consolidations agree: B-cubed over how they group the decisions both have, what each shows in
    force at every year's cutoff (by the decision in force, and by the one that adopted its content), and the effect
    each gives a decision both put in a rule. `parts` (holdout part -> its documents) adds the same over each part's
    decisions alone, counting in force only the rules that hold one of them."""
    live = {d.ref for d in decisions_a} & {d.ref for d in decisions_b}
    a, b = clusters_of(raw_a, live), clusters_of(raw_b, live)
    rules_a = render.build_rules(raw_a, {d.ref: d for d in decisions_a})
    rules_b = render.build_rules(raw_b, {d.ref: d for d in decisions_b})
    cutoffs = [render.year_cutoff(year, as_of) for year in render.covered_years([*decisions_a, *decisions_b], as_of)]
    effect_a = {v["ref"]: v["effekt"] for raw in raw_a for v in raw["versioner"]}
    effect_b = {v["ref"]: v["effekt"] for raw in raw_b for v in raw["versioner"]}

    def agreement(refs: Collection[str] | None) -> Agreement:
        items = sorted(ref for ref in a.keys() | b.keys() if refs is None or ref in refs)

        def holding(rules: list[render.Rule]) -> list[render.Rule]:
            return rules if refs is None else [r for r in rules if any(v.decision.ref in refs for v in r.versions)]

        force_a, force_b = in_force(holding(rules_a), cutoffs), in_force(holding(rules_b), cutoffs)
        years = {c: (jaccard(force_a[c][0], force_b[c][0]), jaccard(force_a[c][1], force_b[c][1])) for c in cutoffs}
        both = [ref for ref in effect_a.keys() & effect_b.keys() & live if refs is None or ref in refs]
        return Agreement(len(items), bcubed(a, b, items), years,
                         (sum(effect_a[ref] == effect_b[ref] for ref in both), len(both)))

    held = {part: {ref for ref in live if ref.rpartition("#")[0] in docs} for part, docs in (parts or {}).items()}
    return RulesComparison(agreement(None), {part: agreement(refs) for part, refs in held.items()},
                           len(a.keys() - b.keys()), len(b.keys() - a.keys()))


def holdout_of(rules_dir: Path) -> Holdout | None:
    """The holdout of a replay's rules directory (eval/replays/<name>/data/regler), None for any other."""
    meta = rules_dir.parent.parent / "holdout.json"
    if not meta.exists():
        return None
    stored = read_json(meta)
    parts = stored.get("parts") or {stored["spec"]: stored["documents"]}
    return Holdout(stored["spec"], stored["seed"], tuple((part, tuple(docs)) for part, docs in parts.items()))


def _agreement_lines(agreement: Agreement) -> list[str]:
    s = agreement.shares()
    return [f"- Grouping of decisions, B-cubed precision / recall / F1 (A against B): "
            f"{' / '.join(_pct(v) for v in agreement.bcubed)}",
            f"- In force at the year cutoffs, pooled: same decision {_pct(s['event_agreement'])}, same adopting "
            f"decision {_pct(s['content_agreement'])}",
            f"- Same effect for decisions both put in a rule: {_pct(s['effect_agreement'])} of {agreement.effects[1]}"]


def comparison_report(label_a: str, label_b: str, c: RulesComparison,
                      keys: Mapping[str, list[RuleScore]] | None, soft: Mapping[str, str]) -> str:
    lines = [f"# {label_a} against {label_b}", "", *_agreement_lines(c.overall),
             f"- Decisions in a rule in A only: {c.only_a}; in B only: {c.only_b}", ""]
    for part, agreement in c.parts.items():
        kind = "late insertion: mostly older documents" if part.startswith("random") else "the newest documents"
        lines += [f"## Held out: {part} ({kind}, {agreement.decisions} decisions)", "", *_agreement_lines(agreement),
                  ""]
    lines += ["## Per year, all rules", "", "| Year | Same decision in force | Same adopting decision |",
              "|---|--:|--:|"]
    for cutoff, ((event, either_e), (content, either_c)) in c.overall.years.items():
        lines.append(f"| {cutoff[:4]} | {event}/{either_e} | {content}/{either_c} |")
    if keys is not None:
        summaries = {side: rules_summary([s for s in scores if s.slug not in soft]) for side, scores in keys.items()}
        lines += ["", "## Against the rules key", "", f"| Measure | {label_a} | {label_b} |", "|---|--:|--:|"]
        for name, measure in (("Events found", "found_share"), ("Effects agree", "effect_share"),
                              ("Fragmented rules", "fragmented"), ("Years, same event", "event_share"),
                              ("Years, same content", "content_share")):
            cells = [summaries[side][measure] for side in ("a", "b")]
            shown = [str(v) if isinstance(v, int) else _pct(v) for v in cells]
            lines.append(f"| {name} | {shown[0]} | {shown[1]} |")
        lines += ["", "Per rule: events found of certain events, rules holding them, years with the same content in "
                  "force of certain years" + (" (soft rules are left out of the totals above)." if soft else "."), "",
                  f"| Rule | {label_a} | {label_b} |", "|---|---|---|"]
        for s_a, s_b in zip(keys["a"], keys["b"]):
            name = f"{s_a.slug} (soft)" if s_a.slug in soft else s_a.slug
            lines.append(f"| {name} | {_rule_cell(s_a)} | {_rule_cell(s_b)} |")
    return "\n".join(lines) + "\n"


def _rule_cell(s: RuleScore) -> str:
    return f"{s.found}/{s.events} found, {s.rules} rules, {s.same_content}/{s.years} years"


# --------------------------------------------------------------------------- commands

def cmd_select(args: argparse.Namespace) -> None:
    """Choose and store the selection with today's date (as_of). Rerun on the same data, it keeps the stored one and
    its date, so the rules judges' input stays the same."""
    docs = scrape.load_manifest()
    texts = Texts()
    selection = build_selection(docs, analyze.load_decisions(docs), analyze.load_rules(),
                                {doc.id: len(texts.words(doc).words) for doc in docs})
    path = selection_path()
    stored = read_json(path) if path.exists() else None
    if stored is not None and {k: v for k, v in stored.items() if k != "as_of"} == selection:
        selection = stored
        print(f"{path} is unchanged (as of {stored.get('as_of')})")
    elif stored is not None and not args.force:
        raise SystemExit(f"{path} exists with another selection, and keys built on it would no longer match; "
                         f"pass --force to replace it")
    else:
        selection = {"as_of": today().isoformat(), **selection}
        write_json(path, selection)
    print(f"Rules ({len(selection['rules'])}):")
    for r in selection["rules"]:
        print(f"  {r['slug']} ({r['kategori']}): {r['reason']}")
    print(f"Documents ({len(selection['documents'])}):")
    for d in selection["documents"]:
        print(f"  {d['id']} ({d['organ']}, {d['date']}, {d['words']} words, {d['decisions']} decisions): {d['reason']}")
    s = selection["summary"]
    print(f"{s['decisions']} decisions; the two largest documents ({', '.join(s['two_largest'])}) hold "
          f"{_pct(s['two_largest_share'])}; without decisions: {', '.join(s['without_decisions']) or 'none'}")


def cmd_extract(args: argparse.Namespace) -> None:
    run = check_run_name(args.name)
    docs = selected_docs(load_selection(), args.pilot, args.docs)
    if run == STORED_RUN:
        copied = copy_stored(docs, args.force)
        print(f"Copied {len(copied)} of {len(docs)} extractions from {analyze.DECISIONS_DIR} to {run_dir(run)}")
        return
    if not args.model or args.max_cost is None:
        raise SystemExit("extract calls Claude: give --model and --max-cost")
    states = {doc.id: run_state(run, doc, args.model, args.effort) for doc in docs}
    stale = [doc_id for doc_id, state in states.items() if state == "stale"]
    if stale and not args.force:
        raise SystemExit(f"Run {run} has extractions of {', '.join(stale)} from another file version, prompt or "
                         f"effort; pass --force to extract them again (paid), or use another --name")
    todo = [doc for doc in docs if states[doc.id] != "current"]
    texts = Texts()
    tokens_in = sum(estimate_tokens(analyze.EXTRACT_SYSTEM, analyze.document_prompt(doc, texts.text(doc)),
                                    analyze.EXTRACT_SCHEMA) for doc in todo)
    print_plan(f"Extract {run}", len(todo), tokens_in, args.model, args.effort,
               f"; {len(docs) - len(todo)} of {len(docs)} documents extracted already")
    if not todo:
        return
    budget = RunBudget(max_cost_usd=args.max_cost)
    cli = analyze.cli_version()
    step = analyze.run_parallel(
        todo, lambda doc: extract_into_run(doc, run, model=args.model, effort=args.effort, cli=cli, budget=budget),
        workers_for(args), f"Extract {run}", budget)
    done = [doc.id for doc in todo if run_state(run, doc, args.model, args.effort) == "current"]
    log_run("extract", {"run": run, "model": args.model, "effort": args.effort, "documents": done},
            {"extract": step})
    finish({"extract": step})


def _judging(args: argparse.Namespace) -> Judging:
    if args.rederive and args.rejudge:
        raise SystemExit("--rederive uses stored answers only; it cannot be combined with --rejudge")
    return Judging(args.judge_model, args.judge_effort, RunBudget(max_cost_usd=args.max_cost), workers_for(args),
                   args.rejudge, args.rederive)


def cmd_key_decisions(args: argparse.Namespace) -> None:
    docs = selected_docs(load_selection(), args.pilot, args.docs)
    runs = tuple(check_run_name(run) for run in args.runs)
    texts = Texts()
    tasks = [decisions_task(doc, runs, texts) for doc in docs]
    judging = _judging(args)
    corrections = load_corrections()
    steps = _judge_in_rounds("decisions", tasks, judging, lambda task, answers: task.needs_tiebreak(answers))
    written = 0
    for task in tasks:
        answers = _answers(task, judging, lambda t, a: t.needs_tiebreak(a))
        if answers is None:
            continue
        key = correct_decisions_key(decisions_key(task, answers), corrections)
        key["judges"] = stored_judges(task.call(n) for n in JUDGES[:len(answers)])
        write_json(key_dir("decisions") / f"{task.doc.id}.json", key)
        written += 1
        certain = sum(d["status"] == "certain" for d in key["decisions"])
        print(f"{task.doc.id}: {len(task.clusters)} candidates -> {certain} certain decisions, "
              f"{len(key['decisions']) - certain} uncertain, {len(answers)} judges", flush=True)
    log_run("key-decisions", {"runs": list(runs), "model": judging.model, "effort": judging.effort,
                              "documents": [d.id for d in docs], "keys_written": written}, steps)
    finish(steps, "" if written == len(tasks) else f"{len(tasks) - written} keys are not complete")


def cmd_key_rules(args: argparse.Namespace) -> None:
    selection = load_selection()
    as_of = selection_date(selection)
    slugs = chosen_items([r["slug"] for r in selection["rules"]], args.pilot, args.rules, "rules")
    docs = scrape.load_manifest()
    by_id = {doc.id: doc for doc in docs}
    decisions = analyze.load_decisions(docs)
    raw_rules = {raw["slug"]: raw for raw in analyze.load_rules()}
    targets = analyze.load_slugs().targets()
    texts = Texts()
    extracted = extracted_spans(decisions, by_id, texts)
    index = None if args.rederive else KeywordIndex(docs, texts)
    groups = load_synonyms()
    corrections = load_corrections()
    tasks = []
    for slug in slugs:
        current = targets.get(slug, slug)
        if current not in raw_rules:
            raise SystemExit(f"Selected rule {slug} is in no rule file and leads to no rule")
        if args.rederive:
            path = key_dir("rules") / f"{slug}.json"
            if not path.exists():
                raise SystemExit(f"--rederive: {slug} has no key yet")
            tasks.append(stored_rule_task(read_json(path), raw_rules[current], decisions, by_id, texts, as_of,
                                          extracted))
            continue
        task = rule_task({"slug": slug}, raw_rules[current], decisions, by_id, index, groups, as_of, extracted)
        print(f"{slug}: {len(task.passages)} passages, {sum(p.words for p in task.passages):,} words; "
              f"{task.unread} keyword hits next to a number left unread", flush=True)
        for hit in task.unread_amounts[:5]:
            print(f"    {hit['doc']}: {hit['text'][:110]}")
        tasks.append(task)
    judging = _judging(args)

    def needs(task: RuleTask, answers: Sequence[dict]) -> bool:
        return RuleTimeline.of(task, answers).open

    steps = _judge_in_rounds("rules", tasks, judging, needs)
    written = 0
    for task in tasks:
        answers = _answers(task, judging, needs)
        if answers is None:
            continue
        key = correct_rule_key(rules_key(task, answers), corrections)
        key["judges"] = stored_judges(task.call(n) for n in JUDGES[:len(answers)])
        write_json(key_dir("rules") / f"{task.slug}.json", key)
        written += 1
        certain = sum(y["status"] == "certain" for y in key["years"])
        print(f"{task.slug}: {len(key['events'])} events, {certain} of {len(key['years'])} years certain, "
              f"{len(answers)} judges", flush=True)
    log_run("key-rules", {"model": judging.model, "effort": judging.effort, "rules": [t.slug for t in tasks],
                          "keys_written": written}, steps)
    finish(steps, "" if written == len(tasks) else f"{len(tasks) - written} keys are not complete")


def _judge_in_rounds(kind: str, tasks: list, judging: Judging, needs_tiebreak: Callable) -> dict[str, StepSummary]:
    """Judges 1 and 2 for every task, then judge 3 (blind, the same input) for those whose judges disagree."""
    steps: dict[str, StepSummary] = {}
    first = [task.call(n) for task in tasks for n in JUDGES[:2]]
    note = f"; then up to {len(tasks)} calls to a third judge where the two disagree"
    if step := run_judges(f"Judge {kind}", first, judging, note):
        steps["judges"] = step
    third = []
    for task in tasks:
        earlier = [stored_answer(task.call(n), judging) for n in JUDGES[:2]]
        if None not in earlier and needs_tiebreak(task, earlier):
            third.append(task.call(3))
    if step := run_judges(f"Third judge {kind}", third, judging):
        steps["third"] = step
    return steps


def _answers(task, judging: Judging, needs_tiebreak: Callable) -> list[dict] | None:
    """The answers a task's key is built from, or None while one is missing."""
    earlier = [stored_answer(task.call(n), judging) for n in JUDGES[:2]]
    if None in earlier:
        return None
    if not needs_tiebreak(task, earlier):
        return earlier
    third = stored_answer(task.call(3), judging)
    return None if third is None else [*earlier, third]


def cmd_score(args: argparse.Namespace) -> None:
    runs = [check_run_name(run) for run in args.run]
    corrections = load_corrections()
    keys = {path.stem: correct_decisions_key(read_json(path), corrections)
            for path in sorted(key_dir("decisions").glob("*.json"))}
    if not keys:
        raise SystemExit(f"No decisions key in {key_dir('decisions')}; run `evaluate.py key-decisions` first")
    doc_ids = [doc_id for doc_id in keys if all(run_path(run, doc_id).exists() for run in runs)]
    left_out = sorted(set(keys) - set(doc_ids))
    if not doc_ids:
        raise SystemExit("No document has a key and an extraction by every run")
    stored = {(run, doc_id): read_json(run_path(run, doc_id)) for run in runs for doc_id in doc_ids}
    warnings = [f"run {run} extracted another version of {doc_id} than the key judged"
                for (run, doc_id), record in stored.items() if record.get("sha256") != keys[doc_id].get("sha256")]
    for warning in warnings:
        log.warning("%s", warning)
    scores = {run: [score_document(keys[doc_id], stored[run, doc_id]["beslutninger"]) for doc_id in doc_ids]
              for run in runs}
    report = decisions_report(scores, doc_ids, {doc_id: keys[doc_id] for doc_id in doc_ids}, args.seed, warnings)
    if left_out:
        report += f"\nLeft out, not extracted by every run: {', '.join(left_out)}\n"
    name = args.report or f"decisions-{'+'.join(runs)}"
    write_text(report_path(name), report)
    print(report)
    metrics = {run: {m: _round(METRICS[m](total(s))) for m in METRICS}
               | {"recall_doc_avg": _round(per_document(s, METRICS["recall"])),
                  "precision_doc_avg": _round(per_document(s, METRICS["precision"]))} for run, s in scores.items()}
    log_run("score", {"runs": runs, "documents": len(doc_ids), "report": f"reports/{name}.md", "metrics": metrics})


def cmd_score_rules(args: argparse.Namespace) -> None:
    corrections = load_corrections()
    keys = [correct_rule_key(read_json(path), corrections) for path in sorted(key_dir("rules").glob("*.json"))]
    if not keys:
        raise SystemExit(f"No rules key in {key_dir('rules')}; run `evaluate.py key-rules` first")
    selection = load_selection() if selection_path().exists() else {"rules": []}
    soft = {r["slug"]: r.get("soft_reason", "soft") for r in selection["rules"] if r.get("soft")}
    docs = scrape.load_manifest()
    decisions = analyze.load_decisions(docs, args.decisions_dir)
    raw_rules = analyze.load_rules(args.rules_dir)
    texts = Texts()
    by_id = {doc.id: doc for doc in docs}
    scores = [score_rule(key, decisions, raw_rules, by_id, texts) for key in keys]
    report = rules_report(scores, args.rules_dir or analyze.RULES_DIR, soft,
                          [c for key in keys for c in key["corrections"]])
    name = args.report or f"rules-{(args.rules_dir or analyze.RULES_DIR).name}"
    write_text(report_path(name), report)
    print(report)
    summary = rules_summary([s for s in scores if s.slug not in soft])
    log_run("score-rules", {"rules_dir": str(args.rules_dir or analyze.RULES_DIR), "rules": len(scores),
                            "soft": sorted(soft),
                            "report": f"reports/{name}.md",
                            "metrics": {k: _round(v) if isinstance(v, float) else v for k, v in summary.items()}})


def cmd_candidate_recall(args: argparse.Namespace) -> None:
    docs = scrape.load_manifest()
    decisions = analyze.load_decisions(docs)
    raw_rules = analyze.load_rules()
    live = {d.ref for d in decisions}
    hidden = hide_one_ranks(raw_rules, decisions)
    single = sum(1 for raw in raw_rules if sum(v["ref"] in live for v in raw["versioner"]) == 1)
    report = recall_report(hidden, single)
    write_text(report_path("candidate-recall"), report)
    print(report)
    log_run("candidate-recall", {"decisions": len(hidden), "single": single, "k": choose_k(hidden),
                                 "recall": {f"@{k}": _round(recall_at(hidden, k)) for k in RECALL_KS},
                                 "relabelled": {f"@{k}": _round(recall_at(hidden, k, relabelled=True))
                                                for k in RECALL_KS}})


def cmd_replay(args: argparse.Namespace) -> None:
    """Withhold documents from a copy of data/ and consolidate them again on that copy; data/ is only read."""
    run = check_run_name(args.name)
    docs = scrape.load_manifest()
    holdout = Holdout(args.holdout, args.seed, holdout_parts(args.holdout, analyze.load_decisions(docs), args.seed))
    data = prepare_replay(run, holdout, args.mode, args.force)
    budget = RunBudget(minutes=args.time_budget, max_cost_usd=args.max_cost)
    workers = workers_for(args)
    with data_paths(data):
        decisions = analyze.load_decisions(docs)
        if args.mode == "incremental":
            queue = incremental.work_queue(decisions, incremental.RuleBook.load(), docs)
            print(f"Replay {run}: {len(queue)} documents with {sum(len(w.new) for w in queue)} decisions to file "
                  f"({', '.join(f'{part}: {len(ids)}' for part, ids in holdout.parts)} withheld); per document 3 votes "
                  f"by {args.assign_model} and one call per touched rule to {args.model}, until {args.max_cost:.2f} "
                  f"USD", flush=True)
            settings = incremental.Settings(args.assign_model, args.model, args.effort, workers)
            step = incremental.consolidate(docs, decisions, settings, budget)
        else:
            step = analyze.consolidate(decisions, {d.id: d.organ_label for d in docs}, model=args.model,
                                       effort=args.effort, workers=workers, budget=budget)
    print(f"Replayed rule files: {data / 'regler'}")
    log_run("replay", {"run": run, "mode": args.mode, "holdout": args.holdout, "seed": args.seed,
                       "parts": {part: list(ids) for part, ids in holdout.parts}, "rules_dir": str(data / "regler")},
            {"consolidate": step})
    finish({"consolidate": step})


def rules_label(rules_dir: Path) -> str:
    """A short name for a rules directory in reports: the replay's name, else its last two path parts."""
    holdout_meta = rules_dir.parent.parent / "holdout.json"
    name = rules_dir.parent.parent.name if holdout_meta.exists() else f"{rules_dir.parent.name}-{rules_dir.name}"
    return re.sub(r"[^\w.-]+", "-", name)


def cmd_compare_rules(args: argparse.Namespace) -> None:
    corrections = load_corrections()
    keys = [correct_rule_key(read_json(path), corrections) for path in sorted(key_dir("rules").glob("*.json"))]
    if not keys and not args.no_key:
        raise SystemExit(f"No rules key in {key_dir('rules')}: build it with `evaluate.py key-rules`, or pass --no-key "
                         f"to compare without it")
    selection = load_selection() if selection_path().exists() else {"rules": []}
    soft = {r["slug"]: r.get("soft_reason", "soft") for r in selection["rules"] if r.get("soft")}
    docs = scrape.load_manifest()
    sides = {}
    for side, rules_dir in (("a", args.a), ("b", args.b)):
        if not rules_dir.is_dir():
            raise SystemExit(f"{rules_dir} is not a directory of rule files")
        sides[side] = (analyze.load_rules(rules_dir),
                       analyze.load_decisions(docs, side_decisions(rules_dir, args.decisions_dir)))
    parts: dict[str, set[str]] = {}
    for rules_dir in (args.a, args.b):
        for part, ids in (holdout.parts if (holdout := holdout_of(rules_dir)) else ()):
            parts.setdefault(part, set()).update(ids)
    comparison = compare_rules(sides["a"][0], sides["b"][0], sides["a"][1], sides["b"][1], args.as_of or today(),
                               parts)
    scores = None
    if keys:
        texts, by_id = Texts(), {doc.id: doc for doc in docs}
        scores = {side: [score_rule(key, decisions, raw, by_id, texts) for key in keys]
                  for side, (raw, decisions) in sides.items()}
    label_a, label_b = rules_label(args.a), rules_label(args.b)
    report = comparison_report(label_a, label_b, comparison, scores, soft)
    name = args.report or f"compare-{label_a}-vs-{label_b}"
    write_text(report_path(name), report)
    print(report)
    metrics = {k: _round(v) for k, v in comparison.overall.shares().items()}
    metrics |= {part: {k: _round(v) for k, v in agreement.shares().items()}
                for part, agreement in comparison.parts.items()}
    key = None
    if scores:
        key = {side: {k: _round(v) if isinstance(v, float) else v
                      for k, v in rules_summary([s for s in found if s.slug not in soft]).items()}
               for side, found in scores.items()}
    log_run("compare-rules", {"a": str(args.a), "b": str(args.b), "report": f"reports/{name}.md",
                              "metrics": metrics, "key": key})


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def _ids(text: str) -> list[str]:
    return [item for item in text.split(",") if item]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    def claude_options(command: argparse.ArgumentParser, items: str, required_cost: bool = True) -> None:
        command.add_argument("--max-cost", type=float, required=required_cost, metavar="USD",
                             help="start no new Claude calls once this much is used at list price")
        command.add_argument("--workers", type=int,
                             help=f"parallel Claude calls; calls running when the limit is reached finish, so the "
                                  f"cost can exceed it by one call per worker (default: 1 below {SMALL_BUDGET_USD:.0f} "
                                  f"USD, else 4)")
        only = command.add_mutually_exclusive_group()
        only.add_argument("--pilot", type=int, metavar="N", help=f"only the first N selected {items}")
        only.add_argument(f"--{items}", type=_ids, metavar="ID,...",
                          help=f"only these selected {items} (their answers are reused by the full build)")

    select = sub.add_parser("select", help="choose the rules and documents (no Claude)")
    select.add_argument("--force", action="store_true", help="replace a different existing selection")
    select.set_defaults(func=cmd_select)

    extract = sub.add_parser("extract", help="run today's extraction on the selected documents")
    extract.add_argument("--name", required=True, help=f"run name; '{STORED_RUN}' copies data/beslutninger")
    extract.add_argument("--model", help="model id, e.g. claude-sonnet-5-5")
    extract.add_argument("--effort", choices=update.EFFORTS, help="effort (default: Claude Code's own)")
    extract.add_argument("--force", action="store_true",
                         help="replace extractions of another file version, prompt or effort (paid; for 'stored': "
                              "other copies)")
    claude_options(extract, "docs", required_cost=False)
    extract.set_defaults(func=cmd_extract)

    for name, func, items, help_ in (
            ("key-decisions", cmd_key_decisions, "docs", "Part B: judge the candidates per document"),
            ("key-rules", cmd_key_rules, "rules", "Part A: judge each selected rule's timeline")):
        command = sub.add_parser(name, help=help_)
        command.add_argument("--judge-model", default=JUDGE_MODEL, help=f"default: {JUDGE_MODEL}")
        command.add_argument("--judge-effort", choices=update.EFFORTS, default=JUDGE_EFFORT,
                             help=f"default: {JUDGE_EFFORT}")
        command.add_argument("--rejudge", action="store_true",
                             help="ask again where a stored answer was given for other input (paid)")
        command.add_argument("--rederive", action="store_true",
                             help="rebuild the keys from stored answers only, never calling Claude (key-rules: on "
                                  "the passages each key was judged on), e.g. after a change to the agreement rules "
                                  "or the corrections")
        if name == "key-decisions":
            command.add_argument("--runs", nargs="+", default=list(CANDIDATE_RUNS),
                                 help=f"extraction runs whose decisions are the candidates (default: "
                                      f"{' '.join(CANDIDATE_RUNS)})")
        claude_options(command, items)
        command.set_defaults(func=func)

    score = sub.add_parser("score", help="Part B: score extraction runs against the decisions key (no Claude)")
    score.add_argument("--run", action="append", required=True, help="a run to score; repeat to compare runs")
    score.add_argument("--seed", type=int, default=SEED, help="bootstrap seed")
    score.add_argument("--report", help="report name under eval/reports/")
    score.set_defaults(func=cmd_score)

    score_rules = sub.add_parser("score-rules", help="Part A: score rule files against the rules key (no Claude)")
    score_rules.add_argument("--rules-dir", type=Path, help="default: data/regler")
    score_rules.add_argument("--decisions-dir", type=Path, help="default: data/beslutninger")
    score_rules.add_argument("--report", help="report name under eval/reports/")
    score_rules.set_defaults(func=cmd_score_rules)

    recall = sub.add_parser("candidate-recall",
                            help="incremental gate: how often the candidate ranking offers a decision's own rule (no "
                                 "Claude)")
    recall.set_defaults(func=cmd_candidate_recall)

    replay = sub.add_parser("replay", help="incremental gate: withhold documents from a copy of data/ and consolidate "
                                           "them again on that copy")
    replay.add_argument("--name", required=True, help="the replay's name: eval/replays/<name>/")
    replay.add_argument("--holdout", required=True, metavar="SPEC",
                        help="documents to withhold, e.g. newest:20,random:10 (the N newest, N drawn with the seed)")
    replay.add_argument("--seed", type=int, default=SEED, help=f"seed for random: (default: {SEED})")
    replay.add_argument("--mode", choices=update.CONSOLIDATE_MODES, default="incremental",
                        help="how to consolidate them (default: incremental)")
    replay.add_argument("--assign-model", default="claude-sonnet-5-5", help="incremental: the voting model")
    replay.add_argument("--model", default="claude-opus-5-5",
                        help="the consolidation model; in incremental mode it breaks ties and updates the rules")
    replay.add_argument("--effort", choices=update.EFFORTS, help="consolidation effort (default: Claude Code's own)")
    replay.add_argument("--max-cost", type=float, required=True, metavar="USD",
                        help="start no new Claude calls once this much is used at list price")
    replay.add_argument("--time-budget", type=float, metavar="MIN", help="start no new Claude calls after this long")
    replay.add_argument("--workers", type=int, help="parallel Claude calls (default: 1 below "
                                                    f"{SMALL_BUDGET_USD:.0f} USD, else 4)")
    replay.add_argument("--force", action="store_true", help="replace a replay of another holdout or mode")
    replay.set_defaults(func=cmd_replay)

    compare = sub.add_parser("compare-rules", help="compare two rules directories and score both against the rules "
                                                   "key (no Claude)")
    compare.add_argument("a", type=Path, help="a rules directory, e.g. eval/replays/<name>/data/regler")
    compare.add_argument("b", type=Path, help="another, e.g. data/regler")
    compare.add_argument("--decisions-dir", type=Path,
                         help="decisions of both (default: the beslutninger/ next to each, else data/beslutninger)")
    compare.add_argument("--as-of", type=date.fromisoformat,
                         help="the date of the current year's cutoff (default: today)")
    compare.add_argument("--no-key", action="store_true", help="compare without the rules key (eval/key/rules)")
    compare.add_argument("--report", help="report name under eval/reports/")
    compare.set_defaults(func=cmd_compare_rules)
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
