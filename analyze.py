"""Claude steps: extract decisions per document, then consolidate them into rule histories.

Both steps call the Claude Code CLI headless (`claude -p`), so they run on the logged-in
subscription and need no API key. Results are cached in data/ and only recomputed when the
input (document hash, decisions in a category) or the prompt version changes.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Collection, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from datetime import date
from pathlib import Path

import matching
from matching import Candidate, FormerSlug, LiveRule, RuleRefs, SlugRegistry
from scrape import DATA_DIR, Doc, document_text

# Bump when a prompt or schema changes so cached results are recomputed.
EXTRACT_VERSION = 2
CONSOLIDATE_VERSION = 7

DECISIONS_DIR = DATA_DIR / "beslutninger"
RULES_DIR = DATA_DIR / "regler"
# Slugs no rule holds any more, with what each last stood for; never handed out again.
SLUGS_PATH = DATA_DIR / "slugs.json"

# Seconds one Claude call may take before it is retried.
EXTRACT_TIMEOUT = 600
CONSOLIDATE_TIMEOUT = 1800

# Share of a quote's word trigrams that must line up in the document for it to count as found.
QUOTE_THRESHOLD = 0.8
# Each quote trigram votes for the start its position implies, and votes near a start are pooled.
# Words the document has inside the quote move later votes forward: a page header or footer at a
# page break ("2012-03-30" is 3 words, "Bestyrelsesmøde DSF Dec. 2015" 4) or a hyphenated line break
# that splits a word in two. Words the quote has that the document lacks move them back, which is
# rarer and shorter. So votes are pooled from QUOTE_SLACK words before a start to QUOTE_GAP after it.
QUOTE_SLACK = 2
QUOTE_GAP = 8

# The effect of a decision that did not adopt anything follows from its outcome alone.
PROPOSAL_EFFECT = {"ikke_afgjort": "foreslaaet", "forkastet": "forkastet", "trukket": "trukket"}

CATEGORIES = {
    "medlemskab": "Medlemskab, licens og klubskifte",
    "okonomi": "Gebyrer og økonomi",
    "antidoping": "Antidoping",
    "staevner": "Stævner og mesterskaber",
    "dommere": "Dommere",
    "landshold": "Landshold og udtagelse",
    "master": "Master",
    "udstyr": "Udstyr",
    "uddannelse": "Trænere og uddannelse",
    "organisation": "Organisation og vedtægter",
    "internationalt": "Internationale krav (IPF/EPF/DIF)",
    "andet": "Andet",
}
NIVEAUER = ["vedtaegt", "staevneregel", "bestyrelsesbeslutning", "udvalgsbeslutning", "eksternt_krav"]
EFFEKTER = ["indfoert", "aendret", "bekraeftet", "ophaevet", "foreslaaet", "forkastet", "trukket"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    ref: str  # the decision's stored id, "<doc id>#<n>"; kept when the document is extracted again
    doc_id: str
    dato: str | None  # meeting date (ISO) or year
    emne: str
    kategori: str
    udfald: str
    handling: str
    niveau: str
    tekst: str
    citat: str
    citat_fundet: bool
    side: int | None  # page of the quote as located in the document, else the page Claude gave
    side_rettet: bool  # Claude gave a page, and the quote was located on another one
    rank: int  # reading order within the document
    stemmer: str | None
    forslagsstiller: str | None  # who submitted the proposal, e.g. a club or Bestyrelsen
    gaelder_fra: str | None
    gaelder_til: str | None


def decision_hash(d: Decision) -> str:
    """Fingerprint of the decision as the consolidation saw it; a rule version built from different
    content no longer matches.

    It covers exactly the decision's own fields in `_consolidation_input` plus its category. A change
    to one of them also changes the category's input_hash, so the category is consolidated again and
    the versions match once more. The quote is left out: Claude never saw it, so a re-extraction that
    only changes the quote leaves input_hash alone, and a fingerprint covering it would hide the
    versions for good. The date and organ belong to the document and change input_hash themselves.
    """
    fields = [d.emne, d.kategori, d.udfald, d.handling, d.niveau, d.tekst, d.stemmer, d.forslagsstiller,
              d.gaelder_fra, d.gaelder_til]
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False).encode()).hexdigest()[:12]


def version_matches(version: dict, d: Decision) -> bool:
    """A rule version still describes its decision: its fingerprint equals the decision's."""
    return version.get("dhash") == decision_hash(d)


# --------------------------------------------------------------------------- extraction

