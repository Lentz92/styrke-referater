"""Incremental consolidation: add each new decision to the rule it belongs to and leave every other rule as it is.

Full consolidation (analyze.consolidate) rewrites a whole category for one new decision, which costs Opus output and
rewrites past history at random. Here the work is only what the rule files do not reflect yet (work_queue), done one
document at a time in date order, so later documents see the rules earlier ones created:

1. Candidates (candidates.py, no Claude): the CANDIDATE_K live rules whose words are closest to each new decision.
2. Assign (Sonnet, three independent votes per document): each new decision goes to one of its candidates, to a new
   rule or to its category's one-offs (udeladt); two votes of three decide, else one Opus call does, blind to them.
   Code rejects any answer outside the candidates offered. A changed decision (same id, other content) stays in its
   rule without a vote; a retired one leaves its rule.
3. Update (Opus, one call per touched rule): the rule's whole history, the new or changed decisions and the passage
   of the minutes around each quote. Claude writes the versions from the earliest touched one on (the insertion
   point); the versions before it stay byte-identical, which code checks, and no other rule is touched.

A document is applied only when all its calls succeed: a failure leaves it, whole, for the next run.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

import analyze
import matching
import render
from analyze import (CATEGORIES, PROPOSAL_EFFECT, ClaudeError, Decision, RunBudget, StepSummary, Usage,
                     decision_hash)
from candidates import CANDIDATE_K, CandidateIndex, Query, RuleProfile
from matching import FormerSlug, RuleRefs
from scrape import Doc

log = logging.getLogger(__name__)

NEW = "new"
ONE_OFF = "one-off"
VOTES = 3
# Seconds one assign or update call may take before it is retried.
ASSIGN_TIMEOUT = 600
UPDATE_TIMEOUT = 1200
# Words of the minutes around a quote that an update call gets.
PASSAGE_WORDS = 150
# Latest decisions of a candidate rule that the assign prompt shows.
HISTORY_SHOWN = 6

# What the statuses of a version mean to an update call; those that must come back are RETURNED.
KEEP, ADDED, CHANGED, REWRITE, REMOVED = "keep", "new", "changed", "rewrite", "removed"
RETURNED = (ADDED, CHANGED, REWRITE)


# --------------------------------------------------------------------------- prompts

ASSIGN_SYSTEM = """\
You file new decisions from the minutes of Dansk Styrkeløft Forbund (DSF), the Danish powerlifting federation, into \
an overview of its rules, which shows the version of each rule in force in each year. A rule is one thing of which \
exactly one version is in force at a time; a later decision changes, confirms or abolishes it (e.g. "Licensgebyr": \
200 kr. -> 300 kr.), and proposals to change it that were rejected, withdrawn or not decided yet belong to it too.

The input lists new decisions from one document (<beslutninger>). Each has a ref, date (dato), organ, udfald, \
handling, niveau, kategori, emne, tekst, optionally stemmer, forslagsstiller, gaelder_fra and gaelder_til, and \
kandidater: the slugs of the existing rules whose words are closest to it, in no particular order. <regler> \
describes each candidate rule: its title, category, the rule as it stands now (regel) and its latest decisions \
(historik).

Answer for every decision with its ref and one choice:
- the slug of one of its own candidates, when the decision is about that rule: it introduces, changes, confirms, \
abolishes or proposes to change the same thing, whatever its wording, amount or category;
- "new", when it concerns a standing rule none of its candidates is about. Then give title: a short Danish title for \
the new rule, without years or amounts, that would stay the same if the rule changed later, e.g. "Licensgebyr", \
"Klubskifte", "Kvalifikationskrav EM, klassisk senior". Decisions of this document about the same new rule get \
exactly the same title;
- "one-off", when the decision is clearly not a standing rule (a one-off decision).
title is null unless the choice is "new".

Decisions about different things must be separate rules even when their emne is similar. Decisions describing the \
same rule with different wording or emne belong together. A decision by a lower body about a rule adopted by a \
higher one still belongs to that rule.
"""

ASSIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "answers": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string"},
                    "choice": {"type": "string"},
                    "title": {"type": ["string", "null"]},
                },
                "required": ["ref", "choice", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["answers"],
    "additionalProperties": False,
}


def _consolidation_rules() -> tuple[str, str, str]:
    """CONSOLIDATE_SYSTEM's definitions of a version's fields, of vigtig and note, and its precedence rule, verbatim:
    a rule kept up to date here must read like one the full consolidation wrote."""
    _, found_fields, rest = analyze.CONSOLIDATE_SYSTEM.partition("  - ref: exactly as given in the input\n")
    version_fields, found_rule, rest = rest.partition("- vigtig:")
    rule_fields, found_precedence, rest = rest.partition("\n\nPrecedence:")
    precedence, found_end, _ = rest.partition("\n\nEvery input ref")
    if not (found_fields and found_rule and found_precedence and found_end):
        raise ValueError("CONSOLIDATE_SYSTEM no longer has the sections the update prompt quotes")
    return version_fields.rstrip(), f"- vigtig:{rule_fields}".strip(), f"Precedence:{precedence}".strip()


VERSION_FIELDS, RULE_FIELDS, PRECEDENCE = _consolidation_rules()

UPDATE_SYSTEM = f"""\
You keep the history of one rule of Dansk Styrkeløft Forbund (DSF), the Danish powerlifting federation, up to date \
as new minutes arrive. The history is used to show which version of the rule applied in each year. A rule is one \
thing of which exactly one version is in force at a time; a later version replaces the earlier one (e.g. \
"Licensgebyr": 200 kr. -> 300 kr.).

