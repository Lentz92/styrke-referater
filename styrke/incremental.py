"""Incremental consolidation: add each new decision to the rule it belongs to and leave every other rule as it is.

Full consolidation (analyze.consolidate) rewrites a whole category for one new decision, which costs Opus output and
rewrites past history at random. Here the work is only what the rule files do not reflect yet (work_queue), done one
document at a time in date order, so later documents see the rules earlier ones created:

1. Candidates (styrke/candidates.py, no Claude): the CANDIDATE_K live rules whose words are closest to each new
   decision.
2. Assign (Sonnet, three independent votes per document): each new decision goes to one of its candidates, to a new
   rule or to its category's one-offs (udeladt); two votes of three decide, else one Opus call does, blind to them.
   Code rejects any answer outside the candidates offered. A changed decision (same id, other content) stays in its
   rule without a vote; a retired one leaves its rule.
3. Update (Opus, one call per touched rule): the rule's whole history, the new or changed decisions and the passage
   of the minutes around each quote. Claude writes the versions from the earliest touched one on (the insertion
   point); the versions before it stay byte-identical, which code checks, and no other rule is touched.

A document is applied only when all its calls succeed: a failure leaves it, whole, for the next run. What a rule file
reflects is known by fingerprints of each decision's whole consolidation input (fingerprint), date and organ
included, taken when the run starts: a decision whose document was dated anew is rewritten like a changed one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone

from styrke import analyze, matching, render
from styrke.analyze import (CATEGORIES, PROPOSAL_EFFECT, ClaudeError, Decision, RunBudget, StepSummary, Usage,
                            decision_hash)
from styrke.candidates import CANDIDATE_K, CandidateIndex, Query, RuleProfile
from styrke.matching import FormerSlug, RuleRefs
from styrke.scrape import Doc

log = logging.getLogger(__name__)

NEW = "new"
ONE_OFF = "one-off"
VOTES = 3
# Seconds one assign or update call may take before it is retried.
ASSIGN_TIMEOUT = 600
UPDATE_TIMEOUT = 1200
# The passage of the minutes an update call gets: words before a quote and after it, extended to the decision's vote
# count (stemmer) when that stands within VOTE_REACH words after the quote; a vote count's numbers stand at most
# VOTE_SPAN words apart ("31 stemmer for, 7 imod og 7 blanke").
PASSAGE_BEFORE = 100
PASSAGE_AFTER = 400
VOTE_REACH = 2000
VOTE_SPAN = 12
# Latest decisions of a candidate rule that the assign prompt shows.
HISTORY_SHOWN = 6

# What the statuses of a version mean to an update call; those that must come back are RETURNED.
KEEP, ADDED, CHANGED, REWRITE, REMOVED = "keep", "new", "changed", "rewrite", "removed"
RETURNED = (ADDED, CHANGED, REWRITE)


# --------------------------------------------------------------------------- prompts

def _not_decisions() -> str:
    """EXTRACT_SYSTEM's list of what is no decision, verbatim: a one-off is what the extraction should have left out."""
    _, found, rest = analyze.EXTRACT_SYSTEM.partition("Do NOT extract:\n")
    listed, found_end, _ = rest.partition("\n\nField rules:")
    if not (found and found_end):
        raise ValueError("EXTRACT_SYSTEM no longer has the list of what not to extract that the assign prompt quotes")
    return listed


NOT_DECISIONS = _not_decisions()

ASSIGN_SYSTEM = f"""\
You file new decisions from the minutes of Dansk Styrkeløft Forbund (DSF), the Danish powerlifting federation, into \
an overview of its rules, which shows the version of each rule in force in each year. A rule is one thing of which \
exactly one version is in force at a time; a later decision changes, confirms or abolishes it (e.g. "Licensgebyr": \
200 kr. -> 300 kr.), and proposals to change it that were rejected, withdrawn or not decided yet belong to it too.

The input lists new decisions from one document (<beslutninger>). Each has a ref, date (dato), organ, udfald, \
handling, niveau, kategori, emne, tekst, optionally stemmer, forslagsstiller, gaelder_fra and gaelder_til, and \
kandidater: the slugs of the existing rules whose words are closest to it, in no particular order. <regler> \
describes each candidate rule: its title, the rule as it stands now (regel) and its latest decisions (historik).

Answer for every decision with its ref and one choice:
- the slug of one of its own candidates, when the decision is about that rule: it introduces, changes, confirms, \
abolishes or proposes to change the same thing, whatever its wording or amount. Prefer an existing rule when the \
decision touches any part of what it regulates; when two candidates fit, pick the one whose current version it \
changes or confirms;
- "new", when it concerns a standing rule none of its candidates is about. Then give title: a short Danish title for \
the new rule, without years or amounts, that would stay the same if the rule changed later, e.g. "Licensgebyr", \
"Klubskifte", "Kvalifikationskrav EM, klassisk senior". Decisions of this document about the same new rule get \
exactly the same title;
- "one-off", only when the decision is one of the things the extraction should have left out:
{NOT_DECISIONS}
  These are not one-offs: a fee or rate set or confirmed with a budget, a rule for one season, year or competition \
(one with gaelder_til), and proposals, also rejected or withdrawn ones; they belong to a rule.
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
<kilder> has the passage of the minutes around the quote of each new or changed decision, from about 100 words \
before it to its vote count or about 400 words after it (only the quote where the passage could not be found). When \
the minutes contradict a decision's fields, keep the fields and say so in note.

Return versioner: exactly the versions with status new, changed or rewrite, each once, in chronological order, each \
with
  - ref: exactly as given in the input
{VERSION_FIELDS}

Also return vigtig and note for the rule. vigtig is kept as the input gives it, so repeat it; only when it is null \
(a new rule) decide it. note replaces the current note, so repeat the current one unless the new decisions settle it \
or add a real caveat.
{RULE_FIELDS}

{PRECEDENCE}

misfiled: the decisions with status new that do not belong to this rule at all, because they are about something it \
does not regulate; they are filed elsewhere, and you may leave them out of versioner. Give each one's ref and title: \
a short Danish title for the rule it does belong to, as for a new rule (without years or amounts), or null. Almost \
always empty, and always for a new rule (vigtig null).
"""

UPDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "versioner": analyze.CONSOLIDATE_SCHEMA["properties"]["regler"]["items"]["properties"]["versioner"],
        "vigtig": {"type": "boolean"},
        "note": {"type": ["string", "null"]},
        "misfiled": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"ref": {"type": "string"}, "title": {"type": ["string", "null"]}},
                "required": ["ref", "title"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["versioner", "vigtig", "note", "misfiled"],
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
        for category in sorted(self.files, key=analyze.category_order):
            for rule in self.files[category]["regler"]:
                yield category, rule

    def raw_rules(self) -> list[dict]:
        """The rules with their category, as analyze.load_rules gives them."""
        return [{**rule, "kategori": category} for category, rule in self.rules()]

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

    def held(self, category: str) -> set[str]:
        """The refs a category's file reflects: those of its rules' versions and its one-offs."""
        stored = self.files.get(category, {"regler": []})
        return {v["ref"] for rule in stored["regler"] for v in rule["versioner"]} | set(stored.get("udeladt", []))

    def slugs(self) -> set[str]:
        return {rule["slug"] for _, rule in self.rules()}

    def file(self, category: str, model: str) -> dict:
        """The category's file, created empty when it has none yet (only a run with --allow-rebuild gets here: the
        rebuild guard stops a category with decisions but no file)."""
        if category not in self.files:
            self.files[category] = {"kategori": category, "version": analyze.CONSOLIDATE_VERSION, "input_hash": None,
                                    "model": model, "regler": [], "udeladt": [], "ikke_tildelt": []}
        return self.files[category]

    def write(self, categories: set[str]) -> None:
        for category in sorted(categories):
            analyze._write_json(analyze.RULES_DIR / f"{category}.json", self.files[category])


# --------------------------------------------------------------------------- what the rule files reflect

def fingerprint(d: Decision, organ: str) -> str:
    """Everything a consolidation reads of a decision: its consolidation input, date and organ included, and its
    category. decision_hash leaves the date and organ out (they belong to the document); a re-dated document must
    still reach the rules it is in."""
    text = json.dumps([analyze._consolidation_input(d, organ), d.kategori], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def organs(docs: Sequence[Doc]) -> dict[str, str]:
    return {doc.id: doc.organ_label for doc in docs}


def category_items(decisions: Sequence[Decision], home: Mapping[str, str],
                   organ: Mapping[str, str]) -> dict[str, list[dict]]:
    """Each home category's consolidation input, as analyze.consolidation_todo hashes it."""
    items: dict[str, list[dict]] = {}
    for d in decisions:
        items.setdefault(home[d.ref], []).append(analyze._consolidation_input(d, organ[d.doc_id]))
    return items


@dataclass(frozen=True)
class Known:
    """What the rule files are known to reflect when a run starts."""
    fingerprints: Mapping[str, str]  # ref -> the decision's fingerprint as the rule files reflect it
    current: frozenset[str]  # categories whose input_hash matched their input then


def known_inputs(decisions: Sequence[Decision], book: RuleBook, docs: Sequence[Doc]) -> Known:
    """The fingerprint of every decision a category's file holds, as the files reflect it: all of them when the
    category's input_hash matches its current input, with or without the decisions no file holds yet (the file
    reflects exactly the others), else those the file stores (`inputs`, which incremental runs write). styrke/update.py
    takes this before the extraction, so a decision whose date or organ changes in the extraction differs from it."""
    organ = organs(docs)
    by_ref = {d.ref: d for d in decisions}
    items = category_items(decisions, analyze.home_categories(decisions, book.raw_rules()), organ)
    held_anywhere = set().union(*(book.held(category) for category in book.files))
    unfiled = by_ref.keys() - held_anywhere
    found: dict[str, str] = {}
    current = set()
    for category, stored in book.files.items():
        held = book.held(category) & by_ref.keys()
        version = stored.get("version")
        inputs = items.get(category, [])
        if stored.get("input_hash") in (analyze._hash(inputs, version),
                                        analyze._hash([i for i in inputs if i["ref"] not in unfiled], version)):
            current.add(category)
            found.update({ref: fingerprint(by_ref[ref], organ[by_ref[ref].doc_id]) for ref in held})
        else:
            found.update({ref: fp for ref, fp in stored.get("inputs", {}).items() if ref in held})
    return Known(found, frozenset(current))


# --------------------------------------------------------------------------- work queue

@dataclass(frozen=True)
class DocumentWork:
    """What the rule files do not reflect yet of one document's decisions."""
    doc_id: str
    date: str | None
    new: tuple[str, ...]  # in no rule and not a one-off of its category, in reading order
    changed: tuple[str, ...]  # in a rule whose version was built from other content (dhash, or date or organ)
    retired: tuple[str, ...]  # named by a rule, the one-offs or ikke_tildelt, but no current decision any more


def work_queue(decisions: Sequence[Decision], book: RuleBook, docs: Sequence[Doc],
               known: Known | None = None) -> list[DocumentWork]:
    """The decisions the rule files do not reflect, per document, documents in date order (undated last).

    A decision is reflected by a rule version built from it (its fingerprint, dhash), or as a one-off of its own
    category (udeladt; a one-off filed under another category's file is not one of its category). With `known`, a
    decision whose fingerprint differs from the one the files reflect is queued too: in a rule as changed, as a
    one-off as new. Retired ids belong to the document their id names."""
    live = {d.ref: d for d in decisions}
    versions = {v["ref"]: v for _, rule in book.rules() for v in rule["versioner"]}
    organ = organs(docs)
    by_doc: dict[str, dict[str, list[str]]] = {}

    def add(doc_id: str, kind: str, ref: str) -> None:
        by_doc.setdefault(doc_id, {"new": [], "changed": [], "retired": []})[kind].append(ref)

    def moved(d: Decision) -> bool:
        was = known.fingerprints.get(d.ref) if known is not None else None
        return was is not None and was != fingerprint(d, organ[d.doc_id])

    for d in decisions:
        if d.ref in versions:
            if not analyze.version_matches(versions[d.ref], d) or moved(d):
                add(d.doc_id, "changed", d.ref)
        elif d.ref not in book.files.get(d.kategori, {}).get("udeladt", []) or moved(d):
            add(d.doc_id, "new", d.ref)
    for ref in sorted(set(book.holders()) - set(live)):
        add(ref.rpartition("#")[0], "retired", ref)
    dates = {doc.id: doc.date for doc in docs} | {d.doc_id: d.dato for d in decisions}
    queue = [DocumentWork(doc_id, dates.get(doc_id), tuple(kinds["new"]), tuple(kinds["changed"]),
                          tuple(kinds["retired"])) for doc_id, kinds in by_doc.items()]
    return sorted(queue, key=lambda w: (w.date is None, w.date or "", w.doc_id))


def queue_categories(queue: Sequence[DocumentWork], decisions: Sequence[Decision], book: RuleBook) -> frozenset[str]:
    """The categories the queued work changes: those of new and changed decisions, and the files holding changed or
    retired ids. styrke/update.py's rebuild guard counts them like the categories a full consolidation would redo."""
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


def namesake(book: RuleBook, title: str) -> str | None:
    """The slug of the first live rule a new rule of this title would be named like: the same title, or its slug."""
    wanted = matching.slugify(title)
    return next((rule["slug"] for _, rule in book.rules()
                 if matching.slugify(rule["titel"]) == wanted or rule["slug"] == wanted), None)


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
    """A candidate rule as the assign prompt shows it: the rule now and its latest decisions. Its category is left
    out: the voters should judge by what the rule regulates, and extraction categories are noisy."""
    profile = rule_profile(category, rule, by_ref)
    shown = ordered_versions(rule["versioner"], by_ref)[-HISTORY_SHOWN:]
    history = [f"{by_ref[v['ref']].dato or 'ukendt dato'}: {render.EFFEKT_LABELS[v['effekt']]}: "
               f"{v.get('kort') or by_ref[v['ref']].tekst}" for v in shown]
    return {"slug": rule["slug"], "titel": rule["titel"], "regel": profile.text, "historik": history}


def assign(work: DocumentWork, refs: Sequence[str], book: RuleBook, ctx: Context, steps: list[StepSummary],
           excluded: Mapping[str, set[str]] | None = None) -> dict[str, Choice] | None:
    """The choice for each of these new decisions, or None when a call failed or was skipped, or the tie-break gave
    no valid choice. `excluded`: rules an update call found a decision misfiled in, which it is not offered again.

    A "new" rule named like a live rule (namesake) goes to the Opus tie-break with that rule offered: once filed, a
    wrong new rule is never merged back."""
    excluded = excluded or {}
    index = candidate_index(book, ctx.by_ref)
    offered = {ref: [slug for slug, _ in index.rank(query(ctx.by_ref[ref])) if slug not in excluded.get(ref, ())]
               [:ctx.settings.k] for ref in refs}

    def ask(model: str, vote: int, asked: Sequence[str], label: str) -> tuple[dict, Usage, str]:
        output, usage = ctx.ask(ASSIGN_SYSTEM, assign_prompt(work, asked, offered, book, ctx, vote), ASSIGN_SCHEMA,
                                model=model, effort=None, timeout=ASSIGN_TIMEOUT)
        return output, usage, f"{work.doc_id} {label}"

    answers = _parallel(list(range(VOTES)), lambda vote: ask(ctx.settings.assign_model, vote, refs, f"vote {vote + 1}"),
                        f"Assign {work.doc_id}", ctx, steps)
    if answers is None:
        return None
    decided, open_ = tally([read_vote(answers[vote], offered) for vote in range(VOTES)], refs)
    for ref, choice in list(decided.items()):
        same = namesake(book, choice.title) if choice.target == NEW else None
        if same is not None and same not in excluded.get(ref, ()):
            offered[ref] = offered[ref] if same in offered[ref] else [*offered[ref], same]
            del decided[ref]
            open_.append(ref)
    if open_:
        settled = tie_break(work, {ref: offered[ref] for ref in open_}, book, ctx, steps)
        if settled is None:
            return None
        missing = [ref for ref in open_ if ref not in settled]
        if missing:
            log.error("%s: the tie-break gave no valid choice for %s", work.doc_id, ", ".join(missing))
            return None
        decided |= settled
    log.info("%s: %d decisions assigned, %d by two of three votes, %d by the tie-break", work.doc_id, len(refs),
             len(refs) - len(open_), len(open_))
    return decided


def tie_break(work: DocumentWork, offered: Mapping[str, Sequence[str]], book: RuleBook, ctx: Context,
              steps: list[StepSummary]) -> dict[str, Choice] | None:
    """One Opus call on these decisions, with the same prompt the votes get and blind to them: its valid choices
    (those it gave none for are missing), or None when the call failed or was skipped."""
    refs = list(offered)
    prompt = assign_prompt(work, refs, offered, book, ctx, 0)
    answer = _parallel([0], lambda _: (*ctx.ask(ASSIGN_SYSTEM, prompt, ASSIGN_SCHEMA, model=ctx.settings.update_model,
                                                effort=None, timeout=ASSIGN_TIMEOUT), f"{work.doc_id} tie-break"),
                       f"Tie-break {work.doc_id}", ctx, steps)
    return None if answer is None else read_vote(answer[0], offered)


def file_as_new(work: DocumentWork, titles: Mapping[str, str], namesakes: set[str], book: RuleBook, ctx: Context,
                steps: list[StepSummary], excluded: Mapping[str, set[str]]) -> dict[str, Choice] | None:
    """Choices for decisions misfiled twice: a new rule titled as the update call suggested (else by the decision's
    emne), so the document is filed, not left pending at every run. As for a voted new rule, a title named like a live
    rule the decision was not misfiled in goes to the Opus tie-break with that rule offered, once (`namesakes`: the
    refs to check); without a valid answer, and after that, the new rule stands: a new rule is never misfiled."""
    choices = {ref: Choice(NEW, title) for ref, title in titles.items()}
    offered = {ref: [same] for ref, title in titles.items() if ref in namesakes
               and (same := namesake(book, title)) is not None and same not in excluded.get(ref, ())}
    if offered:
        settled = tie_break(work, offered, book, ctx, steps)
        if settled is None:
            return None
        choices |= settled
    return choices


# --------------------------------------------------------------------------- update

@dataclass(frozen=True)
class Entry:
    """One version of a rule as an update call sees it."""
    ref: str
    status: str  # KEEP, ADDED, CHANGED, REWRITE or REMOVED
    version: dict | None  # the stored version; None for a new decision


@dataclass(frozen=True)
class Plan:
    """What happens to one rule's versions: those kept as they are, and, when a call is needed, what it sees."""
    keep: tuple[dict, ...]  # the versions before the insertion point, in file order
    entries: tuple[Entry, ...] | None  # chronological, removed versions where they stood; None: no call needed
    point: int  # the insertion point: how many stored versions, in render's order, come before it


@dataclass(frozen=True)
class RuleUpdate:
    """One update call: a rule (slug None for a new one), its versions as the call sees them and as they are stored."""
    category: str
    slug: str | None
    title: str
    vigtig: bool | None
    note: str | None
    plan: Plan
    stored: tuple[dict, ...] = ()  # the rule's versions as stored, in file order
    removed: frozenset[str] = frozenset()  # refs of versions whose decision is gone

    @property
    def entries(self) -> tuple[Entry, ...]:
        return self.plan.entries or ()

    @property
    def expected(self) -> list[str]:
        return [e.ref for e in self.entries if e.status in RETURNED]

    @property
    def name(self) -> str:
        return self.slug or f"new rule {self.title!r}"


def plan_update(versions: Sequence[dict], by_ref: Mapping[str, Decision], removed: set[str], changed: set[str],
                new: Sequence[str], removed_dates: Mapping[str, str | None] | None = None) -> Plan:
    """The insertion point of a rule's changes and what an update call sees.

    In render's order (render.version_key) the insertion point is the earliest of: a changed decision; where a new
    decision sorts (after versions with the same dates, as if appended); where a removed one stood, by the date of
    its document (`removed_dates`; it has no decision left to date it). From there on every version is rewritten. A
    removed version with nothing after it needs no call: the versions before it never depended on it.
    """
    removed_dates = removed_dates or {}
    live = [(i, v) for i, v in enumerate(versions) if v["ref"] not in removed]
    order = sorted(live, key=lambda iv: render.version_key(by_ref[iv[1]["ref"]], iv[0]))
    rank = {i: r for r, (i, _) in enumerate(order)}
    keys = [render.version_key(by_ref[v["ref"]], i) for i, v in order]
    removed_at: dict[int, list[dict]] = {}
    for j, v in enumerate(versions):
        if v["ref"] in removed:
            day = removed_dates.get(v["ref"]) or ""
            removed_at.setdefault(sum(1 for key in keys if key < (day, day, j)), []).append(v)
    new_at: dict[int, list[str]] = {}
    for position, ref in sorted((_position(ref, keys, by_ref), ref) for ref in new):
        new_at.setdefault(position, []).append(ref)
    starts = [r for r in removed_at if r < len(order)] + [rank[i] for i, v in live if v["ref"] in changed]
    starts += list(new_at)
    if not starts:
        return Plan(tuple(v for _, v in live), None, len(order))
    p = min(starts)
    entries: list[Entry] = []
    for r in range(len(order) + 1):
        entries += [Entry(v["ref"], REMOVED, v) for v in removed_at.get(r, [])]
        entries += [Entry(ref, ADDED, None) for ref in new_at.get(r, [])]
        if r < len(order):
            v = order[r][1]
            status = KEEP if r < p else CHANGED if v["ref"] in changed else REWRITE
            entries.append(Entry(v["ref"], status, v))
    return Plan(tuple(v for i, v in live if rank[i] < p), tuple(entries), p)


def _position(ref: str, keys: Sequence[tuple], by_ref: Mapping[str, Decision]) -> int:
    """How many versions sort before a new decision: those with the same dates or earlier ones."""
    key = render.version_key(by_ref[ref], math.inf)
    return sum(1 for k in keys if k <= key)


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
    `updated` provenance. Raises UpdateRejected unless the answer has exactly the expected versions, and fails when
    the versions before the insertion point, in render's order, are not those stored."""
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
    versions = [*update.plan.keep, *written]
    stored = [v for v in update.stored if v["ref"] not in update.removed]
    point = update.plan.point
    if _dump(ordered_versions(versions, by_ref)[:point]) != _dump(ordered_versions(stored, by_ref)[:point]):
        raise AssertionError(f"{update.name}: the versions before the insertion point are not those stored")
    return versions


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=1)


@dataclass(frozen=True)
class Updated:
    versions: list[dict]
    vigtig: bool
    note: str | None


@dataclass(frozen=True)
class Misfiled:
    """An update call found new decisions that do not belong to its rule at all."""
    found: tuple[tuple[str, str | None], ...]  # (ref, a title for the rule it belongs to, or None)


def run_update(update: RuleUpdate, ctx: Context, passages: Mapping[str, dict]) -> tuple[Updated | Misfiled, Usage]:
    """One update call, asked once more when code rejects its answer; a second rejection fails the call (the
    document is then left for the next run). Misfiled new decisions of an existing rule come back as Misfiled."""
    usage = Usage()
    error: UpdateRejected | None = None
    added = {e.ref for e in update.entries if e.status == ADDED}
    for attempt in (1, 2):
        try:
            output, spent = ctx.ask(UPDATE_SYSTEM, update_prompt(update, ctx, passages), UPDATE_SCHEMA,
                                    model=ctx.settings.update_model, effort=ctx.settings.update_effort,
                                    timeout=UPDATE_TIMEOUT)
        except ClaudeError as exc:
            raise ClaudeError(str(exc), usage + exc.usage) from exc
        usage += spent
        misfiled = tuple((item["ref"], (item.get("title") or "").strip() or None) for item in output.get("misfiled", [])
                         if isinstance(item, dict) and item.get("ref") in added) if update.slug else ()
        if misfiled:
            return Misfiled(misfiled), usage
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


def passage(text: str, quote: str, page: int | None, stemmer: str | None = None) -> str | None:
    """The document's own text from PASSAGE_BEFORE words before the quote to PASSAGE_AFTER words after it, or on to
    the decision's vote count when that stands further on (minutes often state the vote after the debate); None when
    the quote is not in the document."""
    words = analyze.DocWords.of(text)
    start = analyze.locate_quote(quote, words, page)
    if start is None:
        return None
    spans = analyze.word_spans(text)
    after = start + len(analyze._words(quote))
    end = max(after + PASSAGE_AFTER, _vote_end(words.words, stemmer, after) or 0)
    first, last = max(start - PASSAGE_BEFORE, 0), min(end, len(spans)) - 1
    return text[spans[first][0]:spans[last][1]]


def _vote_end(words: Sequence[str], stemmer: str | None, after: int) -> int | None:
    """The word offset just after the decision's vote count in the document, looked for from `after` on: its numbers
    (or, without numbers, its words) in order, each at most VOTE_SPAN words after the one before, and then the words
    the vote count ends with ("2 blanke"); None when not found."""
    tokens = analyze._words(stemmer or "")
    numbers = [t for t in tokens if t.isdigit()]
    wanted = numbers or tokens
    if not wanted:
        return None
    trailing = tokens[len(tokens) - tokens[::-1].index(numbers[-1]):] if numbers else []
    for i in range(after, min(after + VOTE_REACH, len(words))):
        if words[i] == wanted[0] and (at := _follow(words, i, wanted[1:])) is not None:
            return (_follow(words, at, trailing) or at) + 1
    return None


def _follow(words: Sequence[str], at: int, tokens: Sequence[str]) -> int | None:
    """The offset of the last of `tokens` found in order after `at`, each at most VOTE_SPAN words after the one
    before; None when one is missing."""
    for token in tokens:
        at = next((j for j in range(at + 1, min(at + 1 + VOTE_SPAN, len(words))) if words[j] == token), None)
        if at is None:
            return None
    return at


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
        located = passage(text, d.citat, d.side, d.stemmer) if text is not None else None
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
    """What every document of a run shares, and what the run has done so far."""
    settings: Settings
    budget: RunBudget
    by_ref: dict[str, Decision]
    docs: dict[str, Doc]
    time: str  # the run's start, for the `updated` provenance
    known: Known  # what the rule files reflected when the run started
    worked: frozenset[str] = frozenset()  # categories with queued work when the run started
    processed: set[str] = field(default_factory=set)  # refs this run filed or rewrote, at their current fingerprint
    calls: int = 0  # Claude calls asked
    notes: dict[str, list[str]] = field(default_factory=dict)  # heading -> lines for run-report.md
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def note(self, heading: str, line: str) -> None:
        """Something to look at, logged and listed under `heading` in run-report.md."""
        log.warning("%s: %s", heading, line)
        self.notes.setdefault(heading, []).append(line)

    def organ(self, doc_id: str) -> str:
        doc = self.docs.get(doc_id)
        return doc.organ_label if doc else "?"

    def fingerprint(self, ref: str) -> str:
        d = self.by_ref[ref]
        return fingerprint(d, self.organ(d.doc_id))

    def verified(self, ref: str) -> bool:
        """Whether the rule files reflect the decision as it is now: this run filed or rewrote it, or it is
        unchanged since the run started."""
        return ref in self.processed or self.known.fingerprints.get(ref) == self.fingerprint(ref)

    def inputs(self, book: RuleBook, category: str) -> dict[str, str]:
        """The fingerprints a category's file records (`inputs`): those of the decisions it reflects as they are."""
        return {ref: self.fingerprint(ref) for ref in sorted(book.held(category))
                if ref in self.by_ref and self.verified(ref)}

    def document_date(self, doc_id: str) -> str | None:
        dates = [d.dato for d in self.by_ref.values() if d.doc_id == doc_id and d.dato]
        doc = self.docs.get(doc_id)
        return dates[0] if dates else doc.date if doc else None

    def reflect(self, refs: set[str]) -> None:
        """The rule files now reflect these decisions as they are: they are verified, and no longer queued as moved."""
        self.processed |= refs
        self.known = Known({**self.known.fingerprints, **{ref: self.fingerprint(ref) for ref in refs}},
                           self.known.current)

    def ask(self, system: str, prompt: str, schema: dict, *, model: str, effort: str | None,
            timeout: float) -> tuple[dict, Usage]:
        with self._lock:
            self.calls += 1
        return analyze.ask_claude(system, prompt, schema, model=model, effort=effort, timeout=timeout,
                                  budget=self.budget)


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


@dataclass(frozen=True)
class DocumentPlan:
    """What one document does to the rules, given the choices for its new decisions."""
    updates: tuple[RuleUpdate, ...]  # one call each
    kept: Mapping[tuple[str, str], tuple[dict, ...]]  # rules that only lose versions at their end; none left: retired
    one_offs: tuple[str, ...]
    new_rules: int


def plan_document(work: DocumentWork, book: RuleBook, ctx: Context, choices: Mapping[str, Choice]) -> DocumentPlan:
    """The update calls and code edits for a document. Every rule it touches is brought fully up to date, also where
    another document's decision is gone or changed (that document's work then no longer lists it). New decisions
    that chose the same new title (by slug) form one new rule, in the first one's category."""
    holders = book.holders()
    touched: dict[tuple[str, str], list[str]] = {}
    for ref in (*work.retired, *work.changed):
        category, slug = holders[ref]
        if slug is not None:
            touched.setdefault((category, slug), [])
    new_rules: dict[str, tuple[str, str, list[str]]] = {}  # slugified title -> (category, title, refs)
    one_offs: list[str] = []
    for ref in work.new:
        choice = choices[ref]
        if choice.target == ONE_OFF:
            one_offs.append(ref)
        elif choice.target == NEW:
            new_rules.setdefault(matching.slugify(choice.title), (ctx.by_ref[ref].kategori, choice.title, []))[2] \
                .append(ref)
        else:
            touched.setdefault(_locate(book, choice.target), []).append(ref)
    updates: list[RuleUpdate] = []
    kept: dict[tuple[str, str], tuple[dict, ...]] = {}
    for (category, slug), new in sorted(touched.items()):
        rule = book.rule(category, slug)
        removed = {v["ref"] for v in rule["versioner"] if v["ref"] not in ctx.by_ref}
        changed = {v["ref"] for v in rule["versioner"] if v["ref"] in ctx.by_ref and (
            not analyze.version_matches(v, ctx.by_ref[v["ref"]]) or _moved(v["ref"], ctx))}
        plan = plan_update(rule["versioner"], ctx.by_ref, removed, changed, new,
                           {ref: ctx.document_date(ref.rpartition("#")[0]) for ref in removed})
        if plan.entries is None:
            kept[category, slug] = plan.keep
        else:
            updates.append(RuleUpdate(category, slug, rule["titel"], rule.get("vigtig", True), rule["note"], plan,
                                      tuple(rule["versioner"]), frozenset(removed)))
    for _, (category, title, refs) in sorted(new_rules.items()):
        updates.append(RuleUpdate(category, None, title, None, None, plan_update([], ctx.by_ref, set(), set(), refs)))
    return DocumentPlan(tuple(updates), kept, tuple(one_offs), len(new_rules))


def _moved(ref: str, ctx: Context) -> bool:
    """The decision's date or organ (or other input) changed since the run started: it is rewritten like a changed
    one."""
    was = ctx.known.fingerprints.get(ref)
    return was is not None and was != ctx.fingerprint(ref) and ref not in ctx.processed


def _locate(book: RuleBook, slug: str) -> tuple[str, str]:
    return next((category, rule["slug"]) for category, rule in book.rules() if rule["slug"] == slug)


def run_updates(plan: DocumentPlan, answered: Sequence[tuple[RuleUpdate, Updated]], ctx: Context,
                steps: list[StepSummary], label: str) -> list[Updated | Misfiled] | None:
    """Each planned update's answer: one asked before for exactly the same update is reused, the others are asked."""
    reused = {i: next(result for prev, result in answered if prev == u) for i, u in enumerate(plan.updates)
              if any(prev == u for prev, _ in answered)}
    todo = [i for i in range(len(plan.updates)) if i not in reused]
    sources = passages([e.ref for i in todo for e in plan.updates[i].entries if e.status in (ADDED, CHANGED)], ctx)
    results = _parallel(todo, lambda i: (*run_update(plan.updates[i], ctx, sources), f"{plan.updates[i].name}: done"),
                        label, ctx, steps) if todo else {}
    if results is None:
        return None
    return [reused[i] if i in reused else results[i] for i in range(len(plan.updates))]


def process_document(work: DocumentWork, book: RuleBook, ctx: Context, steps: list[StepSummary]) -> str | None:
    """Assign, update and write one document's work; a summary, or None when it was left for the next run (nothing
    of it is written then).

    Its retired decisions leave their rules, its changed ones are rewritten where they stand, and its new ones are
    filed by vote; every rule this touches is then updated by one call, or, when it only lost versions at its end,
    by code. A new decision an update call finds misfiled is voted on once more without that rule; misfiled again, it
    is filed as a new rule (file_as_new) and listed for review. Every misfiling excludes a rule and a new rule is never
    misfiled, so this ends."""
    choices = assign(work, work.new, book, ctx, steps) if work.new else {}
    if choices is None:
        return None
    excluded: dict[str, set[str]] = {}
    misfilings: Counter[str] = Counter()
    answered: list[tuple[RuleUpdate, Updated]] = []
    while True:
        plan = plan_document(work, book, ctx, choices)
        results = run_updates(plan, answered, ctx, steps, f"Update {work.doc_id}")
        if results is None:
            return None
        misfiled = {ref: (u.slug, title) for u, r in zip(plan.updates, results) if isinstance(r, Misfiled)
                    for ref, title in r.found}
        if not misfiled:
            _apply(work, book, ctx, plan, results, {ref: excluded[ref] for ref in misfilings if misfilings[ref] >= 2})
            filed = len(work.new) - len(plan.one_offs) - sum(len(u.expected) for u in plan.updates if u.slug is None)
            return (f"{work.doc_id}: {len(work.new)} new ({filed} into existing rules, {plan.new_rules} new rules, "
                    f"{len(plan.one_offs)} one-offs), {len(work.changed)} changed, {len(work.retired)} retired; "
                    f"{len(plan.updates)} rules updated, {sum(1 for keep in plan.kept.values() if not keep)} retired")
        answered += [(u, r) for u, r in zip(plan.updates, results) if isinstance(r, Updated)]
        for ref, (slug, _) in misfiled.items():
            excluded.setdefault(ref, set()).add(slug)
            misfilings[ref] += 1
        revote = sorted(ref for ref in misfiled if misfilings[ref] == 1)
        fallback = {ref: title or ctx.by_ref[ref].emne for ref, (_, title) in sorted(misfiled.items())
                    if misfilings[ref] >= 2}
        listed = ", ".join(f"{ref} in {slug}" for ref, (slug, _) in sorted(misfiled.items()))
        log.warning("%s: misfiled (%s); %s", work.doc_id, listed, "; ".join(filter(None, [
            f"voting on {', '.join(revote)} again without those rules" if revote else "",
            f"filing {', '.join(fallback)} as a new rule" if fallback else ""])))
        again = assign(work, revote, book, ctx, steps, excluded) if revote else {}
        if again is None:
            return None
        if fallback:
            again |= file_as_new(work, fallback, {ref for ref in fallback if misfilings[ref] == 2}, book, ctx, steps,
                                 excluded) or {}
            if not all(ref in again for ref in fallback):
                return None  # the namesake tie-break failed or was skipped: the next run tries again
        choices = {**choices, **again}


MISFILED_TWICE = "Filed as a new rule after two misfilings"
UNSEEN = "Categories that may have changed unseen (left for a full consolidation)"


def _misfiled_note(ref: str, rules: set[str], plan: DocumentPlan, new: Sequence[tuple[RuleUpdate, str]],
                   ctx: Context) -> str:
    """Where a decision misfiled twice went, for review: a new rule, or what the namesake tie-break chose."""
    d = ctx.by_ref[ref]
    where = next((f"filed as the new rule {u.title} (`{slug}`)" for u, slug in new if ref in u.expected), None)
    where = where or next((f"filed in `{u.slug}` by the tie-break on a rule named like it" for u in plan.updates
                           if u.slug is not None and ref in u.expected), None)
    where = where or "left out as a one-off by the tie-break on a rule named like it"
    return f"{ref} ({d.emne}): misfiled in {', '.join(f'`{slug}`' for slug in sorted(rules))}; {where}"


def _apply(work: DocumentWork, book: RuleBook, ctx: Context, plan: DocumentPlan, results: Sequence[Updated],
           misfiled_twice: Mapping[str, set[str]] | None = None) -> None:
    """Write one document's edits: rule versions, new and retired rules, one-offs, the files' fingerprints of what they
    reflect (`inputs`) and the slug history; note for review where each decision misfiled twice went
    (`misfiled_twice`: ref -> the rules it was misfiled in).

    As in analyze._consolidate_one, the slug history is written first with revived slugs still in it, so whichever
    write fails, every slug stays taken. Slugs of rules retired here are taken before new rules get theirs."""
    changed: set[str] = set()
    with analyze._SLUGS_LOCK:
        registry = analyze.load_slugs()
        former: dict[str, FormerSlug] = {}
        for (category, slug), keep in plan.kept.items():
            rule = book.rule(category, slug)
            if keep:
                rule["versioner"] = list(keep)
            else:  # every version is gone: the rule is retired, and resolve_slugs leads its slug on
                former[slug] = FormerSlug(category, rule["titel"], analyze._refs(rule))
                book.files[category]["regler"].remove(rule)
            changed.add(category)
        for u, updated in zip(plan.updates, results):
            if u.slug is not None:
                rule = book.rule(u.category, u.slug)
                rule["versioner"], rule["note"] = updated.versions, updated.note
                changed.add(u.category)
        history = registry.with_former(former)
        new = [(u, updated) for u, updated in zip(plan.updates, results) if u.slug is None]
        slugs = matching.carry_slugs([], [RuleRefs(u.title, frozenset(v["ref"] for v in updated.versions))
                                          for u, updated in new], book.slugs() | history.taken(), history.former())
        for (u, updated), slug in zip(new, slugs.slugs):
            book.file(u.category, ctx.settings.update_model)["regler"].append(
                {"titel": u.title, "slug": slug, "vigtig": updated.vigtig, "note": updated.note,
                 "versioner": updated.versions})
            changed.add(u.category)
        for ref, rules in sorted((misfiled_twice or {}).items()):
            ctx.note(MISFILED_TWICE, _misfiled_note(ref, rules, plan, [(u, s) for (u, _), s in zip(new, slugs.slugs)],
                                                    ctx))
        filed = {*work.retired, *work.new}  # no longer one-offs or unassigned, unless one-offs now
        for category, stored in book.files.items():
            for key in ("udeladt", "ikke_tildelt"):
                remaining = [ref for ref in stored.get(key, []) if ref not in filed]
                if remaining != stored.get(key, []):
                    stored[key] = remaining
                    changed.add(category)
        for ref in plan.one_offs:
            category = ctx.by_ref[ref].kategori
            book.file(category, ctx.settings.update_model)["udeladt"].append(ref)
            changed.add(category)
        ctx.reflect({ref for u in plan.updates for ref in u.expected} | set(plan.one_offs))
        for category in changed:
            book.files[category]["inputs"] = ctx.inputs(book, category)
        analyze._save_slugs(history)
        book.write(changed)
        if slugs.revived:
            analyze._save_slugs(history.without(slugs.revived))


# --------------------------------------------------------------------------- the step

def consolidate(docs: list[Doc], decisions: list[Decision], settings: Settings, budget: RunBudget | None = None,
                documents: set[str] | None = None, now: datetime | None = None,
                known: Known | None = None) -> StepSummary:
    """Bring the rule files up to date with the decisions, one document at a time; `documents` limits the work to
    those documents (uv run -m styrke.update --only). `known`: what the rule files reflected when the run started
    (known_inputs, before the extraction); taken now when not given.

    A document whose call fails or is skipped is left whole for the next run, and later documents go on. Categories
    whose decisions are then all reflected get the input_hash of their current input (settle), so the checks and
    `full` see them as consolidated; the slug history is resolved as after a full consolidation. The step counts
    Claude calls; failed and skipped also count documents left for a reason other than a call (a tie-break without a
    valid answer, misfiled twice, the cost limit reached before they started)."""
    started = time.monotonic()
    budget = budget if budget is not None else RunBudget()
    book = RuleBook.load()
    ctx = Context(settings, budget, {d.ref: d for d in decisions}, {doc.id: doc for doc in docs},
                  (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"),
                  known if known is not None else known_inputs(decisions, book, docs))

    def pending(attempted: set[str]) -> list[DocumentWork]:
        """The queue as the rule files stand now: a document's update may have settled another's work."""
        return [work for work in work_queue(decisions, book, docs, ctx.known)
                if work.doc_id not in attempted and (documents is None or work.doc_id in documents)]

    queue = pending(set())
    ctx.worked = queue_categories(queue, decisions, book)
    log.info("Consolidate (incremental): %d documents with %d decisions to file, %d changed, %d retired", len(queue),
             sum(len(w.new) for w in queue), sum(len(w.changed) for w in queue), sum(len(w.retired) for w in queue))
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
                if message is None and not any(step.failed or step.skipped for step in steps[before:]):
                    failed += 1  # left without a failed call: no valid tie-break, or misfiled twice
            if message is None:
                log.warning("Consolidate [%d] %s is left for the next run", n, work.doc_id)
            else:
                done += 1
                log.info("Consolidate [%d] %s", n, message)
        queue = pending(attempted)
    settled = settle(book, decisions, docs, ctx)
    if settled:
        log.info("Consolidate: %s reflect all their decisions again (input_hash updated)", ", ".join(sorted(settled)))
    analyze.resolve_slugs(ctx.by_ref)
    usage = sum((step.usage for step in steps), Usage())
    log.info("Consolidate (incremental) done: %d of %d documents filed, %d Claude calls (%.2f USD at API list price)",
             done, len(attempted), ctx.calls, usage.cost_usd)
    return StepSummary("Consolidate", ctx.calls, failed + sum(step.failed for step in steps),
                       skipped + sum(step.skipped for step in steps), usage, time.monotonic() - started,
                       {heading: tuple(lines) for heading, lines in ctx.notes.items()})


def settle(book: RuleBook, decisions: Sequence[Decision], docs: Sequence[Doc], ctx: Context) -> set[str]:
    """Give each category whose decisions the rule files all reflect the input_hash of its current input, under the
    version its file was written with, and write it with the fingerprints it reflects; returns the categories written.

    input_hash then keeps meaning "this file reflects these decisions": a run of `full` redoes nothing an incremental
    run finished (but still everything after a new CONSOLIDATE_VERSION), and the checks treat only categories with
    work left as pending (checks.Data.pending). A category is never settled while one of its decisions may have
    changed unseen: its input_hash was stale when the run started and nothing was queued for it, or a decision it
    holds has no known fingerprint. A category whose decisions all went to other categories' rules gets an empty file,
    so `full` does not take it for lost."""
    open_ = queue_categories(work_queue(decisions, book, docs, ctx.known), decisions, book)
    items = category_items(decisions, analyze.home_categories(decisions, book.raw_rules()), organs(docs))
    for category in items.keys() - book.files.keys() - open_:
        book.file(category, ctx.settings.update_model)
    changed = set()
    for category, stored in book.files.items():
        input_hash = analyze._hash(items.get(category, []), stored.get("version"))
        if category in open_ or stored.get("input_hash") == input_hash:
            continue
        unseen = sorted(ref for ref in book.held(category) if ref in ctx.by_ref and not ctx.verified(ref))
        if category not in ctx.known.current and category not in ctx.worked or unseen:
            why = (f"no recorded input for {', '.join(unseen[:5])}{' …' if len(unseen) > 5 else ''}" if unseen else
                   "its input changed before this run, and nothing was queued for it")
            ctx.note(UNSEEN, f"{category} may reflect decisions that changed unseen ({why}); it stays to be "
                             f"consolidated: run `uv run -m styrke.update --consolidate-mode full` for it")
            continue
        stored["input_hash"], stored["inputs"] = input_hash, ctx.inputs(book, category)
        changed.add(category)
    book.write(changed)
    return changed