EXTRACT_SYSTEM = """\
You extract standing rules and agreements from minutes ("referater") and rule documents of \
Dansk Styrkeløft Forbund (DSF), the Danish powerlifting federation. The output is used to \
build an overview of which rules applied in each year, when each rule was agreed, and in \
which minutes.

Extract every decision that creates, changes, confirms or abolishes a standing rule, policy, \
requirement, fee, procedure or agreement that applies going forward to clubs, athletes, \
coaches, referees, national teams, competitions, or to how the federation is run. Also \
extract proposals for such rules that were rejected or withdrawn – they explain why a rule \
did NOT change.

Do NOT extract:
- one-off tasks and action items ("X sender mail", "Y undersøger sagen")
- selection of named athletes, appointments or elections of named people
- which club hosts which competition on which date, or other single events
- approval of accounts or budgets as such (DO extract fee/rate changes adopted with a budget)
- internal bookkeeping, such as moving expenses between committee budgets
- discussions, orientations or ideas without a decision
- obituaries, greetings, competition results

Field rules:
- Write every text field in Danish.
- tekst: a self-contained statement of the rule as decided (or as proposed, if rejected or \
withdrawn). A reader who sees only this text must understand the rule. Keep concrete \
numbers, amounts, dates, deadlines, conditions and exceptions. 1-4 sentences.
- emne: a short generic Danish name for the rule that would stay the same if the rule were \
changed in a later year, e.g. "Licensgebyr", "Klubskifte", "Kvalifikationskrav EM, klassisk \
senior". No years or amounts.
- kategori: one of
  medlemskab = licens, klubmedlemskab, klubskifte, klubløse løftere, karantæne ved \
udmeldelse, bopæls-/statsborgerskabskrav, krav om træning i klub
  okonomi = gebyrer og takster (licens, start, årsafgift, administration), betalingsfrister, \
refusion, tilskud, fordeling af startgebyr
  antidoping = dopingsanktioner, klubsanktioner, antidoping-kurser og -kontrol
  staevner = afvikling af stævner og mesterskaber, divisionsturnering, tilmelding og \
frister, arrangørkrav, vægt- og aldersklasser, rekorder, ranglister
  dommere = dommeruddannelse og -autorisation, klubbers dommerforpligtelse, dommerregler
  landshold = udtagelseskriterier, kvalifikationskrav, landsholdsregler, egenbetaling
  master = regler der kun gælder masterløftere og masterudvalget
  udstyr = godkendt udstyr og udstyrsregler
  uddannelse = træner- og coachuddannelse, krav til trænere
  organisation = vedtægter, valg, kompetencer og kommissorier for bestyrelse og udvalg, \
forretningsorden, kommunikation, adfærdskodeks, kåringer og hædersbevisninger
  internationalt = krav og beslutninger fra IPF, EPF eller DIF der binder DSF eller danske \
atleter (not IPF/EPF internal matters such as their own elections or budgets)
  andet = everything else
  Pick the most specific category: master (anything that only concerns masters or \
Masterudvalget, including its budget, composition and selection) and landshold come before \
okonomi, organisation and staevner. A rule about an obligation that includes a fee belongs to \
the obligation's category; pure fee/rate decisions belong to okonomi.
- udfald: vedtaget (adopted or decided), forkastet (voted down or rejected), trukket (only \
when the minutes say the proposer withdrew it), ikke_afgjort (a proposal that is presented or \
discussed without a decision at this meeting, e.g. the board reviewing "indkomne forslag" \
from clubs before Repræsentantskabsmødet, or a proposal postponed).
- forslagsstiller: who submitted the proposal (a club such as "Hvidovre", "Bestyrelsen", a \
committee) when the minutes say so, else null. Many rules start as "indkomne forslag" from \
clubs to Repræsentantskabsmødet.
- handling: ny (new rule), aendring (changes an existing rule), bekraeftelse (restates, \
confirms or clarifies an existing rule without changing it), ophaevelse (abolishes a rule).
- niveau: vedtaegt (change to DSF's statutes, by Repræsentantskabet), staevneregel (other \
rule adopted by Repræsentantskabet, incl. fees adopted with the budget), \
bestyrelsesbeslutning, udvalgsbeslutning, eksternt_krav (imposed by IPF, EPF, DIF or Anti \
Doping Danmark).
- citat: a verbatim excerpt (max about 300 characters) copied exactly from the document that \
supports the decision. Copy, do not paraphrase.
- side: page number from the nearest preceding [Side N] marker, or null.
- stemmer: vote counts if stated, e.g. "31 for, 7 imod, 7 blanke", else null.
- gaelder_fra / gaelder_til: YYYY-MM-DD, only when the document says when the rule takes \
effect or expires, or the rule explicitly applies to one season, year or competition (e.g. \
kvalifikationskrav for 2025 -> gaelder_til 2025-12-31). Otherwise null.
- moededato: the date the meeting was held according to the document (YYYY-MM-DD), else null.

A document that is itself a rule document (e.g. "Regler for masterudvalget", \
"Kvalifikationskrav") lists rules in force: extract each with udfald vedtaget and handling \
bekraeftelse unless it says the rule is new or changed.

Never invent decisions. When unsure whether something was actually decided, leave it out. \
Return an empty list if there are no such decisions.
"""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "moededato": {"type": ["string", "null"]},
        "beslutninger": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "emne": {"type": "string"},
                    "kategori": {"type": "string", "enum": list(CATEGORIES)},
                    "udfald": {"type": "string", "enum": ["vedtaget", "forkastet", "trukket", "ikke_afgjort"]},
                    "forslagsstiller": {"type": ["string", "null"]},
                    "handling": {"type": "string", "enum": ["ny", "aendring", "bekraeftelse", "ophaevelse"]},
                    "niveau": {"type": "string", "enum": NIVEAUER},
                    "tekst": {"type": "string"},
                    "citat": {"type": "string"},
                    "side": {"type": ["integer", "null"]},
                    "stemmer": {"type": ["string", "null"]},
                    "gaelder_fra": {"type": ["string", "null"]},
                    "gaelder_til": {"type": ["string", "null"]},
                },
                "required": ["emne", "kategori", "udfald", "forslagsstiller", "handling", "niveau", "tekst", "citat",
                             "side", "stemmer", "gaelder_fra", "gaelder_til"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["moededato", "beslutninger"],
    "additionalProperties": False,
}


def extract(docs: list[Doc], *, model: str, effort: str | None, workers: int,
            budget: RunBudget | None = None) -> StepSummary:
    """Extract decisions from every document whose cached result is missing or outdated.

    Documents that fail or are skipped are retried on the next run.
    """
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    todo = [doc for doc in docs if not _extraction_is_current(doc)]
    log.info("Extract: %d of %d documents need extracting", len(todo), len(docs))
    cli = cli_version() if todo else ""
    return run_parallel(todo, lambda doc: _extract_one(doc, model=model, effort=effort, cli=cli, budget=budget),
                         workers, "Extract", budget)


def missing_extractions(docs: list[Doc]) -> list[str]:
    """Ids of documents without a current extraction (new, changed, or a failed Claude call)."""
    return sorted(doc.id for doc in docs if not _extraction_is_current(doc))


def _extraction_is_current(doc: Doc) -> bool:
    path = DECISIONS_DIR / f"{doc.id}.json"
    if not path.exists():
        return False
    cached = json.loads(path.read_text())
    return cached.get("sha256") == doc.sha256 and cached.get("version") == EXTRACT_VERSION


def document_prompt(doc: Doc, text: str) -> str:
    """The document as Claude gets it: its id, organ, title and date from styrke.dk, then its text. The extraction
    sends exactly this; the answer key's judge (evaluate.py) sends it before the candidates."""
    return (
        f"Dokument-id: {doc.id}\nOrgan: {doc.organ_label}\nTitel på styrke.dk: {doc.title}\n"
        f"Dato ifølge styrke.dk: {doc.date or 'ukendt'}\n\n<dokument>\n{text}\n</dokument>"
    )


@dataclass(frozen=True)
class Extraction:
    """One answer to the extraction prompt for a document."""
    moededato: str | None  # as Claude read it, not yet checked against styrke.dk's date
    decisions: list[dict]  # Claude's decisions in its order, each with its quote located (quote_fields)
    words: DocWords  # the document's words, to locate other quotes in the same text


def run_extraction(doc: Doc, *, model: str, effort: str | None,
                   budget: RunBudget | None = None) -> tuple[Extraction, Usage]:
    """Run the extraction prompt on one document and locate each decision's quote; writes nothing.

    The one place the extraction prompt runs, so the pipeline (_extract_one, which adds ids and caches the result
    in data/) and the evaluation (evaluate.py extract) measure the same thing. An error after the call carries
    the call's usage (ClaudeError).
    """
    text = document_text(doc)
    output, usage = ask_claude(EXTRACT_SYSTEM, document_prompt(doc, text), EXTRACT_SCHEMA, model=model,
                               effort=effort, timeout=EXTRACT_TIMEOUT, budget=budget)
    with usage_kept(usage):
        words = DocWords.of(text)
        decisions = [{**d, **quote_fields(d["citat"], words, d["side"])} for d in output["beslutninger"]]
        extraction = Extraction(output["moededato"], decisions, words)
    return extraction, usage