The input (<regel>) is the rule's title, vigtig and note, and its versions in chronological order. Each version is \
one decision (beslutning: dato, organ, udfald, handling, niveau, emne, the decision's own tekst, and optionally \
stemmer, forslagsstiller, gaelder_fra and gaelder_til) with what the history says about it (historik: effekt, tekst, \
kort, kort_regel), and a status:
- keep: stays exactly as it is; do not return it.
- new: a decision just filed under this rule; it has no history yet.
- changed: the minutes were read again, and the decision changed since its history was written.
- rewrite: comes after a new, changed or removed version, so its history may have to change to stay true: its tekst \
must still state the whole rule in force after it, and its effekt must still compare it with the version before it. \
Keep what is still right.
- removed: a decision that no longer exists, shown only so you can see what the later versions built on.
<kilder> has the passage of the minutes around the quote of each new or changed decision (only the quote where the \
passage could not be found); where the minutes and a decision's fields differ, the minutes decide.

Return versioner: exactly the versions with status new, changed or rewrite, each once, in chronological order, each \
with
  - ref: exactly as given in the input
{VERSION_FIELDS}

Also return vigtig and note for the rule. vigtig is kept as the input gives it, so repeat it; only when it is null \
(a new rule) decide it. note replaces the current note, so repeat the current one unless the new decisions settle it \
or add a real caveat.
{RULE_FIELDS}