def _extract_one(doc: Doc, *, model: str, effort: str | None, cli: str,
                 budget: RunBudget | None = None) -> tuple[str, Usage]:
    path = DECISIONS_DIR / f"{doc.id}.json"
    # Read before the call, so a previous result without ids fails before it costs anything.
    previous = _with_ids(json.loads(path.read_text()), path) if path.exists() else None
    referenced = _referenced_ids()
    extraction, usage = run_extraction(doc, model=model, effort=effort, budget=budget)
    with usage_kept(usage):
        decisions = extraction.decisions
        ids = assign_ids(doc.id, previous, decisions, extraction.words, date.today(), referenced)
        result = {
            "doc_id": doc.id,
            "sha256": doc.sha256,
            "version": EXTRACT_VERSION,
            "model": model,
            "provenance": provenance(usage, cli, EXTRACT_SYSTEM, EXTRACT_SCHEMA, effort),
            "moededato": extraction.moededato,
            "next_number": ids.next_number,
            "retired": ids.retired,
            "beslutninger": [{"id": id_, **d} for id_, d in zip(ids.ids, decisions)],
        }
        _write_json(path, result)
    missing = sum(not d["citat_fundet"] for d in decisions)
    note = f", {missing} quotes not found in the document" if missing else ""
    if previous is not None:
        retired_now = len(ids.retired) - len(previous["retired"])
        note += f"; ids: {ids.carried} carried over, {len(decisions) - ids.carried} new, {retired_now} retired"
    return f"{doc.id}: {len(decisions)} decisions{note}", usage


@dataclass(frozen=True)
class DecisionIds:
    ids: list[str]  # one per new decision, in order
    next_number: int  # the next unused number in the document
    retired: list[dict]  # every retired decision of the document: {"id", "emne", "reason", "date"}
    carried: int  # new decisions that kept the id of a previous one


def assign_ids(doc_id: str, previous: dict | None, decisions: list[dict], words: DocWords, today: date,
               referenced: Collection[str] = ()) -> DecisionIds:
    """Ids for a document's newly extracted decisions.

    A new decision matched to one of the previous extraction (matching.match_decisions) keeps its id; the
    others get "<doc>#<n>" in the order Claude listed them, from the document's next_number up. Previous
    decisions left without a match are retired. Numbers only grow, so an id is never handed out twice; they
    also start above every id of the document that `referenced` (rules, the slug history) still names, so a
    lost decision file cannot hand a rule's old id to another decision. The previous quotes are located again
    in the current text: a replaced document can move every word offset.
    """
    known = set(referenced)
    if previous is not None:
        known |= {d["id"] for d in previous["beslutninger"]} | {r["id"] for r in previous["retired"]}
    first = max(previous["next_number"] if previous else 1, _highest_number(doc_id, known) + 1)
    if previous is None:
        ids = [f"{doc_id}#{n}" for n in range(first, first + len(decisions))]
        return DecisionIds(ids, first + len(decisions), [], 0)
    old = previous["beslutninger"]
    old_candidates = [_candidate(d, quote_fields(d["citat"], words, d["side"])["citat_pos"]) for d in old]
    new_candidates = [_candidate(d, d["citat_pos"]) for d in decisions]
    matches = matching.match_decisions(old_candidates, new_candidates)
    carried = {m.new: old[m.old]["id"] for m in matches}
    ids, next_number = [], first
    for j in range(len(decisions)):
        if j in carried:
            ids.append(carried[j])
        else:
            ids.append(f"{doc_id}#{next_number}")
            next_number += 1
    kept = {m.old for m in matches}
    retired = [{"id": d["id"], "emne": d["emne"], "reason": "no match in a re-extraction", "date": today.isoformat()}
               for i, d in enumerate(old) if i not in kept]
    return DecisionIds(ids, next_number, previous["retired"] + retired, len(matches))


def _highest_number(doc_id: str, ids: Collection[str]) -> int:
    """The highest n among the ids "<doc_id>#<n>", 0 when there is none."""
    numbers = [int(n) for prefix, _, n in (id_.rpartition("#") for id_ in ids) if prefix == doc_id and n.isdigit()]
    return max(numbers, default=0)


def _referenced_ids() -> set[str]:
    """Every decision id that data/regler/ and data/slugs.json name."""
    refs: set[str] = set()
    for path in RULES_DIR.glob("*.json"):
        cached = json.loads(path.read_text())
        refs.update(v["ref"] for rule in cached["regler"] for v in rule["versioner"])
        refs.update(cached.get("udeladt", []), cached.get("ikke_tildelt", []))
    registry = load_slugs()
    for former in [*registry.aliases.values(), *registry.retired.values()]:
        refs.update(former.refs)
    return refs


def _candidate(d: dict, pos: int | None) -> Candidate:
    return Candidate(d["emne"], d["tekst"], d["udfald"], matching.quote_span(pos, d["citat"]))


def _with_ids(cached: dict, path: Path) -> dict:
    """A cached extraction, checked to carry ids: every result written since ids exist does, so one without
    them is a bug or a file from before the migration, and guessing ids could give a rule the wrong decision."""
    if "next_number" not in cached or "retired" not in cached or any("id" not in d for d in cached["beslutninger"]):
        raise ValueError(f"{path}: decisions without stored ids (\"id\", \"next_number\", \"retired\")")
    return cached


def load_decisions(docs: list[Doc], directory: Path | None = None) -> list[Decision]:
    """All cached decisions for the given documents, in chronological and then reading order; from `directory`
    instead of data/beslutninger when given (evaluate.py scores other extractions)."""
    decisions: list[Decision] = []
    for doc in docs:
        path = (directory or DECISIONS_DIR) / f"{doc.id}.json"
        if not path.exists():
            continue
        cached = _with_ids(json.loads(path.read_text()), path)
        meeting_date = _meeting_date(cached.get("moededato"), doc)
        raw = cached["beslutninger"]
        for d, rank in zip(raw, _reading_order(raw)):
            located_page, claude_page = d.get("citat_side"), d["side"]
            decisions.append(Decision(
                ref=d["id"],
                doc_id=doc.id,
                dato=meeting_date,
                emne=d["emne"],
                kategori=d["kategori"],
                udfald=d["udfald"],
                handling=d["handling"],
                niveau=d["niveau"],
                tekst=d["tekst"],
                citat=d["citat"],
                citat_fundet=d["citat_fundet"],
                side=located_page if located_page is not None else claude_page,
                # Filling in a page Claude left out is not a correction.
                side_rettet=claude_page is not None and located_page not in (None, claude_page),
                rank=rank,
                stemmer=d["stemmer"],
                forslagsstiller=d.get("forslagsstiller"),
                gaelder_fra=_valid_date(d["gaelder_fra"]),
                gaelder_til=_valid_date(d["gaelder_til"]),
            ))
    return sorted(decisions, key=lambda d: (d.dato or "", d.doc_id, d.rank))


def load_retired_ids(docs: list[Doc]) -> set[str]:
    """Ids of the decisions that re-extractions of the given documents retired."""
    retired: set[str] = set()
    for doc in docs:
        path = DECISIONS_DIR / f"{doc.id}.json"
        if path.exists():
            retired.update(r["id"] for r in _with_ids(json.loads(path.read_text()), path)["retired"])
    return retired


def _reading_order(raw: list[dict]) -> list[int]:
    """Rank of each decision by where its quote starts; one whose quote was not located stays
    right after the decision Claude listed before it."""
    keys, last_pos = [], -1
    for i, d in enumerate(raw):
        if d.get("citat_pos") is not None:
            last_pos = d["citat_pos"]
        keys.append((last_pos, i))
    ranks = [0] * len(raw)
    for rank, i in enumerate(sorted(range(len(raw)), key=keys.__getitem__)):
        ranks[i] = rank
    return ranks


def _meeting_date(extracted: str | None, doc: Doc) -> str | None:
    found = _valid_date(extracted)
    if found and doc.date and abs(int(found[:4]) - int(doc.date[:4])) > 1:
        log.warning("%s: meeting date %s does not match styrke.dk (%s); using the latter", doc.id, found, doc.date)
        return doc.date
    return found or doc.date


def _valid_date(value: str | None) -> str | None:
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower())


_PAGE_MARKER = re.compile(r"\[Side (\d+)\]")


@dataclass(frozen=True)
class DocWords:
    """A document's words without the [Side N] markers, with the page each word is on."""
    words: list[str]
    pages: list[int | None]  # None for documents without page markers (HTML)
    index: dict[tuple[str, ...], list[int]]  # word trigram -> offsets where it starts

    @classmethod
    def of(cls, text: str) -> DocWords:
        parts = _PAGE_MARKER.split(text)  # [before first marker, page, text, page, text, …]
        words = _words(parts[0])
        pages: list[int | None] = [None] * len(words)
        for page, segment in zip(parts[1::2], parts[2::2]):
            segment_words = _words(segment)
            words += segment_words
            pages += [int(page)] * len(segment_words)
        index: dict[tuple[str, ...], list[int]] = defaultdict(list)
        for offset, gram in enumerate(zip(words, words[1:], words[2:])):
            index[gram].append(offset)
        return cls(words, pages, dict(index))


def word_spans(text: str) -> list[tuple[int, int]]:
    """Where each word DocWords counts stands in the text: every word outside the [Side N] markers, so a word
    offset leads back to the text as written."""
    markers = [m.span() for m in _PAGE_MARKER.finditer(text)]
    spans, k = [], 0
    for m in re.finditer(r"\w+", text):
        while k < len(markers) and markers[k][1] <= m.start():
            k += 1
        if k < len(markers) and markers[k][0] <= m.start() < markers[k][1]:
            continue
        spans.append(m.span())
    return spans


def locate_quote(quote: str, doc: DocWords, page: int | None = None) -> int | None:
    """Word offset where the quote starts in the document, or None when it is not there.

    A quote can occur more than once (a proposal and the decision adopting it); then the
    occurrence on `page`, the page Claude cited, wins, else the best and earliest match.
    """
    words = _words(quote)
    occurrences = _exact_occurrences(words, doc) if len(words) < 3 else _trigram_occurrences(words, doc)
    on_page = [start for start in occurrences if page in _start_pages(start, words, doc)]
    return (on_page or occurrences or [None])[0]


def _start_pages(start: int, words: list[str], doc: DocWords) -> set[int | None]:
    """Pages the quote located at `start` may begin on. When its first words don't match there,
    they may stand before a page header, so the pages up to QUOTE_GAP words earlier count too."""
    exact = doc.words[start:start + 3] == words[:3]
    return set(doc.pages[start if exact else max(start - QUOTE_GAP, 0):start + 1])


def _exact_occurrences(words: list[str], doc: DocWords) -> list[int]:
    """Where a quote of one or two words occurs verbatim, in document order (it has no trigrams)."""
    if not words:
        return []
    n = len(words)
    return [i for i in range(len(doc.words) - n + 1) if doc.words[i:i + n] == words]


def _trigram_occurrences(words: list[str], doc: DocWords) -> list[int]:
    """One start per place the quote occurs, best match first.

    Each quote trigram votes for the start its position implies. An anchor (a start with votes)
    pools the votes from QUOTE_SLACK words before it to QUOTE_GAP words after it, taking each
    trigram's vote nearest the anchor, so a stray earlier match of a common trigram doesn't win.
    Anchors with the most pooled votes, then the most votes of their own, come first. The
    occurrence starts where its first matching trigram implies: later trigrams may be pushed
    forward by a header inside the quote. A vote counts for one occurrence only, so a quote
    repeated right after itself (a proposal, then the vote on it) is two occurrences, while the
    part of a quote after a header is not a second one.
    """
    grams = list(zip(words, words[1:], words[2:]))
    voters: dict[int, set[int]] = defaultdict(set)  # implied start -> quote trigrams that imply it
    for j, gram in enumerate(grams):
        for offset in doc.index.get(gram, ()):
            voters[offset - j].add(j)

    def pool(anchor: int, taken: set[tuple[int, int]]) -> dict[int, int]:
        """Quote trigram -> the start nearest the anchor it votes for, among votes not yet taken."""
        window = sorted(range(anchor - QUOTE_SLACK, anchor + QUOTE_GAP + 1), key=lambda s: (abs(s - anchor), s))
        found: dict[int, int] = {}
        for start in window:
            for j in voters.get(start, ()):
                if (start, j) not in taken:
                    found.setdefault(j, start)
        return found

    occurrences: list[int] = []
    taken: set[tuple[int, int]] = set()  # (start, trigram) votes used by an occurrence
    for anchor in sorted(voters, key=lambda a: (-len(pool(a, set())), -len(voters[a]), a)):
        votes = pool(anchor, taken)
        if len(votes) / len(grams) >= QUOTE_THRESHOLD:
            occurrences.append(max(votes[min(votes)], 0))
            taken.update((start, j) for j, start in votes.items())
    return occurrences


def quote_fields(quote: str, doc: DocWords, page: int | None) -> dict:
    """Whether the quote was located in the document, and where: word offset and page (None when not).

    `page` is the page Claude cited; it is kept when the quote occurs there."""
    pos = locate_quote(quote, doc, page)
    if pos is None:
        return {"citat_fundet": False, "citat_pos": None, "citat_side": None}
    on_cited_page = page is not None and page in _start_pages(pos, _words(quote), doc)
    return {"citat_fundet": True, "citat_pos": pos, "citat_side": page if on_cited_page else doc.pages[pos]}


# --------------------------------------------------------------------------- consolidation