{PRECEDENCE}
"""

UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "versioner": analyze.CONSOLIDATE_SCHEMA["properties"]["regler"]["items"]["properties"]["versioner"],
        "vigtig": {"type": "boolean"},
        "note": {"type": ["string", "null"]},
    },
    "required": ["versioner", "vigtig", "note"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------- rule files

class RuleBook:
    """data/regler/ in memory: category -> the file's content as stored. A document's edits are made here and only
    the files they changed are written, each as analyze writes it, so a rule nobody touched keeps its bytes."""

    def __init__(self, files: dict[str, dict]) -> None:
        self.files = files

    @classmethod
    def load(cls) -> RuleBook:
        files = {}
        for path in sorted(analyze.RULES_DIR.glob("*.json")):
            analyze._rules_in(path)  # rules without a slug fail before anything is asked
            stored = json.loads(path.read_text())
            files[stored["kategori"]] = stored
        return cls(files)

    def rules(self) -> Iterator[tuple[str, dict]]:
        """(category, rule) of every rule, in category order and then file order (analyze.live_rules' order)."""
        for category in sorted(self.files, key=_category_order):
            for rule in self.files[category]["regler"]:
                yield category, rule

    def rule(self, category: str, slug: str) -> dict:
        return next(rule for rule in self.files[category]["regler"] if rule["slug"] == slug)

    def holders(self) -> dict[str, tuple[str, str | None]]:
        """Ref -> (category, slug) of the rule holding it, or (category, None) for the one-offs (udeladt) and
        decisions a full consolidation did not assign (ikke_tildelt) of a category. The first one wins."""
        found: dict[str, tuple[str, str | None]] = {}
        for category, rule in self.rules():
            for v in rule["versioner"]:
                found.setdefault(v["ref"], (category, rule["slug"]))
        for category, stored in self.files.items():
            for ref in [*stored.get("udeladt", []), *stored.get("ikke_tildelt", [])]:
                found.setdefault(ref, (category, None))
        return found

    def one_offs(self) -> set[str]:
        return {ref for stored in self.files.values() for ref in stored.get("udeladt", [])}

    def slugs(self) -> set[str]:
        return {rule["slug"] for _, rule in self.rules()}

    def file(self, category: str, model: str) -> dict:
        """The category's file, created empty when it has none yet (only an approved run gets here: the rebuild
        guard stops a category with decisions but no file)."""
        if category not in self.files:
            self.files[category] = {"kategori": category, "version": analyze.CONSOLIDATE_VERSION, "input_hash": None,
                                    "model": model, "regler": [], "udeladt": [], "ikke_tildelt": []}
        return self.files[category]

    def write(self, categories: set[str]) -> None:
        for category in sorted(categories):
            analyze._write_json(analyze.RULES_DIR / f"{category}.json", self.files[category])


def _category_order(category: str) -> int:
    order = list(CATEGORIES)
    return order.index(category) if category in order else len(order)


# --------------------------------------------------------------------------- work queue

@dataclass(frozen=True)
class DocumentWork:
    """What the rule files do not reflect yet of one document's decisions."""
    doc_id: str
    date: str | None
    new: tuple[str, ...]  # in no rule and not a one-off, in reading order
    changed: tuple[str, ...]  # in a rule whose version was built from other content (its fingerprint differs)
    retired: tuple[str, ...]  # named by a rule, the one-offs or ikke_tildelt, but no current decision any more


def work_queue(decisions: Sequence[Decision], book: RuleBook, docs: Sequence[Doc]) -> list[DocumentWork]:
    """The decisions the rule files do not reflect, per document, documents in date order (undated last).

    A one-off stays one while its id lives: a re-extraction keeps an id only for the same decision (matching.py), and
    the one-offs keep no fingerprint to tell otherwise. Retired ids belong to the document their id names."""
    live = {d.ref: d for d in decisions}
    versions = {v["ref"]: v for _, rule in book.rules() for v in rule["versioner"]}
    one_offs = book.one_offs()
    by_doc: dict[str, dict[str, list[str]]] = {}

    def add(doc_id: str, kind: str, ref: str) -> None:
        by_doc.setdefault(doc_id, {"new": [], "changed": [], "retired": []})[kind].append(ref)

    for d in decisions:
        if d.ref not in versions and d.ref not in one_offs:
            add(d.doc_id, "new", d.ref)
        elif d.ref in versions and not analyze.version_matches(versions[d.ref], d):
            add(d.doc_id, "changed", d.ref)
    for ref in sorted(set(book.holders()) - set(live)):
        add(ref.rpartition("#")[0], "retired", ref)
    dates = {doc.id: doc.date for doc in docs} | {d.doc_id: d.dato for d in decisions}
    queue = [DocumentWork(doc_id, dates.get(doc_id), tuple(kinds["new"]), tuple(kinds["changed"]),
                          tuple(kinds["retired"])) for doc_id, kinds in by_doc.items()]
    return sorted(queue, key=lambda w: (w.date is None, w.date or "", w.doc_id))


def queue_categories(queue: Sequence[DocumentWork], decisions: Sequence[Decision], book: RuleBook) -> frozenset[str]:
    """The categories the queued work changes: those of new and changed decisions, and the files holding changed or
    retired ids. update.py's rebuild guard counts them like the categories a full consolidation would redo."""
    by_ref = {d.ref: d for d in decisions}
    holders = book.holders()
    found: set[str] = set()
    for work in queue:
        found.update(by_ref[ref].kategori for ref in (*work.new, *work.changed))
        found.update(holders[ref][0] for ref in (*work.changed, *work.retired))
    return frozenset(found)


# --------------------------------------------------------------------------- candidates

def ordered_versions(versions: Sequence[dict], by_ref: Mapping[str, Decision]) -> list[dict]:
    """The versions whose decision exists, in the order render shows them (render.version_key)."""
    known = [(i, v) for i, v in enumerate(versions) if v["ref"] in by_ref]
    return [v for i, v in sorted(known, key=lambda iv: render.version_key(by_ref[iv[1]["ref"]], iv[0]))]


def rule_profile(category: str, rule: dict, by_ref: Mapping[str, Decision]) -> RuleProfile:
    """What the candidate ranking reads of a rule: its title, the text and kort_regel of its latest version with
    content (render's Version.text; the latest version when it never applied) and the emne of its decisions."""
    ordered = ordered_versions(rule["versioner"], by_ref) or rule["versioner"]
    content = [v for v in ordered if v["effekt"] in render.CONTENT_EFFECTS]
    latest = (content or ordered or [None])[-1]
    if latest is None:
        return RuleProfile(rule["slug"], category, rule["titel"], "", None, ())
    decision = by_ref.get(latest["ref"])
    text = latest.get("tekst") or (decision.tekst if decision else latest.get("kort") or "")
    return RuleProfile(rule["slug"], category, rule["titel"], text, latest.get("kort_regel"),
                       tuple(by_ref[v["ref"]].emne for v in rule["versioner"] if v["ref"] in by_ref))


def candidate_index(book: RuleBook, by_ref: Mapping[str, Decision]) -> CandidateIndex:
    return CandidateIndex.of([rule_profile(category, rule, by_ref) for category, rule in book.rules()])


def query(d: Decision) -> Query:
    return Query(d.emne, d.tekst, d.kategori)


# --------------------------------------------------------------------------- assign

@dataclass(frozen=True)
class Choice:
    target: str  # a candidate's slug, NEW or ONE_OFF
    title: str | None = None  # the new rule's title (NEW only)


def read_vote(output: dict, offered: Mapping[str, Sequence[str]]) -> dict[str, Choice]:
    """One answer's valid choices by ref: a slug among the decision's own candidates, NEW with a title, or ONE_OFF.
    Anything else (another rule, a ref not asked about, NEW without a title) counts as no vote; a ref answered twice
    counts once, the first time."""
    choices: dict[str, Choice] = {}
    answered: set[str] = set()
    for answer in output.get("answers", []):
        ref, target, title = answer.get("ref"), answer.get("choice"), (answer.get("title") or "").strip()
        if ref not in offered or ref in answered:
            continue
        answered.add(ref)
        if target == NEW and title:
            choices[ref] = Choice(NEW, title)
        elif target == ONE_OFF or target in offered[ref]:
            choices[ref] = Choice(target)
    return choices


def tally(votes: Sequence[Mapping[str, Choice]], refs: Sequence[str]) -> tuple[dict[str, Choice], list[str]]:
    """The choices at least two votes agree on, and the refs without such a majority. A new rule's title is the one
    most of the votes for it gave, the earliest vote's on a tie."""
    decided, open_ = {}, []
    for ref in refs:
        given = [vote[ref] for vote in votes if ref in vote]
        targets = Counter(c.target for c in given)
        target, count = max(targets.items(), key=lambda item: (item[1], -_first(given, item[0])), default=(None, 0))
        if count < 2:
            open_.append(ref)
            continue
        titles = Counter(c.title for c in given if c.target == target)
        title = max(titles, key=lambda t: (titles[t], -[c.title for c in given].index(t))) if target == NEW else None
        decided[ref] = Choice(target, title)
    return decided, open_


def _first(given: Sequence[Choice], target: str) -> int:
    return next(i for i, c in enumerate(given) if c.target == target)


def rotated(items: Sequence[str], vote: int) -> list[str]:
    """The candidates starting a third further on for each vote, so the votes do not read them in the same order."""
    k = vote * len(items) // VOTES
    return [*items[k:], *items[:k]]


def assign_prompt(work: DocumentWork, refs: Sequence[str], offered: Mapping[str, Sequence[str]], book: RuleBook,
                  ctx: Context, vote: int) -> str:
    doc = ctx.docs.get(work.doc_id)
    decisions = []
    for ref in refs:
        d = ctx.by_ref[ref]
        decisions.append({**analyze._consolidation_input(d, ctx.organ(d.doc_id)), "kategori": d.kategori,
                          "kandidater": rotated(offered[ref], vote)})
    slugs = list(dict.fromkeys(slug for ref in refs for slug in offered[ref]))
    holders = {rule["slug"]: (category, rule) for category, rule in book.rules()}
    rules = [rule_view(*holders[slug], ctx.by_ref) for slug in slugs]
    title = f"{doc.organ_label}: {doc.title}" if doc else work.doc_id
    return (f"Dokument: {work.doc_id} ({title}, {work.date or 'ukendt dato'})\n\n<beslutninger>\n"
            f"{json.dumps(decisions, ensure_ascii=False, indent=0)}\n</beslutninger>\n\n<regler>\n"
            f"{json.dumps(rules, ensure_ascii=False, indent=0)}\n</regler>")


def rule_view(category: str, rule: dict, by_ref: Mapping[str, Decision]) -> dict:
    """A candidate rule as the assign prompt shows it: the rule now and its latest decisions."""
    profile = rule_profile(category, rule, by_ref)
    shown = ordered_versions(rule["versioner"], by_ref)[-HISTORY_SHOWN:]
    history = [f"{by_ref[v['ref']].dato or 'ukendt dato'}: {render.EFFEKT_LABELS[v['effekt']]}: "
               f"{v.get('kort') or by_ref[v['ref']].tekst}" for v in shown]
    return {"slug": rule["slug"], "titel": rule["titel"], "kategori": category, "regel": profile.text,
            "historik": history}


def assign(work: DocumentWork, book: RuleBook, ctx: Context, steps: list[StepSummary]) -> dict[str, Choice] | None:
    """The choice for each new decision of the document, or None when a call failed or was skipped, or the tie-break
    gave no valid choice."""
    refs = list(work.new)
    index = candidate_index(book, ctx.by_ref)
    offered = {ref: index.top(query(ctx.by_ref[ref]), ctx.settings.k) for ref in refs}

    def ask(model: str, vote: int, asked: Sequence[str], label: str) -> tuple[dict, Usage, str]:
        output, usage = analyze.ask_claude(ASSIGN_SYSTEM, assign_prompt(work, asked, offered, book, ctx, vote),
                                           ASSIGN_SCHEMA, model=model, effort=None, timeout=ASSIGN_TIMEOUT,
                                           budget=ctx.budget)
        return output, usage, f"{work.doc_id} {label}"

    answers = _parallel(list(range(VOTES)), lambda vote: ask(ctx.settings.assign_model, vote, refs, f"vote {vote + 1}"),
                        f"Assign {work.doc_id}", ctx, steps)
    if answers is None:
        return None
    decided, open_ = tally([read_vote(answers[vote], offered) for vote in range(VOTES)], refs)
    if open_:
        tie = _parallel([0], lambda _: ask(ctx.settings.update_model, 0, open_, "tie-break"),
                        f"Tie-break {work.doc_id}", ctx, steps)
        if tie is None:
            return None
        settled = read_vote(tie[0], {ref: offered[ref] for ref in open_})
        missing = [ref for ref in open_ if ref not in settled]
        if missing:
            log.error("%s: the tie-break gave no valid choice for %s", work.doc_id, ", ".join(missing))
            return None
        decided |= settled
    log.info("%s: %d decisions assigned, %d by two of three votes, %d by the tie-break", work.doc_id, len(refs),
             len(refs) - len(open_), len(open_))
    return decided


# --------------------------------------------------------------------------- update

@dataclass(frozen=True)
class Entry:
    """One version of a rule as an update call sees it."""
    ref: str
    status: str  # KEEP, ADDED, CHANGED, REWRITE or REMOVED
    version: dict | None  # the stored version; None for a new decision


@dataclass(frozen=True)
class RuleUpdate:
    """One update call: a rule (slug None for a new one), its versions as the call sees them, and the versions before
    the insertion point, which are written back as they are."""
    category: str
    slug: str | None
    title: str
    vigtig: bool | None
    note: str | None
    entries: tuple[Entry, ...]  # chronological; removed versions where they stood
    keep: tuple[dict, ...]  # the versions before the insertion point, in file order

    @property
    def expected(self) -> list[str]:
        return [e.ref for e in self.entries if e.status in RETURNED]

    @property
    def name(self) -> str:
        return self.slug or f"new rule {self.title!r}"


def plan_update(versions: Sequence[dict], by_ref: Mapping[str, Decision], removed: set[str], changed: set[str],
                new: Sequence[str]) -> tuple[list[dict], list[Entry] | None]:
    """The versions kept as they are and, when a call is needed, what it sees.

    In render's order (render.version_key) the insertion point is the earliest of: a changed decision; where a new
    decision sorts (after versions with the same dates, as if appended); the first version after a removed one, by
    file order (a removed version leaves no decision to date it; file order is render's order for all but a few
    rules). From there on every version is rewritten. A removed version with nothing after it needs no call: the
    versions before it never depended on it.
    """
    live = [(i, v) for i, v in enumerate(versions) if v["ref"] not in removed]
    order = sorted(live, key=lambda iv: render.version_key(by_ref[iv[1]["ref"]], iv[0]))
    rank = {i: r for r, (i, _) in enumerate(order)}
    removed_at: dict[int, list[dict]] = {}
    for j, v in enumerate(versions):
        if v["ref"] in removed:
            removed_at.setdefault(min((rank[i] for i, _ in live if i > j), default=len(order)), []).append(v)
    new_at: dict[int, list[str]] = {}
    for position, ref in sorted((_position(ref, order, by_ref), ref) for ref in new):
        new_at.setdefault(position, []).append(ref)
    starts = [r for r in removed_at if r < len(order)] + [rank[i] for i, v in live if v["ref"] in changed]
    starts += list(new_at)
    if not starts:
        return [v for _, v in live], None
    p = min(starts)
    keep = [v for i, v in live if rank[i] < p]
    entries: list[Entry] = []
    for r in range(len(order) + 1):
        entries += [Entry(v["ref"], REMOVED, v) for v in removed_at.get(r, [])]
        entries += [Entry(ref, ADDED, None) for ref in new_at.get(r, [])]
        if r < len(order):
            v = order[r][1]
            status = KEEP if r < p else CHANGED if v["ref"] in changed else REWRITE
            entries.append(Entry(v["ref"], status, v))
    return keep, entries


def _position(ref: str, order: Sequence[tuple[int, dict]], by_ref: Mapping[str, Decision]) -> int:
    """How many versions sort before a new decision: those with the same dates or earlier ones."""
    key = render.version_key(by_ref[ref], math.inf)
    return sum(1 for i, v in order if render.version_key(by_ref[v["ref"]], i) <= key)


def update_prompt(update: RuleUpdate, ctx: Context, passages: Mapping[str, dict]) -> str:
    versions = []
    for e in update.entries:
        d = ctx.by_ref.get(e.ref) if e.status != REMOVED else None
        decision = None
        if d is not None:
            decision = {k: v for k, v in analyze._consolidation_input(d, ctx.organ(d.doc_id)).items() if k != "ref"}
        history = {k: e.version.get(k) for k in ("effekt", "tekst", "kort", "kort_regel")} if e.version else None
        versions.append({"ref": e.ref, "status": e.status, "beslutning": decision, "historik": history})
    rule = {"titel": update.title, "vigtig": update.vigtig, "note": update.note, "versioner": versions}
    sources = [passages[e.ref] for e in update.entries if e.status in (ADDED, CHANGED) and e.ref in passages]
    return (f"Kategori: {CATEGORIES.get(update.category, update.category)}\n\n<regel>\n"
            f"{json.dumps(rule, ensure_ascii=False, indent=0)}\n</regel>\n\n<kilder>\n"
            f"{json.dumps(sources, ensure_ascii=False, indent=0)}\n</kilder>")


class UpdateRejected(ValueError):
    """An update answer code does not accept: versions missing, unknown or before the insertion point."""


def merge(update: RuleUpdate, output: dict, by_ref: Mapping[str, Decision], stamp: dict) -> list[dict]:
    """The rule's versions after an update answer: the kept ones as they were, then Claude's, sorted as render shows
    them (Claude's order on equal dates), each with its proposal effect fixed by its outcome, its fingerprint and
    `updated` provenance. Raises UpdateRejected unless the answer has exactly the expected versions."""
    got = [v["ref"] for v in output["versioner"]]
    expected = update.expected
    if sorted(got) != sorted(expected):
        before = sorted(set(got) & {e.ref for e in update.entries if e.status == KEEP})
        detail = f"rewrote versions before the insertion point ({', '.join(before)})" if before else (
            f"returned {', '.join(got) or 'nothing'} instead of {', '.join(expected)}")
        raise UpdateRejected(f"{update.name}: {detail}")
    tail = sorted(enumerate(output["versioner"]), key=lambda iv: render.version_key(by_ref[iv[1]["ref"]], iv[0]))
    written = [{"ref": v["ref"], "effekt": PROPOSAL_EFFECT.get(by_ref[v["ref"]].udfald, v["effekt"]),
                "tekst": v["tekst"], "kort": v["kort"], "kort_regel": v["kort_regel"],
                "dhash": decision_hash(by_ref[v["ref"]]), "updated": stamp} for _, v in tail]
    versions = [*update.keep, *written]
    if _dump(versions[:len(update.keep)]) != _dump(update.keep):
        raise AssertionError(f"{update.name}: versions before the insertion point changed")
    return versions


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


@dataclass(frozen=True)
class Updated:
    versions: list[dict]
    vigtig: bool
    note: str | None


def run_update(update: RuleUpdate, ctx: Context, passages: Mapping[str, dict]) -> tuple[Updated, Usage]:
    """One update call, asked once more when code rejects its answer; a second rejection fails the call (the
    document is then left for the next run)."""
    usage = Usage()
    error: UpdateRejected | None = None
    for attempt in (1, 2):
        try:
            output, spent = analyze.ask_claude(UPDATE_SYSTEM, update_prompt(update, ctx, passages), UPDATE_SCHEMA,
                                               model=ctx.settings.update_model, effort=ctx.settings.update_effort,
                                               timeout=UPDATE_TIMEOUT, budget=ctx.budget)
        except ClaudeError as exc:
            raise ClaudeError(str(exc), usage + exc.usage) from exc
        usage += spent
        stamp = {"model": spent.answered_by, "prompt": analyze.prompt_hash(UPDATE_SYSTEM, UPDATE_SCHEMA),
                 "time": ctx.time}
        try:
            with analyze.usage_kept(usage):
                return Updated(merge(update, output, ctx.by_ref, stamp), output["vigtig"], output["note"]), usage
        except ClaudeError as exc:
            if not isinstance(exc.__cause__, UpdateRejected):
                raise
            error = exc.__cause__
            log.warning("Update %s, attempt %d: %s", update.name, attempt, error)
    raise ClaudeError(f"answer rejected twice: {error}", usage)


def passage(text: str, quote: str, page: int | None, context: int = PASSAGE_WORDS) -> str | None:
    """The document's own text from `context` words before the quote to `context` words after it; None when the
    quote is not in the document."""
    words = analyze.DocWords.of(text)
    start = analyze.locate_quote(quote, words, page)
    if start is None:
        return None
    spans = analyze.word_spans(text)
    first, last = max(start - context, 0), min(start + len(analyze._words(quote)) + context, len(spans)) - 1
    return text[spans[first][0]:spans[last][1]]


def passages(refs: Sequence[str], ctx: Context) -> dict[str, dict]:
    """The passage of the minutes around each decision's quote, each document read once. Where the quote cannot be
    located, or the document read, the quote itself goes instead: the decision's own fields still go to the call."""
    texts: dict[str, str | None] = {}
    found = {}
    for ref in refs:
        d = ctx.by_ref[ref]
        if d.doc_id not in texts:
            texts[d.doc_id] = _text(ctx.docs.get(d.doc_id))
        text = texts[d.doc_id]
        located = passage(text, d.citat, d.side) if text is not None else None
        found[ref] = {"ref": ref, "dokument": d.doc_id, "side": d.side,
                      **({"passage": located} if located else {"passage": None, "citat": d.citat})}
    return found


def _text(doc: Doc | None) -> str | None:
    if doc is None:
        return None
    try:
        return analyze.document_text(doc)
    except Exception as exc:  # a lost file must not keep its decisions from ever being filed
        log.warning("%s: no passages, the document cannot be read: %s", doc.id, exc)
        return None


# --------------------------------------------------------------------------- one document

@dataclass(frozen=True)
class Settings:
    assign_model: str = "claude-sonnet-5-5"
    update_model: str = "claude-opus-5-5"
    update_effort: str | None = None
    workers: int = 4
    k: int = CANDIDATE_K


@dataclass
class Context:
    """What every document of a run shares."""
    settings: Settings
    budget: RunBudget
    by_ref: dict[str, Decision]
    docs: dict[str, Doc]
    time: str  # the run's start, for the `updated` provenance

    def organ(self, doc_id: str) -> str:
        doc = self.docs.get(doc_id)
        return doc.organ_label if doc else "?"


@dataclass
class Touched:
    """An existing rule a document touches: the new decisions filed into it (its gone and changed ones follow from
    the rule itself)."""
    new: list[str] = field(default_factory=list)


def _parallel(jobs: list, fn: Callable[[object], tuple], label: str, ctx: Context,
              steps: list[StepSummary]) -> dict | None:
    """Run fn (returning output, usage, message) over the jobs; their outputs by job, or None when one failed or was
    skipped."""
    results = {}

    def job(item):
        output, usage, message = fn(item)
        results[item] = output
        return message, usage

    step = analyze.run_parallel(jobs, job, ctx.settings.workers, label, ctx.budget)
    steps.append(step)
    return results if len(results) == len(jobs) else None


def process_document(work: DocumentWork, book: RuleBook, ctx: Context, steps: list[StepSummary]) -> str | None:
    """Assign, update and write one document's work; a summary, or None when it was left for the next run (nothing
    of it is written then).

    Its retired decisions leave their rules, its changed ones are rewritten where they stand, and its new ones are
    filed by vote; every rule this touches is then updated by one call, or, when it only lost versions at its end,
    by code."""
    holders = book.holders()
    touched: dict[tuple[str, str], Touched] = {}
    for ref in (*work.retired, *work.changed):
        category, slug = holders[ref]
        if slug is not None:
            touched.setdefault((category, slug), Touched())

    choices = assign(work, book, ctx, steps) if work.new else {}
    if choices is None:
        return None
    new_rules: dict[tuple[str, str], tuple[str, list[str]]] = {}  # (category, slugified title) -> (title, refs)
    one_offs: list[str] = []
    for ref in work.new:
        choice = choices[ref]
        if choice.target == ONE_OFF:
            one_offs.append(ref)
        elif choice.target == NEW:
            category = ctx.by_ref[ref].kategori
            new_rules.setdefault((category, matching.slugify(choice.title)), (choice.title, []))[1].append(ref)
        else:
            touched.setdefault(_locate(book, choice.target), Touched()).new.append(ref)

    updates: list[RuleUpdate] = []
    kept: dict[tuple[str, str], list[dict]] = {}  # rules that only lose a version at their end
    for (category, slug), change in sorted(touched.items()):
        rule = book.rule(category, slug)
        # A touched rule is brought fully up to date, also where another document's decision is gone or changed
        # (that document's work then no longer lists it).
        removed = {v["ref"] for v in rule["versioner"] if v["ref"] not in ctx.by_ref}
        changed = {v["ref"] for v in rule["versioner"]
                   if v["ref"] in ctx.by_ref and not analyze.version_matches(v, ctx.by_ref[v["ref"]])}
        keep, entries = plan_update(rule["versioner"], ctx.by_ref, removed, changed, change.new)
        if entries is None:
            kept[category, slug] = keep
        else:
            updates.append(RuleUpdate(category, slug, rule["titel"], rule.get("vigtig", True), rule["note"],
                                      tuple(entries), tuple(keep)))
    for (category, _), (title, refs) in sorted(new_rules.items()):
        _, entries = plan_update([], ctx.by_ref, set(), set(), refs)
        updates.append(RuleUpdate(category, None, title, None, None, tuple(entries), ()))

    needed = [e.ref for u in updates for e in u.entries if e.status in (ADDED, CHANGED)]
    sources = passages(needed, ctx)
    results = _parallel(list(range(len(updates))), lambda i: (*run_update(updates[i], ctx, sources),
                                                               f"{updates[i].name}: updated"),
                        f"Update {work.doc_id}", ctx, steps) if updates else {}
    if results is None:
        return None
    _apply(work, book, ctx, kept, [(updates[i], results[i]) for i in range(len(updates))], one_offs)
    filed = len(work.new) - len(one_offs) - sum(len(refs) for _, refs in new_rules.values())
    return (f"{work.doc_id}: {len(work.new)} new ({filed} into existing rules, {len(new_rules)} new rules, "
            f"{len(one_offs)} one-offs), {len(work.changed)} changed, {len(work.retired)} retired; {len(updates)} "
            f"rules updated, {sum(1 for keep in kept.values() if not keep)} retired")


def _locate(book: RuleBook, slug: str) -> tuple[str, str]:
    return next((category, rule["slug"]) for category, rule in book.rules() if rule["slug"] == slug)


def _apply(work: DocumentWork, book: RuleBook, ctx: Context, kept: Mapping[tuple[str, str], list[dict]],
           results: Sequence[tuple[RuleUpdate, Updated]], one_offs: Sequence[str]) -> None:
    """Write one document's edits: rule versions, new and retired rules, one-offs and the slug history.

    As in analyze._consolidate_one, the slug history is written first with revived slugs still in it, so whichever
    write fails, every slug stays taken."""
    changed: set[str] = set()
    with analyze._SLUGS_LOCK:
        registry = analyze.load_slugs()
        former: dict[str, FormerSlug] = {}
        for (category, slug), keep in kept.items():
            rule = book.rule(category, slug)
            if keep:
                rule["versioner"] = keep
            else:  # every version is gone: the rule is retired, and resolve_slugs leads its slug on
                former[slug] = FormerSlug(category, rule["titel"], analyze._refs(rule))
                book.files[category]["regler"].remove(rule)
            changed.add(category)
        for u, updated in results:
            if u.slug is not None:
                rule = book.rule(u.category, u.slug)
                rule["versioner"], rule["note"] = updated.versions, updated.note
                changed.add(u.category)
        new = [(u, updated) for u, updated in results if u.slug is None]
        plan = matching.carry_slugs([], [RuleRefs(u.title, frozenset(v["ref"] for v in updated.versions))
                                         for u, updated in new], book.slugs() | registry.taken(), registry.former())
        for (u, updated), slug in zip(new, plan.slugs):
            book.file(u.category, ctx.settings.update_model)["regler"].append(
                {"titel": u.title, "slug": slug, "vigtig": updated.vigtig, "note": updated.note,
                 "versioner": updated.versions})
            changed.add(u.category)
        filed = {*work.retired, *work.new}  # no longer one-offs or unassigned, unless one-offs now
        for category, stored in book.files.items():
            for key in ("udeladt", "ikke_tildelt"):
                remaining = [ref for ref in stored.get(key, []) if ref not in filed]
                if remaining != stored.get(key, []):
                    stored[key] = remaining
                    changed.add(category)
        for ref in one_offs:
            category = ctx.by_ref[ref].kategori
            book.file(category, ctx.settings.update_model)["udeladt"].append(ref)
            changed.add(category)
        history = registry.with_former(former)
        analyze._save_slugs(history)
        book.write(changed)
        if plan.revived:
            analyze._save_slugs(history.without(plan.revived))


# --------------------------------------------------------------------------- the step

def consolidate(docs: list[Doc], decisions: list[Decision], settings: Settings, budget: RunBudget | None = None,
                documents: set[str] | None = None, now: datetime | None = None) -> StepSummary:
    """Bring the rule files up to date with the decisions, one document at a time; `documents` limits the work to
    those documents (update.py --only).

    A document whose call fails or is skipped is left whole for the next run, and later documents go on. Categories
    whose decisions are then all reflected get the input_hash of their current input (settle), so the checks and
    `full` see them as consolidated; the slug history is resolved as after a full consolidation."""
    started = time.monotonic()
    budget = budget if budget is not None else RunBudget()
    book = RuleBook.load()

    def pending(attempted: set[str]) -> list[DocumentWork]:
        """The queue as the rule files stand now: a document's update may have settled another's work."""
        return [work for work in work_queue(decisions, book, docs)
                if work.doc_id not in attempted and (documents is None or work.doc_id in documents)]

    queue = pending(set())
    log.info("Consolidate (incremental): %d documents with %d decisions to file, %d changed, %d retired", len(queue),
             sum(len(w.new) for w in queue), sum(len(w.changed) for w in queue), sum(len(w.retired) for w in queue))
    ctx = Context(settings, budget, {d.ref: d for d in decisions}, {doc.id: doc for doc in docs},
                  (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    steps: list[StepSummary] = []
    attempted: set[str] = set()
    done = failed = skipped = 0
    while queue:
        work, n = queue[0], len(attempted) + 1
        attempted.add(work.doc_id)
        if limit := budget.exhausted():
            log.warning("Consolidate: %s is left for the next run because %s", work.doc_id, limit)
            skipped += 1
        else:
            before = len(steps)
            try:
                message = process_document(work, book, ctx, steps)
            except Exception as exc:  # one document must not stop the others; the next run tries it again
                log.error("Consolidate [%d] %s failed: %s", n, work.doc_id, exc)
                book = RuleBook.load()  # drop whatever the failure left half-edited in memory
                message, failed = None, failed + 1
            else:
                if message is None and any(step.failed for step in steps[before:]):
                    failed += 1
                elif message is None:
                    skipped += 1
            if message is None:
                log.warning("Consolidate [%d] %s is left for the next run", n, work.doc_id)
            else:
                done += 1
                log.info("Consolidate [%d] %s", n, message)
        queue = pending(attempted)
    settled = settle(book, decisions, docs, settings.update_model)
    if settled:
        log.info("Consolidate: %s reflect all their decisions again (input_hash updated)", ", ".join(sorted(settled)))
    analyze.resolve_slugs(ctx.by_ref)
    usage = sum((step.usage for step in steps), Usage())
    log.info("Consolidate (incremental) done: %d documents filed, %d failed, %d left (%.2f USD at API list price)",
             done, failed, skipped, usage.cost_usd)
    return StepSummary("Consolidate", done + failed, failed, skipped, usage, time.monotonic() - started)


def settle(book: RuleBook, decisions: Sequence[Decision], docs: Sequence[Doc], model: str) -> set[str]:
    """Give each category whose decisions the rule files all reflect the input_hash of its current input, under the
    version its file was written with, and write it; returns the categories written.

    input_hash then keeps meaning "this file reflects these decisions": a run of `full` redoes nothing an incremental
    run finished (but still everything after a new CONSOLIDATE_VERSION), and the checks treat only categories with
    work left as pending (checks.Data.pending). A category whose decisions all went to other categories' rules gets an
    empty file, so `full` does not take it for lost."""
    open_ = queue_categories(work_queue(decisions, book, docs), decisions, book)
    organ = {doc.id: doc.organ_label for doc in docs}
    items: dict[str, list[dict]] = {}
    for d in decisions:
        items.setdefault(d.kategori, []).append(analyze._consolidation_input(d, organ[d.doc_id]))
    for category in items.keys() - book.files.keys() - open_:
        book.file(category, model)
    changed = set()
    for category, stored in book.files.items():
        if category in open_:
            continue
        input_hash = analyze._hash(items.get(category, []), stored.get("version"))
        if stored.get("input_hash") != input_hash:
            stored["input_hash"] = input_hash
            changed.add(category)
    book.write(changed)
    return changed