CONSOLIDATE_SYSTEM = """\
You consolidate decisions extracted from the minutes of Dansk Styrkeløft Forbund (DSF) into \
rule histories for one category. The result is used to show which version of each rule \
applied in each year.

The input is a JSON list of decisions in chronological order. Each has a ref, date (dato), \
organ, udfald, handling, niveau, emne, tekst, and optionally stemmer, gaelder_fra and \
gaelder_til.

Group the decisions into rules ("regler"). A rule is one thing of which exactly one version \
is in force at a time; a later version replaces the earlier one (e.g. "Licensgebyr": 200 kr. \
-> 300 kr.). Decisions about different things must be separate rules even when their emne is \
similar. Decisions describing the same rule with different wording or emne belong together.

For each rule return:
- titel: short Danish title, without years or amounts.
- versioner: the rule's decisions in chronological order, each with
  - ref: exactly as given in the input
  - effekt: indfoert (the rule first appears), aendret (its content changes), bekraeftet \
(repeated, confirmed or clarified without changing content), ophaevet (abolished), foreslaaet \
(an incoming proposal was presented or discussed without a decision yet – rule unchanged), \
forkastet (a proposal was voted down – rule unchanged), trukket (the proposer withdrew the \
proposal – rule unchanged). Use the decision's udfald: ikke_afgjort -> foreslaaet, forkastet \
-> forkastet, trukket -> trukket.
  - tekst: for aendret and bekraeftet, the complete rule text in force AFTER this decision, in \
Danish, merging earlier parts that still apply so the text stands alone, and covering only \
this rule (leave out other rules mentioned in the same decision, e.g. other fees in a budget \
confirmation). An amendment that rewrites only part of a rule (e.g. "Rettes til" for one \
paragraph, or a quoted text ending in "…") keeps the earlier clauses it does not replace or \
remove. For indfoert, null when the decision's own tekst states exactly this rule and \
nothing else (the usual case), otherwise the rule-specific text. For other effects null.
  - kort: at most 12 Danish words saying what this decision did, keeping key numbers and \
dates; no full sentence needed, e.g. "hævet fra 200 til 300 kr.", "ny klub anmoder via \
licens@styrke.dk". For proposals name the proposer when known, e.g. "Hvidovre foreslår \
spærring ved skyldigt kontingent".
  - kort_regel: for indfoert, aendret and bekraeftet, at most 12 Danish words giving the \
essence of the rule after this decision, e.g. "300 kr. pr. løfter pr. år", "mindst én dommer \
ved over 5 tilmeldte"; null for other effects.
- vigtig: true when a club, athlete, coach or referee needs to know the rule: licence and \
membership, fees and payment deadlines, eligibility, qualification and selection criteria, \
sanctions and doping consequences for athletes, obligations on clubs, how championships and \
competitions are run, and anything that affects a referee's or jury's decision (lifting \
rules, disqualification grounds, attempt changes, records, equipment checks). false only for \
internal routines of the board or committees (composition, meetings, internal \
communication), bookkeeping and internal payments, IPF/EPF internal governance, details about \
one venue or one named event, naming, and logistics. When in doubt, true.
- note: null for almost every rule. Only give a short Danish note for a real caveat: \
conflicting decisions, a lower body appearing to override a higher-level rule, a very weak \
vote, or doubt about whether the rule is still in force. Never restate dates, the level, the \
effect, or that no earlier version is known.

Precedence: Repræsentantskabet (vedtaegt, staevneregel) outranks Bestyrelsen, which outranks \
the committees; eksternt_krav comes from IPF/EPF/DIF/ADD. A board or committee decision \
cannot abolish a rule adopted by Repræsentantskabet; if one appears to, keep the rule and \
explain in note.

Every input ref must appear in exactly one rule, except refs that are clearly not standing \
rules (one-off decisions) – list those in udeladt. Do not invent refs.
"""

CONSOLIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "regler": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "titel": {"type": "string"},
                    "vigtig": {"type": "boolean"},
                    "note": {"type": ["string", "null"]},
                    "versioner": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "ref": {"type": "string"},
                                "effekt": {"type": "string", "enum": EFFEKTER},
                                "tekst": {"type": ["string", "null"]},
                                "kort": {"type": "string"},
                                "kort_regel": {"type": ["string", "null"]},
                            },
                            "required": ["ref", "effekt", "tekst", "kort", "kort_regel"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["titel", "vigtig", "note", "versioner"],
                "additionalProperties": False,
            },
        },
        "udeladt": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["regler", "udeladt"],
    "additionalProperties": False,
}


def consolidate(decisions: list[Decision], organ_of: dict[str, str], *, model: str, effort: str | None,
                workers: int, budget: RunBudget | None = None, categories: set[str] | None = None) -> StepSummary:
    """Group decisions into rule histories, one Claude call per category whose input changed.

    `categories` limits the calls to those categories (update.py --only); None means all. Categories that
    fail or are skipped are retried on the next run.
    """
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    with_decisions = {d.kategori for d in decisions}
    fingerprints = {d.ref: decision_hash(d) for d in decisions}

    for stale in RULES_DIR.glob("*.json"):
        if stale.stem not in with_decisions:
            with _SLUGS_LOCK:  # its decisions went elsewhere or are gone; resolve_slugs follows them
                orphans = {rule["slug"]: _former(rule, stale.stem) for rule in _rules_in(stale)}
                _save_slugs(load_slugs().with_former(orphans))
                stale.unlink()
            log.info("Consolidate: removed %s, which has no decisions any more (%d slugs to resolve)",
                     stale.name, len(orphans))

    todo = consolidation_todo(decisions, organ_of)
    if categories is not None:
        todo = [job for job in todo if job.category in categories]
    log.info("Consolidate: %d of %d categories need updating%s", len(todo), len(with_decisions),
             "" if categories is None else f" among those of the selected documents ({len(categories)})")
    cli = cli_version() if todo else ""
    summary = run_parallel(
        todo,
        lambda job: _consolidate_one(job.category, job.items, job.input_hash, fingerprints, model=model,
                                     effort=effort, cli=cli, budget=budget),
        workers, "Consolidate", budget,
    )
    resolve_slugs({d.ref for d in decisions})
    return summary


@dataclass(frozen=True)
class CategoryJob:
    category: str
    items: list[dict]  # the consolidation input
    input_hash: str
    rules_missing: bool  # no rule file at all, as opposed to one built from other input


def consolidation_todo(decisions: list[Decision], organ_of: dict[str, str]) -> list[CategoryJob]:
    """Categories whose rule file is missing or was built from other input: other decisions, or another
    CONSOLIDATE_VERSION. Reads data/regler/ only, so the rebuild guard can ask before any Claude call."""
    by_category: dict[str, list[dict]] = {}
    for d in decisions:
        by_category.setdefault(d.kategori, []).append(_consolidation_input(d, organ_of[d.doc_id]))
    todo = []
    for category, items in by_category.items():
        input_hash = _hash(items, CONSOLIDATE_VERSION)
        path = RULES_DIR / f"{category}.json"
        if not path.exists():
            todo.append(CategoryJob(category, items, input_hash, rules_missing=True))
        elif json.loads(path.read_text()).get("input_hash") != input_hash:
            todo.append(CategoryJob(category, items, input_hash, rules_missing=False))
    return todo


def _consolidation_input(d: Decision, organ: str) -> dict:
    item = {
        "ref": d.ref, "dato": d.dato, "organ": organ, "udfald": d.udfald, "handling": d.handling,
        "niveau": d.niveau, "emne": d.emne, "tekst": d.tekst, "stemmer": d.stemmer,
        "forslagsstiller": d.forslagsstiller,
        "gaelder_fra": d.gaelder_fra, "gaelder_til": d.gaelder_til,
    }
    return {k: v for k, v in item.items() if v is not None}


def _consolidate_one(category: str, items: list[dict], input_hash: str, fingerprints: dict[str, str], *,
                     model: str, effort: str | None, cli: str, budget: RunBudget | None = None) -> tuple[str, Usage]:
    path = RULES_DIR / f"{category}.json"
    # Read before the call, so rules without slugs fail before it costs anything.
    previous = _rules_in(path) if path.exists() else []
    prompt = (
        f"Kategori: {CATEGORIES[category]}\n\n<beslutninger>\n"
        f"{json.dumps(items, ensure_ascii=False, indent=0)}\n</beslutninger>"
    )
    output, usage = ask_claude(CONSOLIDATE_SYSTEM, prompt, CONSOLIDATE_SCHEMA, model=model, effort=effort,
                               timeout=CONSOLIDATE_TIMEOUT, budget=budget)
    with usage_kept(usage):
        rules, skipped, unassigned = _rules_from(output, items, fingerprints)
        with _SLUGS_LOCK:  # categories run in parallel, and a new slug must be unique across all of them
            registry = load_slugs()
            plan = matching.carry_slugs([RuleRefs(rule["slug"], _refs(rule)) for rule in previous],
                                        [RuleRefs(rule["titel"], _refs(rule)) for rule in rules],
                                        _live_slugs() | registry.taken(), registry.former())
            by_slug = {rule["slug"]: rule for rule in previous}
            former = {slug: _former(by_slug[slug], category, to) for slug, to in plan.aliases.items()}
            former |= {slug: _former(by_slug[slug], category) for slug in plan.orphaned}
            # The history first, revived slugs still in it: whichever write fails, every slug stays taken, in a
            # rule file or in the history (resolve_slugs drops a slug from the history once a rule holds it).
            history = registry.with_former(former)
            _save_slugs(history)
            _write_json(path, {
                "kategori": category,
                "version": CONSOLIDATE_VERSION,
                "input_hash": input_hash,
                "model": model,
                "provenance": provenance(usage, cli, CONSOLIDATE_SYSTEM, CONSOLIDATE_SCHEMA, effort),
                "regler": [{"titel": rule["titel"], "slug": slug, **rule} for rule, slug in zip(rules, plan.slugs)],
                "udeladt": skipped,
                "ikke_tildelt": unassigned,
            })
            if plan.revived:
                _save_slugs(history.without(plan.revived))
    note = f", {len(unassigned)} unassigned" if unassigned else ""
    minted = len(rules) - plan.kept - len(plan.revived)
    slugs = (f"; slugs: {plan.kept} kept, {len(plan.revived)} taken up again, {minted} new, {len(plan.aliases)} "
             f"merged into another rule, {len(plan.orphaned)} without a successor in the category")
    return f"{category}: {len(items)} decisions -> {len(rules)} rules{note}{slugs}", usage


def _rules_from(output: dict, items: list[dict],
                fingerprints: dict[str, str]) -> tuple[list[dict], list[str], list[str]]:
    """Claude's rules with only known refs, each once, the effect of proposals fixed by their outcome and each
    version's fingerprint; plus the refs Claude left out on purpose and those it did not assign at all."""
    outcome = {item["ref"]: item["udfald"] for item in items}
    seen: set[str] = set()
    rules = []
    for rule in output["regler"]:
        versions = [
            {**v, "effekt": PROPOSAL_EFFECT.get(outcome[v["ref"]], v["effekt"]), "dhash": fingerprints[v["ref"]]}
            for v in rule["versioner"] if v["ref"] in outcome and v["ref"] not in seen
        ]
        seen.update(v["ref"] for v in versions)
        if versions:
            rules.append({**rule, "versioner": versions})
    skipped = [ref for ref in output["udeladt"] if ref in outcome and ref not in seen]
    unassigned = sorted(set(outcome) - seen - set(skipped))
    return rules, skipped, unassigned


def load_rules(directory: Path | None = None) -> list[dict]:
    """Every rule with its category, from `directory` instead of data/regler when given."""
    rules = []
    for path in sorted((directory or RULES_DIR).glob("*.json")):
        cached = json.loads(path.read_text())
        rules.extend({**rule, "kategori": cached["kategori"]} for rule in cached["regler"])
    return rules


def load_one_offs() -> dict[str, frozenset[str]]:
    """Category -> the refs its consolidation left out as one-off decisions, not standing rules (udeladt)."""
    one_offs = {}
    for path in sorted(RULES_DIR.glob("*.json")):
        cached = json.loads(path.read_text())
        one_offs[cached["kategori"]] = frozenset(cached.get("udeladt", []))
    return one_offs


# --------------------------------------------------------------------------- rule slugs

# Held while slugs are handed out or retired and the files holding them are written: consolidations run in
# parallel, and a slug must be unique across every category and data/slugs.json.
_SLUGS_LOCK = threading.Lock()


def load_slugs() -> SlugRegistry:
    """data/slugs.json: {"aliases": {slug: {"to", "category", "title", "refs"}}, "retired": {slug: {"category",
    "title", "refs"}}}."""
    if not SLUGS_PATH.exists():
        return SlugRegistry()
    stored = json.loads(SLUGS_PATH.read_text())

    def former(entry: dict, to: str | None) -> FormerSlug:
        return FormerSlug(entry["category"], entry["title"], frozenset(entry["refs"]), to)

    return SlugRegistry({slug: former(entry, entry["to"]) for slug, entry in stored["aliases"].items()},
                        {slug: former(entry, None) for slug, entry in stored["retired"].items()})


def _save_slugs(registry: SlugRegistry) -> None:
    def entry(former: FormerSlug) -> dict:
        return {**({"to": former.to} if former.to else {}), "category": former.category, "title": former.title,
                "refs": sorted(former.refs)}

    _write_json(SLUGS_PATH, {"aliases": {slug: entry(f) for slug, f in sorted(registry.aliases.items())},
                             "retired": {slug: entry(f) for slug, f in sorted(registry.retired.items())}})


def resolve_slugs(live_ids: Collection[str]) -> None:
    """Lead every former slug to where its decisions are now (matching.successor); `live_ids` are the ids of all
    current decisions. Run after all categories of a run are consolidated, whether or not some failed."""
    with _SLUGS_LOCK:
        registry = load_slugs()
        resolved = registry.resolved(live_rules(load_rules()), live_ids)
        if resolved != registry:
            _save_slugs(resolved)
        log.info("Slugs: %d former slugs lead to a live rule, %d lead nowhere (retired)",
                 len(resolved.aliases), len(resolved.retired))


def live_rules(raw_rules: list[dict]) -> list[LiveRule]:
    """The rules with a slug, in category order and then Claude's order: the order ties are settled in."""
    order = list(CATEGORIES)
    ranked = sorted(raw_rules, key=lambda raw: order.index(raw["kategori"]))  # stable: keeps Claude's order
    return [LiveRule(raw["slug"], raw["kategori"], raw["titel"], _refs(raw)) for raw in ranked if has_slug(raw)]


def has_slug(rule: dict) -> bool:
    """Whether a rule carries a usable slug: a non-empty string."""
    return isinstance(rule.get("slug"), str) and bool(rule["slug"])


def _former(rule: dict, category: str, to: str | None = None) -> FormerSlug:
    return FormerSlug(category, rule["titel"], _refs(rule), to)


def _rules_in(path: Path) -> list[dict]:
    """The rules of one category file, checked to carry slugs: a rule without one is a bug or a file from before
    slugs were stored, and giving it a new slug would break its links."""
    rules = json.loads(path.read_text())["regler"]
    if not all(has_slug(rule) for rule in rules):
        raise ValueError(f"{path}: rules without a stored slug (a non-empty string)")
    return rules


def _refs(rule: dict) -> frozenset[str]:
    return frozenset(v["ref"] for v in rule["versioner"])


def _live_slugs() -> set[str]:
    """The slugs of every rule in data/regler/."""
    return {rule["slug"] for path in RULES_DIR.glob("*.json") for rule in _rules_in(path)}


# --------------------------------------------------------------------------- claude CLI

@dataclass(frozen=True)
class Usage:
    """What Claude calls used, as the CLI reports it; adds up over the attempts of a call and the calls of a step.

    cost_usd is the CLI's estimate at API list price (`total_cost_usd`). The subscription is not billed
    per call, but the estimate measures how much of it a run takes, and RunBudget limits it.
    """
    model: str = ""  # as requested: an alias such as "sonnet", or a full id
    output_by_model: dict[str, int] = field(default_factory=dict)  # canonical id -> output tokens
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0  # time spent waiting on the CLI, retries included, back-off pauses not
    attempts: int = 0

    @classmethod
    def of(cls, result: dict, model: str, duration_s: float) -> Usage:
        """One attempt's usage from the CLI's JSON result, whose `modelUsage` has an entry per model used.
        Entries it cannot read count as nothing rather than losing the attempt's cost."""
        per_model = result.get("modelUsage")
        per_model = per_model if isinstance(per_model, dict) else {}
        entries = [(name, entry) for name, entry in per_model.items() if isinstance(entry, dict)]
        output_by_model: dict[str, int] = {}
        for name, entry in entries:
            canonical = str(entry.get("canonicalModel") or name)
            output_by_model[canonical] = output_by_model.get(canonical, 0) + _count(entry.get("outputTokens"))
        return cls(
            model=model,
            output_by_model=output_by_model,
            input_tokens=sum(_count(entry.get("inputTokens")) for _, entry in entries),
            cache_read_tokens=sum(_count(entry.get("cacheReadInputTokens")) for _, entry in entries),
            cache_write_tokens=sum(_count(entry.get("cacheCreationInputTokens")) for _, entry in entries),
            cost_usd=_cost(result),
            duration_s=duration_s,
            attempts=1,
        )

    def __add__(self, other: Usage) -> Usage:
        output_by_model = dict(self.output_by_model)
        for name, tokens in other.output_by_model.items():
            output_by_model[name] = output_by_model.get(name, 0) + tokens
        counts = {f.name: getattr(self, f.name) + getattr(other, f.name)
                  for f in fields(self) if f.name not in ("model", "output_by_model")}
        return Usage(model=self.model or other.model, output_by_model=output_by_model, **counts)

    @property
    def models(self) -> tuple[str, ...]:
        """Canonical ids the CLI reports having used, sorted."""
        return tuple(sorted(self.output_by_model))

    @property
    def output_tokens(self) -> int:
        return sum(self.output_by_model.values())

    @property
    def all_input_tokens(self) -> int:
        """Input including the cached prompt: with caching, input_tokens alone is only the uncached tail."""
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def answered_by(self) -> str:
        """The canonical model behind the answer: the requested id when the CLI reports it, else the model that
        wrote the most (an alias call may also use a small helper model)."""
        if self.model in self.output_by_model or not self.output_by_model:
            return self.model
        return max(self.models, key=self.output_by_model.__getitem__)


def _count(value: object) -> int:
    return int(value) if isinstance(value, (int, float)) else 0


def _cost(result: dict) -> float:
    value = result.get("total_cost_usd")
    return float(value) if isinstance(value, (int, float)) else 0.0


class RunBudget:
    """The run's limits: no Claude call or retry starts once the time is up, the list-price cost is reached,
    or the run was stopped (a wrong CLI makes every call fail).

    Calls already running finish, so the cost can end up to one call per worker over the limit.
    Thread-safe: worker threads add the cost of each attempt as it completes.
    """

    def __init__(self, *, minutes: float | None = None, max_cost_usd: float | None = None) -> None:
        self._deadline = None if minutes is None else time.monotonic() + 60 * minutes
        self._max_cost_usd = max_cost_usd
        self._spent_usd = 0.0
        self._stopped: str | None = None
        self._lock = threading.Lock()

    @property
    def spent_usd(self) -> float:
        with self._lock:
            return self._spent_usd

    def add(self, cost_usd: float) -> None:
        with self._lock:
            self._spent_usd += cost_usd

    def stop(self, reason: str) -> None:
        """Start no more work in this run; the first reason given is the one reported."""
        with self._lock:
            self._stopped = self._stopped or reason

    def exhausted(self) -> str | None:
        """Why no new work may start, worded for the log, or None while there is room."""
        with self._lock:
            stopped, spent = self._stopped, self._spent_usd
        if stopped:
            return stopped
        if self._deadline is not None and time.monotonic() >= self._deadline:
            return "the time budget is spent"
        if self._max_cost_usd is not None and spent >= self._max_cost_usd:
            return f"the cost limit of {self._max_cost_usd:.2f} USD is reached"
        return None


class ClaudeError(RuntimeError):
    """A Claude job failed, in the call or in handling its answer; `usage` is what it used all the same."""

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or Usage()


class ModelMismatch(ClaudeError):
    """The CLI answered with another model than the full id asked for: an older CLI may map an id it
    does not know to another model. Not retried, and it stops the run: every other call would get the same."""


class BudgetExhausted(RuntimeError):
    """A run limit was reached before the job started; the job is left for the next run."""


@contextmanager
def usage_kept(usage: Usage) -> Iterator[None]:
    """Errors after a successful call (a quote that breaks the locator, a full disk) still carry the call's
    usage, so its cost reaches the step summary and data/runs.jsonl."""
    try:
        yield
    except Exception as exc:
        raise ClaudeError(f"handling the answer failed: {type(exc).__name__}: {exc}", usage) from exc


def provenance(usage: Usage, cli: str, system: str, schema: dict, effort: str | None) -> dict:
    """What produced a cached result, to tell results apart when the model, CLI, prompt or effort changes.

    Not part of any cache key: switching from an alias to the id it stands for must not re-run anything.
    Claude Code does not report its default effort, so an unset one is recorded as "default".
    """
    return {"model": usage.answered_by, "cli": cli, "prompt": prompt_hash(system, schema),
            "effort": effort or "default"}


def prompt_hash(system: str, schema: dict) -> str:
    """Short fingerprint of what Claude is told: the system prompt and the output schema."""
    text = system + json.dumps(schema, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@functools.cache
def cli_version() -> str:
    """The Claude Code version, e.g. "2.1.294", read once per run. It matters: the CLI decides which model
    an alias stands for and what list price it reports. It never stops a run: results are just as valid
    without it, so a version that cannot be read is recorded as "unknown"."""
    claude = shutil.which("claude")
    if claude is None:
        log.warning("Cannot read the Claude Code version: 'claude' is not on PATH")
        return "unknown"
    try:
        proc = subprocess.run([claude, "--version"], capture_output=True, text=True, encoding="utf-8",
                              errors="replace", check=True, timeout=60)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.warning("Cannot read the Claude Code version: %s", exc)
        return "unknown"
    match = re.match(r"\s*(\d+(?:\.\d+)+)", proc.stdout)
    if not match:
        log.warning("Cannot read the Claude Code version from %r", proc.stdout[:80])
        return "unknown"
    return match[1]


def _claude_path() -> str:
    claude = shutil.which("claude")
    if claude is None:
        raise SystemExit("Claude Code CLI ('claude') was not found on PATH.")
    return claude


def ask_claude(system: str, prompt: str, schema: dict, *, model: str, effort: str | None, timeout: float,
               budget: RunBudget | None = None, attempts: int = 3) -> tuple[dict, Usage]:
    """Run one headless Claude Code call with structured output; returns (output, usage of all attempts).

    Each attempt may take `timeout` seconds and adds its cost to `budget` before anything else is read from
    its answer; no retry starts once the budget is exhausted. A call that failed and was not retried for
    that reason is still a failure: ClaudeError with the real error, not BudgetExhausted, which only means
    the job never started. A full model id ("claude-…") must be among the models the CLI reports using,
    else ModelMismatch.
    """
    cmd = [
        _claude_path(), "-p", "--output-format", "json", "--no-session-persistence",
        # No tools, plugins, hooks, CLAUDE.md or MCP servers: just the prompt and the schema.
        "--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--tools", "",
        "--model", model, "--system-prompt", system, "--json-schema", json.dumps(schema),
    ]
    if effort:
        cmd += ["--effort", effort]
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")}

    usage = Usage(model=model)
    error = ""
    for attempt in range(1, attempts + 1):
        limit = budget.exhausted() if budget is not None and attempt > 1 else None
        if limit:
            raise ClaudeError(f"{error} (not retried after {attempt - 1} attempts because {limit})", usage)
        started = time.monotonic()
        with tempfile.TemporaryDirectory() as cwd:
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, check=False,
                                      timeout=timeout, cwd=cwd, env=env)
            except subprocess.TimeoutExpired:
                usage += Usage(model=model, duration_s=time.monotonic() - started, attempts=1)
                error = "timeout"
                continue
        result = _json_object(proc.stdout)
        if budget is not None:
            budget.add(_cost(result))
        spent = Usage.of(result, model, time.monotonic() - started)
        usage += spent
        output = result.get("structured_output")
        if proc.returncode == 0 and not result.get("is_error") and isinstance(output, dict):
            if model.startswith("claude-") and model not in spent.models:
                raise ModelMismatch(f"asked for {model}, but Claude Code answered with "
                                    f"{', '.join(spent.models) or 'an unknown model'}; check its version", usage)
            return output, usage
        error = str(result.get("result") or proc.stderr or proc.stdout)[-500:]
        if attempt < attempts:
            time.sleep(15 * attempt)
    raise ClaudeError(error, usage)


def _json_object(text: str) -> dict:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class StepSummary:
    """One step's Claude calls, for the log, data/runs.jsonl and the GitHub step summary."""
    label: str
    calls: int  # jobs that started, whether they succeeded or failed
    failed: int
    skipped: int  # jobs that never started because a limit was reached; they run next time
    usage: Usage  # summed over every attempt, failed ones included
    seconds: float  # wall-clock time of the step


def run_parallel(jobs: list, fn, workers: int, label: str, budget: RunBudget | None = None) -> StepSummary:
    """Run fn over jobs in a thread pool; log progress and failures, keep going on errors.

    fn returns (message, Usage). Jobs that have not started when the budget is exhausted are skipped.
    The first ModelMismatch stops the budget, so the rest of the run is skipped too.
    """
    budget = budget if budget is not None else RunBudget()

    def start(job):
        if limit := budget.exhausted():
            raise BudgetExhausted(limit)
        try:
            return fn(job)
        except ModelMismatch:  # stop here, before this worker takes the next job
            budget.stop("Claude Code answered with another model than requested (check its version)")
            raise

    started = time.monotonic()
    usage = Usage()
    failed = skipped = 0
    limits: set[str] = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(start, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                message, job_usage = future.result()
            except BudgetExhausted as exc:
                skipped += 1
                limits.add(str(exc))
                continue
            except Exception as exc:  # one failed call must not stop the batch; rerun picks it up
                failed += 1
                if isinstance(exc, ClaudeError):
                    usage += exc.usage
                log.error("%s [%d/%d] failed: %s", label, done, len(jobs), exc)
                continue
            usage += job_usage
            log.info("%s [%d/%d] %s", label, done, len(jobs), message)
    if failed:
        log.warning("%s: %d failed; run again to retry them", label, failed)
    if skipped:
        log.warning("%s: skipped %d because %s; they run next time", label, skipped, " and ".join(sorted(limits)))
    log.info("%s done: %d calls, %d tokens in and %d out (%.2f USD at API list price, paid by the subscription)",
             label, len(jobs) - skipped, usage.all_input_tokens, usage.output_tokens, usage.cost_usd)
    return StepSummary(label, len(jobs) - skipped, failed, skipped, usage, time.monotonic() - started)


def _hash(value: object, version: int) -> str:
    return hashlib.sha256(json.dumps([version, value], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n")
