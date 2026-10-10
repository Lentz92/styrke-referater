"""Audit the rules' structure: merge, split, rename and move rules through a reviewed ops file.

    uv run -m styrke.audit propose --max-cost 25  # Opus proposes ops per category, twice: data/regler_ops.json
    uv run -m styrke.audit apply --max-cost 10    # the agreed ops applied to data/; merged and split rules rewritten

Incremental consolidation files each new decision into a rule and never merges, splits or renames rules, so a rule
spread over several stays spread. The audit fixes the structure: code finds rules whose words are close
(styrke/candidates.py, rule against rule), Opus proposes ops per category in two independent runs, and only the ops both
runs propose are applied, by code: decision ids and slugs are kept, a merged-away slug becomes an alias in
data/slugs.json, a split-off part gets a new slug. Each merged or split rule then gets one Opus call that rewrites its
history's texts. An audit changes earlier years by design, so it always goes to a pull request
(.github/workflows/audit.yml), never directly to the website.

Every Claude answer is kept under data/audit/ with a fingerprint of what it was asked, so a cut-off or repeated command
never pays twice for the same question; each command that calls Claude adds a line to data/runs.jsonl, from which the
report counts what the audit cost. audit-report.md (not committed) is the pull request's body.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import tempfile
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import cached_property
from datetime import date, datetime, timezone
from itertools import groupby
from pathlib import Path

from styrke import analyze, checks, claude, evaluate, incremental, matching, render, scrape, update, website
from styrke.analyze import CATEGORIES, Decision
from styrke.claude import ClaudeError, RunBudget, StepSummary, Usage
from styrke.incremental import REWRITE, Entry, RuleBook, RuleUpdate, UpdateRejected
from styrke.matching import FormerSlug, RuleRefs, SlugRegistry
from styrke.scrape import Doc

log = logging.getLogger(__name__)

OPS_PATH = scrape.DATA_DIR / "regler_ops.json"
CACHE_DIR = scrape.DATA_DIR / "audit"  # every Claude answer, by call
REPORT = scrape.ROOT / "audit-report.md"
OPS_VERSION = 1
MODEL = "claude-opus-5-5"
EFFORT = "high"
RUNS = 2
OP_KINDS = ("merge", "split", "rename", "move")
# Rules whose TF-IDF vectors (styrke/candidates.py's profiles: title, latest text, kort_regel and the emne of each
# decision) have at least this cosine similarity may be one rule. A propose call gets its category's rules and every
# rule of another category this similar to one of them. Chosen on the answer key with the retired `audit.py candidates`
# (commit 9f39874): at least 95% of the pairs of rules a key rule is spread over must land in one call. Before the v3
# migration 0.1 was the highest threshold that did (97%; 0.12: 94%); on the migrated data it puts 100% into one call,
# and 0.15 only 96.7%, one pair above the target, so 0.1 keeps a margin for about 2 USD more per audit
# (eval/reports/audit-candidates.md).
SIMILARITY = 0.1
# The closest rules a rule lists as `ligner` in the propose prompt: a hint, not a limit.
SIMILAR_SHOWN = 5
SHORT_TEXT = 200  # characters of a decision's tekst shown where a version has no kort
PROPOSE_TIMEOUT = 1800
TITLE_TIMEOUT = 600
# Minutes after which no new Claude call starts, for propose and apply together in the workflow (it gives apply what
# propose left). A call running then may go on for PROPOSE_TIMEOUT (30 minutes), and the result must still reach its
# pull request before the job's 120 minutes are up, kept answers included, or a rerun pays for them again.
DEFAULT_TIME_BUDGET = 75
TEXT_TIMEOUT = incremental.UPDATE_TIMEOUT
# List price per token of MODEL as Claude Code reports it, fitted to every Opus step in eval/runs.jsonl (exactly): a
# new prompt is written to the cache at 8 USD per million tokens, output costs 20. Output per call is a guess at
# effort high (the answer key's judges wrote about 4K tokens per call); the estimate is printed before any call.
PROMPT_USD = 8e-6
OUTPUT_USD = 20e-6
PROPOSE_OUTPUT = 8000
TITLE_OUTPUT = 2000
TEXT_OUTPUT = 2000
TEXT_OUTPUT_PER_VERSION = 250
# The fields of a version the text rewrite may change; everything else must stay as apply set it.
TEXT_FIELDS = ("effekt", "tekst", "kort", "kort_regel")


# --------------------------------------------------------------------------- prompts

def _category_definitions() -> str:
    """EXTRACT_SYSTEM's definitions of the categories, verbatim: a move follows the extraction's own criteria."""
    _, found, rest = analyze.EXTRACT_SYSTEM.partition("- kategori: one of\n")
    listed, end, _ = rest.partition("\n- udfald:")
    if not (found and end):
        raise ValueError("EXTRACT_SYSTEM no longer has the category definitions the propose prompt quotes")
    return listed


CATEGORY_DEFINITIONS = _category_definitions()

PROPOSE_SYSTEM = f"""\
You review the structure of an overview of the rules of Dansk Styrkeløft Forbund (DSF), the Danish powerlifting \
federation, which shows the version of each rule in force in each year. A rule is one thing of which exactly one \
version is in force at a time; a later decision changes, confirms or abolishes it (e.g. "Licensgebyr": 200 kr. -> \
300 kr.), and proposals to change it that were rejected, withdrawn or not decided yet belong to it too. The rules \
were filed one decision at a time and never regrouped, so some went wrong: one rule spread over several (a fee's \
yearly confirmations filed under another rule), one rule holding decisions about different things, a title that \
misnames what the rule regulates, a rule in the wrong category.

The input is one category's rules (<regler>) and the rules of other categories whose words are closest to them \
(<lignende>). A rule in <regler> has its slug, titel, regel (what it says now, in a few words), ligner (the slugs of \
the rules whose words are closest to its own, in either list or in neither; only a hint) and its versions in \
chronological order, each a decision: ref, dato, organ, effekt, emne (the subject the decision was extracted under) \
and kort (what the decision did). A rule in <lignende> has its slug, kategori, titel, regel, the emner of its \
decisions and the dates of its first and last decision. <kategorier> lists the categories; what each covers:
{CATEGORY_DEFINITIONS}

Propose the ops that make the overview truer. Most rules need none, and an empty list is a valid answer: an op \
that is not clearly right makes the overview worse. Each op has:
- op and rules, the slugs exactly as given:
  - merge: two or more rules that are one rule, so that a version of one replaces the version of the other in \
force: the same fee, deadline, requirement or procedure for the same people. Rules that are only related stay \
apart: different fees, the same kind of requirement for different groups, championships or seasons, a rule and an \
exception to it. At least one of the rules is in <regler>.
  - split: one rule of <regler> that holds decisions about different things, each of which should have its own \
version in force.
  - rename: one rule of <regler> whose title misnames what its versions regulate.
  - move: one rule of <regler> that belongs to another category.
- title: for rename the new title; for merge a new title only when no title of the merged rules fits all of them, \
else null; null for split and move. A title is short and Danish, without years or amounts, and would stay the same \
if the rule changed later, e.g. "Licensgebyr", "Klubskifte", "Kvalifikationskrav EM, klassisk senior".
- category: for move the key of the category the rule belongs to; for merge the merged rule's category when its \
rules are in different categories, else null; null for split and rename.
- parts: for split each part's title and refs: every ref of the rule goes to exactly one part, and every part \
gets at least one; [] for the other ops.
- reason: one or two English sentences saying why, naming what the versions regulate.
- refs: the refs of the decisions the reason rests on, from <regler>.
A rule may be in one op only, except that one rename and one move may go together.
"""

PROPOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(OP_KINDS)},
                    "rules": {"type": "array", "items": {"type": "string"}},
                    "title": {"type": ["string", "null"]},
                    "category": {"type": ["string", "null"]},
                    "parts": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"title": {"type": "string"},
                                           "refs": {"type": "array", "items": {"type": "string"}}},
                            "required": ["title", "refs"],
                            "additionalProperties": False,
                        },
                    },
                    "reason": {"type": "string"},
                    "refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["op", "rules", "title", "category", "parts", "reason", "refs"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ops"],
    "additionalProperties": False,
}

TITLE_SYSTEM = """\
You choose titles for rules in an overview of the rules of Dansk Styrkeløft Forbund (DSF), the Danish powerlifting \
federation, which shows the version of each rule in force in each year. Two reviewers proposed titles for each rule \
in <regler>, which has its id, its current title where it has one (titel), what it says now (regel), the subjects of \
its decisions (emner) and the options (muligheder). Choose for each rule the option that best names what it \
regulates: short and Danish, without years or amounts, and the same if the rule changed later. Keep the current \
title, when it is among the options, unless another option is clearly better. Answer for every id with the option \
exactly as given.
"""

TITLE_SCHEMA = {
    "type": "object",
    "properties": {
        "valg": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "titel": {"type": "string"}},
                "required": ["id", "titel"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["valg"],
    "additionalProperties": False,
}

# incremental's update prompt, told why every version is rewritten: the rule's history was written for other rules.
AUDIT_UPDATE_SYSTEM = incremental.UPDATE_SYSTEM + """
This call comes from an audit of the overview, not from new minutes: the rule was just formed by merging rules that \
were one rule, or by splitting a rule that held decisions about different things into parts, of which this is one. \
Its historik was written for the rules as they were, so every version has status rewrite: rewrite the history from \
the first version on, so that each version's effekt compares it with the version before it in this rule and its \
tekst states the whole rule in force after it, covering only this rule. There are no new decisions and no <kilder>; \
misfiled stays empty.
"""


# --------------------------------------------------------------------------- data

@dataclass(frozen=True)
class Data:
    """What the audit reads of data/."""
    docs: list[Doc]
    decisions: list[Decision]
    book: RuleBook
    registry: SlugRegistry

    @classmethod
    def load(cls) -> Data:
        docs = scrape.load_manifest()
        return cls(docs, analyze.load_decisions(docs), RuleBook.load(), analyze.load_slugs())

    @cached_property
    def by_ref(self) -> dict[str, Decision]:
        return {d.ref: d for d in self.decisions}

    @cached_property
    def organ(self) -> dict[str, str]:
        return incremental.organs(self.docs)

    def located(self) -> dict[str, tuple[str, dict]]:
        """Slug -> (category, rule) of every rule."""
        return {rule["slug"]: (category, rule) for category, rule in self.book.rules()}


def unsettled(data: Data) -> list[str]:
    """Why data/ is not ready for an audit, empty when it is: the rule files must reflect every decision as it is
    (nothing for styrke/update.py to file, no category to consolidate again) and pass the checks, or the audit would
    build on rules that are about to change, and its own checks could not tell its errors from those already there."""
    reasons = []
    work = update.pending_work(data.docs)
    if work.categories:
        reasons.append(f"decisions to file in {', '.join(sorted(work.categories))} (run `uv run -m styrke.update`)")
    if work.outdated:
        reasons.append(f"categories consolidated with another CONSOLIDATE_VERSION, which a full consolidation "
                       f"regroups anew: {', '.join(sorted(work.outdated))} (migrate first: `{update.MIGRATE}`)")
    todo = analyze.consolidation_todo(data.decisions, {d.id: d.organ_label for d in data.docs})
    if todo:
        reasons.append(f"categories whose rule file does not reflect its decisions: "
                       f"{', '.join(sorted(job.category for job in todo))}")
    errors = checks.errors(checks.find_problems(checks.Data.load(data.docs)))
    if errors:
        reasons.append(f"{len(errors)} check errors (`uv run -m styrke.checks` lists them)")
    if work.documents:
        log.warning("%d documents are not extracted yet (%s …); the audit goes ahead, and styrke/update.py files "
                    "their decisions into the audited rules later", len(work.documents),
                    ", ".join(sorted(work.documents)[:3]))
    return reasons


def data_fingerprint() -> str:
    """Fingerprint of the rule files and the slug history: ops proposed for other rules are not applied."""
    digest = hashlib.sha256()
    for path in [*sorted(analyze.RULES_DIR.glob("*.json")), analyze.SLUGS_PATH]:
        if path.exists():
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


# --------------------------------------------------------------------------- candidates

def similar_rules(data: Data) -> dict[str, list[tuple[str, float]]]:
    """Slug -> the other rules, in any category, at least SIMILARITY similar to it, most similar first."""
    index = incremental.candidate_index(data.book, data.by_ref)
    return {slug: [(other, score) for other, score in index.similar(slug) if score >= SIMILARITY]
            for slug in index.vectors}


# --------------------------------------------------------------------------- Claude calls, kept by fingerprint

@dataclass(frozen=True)
class Call:
    """One Claude call; its answer is kept in CACHE_DIR/<name>.json with a fingerprint of everything it depends on."""
    name: str
    system: str
    prompt: str
    schema: dict
    timeout: float
    output_tokens: int  # expected, for the estimate

    def fingerprint(self) -> str:
        text = json.dumps([self.system, self.prompt, self.schema, MODEL, EFFORT], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    @property
    def path(self) -> Path:
        return CACHE_DIR / f"{self.name}.json"

    def stored(self) -> dict | None:
        """The kept answer to exactly this call: {"fingerprint", "time", "provenance", "usage", "output"}, or None."""
        if not self.path.exists():
            return None
        stored = json.loads(self.path.read_text())
        return stored if stored.get("fingerprint") == self.fingerprint() else None

    def estimate(self) -> tuple[int, float]:
        """Input tokens and list-price USD, roughly."""
        tokens = claude.estimate_tokens(self.system, self.prompt, self.schema)
        return tokens, tokens * PROMPT_USD + self.output_tokens * OUTPUT_USD


def ask(call: Call, budget: RunBudget, cli: str, accept: Callable[[dict], object] | None = None) -> tuple[str, Usage]:
    """Ask Claude and keep the answer; `accept` checks it first (raising keeps it out of the cache)."""
    output, usage = claude.ask(call.system, call.prompt, call.schema, model=MODEL, effort=EFFORT, timeout=call.timeout,
                               budget=budget)
    with claude.usage_kept(usage):
        if accept is not None:
            accept(output)
        scrape.write_atomic(call.path, analyze.json_text({
            "fingerprint": call.fingerprint(), "time": _now(),
            "provenance": claude.provenance(usage, cli, call.system, call.schema, EFFORT),
            "usage": claude.usage_json(usage), "output": output}).encode())
    return f"{call.name}: {claude.describe_usage(usage)}", usage


def plan_calls(label: str, calls: Sequence[Call], max_cost: float, note: str = "") -> list[Call]:
    """The calls without a kept answer; prints how many and what they should cost before any is made."""
    todo = [call for call in calls if call.stored() is None]
    tokens = sum(call.estimate()[0] for call in todo)
    usd = sum(call.estimate()[1] for call in todo)
    over = "; more than the limit, so the calls left when it is reached are asked by the next run" \
        if usd > max_cost else ""
    print(f"{label}: {len(todo)} Claude calls to {MODEL} (effort {EFFORT}), {len(calls) - len(todo)} answers kept "
          f"already; about {tokens / 1000:.0f}K tokens in, {sum(c.output_tokens for c in todo) / 1000:.0f}K out: "
          f"about {usd:.2f} USD at list price{note} (limit {max_cost:.2f}{over})", flush=True)
    return todo


def run_calls(label: str, calls: Sequence[Call], budget: RunBudget, workers: int,
              job: Callable[[Call, RunBudget, str], tuple[str, Usage]] = ask) -> StepSummary | None:
    if not calls:
        return None
    cli = claude.cli_version()
    return claude.run_parallel(list(calls), lambda call: job(call, budget, cli), workers, label, budget)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- propose: what each call sees

@dataclass(frozen=True)
class View:
    """One propose call: its category's rules in full and similar rules of other categories in brief, rotated for
    each run so the runs do not read them in the same order."""
    category: str
    run: int
    full: tuple[str, ...]  # slugs, as shown
    brief: tuple[str, ...]
    refs: frozenset[str]  # the decision refs shown: those of the full rules
    prompt: str

    @property
    def call(self) -> Call:
        return Call(f"propose-{self.category}-{self.run}", PROPOSE_SYSTEM, self.prompt, PROPOSE_SCHEMA,
                    PROPOSE_TIMEOUT, PROPOSE_OUTPUT)


def rotated(items: Sequence[str], run: int) -> list[str]:
    k = (run - 1) * len(items) // RUNS
    return [*items[k:], *items[:k]]


def views(data: Data, categories: Sequence[str], similar: Mapping[str, list[tuple[str, float]]]) -> list[View]:
    located = data.located()
    by_ref, organ = data.by_ref, data.organ
    found = []
    for category in categories:
        own = [rule["slug"] for c, rule in data.book.rules() if c == category]
        if not own:
            continue
        closeness: dict[str, float] = {}
        for slug in own:
            for other, score in similar[slug]:
                if located[other][0] != category:
                    closeness[other] = max(closeness.get(other, 0.0), score)
        brief = sorted(closeness, key=lambda slug: (-closeness[slug], slug))
        refs = frozenset(v["ref"] for slug in own for v in located[slug][1]["versioner"])
        for run in range(1, RUNS + 1):
            full, near = rotated(own, run), rotated(brief, run)
            rules = [full_view(*located[slug], by_ref, organ, similar[slug]) for slug in full]
            others = [brief_view(*located[slug], by_ref) for slug in near]
            prompt = (f"Kategori: {category} ({CATEGORIES[category]})\n\n<kategorier>\n"
                      f"{json.dumps(CATEGORIES, ensure_ascii=False, indent=0)}\n</kategorier>\n\n<regler>\n"
                      f"{json.dumps(rules, ensure_ascii=False, indent=0)}\n</regler>\n\n<lignende>\n"
                      f"{json.dumps(others, ensure_ascii=False, indent=0)}\n</lignende>")
            found.append(View(category, run, tuple(full), tuple(near), refs, prompt))
    return found


def full_view(category: str, rule: dict, by_ref: Mapping[str, Decision], organ: Mapping[str, str],
              similar: Sequence[tuple[str, float]]) -> dict:
    versions = []
    for v in incremental.ordered_versions(rule["versioner"], by_ref):
        d = by_ref[v["ref"]]
        versions.append({"ref": v["ref"], "dato": d.dato, "organ": organ.get(d.doc_id, "?"), "effekt": v["effekt"],
                         "emne": d.emne, "kort": v.get("kort") or _short(d.tekst)})
    return {"slug": rule["slug"], "titel": rule["titel"], "regel": _now_says(category, rule, by_ref),
            "ligner": [slug for slug, _ in similar[:SIMILAR_SHOWN]], "versioner": versions}


def brief_view(category: str, rule: dict, by_ref: Mapping[str, Decision]) -> dict:
    ordered = incremental.ordered_versions(rule["versioner"], by_ref)
    dates = [by_ref[v["ref"]].dato or "ukendt" for v in ordered]
    return {"slug": rule["slug"], "kategori": category, "titel": rule["titel"],
            "regel": _now_says(category, rule, by_ref),
            "emner": list(dict.fromkeys(by_ref[v["ref"]].emne for v in ordered))[:6],
            "datoer": f"{dates[0]} – {dates[-1]}" if dates else None}


def _now_says(category: str, rule: dict, by_ref: Mapping[str, Decision]) -> str:
    """The rule's essence in force now, or last: its latest kort_regel, else its latest text, shortened."""
    profile = incremental.rule_profile(category, rule, by_ref)
    return profile.kort_regel or _short(profile.text)


def _short(text: str | None) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= SHORT_TEXT else text[:SHORT_TEXT].rsplit(" ", 1)[0] + " …"


# --------------------------------------------------------------------------- propose: ops

@dataclass(frozen=True)
class Part:
    title: str
    refs: tuple[str, ...]  # in the rule's order


@dataclass(frozen=True)
class Op:
    kind: str
    rules: tuple[str, ...]  # merge: its slugs, sorted; the others: one slug
    title: str | None = None
    category: str | None = None
    parts: tuple[Part, ...] = ()  # split: in the order of each part's first version

    def key(self) -> tuple:
        """What two runs must agree on: merge the same rules, split the same partition, move to the same category;
        a rename only the rule (the title is chosen by a third call)."""
        if self.kind == "merge":
            return "merge", self.rules
        if self.kind == "split":
            return "split", self.rules[0], frozenset(frozenset(p.refs) for p in self.parts)
        if self.kind == "move":
            return "move", self.rules[0], self.category
        return "rename", self.rules[0]


@dataclass(frozen=True)
class Proposal:
    op: Op
    run: int
    call: str  # the category whose call proposed it
    reason: str
    refs: tuple[str, ...]  # the decisions it cites


@dataclass(frozen=True)
class Rejection:
    run: int
    call: str
    answer: dict  # the op as answered
    why: str


def read_answer(output: dict, view: View, located: Mapping[str, tuple[str, dict]],
                by_ref: Mapping[str, Decision]) -> tuple[list[Proposal], list[Rejection]]:
    """One answer's ops that code accepts, and those it rejects with the reason."""
    proposals, rejected = [], []
    for raw in output.get("ops", []):
        if not isinstance(raw, dict):
            continue
        result = validate(raw, view, located, by_ref)
        if isinstance(result, str):
            rejected.append(Rejection(view.run, view.category, raw, result))
        else:
            proposals.append(Proposal(result, view.run, view.category, str(raw.get("reason") or "").strip(),
                                      tuple(dict.fromkeys(raw.get("refs") or []))))
    return proposals, rejected


def validate(raw: dict, view: View, located: Mapping[str, tuple[str, dict]],
             by_ref: Mapping[str, Decision]) -> Op | str:
    """The op an answer describes, or why code rejects it: an unknown op, slug, ref or category, a merge without a
    rule of the call's category, another op on a rule of another category, a rename to the same title, a move to the
    same category, or a split whose parts do not partition the rule's versions exactly."""
    kind = raw.get("op")
    if kind not in OP_KINDS:
        return f"unknown op {kind!r}"
    slugs = list(dict.fromkeys(str(slug) for slug in raw.get("rules") or []))
    unknown = [slug for slug in slugs if slug not in view.full and slug not in view.brief]
    if unknown:
        return f"unknown slug {', '.join(unknown)}"
    cited = [str(ref) for ref in raw.get("refs") or []]
    if not cited:
        return "cites no decision"
    if bad := [ref for ref in cited if ref not in view.refs]:
        return f"unknown ref {', '.join(bad)}"
    category = raw.get("category")
    if category is not None and category not in CATEGORIES:
        return f"unknown category {category!r}"
    title = _title(raw.get("title"))
    if kind == "merge":
        if len(slugs) < 2:
            return "a merge needs two or more rules"
        if not set(slugs) & set(view.full):
            return f"a merge needs a rule of {view.category}"
        return Op("merge", tuple(sorted(slugs)), title, category)
    if len(slugs) != 1:
        return f"a {kind} is about exactly one rule"
    slug = slugs[0]
    if slug not in view.full:
        return f"{slug} is not a rule of {view.category}"
    rule_category, rule = located[slug]
    if kind == "rename":
        if not title or title == rule["titel"]:
            return "a rename needs a new title"
        return Op("rename", (slug,), title)
    if kind == "move":
        if category is None or category == rule_category:
            return "a move needs another category"
        return Op("move", (slug,), category=category)
    return _split(slug, rule, raw.get("parts") or [], by_ref)


def _split(slug: str, rule: dict, raw_parts: list, by_ref: Mapping[str, Decision]) -> Op | str:
    order = {v["ref"]: i for i, v in enumerate(incremental.ordered_versions(rule["versioner"], by_ref))}
    parts = []
    for raw in raw_parts:
        refs = [str(ref) for ref in (raw.get("refs") or [])] if isinstance(raw, dict) else []
        if unknown := [ref for ref in refs if ref not in order]:
            return f"the parts name refs {slug} does not hold: {', '.join(unknown)}"
        title = _title(raw.get("title")) if isinstance(raw, dict) else None
        if not title or not refs:
            return "every part needs a title and at least one ref"
        parts.append(Part(title, tuple(sorted(set(refs), key=order.__getitem__))))
    given = Counter(ref for part in parts for ref in part.refs)
    if len(parts) < 2 or any(n > 1 for n in given.values()) or set(given) != set(order):
        missing = sorted(set(order) - set(given))
        twice = sorted(ref for ref, n in given.items() if n > 1)
        detail = "; ".join(filter(None, [f"missing {', '.join(missing)}" if missing else "",
                                         f"twice {', '.join(twice)}" if twice else "",
                                         "fewer than two parts" if len(parts) < 2 else ""]))
        return f"the parts do not partition the versions of {slug} ({detail})"
    return Op("split", (slug,), parts=tuple(sorted(parts, key=lambda p: order[p.refs[0]])))


def _title(value: object) -> str | None:
    return " ".join(value.split()) or None if isinstance(value, str) else None


def conflicts(ops: Sequence[Op]) -> dict[tuple, str]:
    """The ops of one run that share a rule with another op of the run, by key, with why: a rule may be in one op
    only, except one rename and one move together."""
    by_rule: dict[str, list[Op]] = defaultdict(list)
    for op in ops:
        for slug in op.rules:
            by_rule[slug].append(op)
    found: dict[tuple, str] = {}
    for slug, shared in sorted(by_rule.items()):
        kinds = Counter(op.kind for op in shared)
        if len(shared) > 1 and (kinds["merge"] or kinds["split"] or kinds["rename"] > 1 or kinds["move"] > 1):
            for op in shared:
                others = ", ".join(sorted(other.kind for other in shared if other is not op))
                found.setdefault(op.key(), f"conflicts with {others} on {slug} in the same run")
    return found


@dataclass
class Proposed:
    """One op as the runs proposed it; `agreed` when every run did. For an agreed op code fills in what apply does:
    the slug a merge keeps (`keeps`), its title and category, and the titles of a split's parts."""
    op: Op
    proposals: list[Proposal]
    agreed: bool
    keeps: str | None = None
    title: str | None = None
    category: str | None = None
    parts: tuple[Part, ...] = ()


def agree(proposals: Sequence[Proposal]) -> tuple[list[Proposed], list[Rejection]]:
    """The ops of all runs, each agreed when every run proposes it without a conflict, and those rejected for one."""
    rejected = []
    valid: dict[int, set[tuple]] = {}
    by_key: dict[tuple, list[Proposal]] = {}
    for run in range(1, RUNS + 1):
        mine = [p for p in proposals if p.run == run]
        ops = {p.op.key(): p.op for p in reversed(mine)}  # the first proposal of a key stands for it
        bad = conflicts(list(ops.values()))
        for p in mine:
            if p.op.key() in bad:
                rejected.append(Rejection(run, p.call, op_json(p.op), bad[p.op.key()]))
            else:
                by_key.setdefault(p.op.key(), []).append(p)
        valid[run] = set(ops) - set(bad)
    found = [Proposed(given[0].op, given, all(key in valid[run] for run in valid)) for key, given in by_key.items()]
    found.sort(key=lambda p: (not p.agreed, OP_KINDS.index(p.op.kind), p.op.rules, str(p.op.category)))
    return found, rejected


def survivor(rules: Sequence[tuple[str, dict]], by_ref: Mapping[str, Decision]) -> int:
    """Which of the merged rules keeps its slug (they are given in category and file order): the one with most
    versions, on a tie the one whose first version is oldest (render's order), then the first given."""
    def key(i: int) -> tuple:
        ordered = incremental.ordered_versions(rules[i][1]["versioner"], by_ref)
        first = render.version_key(by_ref[ordered[0]["ref"]], 0)[:2] if ordered else ("", "")
        return -len(rules[i][1]["versioner"]), first, i

    return min(range(len(rules)), key=key)


def keeping_part(parts: Sequence[Part]) -> int:
    """The part of a split that keeps the rule's slug: the largest, on a tie the first (matching.carry_slugs)."""
    return min(range(len(parts)), key=lambda i: (-len(parts[i].refs), i))


@dataclass(frozen=True)
class TitleSlot:
    """A title an agreed op sets: chosen by the third call when the runs differ (always for a rename)."""
    id: str  # "<op number>" or "<op number>.<part number>"
    current: str | None  # the title kept unless another is chosen; None for a split-off part
    options: tuple[str, ...]
    ask: bool
    context: dict  # what the third call is shown of the rule

    def view(self) -> dict:
        return {"id": self.id, "titel": self.current, **self.context, "muligheder": list(self.options)}


def settle(proposed: Sequence[Proposed], data: Data) -> list[TitleSlot]:
    """Fill in what code decides for each agreed op (the slug a merge keeps, a merge's category: the runs' when they
    agree, else the kept rule's), and return the title slots: each run's title, the current title where one stays."""
    located, by_ref = data.located(), data.by_ref
    order = {slug: i for i, slug in enumerate(located)}
    slots = []
    for number, p in enumerate(proposed, start=1):
        if not p.agreed:
            continue
        first = [next(q for q in p.proposals if q.run == run) for run in range(1, RUNS + 1)]
        if p.op.kind == "merge":
            rules = [located[slug] for slug in sorted(p.op.rules, key=order.__getitem__)]
            kept = rules[survivor(rules, by_ref)][1]
            p.keeps = kept["slug"]
            targets = {q.op.category for q in first}
            p.category = targets.pop() if len(targets) == 1 else None
            titles = [q.op.title or kept["titel"] for q in first]
            slots.append(TitleSlot(str(number), kept["titel"], _options(*titles, kept["titel"]), len(set(titles)) > 1,
                                   _context([rule for _, rule in rules], by_ref)))
        elif p.op.kind == "split":
            _, rule = located[p.op.rules[0]]
            p.parts = tuple(Part("", part.refs) for part in p.op.parts)  # titled once chosen (name_titles)
            keeps = keeping_part(p.op.parts)
            for i, part in enumerate(p.op.parts):
                titles = [next(x.title for x in q.op.parts if x.refs == part.refs) for q in first]
                current = rule["titel"] if i == keeps else None
                versions = [v for v in rule["versioner"] if v["ref"] in part.refs]
                slots.append(TitleSlot(f"{number}.{i + 1}", current, _options(*titles, current), len(set(titles)) > 1,
                                       _context([{**rule, "versioner": versions}], by_ref)))
        elif p.op.kind == "rename":
            _, rule = located[p.op.rules[0]]
            slots.append(TitleSlot(str(number), rule["titel"], _options(*(q.op.title for q in first), rule["titel"]),
                                   True, _context([rule], by_ref)))
        else:
            p.category = p.op.category
    return slots


def _options(*titles: str | None) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t for t in titles if t))


def _context(rules: Sequence[dict], by_ref: Mapping[str, Decision]) -> dict:
    versions = [v for rule in rules for v in incremental.ordered_versions(rule["versioner"], by_ref)]
    content = [v for v in versions if v["effekt"] in render.CONTENT_EFFECTS]
    latest = (content or versions)[-1]
    return {"regel": latest.get("kort_regel") or _short(latest.get("tekst") or by_ref[latest["ref"]].tekst),
            "emner": list(dict.fromkeys(by_ref[v["ref"]].emne for v in versions))[:8]}


def title_call(slots: Sequence[TitleSlot]) -> Call:
    prompt = f"<regler>\n{json.dumps([s.view() for s in slots], ensure_ascii=False, indent=0)}\n</regler>"
    return Call("titles", TITLE_SYSTEM, prompt, TITLE_SCHEMA, TITLE_TIMEOUT, TITLE_OUTPUT)


def chosen_titles(slots: Sequence[TitleSlot], output: dict | None) -> tuple[dict[str, str], list[str]]:
    """Slot id -> its title: the third call's choice among the options, else (no choice asked, or none valid) the
    runs' common title, or the current one; and notes on choices code rejected."""
    answered = {str(item.get("id")): item.get("titel") for item in (output or {}).get("valg", [])
                if isinstance(item, dict)}
    titles, notes = {}, []
    for slot in slots:
        choice = answered.get(slot.id) if slot.ask else None
        if slot.ask and choice not in slot.options:
            fallback = slot.current or slot.options[0]
            notes.append(f"title {slot.id}: the choice {choice!r} is not among the options; kept {fallback!r}")
            choice = fallback
        titles[slot.id] = choice or slot.options[0]
    return titles, notes


def name_titles(proposed: Sequence[Proposed], titles: Mapping[str, str]) -> None:
    for number, p in enumerate(proposed, start=1):
        if not p.agreed:
            continue
        if p.op.kind in ("merge", "rename"):
            p.title = titles.get(str(number))
        elif p.op.kind == "split":
            p.parts = tuple(Part(titles.get(f"{number}.{i + 1}", ""), part.refs) for i, part in enumerate(p.parts))


# --------------------------------------------------------------------------- propose: the command

def propose(categories: Sequence[str], max_cost: float, workers: int, minutes: float = DEFAULT_TIME_BUDGET) -> int:
    previous = json.loads(OPS_PATH.read_text()) if OPS_PATH.exists() else None
    if previous and (previous["applied"] or {}).get("data") == data_fingerprint():
        raise SystemExit(f"Stopped before any Claude call: {OPS_PATH.name} was applied to exactly these rules. Merge "
                         f"or close its pull request first; to audit the result again, delete {OPS_PATH.name}.")
    data = Data.load()
    if reasons := unsettled(data):
        raise SystemExit(f"Stopped before any Claude call: data/ is not ready for an audit: {'; '.join(reasons)}.")
    # An audit not applied yet goes on (its cost adds up in data/runs.jsonl); else this is a new one.
    audit_id = previous["audit"] if previous and not previous["applied"] else _now()
    steps: dict[str, StepSummary] = {}
    try:
        return _propose(categories, data, audit_id, RunBudget(minutes=minutes, max_cost_usd=max_cost), max_cost,
                        workers, steps)
    finally:
        log_command("propose", audit_id, steps)


def _propose(categories: Sequence[str], data: Data, audit_id: str, budget: RunBudget, max_cost: float, workers: int,
             steps: dict[str, StepSummary]) -> int:
    shown = views(data, categories, similar_rules(data))
    todo = plan_calls("Propose", [view.call for view in shown], max_cost,
                      "; then one call choosing titles where the runs differ")
    if step := run_calls("Propose", todo, budget, workers):
        steps["propose"] = step
    located, by_ref = data.located(), data.by_ref
    proposals, rejected, missing = [], [], []
    for view in shown:
        stored = view.call.stored()
        if stored is None:
            missing.append(f"propose {view.category} run {view.run}")
            continue
        found, refused = read_answer(stored["output"], view, located, by_ref)
        proposals += found
        rejected += refused
    proposed, conflicting = agree(proposals)
    slots = settle(proposed, data)
    asked = [slot for slot in slots if slot.ask]
    notes: list[str] = []
    if not missing:  # the slots change until every propose call has answered
        output = None
        if asked:
            call = title_call(asked)
            if step := run_calls("Titles", plan_calls("Titles", [call], max_cost), budget, workers):
                steps["titles"] = step
            stored = call.stored()
            output = stored["output"] if stored else None
            if stored is None:
                missing.append("title choice")
        if not missing:
            titles, notes = chosen_titles(slots, output)
            name_titles(proposed, titles)
    document = ops_document(audit_id, categories, proposed, rejected + conflicting, missing, notes)
    scrape.write_atomic(OPS_PATH, analyze.json_text(document).encode())
    write_report(audit_report(document, steps, "propose"))
    agreed = sum(p.agreed for p in proposed)
    print(f"Wrote {OPS_PATH.name}: {agreed} agreed ops, {len(proposed) - agreed} not agreed, "
          f"{len(rejected) + len(conflicting)} rejected" + (f"; missing: {', '.join(missing)}" if missing else ""),
          flush=True)
    failed = sum(step.failed + step.skipped for step in steps.values())
    return 1 if missing or failed else 0


def ops_document(audit_id: str, categories: Sequence[str], proposed: Sequence[Proposed],
                 rejected: Sequence[Rejection], missing: Sequence[str], notes: Sequence[str]) -> dict:
    return {
        "version": OPS_VERSION,
        "audit": audit_id,  # names this audit's lines in data/runs.jsonl, which hold what it cost
        "proposed": _now(),
        "model": MODEL,
        "effort": EFFORT,
        "similarity": SIMILARITY,
        "categories": list(categories),
        "data": data_fingerprint(),
        "complete": not missing,
        "missing": list(missing),
        "notes": list(notes),
        "ops": [proposed_json(p, number) for number, p in enumerate(proposed, start=1)],
        "rejected": [{"run": r.run, "call": r.call, "op": r.answer, "why": r.why} for r in rejected],
        "applied": None,
    }


def op_json(op: Op) -> dict:
    found: dict = {"op": op.kind, "rules": list(op.rules)}
    if op.kind in ("merge", "rename"):
        found["title"] = op.title
    if op.kind in ("merge", "move"):
        found["category"] = op.category
    if op.kind == "split":
        found["parts"] = [{"title": part.title, "refs": list(part.refs)} for part in op.parts]
    return found


def proposed_json(p: Proposed, number: int) -> dict:
    entry: dict = {"id": number, "op": p.op.kind, "rules": list(p.op.rules), "agreed": p.agreed}
    if p.agreed:
        if p.op.kind == "merge":
            entry |= {"keeps": p.keeps, "title": p.title, "category": p.category}
        elif p.op.kind == "split":
            keeps = keeping_part(p.parts)
            entry["parts"] = [{"title": part.title or None, "refs": list(part.refs), "keeps": i == keeps}
                              for i, part in enumerate(p.parts)]
        elif p.op.kind == "rename":
            entry["title"] = p.title
        else:
            entry["category"] = p.category
    entry["proposals"] = [{"run": q.run, "call": q.call, **{k: v for k, v in op_json(q.op).items()
                                                             if k not in ("op", "rules")},
                           "reason": q.reason, "refs": list(q.refs)} for q in p.proposals]
    entry["applied"] = None
    return entry


# --------------------------------------------------------------------------- apply: structure

@dataclass
class Restructured:
    """data/regler/ in memory after the agreed ops, before the texts are rewritten."""
    files: dict[str, dict]
    former: dict[str, FormerSlug] = field(default_factory=dict)  # merged-away slugs -> alias to the kept rule
    rewrite: list[str] = field(default_factory=list)  # slugs of merged and split rules: their texts are rewritten
    revived: set[str] = field(default_factory=set)  # former slugs a split-off part takes up again
    touched: set[str] = field(default_factory=set)  # categories whose file changes
    applied: dict[int, dict] = field(default_factory=dict)  # op id -> what apply did, for the ops file

    def rule(self, slug: str) -> tuple[str, dict]:
        return next((category, rule) for category, rule in RuleBook(self.files).rules() if rule["slug"] == slug)


def restructure(data: Data, ops: Sequence[dict]) -> Restructured:
    """The rule files with the agreed ops applied, in code: merges, then splits, moves and renames (agreed ops share
    no rule, but a rename and a move). Decision ids and slugs are kept: a merge keeps the slug of the rule with most
    versions, its versions are the union in render's order and the other slugs become aliases; a split's largest part
    keeps the slug and the others get new ones, minted as for a new rule (matching.carry_slugs)."""
    out = Restructured(copy.deepcopy(data.book.files))
    by_ref = data.by_ref
    taken = data.book.slugs() | data.registry.taken()
    order = {slug: i for i, slug in enumerate(data.located())}
    for entry in sorted(ops, key=lambda e: (OP_KINDS.index(e["op"]), e["id"])):
        if entry["op"] == "merge":
            rules = [out.rule(slug) for slug in sorted(entry["rules"], key=order.__getitem__)]
            category, kept = rules[survivor(rules, by_ref)]
            if kept["slug"] != entry["keeps"]:
                raise ValueError(f"op {entry['id']}: code keeps {kept['slug']}, the ops file says {entry['keeps']}")
            others = [(c, rule) for c, rule in rules if rule is not kept]
            for c, rule in others:
                out.former[rule["slug"]] = FormerSlug(c, rule["titel"], analyze._refs(rule), kept["slug"])
                _remove(out.files[c], rule)
                out.touched.add(c)
            kept["versioner"] = incremental.ordered_versions(
                [v for _, rule in [(category, kept), *others] for v in rule["versioner"]], by_ref)
            kept["titel"] = entry["title"] or kept["titel"]
            kept["vigtig"] = any(rule.get("vigtig", True) for _, rule in rules)
            kept["note"] = " ".join(dict.fromkeys(rule["note"] for _, rule in rules if rule["note"])) or None
            target = entry["category"] or category
            if target != category:
                _remove(out.files[category], kept)
                RuleBook(out.files).file(target, MODEL)["regler"].append(kept)
            out.touched |= {category, target}
            out.rewrite.append(kept["slug"])
            out.applied[entry["id"]] = {"slug": kept["slug"], "aliases": [rule["slug"] for _, rule in others],
                                        "category": target, "title": kept["titel"]}
        elif entry["op"] == "split":
            category, rule = out.rule(entry["rules"][0])
            parts = entry["parts"]
            plan = matching.carry_slugs([RuleRefs(rule["slug"], analyze._refs(rule))],
                                        [RuleRefs(part["title"], frozenset(part["refs"])) for part in parts],
                                        taken, data.registry.former())
            if [slug == rule["slug"] for slug in plan.slugs] != [part["keeps"] for part in parts]:
                raise ValueError(f"op {entry['id']}: the slug {rule['slug']} goes to another part than the ops file "
                                 f"says")
            new = [{"titel": part["title"], "slug": slug, "vigtig": rule.get("vigtig", True), "note": rule["note"],
                    "versioner": incremental.ordered_versions(
                        [v for v in rule["versioner"] if v["ref"] in part["refs"]], by_ref)}
                   for part, slug in zip(parts, plan.slugs)]
            stored = out.files[category]["regler"]
            at = next(i for i, other in enumerate(stored) if other is rule)
            stored[at:at + 1] = new
            taken |= set(plan.slugs)
            out.revived |= set(plan.revived)
            out.touched.add(category)
            out.rewrite += plan.slugs
            out.applied[entry["id"]] = {"slugs": list(plan.slugs), "category": category,
                                        **({"revived": list(plan.revived)} if plan.revived else {})}
        elif entry["op"] == "move":
            category, rule = out.rule(entry["rules"][0])
            _remove(out.files[category], rule)
            RuleBook(out.files).file(entry["category"], MODEL)["regler"].append(rule)
            out.touched |= {category, entry["category"]}
            out.applied[entry["id"]] = {"category": entry["category"], "from": category}
        else:
            category, rule = out.rule(entry["rules"][0])
            out.applied[entry["id"]] = {"title": entry["title"], "from": rule["titel"]}
            rule["titel"] = entry["title"]
            out.touched.add(category)
    return out


def _remove(stored: dict, rule: dict) -> None:
    stored["regler"] = [other for other in stored["regler"] if other is not rule]


def check_ops(ops: Sequence[dict], data: Data) -> list[str]:
    """What is wrong with agreed ops against today's rules; empty when they still apply as proposed."""
    located = data.located()
    problems = []
    for entry in ops:
        missing = [slug for slug in entry["rules"] if slug not in located]
        if missing:
            problems.append(f"op {entry['id']}: no rule {', '.join(missing)}")
        elif entry["op"] == "split":
            refs = sorted(v["ref"] for v in located[entry["rules"][0]][1]["versioner"])
            if sorted(ref for part in entry["parts"] for ref in part["refs"]) != refs:
                problems.append(f"op {entry['id']}: the parts no longer partition {entry['rules'][0]}")
            if not all(part["title"] for part in entry["parts"]):
                problems.append(f"op {entry['id']}: a part has no title")
        elif entry["op"] == "rename" and not entry["title"]:
            problems.append(f"op {entry['id']}: no title chosen")
        elif entry["op"] == "move" and entry["category"] not in CATEGORIES:
            problems.append(f"op {entry['id']}: unknown category {entry['category']}")
    return problems


# --------------------------------------------------------------------------- apply: texts

def rule_update(category: str, rule: dict) -> RuleUpdate:
    """An update call that rewrites every version of a rule, from the first on (incremental's machinery)."""
    versions = tuple(rule["versioner"])
    plan = incremental.Plan((), tuple(Entry(v["ref"], REWRITE, v) for v in versions), 0)
    return RuleUpdate(category, rule["slug"], rule["titel"], rule.get("vigtig", True), rule["note"], plan, versions)


def text_call(u: RuleUpdate, ctx: incremental.Context) -> Call:
    output = TEXT_OUTPUT + TEXT_OUTPUT_PER_VERSION * len(u.entries)
    return Call(f"text-{u.slug}", AUDIT_UPDATE_SYSTEM, incremental.update_prompt(u, ctx, {}), incremental.UPDATE_SCHEMA,
                TEXT_TIMEOUT, output)


def rewritten(u: RuleUpdate, output: dict, stamp: dict, by_ref: Mapping[str, Decision]) -> list[dict]:
    """The rule's versions with Claude's texts (incremental.merge, which fixes the effect of proposals). Raises
    UpdateRejected unless the refs and their order are exactly those apply set; fails when anything but the text
    fields changed."""
    versions = incremental.merge(u, output, by_ref, stamp)
    if [v["ref"] for v in versions] != [v["ref"] for v in u.stored]:
        raise UpdateRejected(f"{u.name}: the versions came back in another order")
    for before, after in zip(u.stored, versions):
        kept = [{k: v for k, v in version.items() if k not in (*TEXT_FIELDS, "updated")} for version in (before, after)]
        if kept[0] != kept[1]:
            raise AssertionError(f"{u.name}: the rewrite changed {before['ref']} beyond its texts")
    return versions


def stamp_of(stored: dict) -> dict:
    """The `updated` provenance of a rewritten version, from the kept answer, so a repeated apply writes the same."""
    return {"model": stored["provenance"]["model"],
            "prompt": claude.prompt_hash(AUDIT_UPDATE_SYSTEM, incremental.UPDATE_SCHEMA), "time": stored["time"]}


def ask_text(call: Call, budget: RunBudget, cli: str, u: RuleUpdate,
             by_ref: Mapping[str, Decision]) -> tuple[str, Usage]:
    """`u`'s text call, asked once more when code rejects its answer (as incremental.run_update); a second rejection
    fails it, and apply writes nothing."""
    usage = Usage()
    error: Exception | None = None
    for attempt in (1, 2):
        if attempt > 1 and (limit := budget.exhausted()):
            raise ClaudeError(f"answer rejected: {error}; not asked again because {limit}", usage)
        try:
            message, spent = ask(call, budget, cli, lambda output: rewritten(u, output, {}, by_ref))
            return message, usage + spent
        except ClaudeError as exc:
            usage += exc.usage
            if not isinstance(exc.__cause__, UpdateRejected):  # a ModelMismatch stays one: it stops the run
                raise type(exc)(str(exc), usage) from exc
            error = exc.__cause__
            log.warning("Rewrite %s, attempt %d: %s", u.name, attempt, error)
    raise ClaudeError(f"answer rejected twice: {error}", usage)


# --------------------------------------------------------------------------- apply: the command

@contextmanager
def rules_at(root: Path) -> Iterator[None]:
    """Point analyze's rule files and slug history at `root`, a copy of data/ the result is checked in before any of
    it is written to data/."""
    saved = analyze.RULES_DIR, analyze.SLUGS_PATH
    analyze.RULES_DIR, analyze.SLUGS_PATH = root / "regler", root / "slugs.json"
    try:
        yield
    finally:
        analyze.RULES_DIR, analyze.SLUGS_PATH = saved


def settle_inputs(files: dict[str, dict], categories: Collection[str], data: Data) -> None:
    """Give each touched category the input_hash of its input after the ops (and its decisions' fingerprints, where
    the file records them), as incremental.settle does: the files reflected every decision before (unsettled), and the
    ops only move decisions between rules, so full and the checks see them as consolidated."""
    by_ref, organ = data.by_ref, data.organ
    book = RuleBook(files)
    items = incremental.category_items(data.decisions, analyze.home_categories(data.decisions, book.raw_rules()),
                                       organ)
    for category in categories:
        stored = files[category]
        stored["input_hash"] = analyze._hash(items.get(category, []), stored.get("version"))
        if "inputs" in stored:
            stored["inputs"] = {ref: incremental.fingerprint(by_ref[ref], organ[by_ref[ref].doc_id])
                                for ref in sorted(book.held(category)) if ref in by_ref}


def verify(after: checks.Data, docs: list[Doc], old_slugs: Collection[str]) -> list[str]:
    """What must hold after an audit, read where analyze points: no check errors (history is compared by the report),
    no work for styrke/update.py (no decision is filed again because the audit moved it), and every slug that led to a
    rule still does (itself, or as an alias)."""
    failures = [f"check error ({p.kind}): {p.message}" for p in checks.errors(checks.find_problems(after))]
    work = update.pending_work(docs)  # incremental: its lost categories are among those to consolidate
    if work.categories or work.outdated:
        failures.append(f"styrke/update.py would have work: {', '.join(sorted(work.categories | work.outdated))}")
    if after.pending:
        failures.append(f"categories to consolidate again: {', '.join(sorted(after.pending))}")
    live = {rule["slug"] for rule in after.raw_rules}
    targets = after.slugs.targets()
    if lost := sorted(slug for slug in old_slugs if slug not in live and targets.get(slug) not in live):
        failures.append(f"slugs that lead nowhere now: {', '.join(lost)}")
    return failures


@dataclass(frozen=True)
class Checked:
    """The agreed ops' result as built and checked in a temporary copy of data/: what apply writes, byte for byte, and
    what it shows."""
    failures: list[str]  # why it must not be written; empty when it may
    files: dict[str, bytes]  # rule file name -> its content
    slugs: bytes  # data/slugs.json, every former slug resolved
    taken: bytes  # the same with the slugs a split-off part takes up again still in it, written before the rules
    fingerprint: str  # data_fingerprint() once written
    raw_rules: list[dict]
    history: checks.HistoryCheck  # what each year shows before and after
    pages: dict[str, str]  # regelsaet/
    html: str  # _site/index.html


def checked(out: Restructured, data: Data, today: date) -> Checked:
    """Write the result to a temporary copy of data/regler and data/slugs.json, resolve the slugs there as a
    consolidation does, check it (verify) and build the pages from it."""
    old_slugs = data.book.slugs() | set(data.registry.targets())
    before = checks.snapshot(checks.Data.load(data.docs), today)
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "regler").mkdir()
        for category, stored in out.files.items():
            analyze._write_json(root / "regler" / f"{category}.json", stored)
        with rules_at(root):
            analyze._save_slugs(data.registry.with_former(out.former))
            analyze.resolve_slugs(data.by_ref)
            after = checks.Data.load(data.docs)
            failures = verify(after, data.docs, old_slugs)
            problems = Counter(problem.kind for problem in checks.find_problems(after))
            missing = analyze.missing_extractions(data.docs, model=update.EXTRACT_MODEL)
            files = {path.name: path.read_bytes() for path in sorted(analyze.RULES_DIR.glob("*.json"))}
            slugs, fingerprint = analyze.SLUGS_PATH.read_bytes(), data_fingerprint()
            former = data.registry.former()
            analyze._save_slugs(after.slugs.with_former({slug: former[slug] for slug in out.revived}))
            return Checked(
                failures, files, slugs, analyze.SLUGS_PATH.read_bytes(), fingerprint, after.raw_rules,
                checks.check_history(before, checks.snapshot(after, today), after.slugs.targets()),
                render.build_pages({doc.id: doc for doc in data.docs}, after.decisions, after.raw_rules, missing,
                                   problems, today),
                website.page_html(data.docs, after.decisions, after.raw_rules, after.slugs.targets(), today))


def apply(max_cost: float, workers: int, minutes: float = DEFAULT_TIME_BUDGET) -> int:
    if not OPS_PATH.exists():
        raise SystemExit(f"No {OPS_PATH.name}: run `uv run -m styrke.audit propose` first")
    document = json.loads(OPS_PATH.read_text())
    if document["applied"]:
        raise SystemExit(f"{OPS_PATH.name} was applied already ({document['applied']['time']}); propose again for "
                         f"another audit")
    if not document["complete"]:
        raise SystemExit(f"{OPS_PATH.name} is incomplete (missing: {', '.join(document['missing'])}); run "
                         f"`uv run -m styrke.audit propose` again to finish it (kept answers are not paid again)")
    if document["data"] != data_fingerprint():
        raise SystemExit("data/regler/ or data/slugs.json changed since the ops were proposed; run "
                         "`uv run -m styrke.audit propose` again (answers to unchanged questions are not paid again)")
    data = Data.load()
    if reasons := unsettled(data):
        raise SystemExit(f"Stopped before any Claude call: data/ is not ready for an audit: {'; '.join(reasons)}.")
    agreed = [entry for entry in document["ops"] if entry["agreed"]]
    if problems := check_ops(agreed, data):
        raise SystemExit(f"The agreed ops do not apply to data/: {'; '.join(problems)}")
    steps: dict[str, StepSummary] = {}
    try:
        return _apply(document, data, agreed, max_cost, minutes, workers, steps)
    finally:
        log_command("apply", document["audit"], steps)


def _apply(document: dict, data: Data, agreed: list[dict], max_cost: float, minutes: float, workers: int,
           steps: dict[str, StepSummary]) -> int:
    """Everything that can fail first: the structure, the texts (Claude), the checks, the pages, the scores and the
    report, with nothing written but the answers kept. Then the slug history, the rule files and the ops file, each
    replaced whole, and the pages."""
    today = date.today()
    budget = RunBudget(minutes=minutes, max_cost_usd=max_cost)
    out = restructure(data, agreed)
    by_ref = data.by_ref
    ctx = incremental.Context(incremental.Settings(), RunBudget(), by_ref, {doc.id: doc for doc in data.docs}, _now(),
                              incremental.Known({}, frozenset()))
    updates = {u.slug: u for u in (rule_update(*out.rule(slug)) for slug in out.rewrite)}
    calls = {slug: text_call(u, ctx) for slug, u in updates.items()}
    by_call = {call.name: updates[slug] for slug, call in calls.items()}
    if step := run_calls("Rewrite", plan_calls("Rewrite", list(calls.values()), max_cost), budget, workers,
                         lambda call, spend, cli: ask_text(call, spend, cli, by_call[call.name], by_ref)):
        steps["rewrite"] = step
    if missing := [slug for slug, call in calls.items() if call.stored() is None]:
        return _not_applied(document, steps, [f"no accepted rewrite of {', '.join(missing)} yet"])
    try:
        for slug, u in updates.items():
            stored = calls[slug].stored()
            _, rule = out.rule(slug)
            rule["versioner"] = rewritten(u, stored["output"], stamp_of(stored), by_ref)
            rule["note"] = stored["output"]["note"]
        settle_inputs(out.files, out.touched, data)
        before = analyze.load_rules()
        result = checked(out, data, today)
        if result.failures:
            return _not_applied(document, steps, result.failures)
        applied = {**document, "ops": [{**entry, "applied": out.applied.get(entry["id"])} for entry in document["ops"]],
                   "applied": {"time": _now(), "data": result.fingerprint}}
        report = audit_report(applied, steps, "apply", history=result.history,
                              score=evaluate.score_section(before, result.raw_rules, data.docs, data.decisions))
    except Exception as exc:  # nothing is written: the report says why, the log has the traceback
        log.exception("Applying the ops failed")
        return _not_applied(document, steps, [f"{type(exc).__name__}: {exc}"])
    # As a consolidation writes (analyze._consolidate_one): the slug history first, with every slug in it a rule
    # takes up again, so whichever write fails every slug stays taken (a merged-away slug that is still live then
    # is reported by styrke/checks.py and dropped by resolve_slugs); then the rule files, the final history and the
    # ops file.
    scrape.write_atomic(analyze.SLUGS_PATH, result.taken)
    for name, content in result.files.items():
        path = analyze.RULES_DIR / name
        if not path.exists() or path.read_bytes() != content:
            scrape.write_atomic(path, content)
    if result.taken != result.slugs:
        scrape.write_atomic(analyze.SLUGS_PATH, result.slugs)
    scrape.write_atomic(OPS_PATH, analyze.json_text(applied).encode())
    render.write_pages(result.pages)
    website.write_site(result.html)
    write_report(report)
    prune({call.name for call in calls.values()} | {f"propose-{c}-{run}" for c in document["categories"]
                                                  for run in range(1, RUNS + 1)} | {"titles"})
    print(f"Applied {len(agreed)} ops; the pages are rebuilt. Review the diff and {REPORT.name}, and send it to a "
          f"pull request.", flush=True)
    return 0


def prune(kept: Collection[str]) -> None:
    """Remove the answers of earlier audits from CACHE_DIR, keeping those named (this audit's calls)."""
    for path in sorted(CACHE_DIR.glob("*.json")):
        if path.stem not in kept:
            path.unlink()


def _not_applied(document: dict, steps: Mapping[str, StepSummary], failures: Sequence[str]) -> int:
    write_report(audit_report(document, steps, "apply", failures=failures))
    print(f"Nothing written to data/: {'; '.join(failures)}", flush=True)
    return 1


# --------------------------------------------------------------------------- the audit's cost

def ledger(audit_id: str, command: str, steps: Mapping[str, StepSummary]) -> dict[str, float]:
    """USD at list price per command of this audit, every call counted (failed attempts and rejected answers too): the
    lines data/runs.jsonl has for it, and this command's steps, which it logs when it ends."""
    spent: dict[str, float] = defaultdict(float)
    for entry in claude.read_run_log(update.RUNS_LOG):
        if entry.get("audit") == audit_id:
            spent[entry["command"].removeprefix("audit ")] += sum(s["cost_usd"] for s in entry["steps"].values())
    spent[command] += sum(step.usage.cost_usd for step in steps.values())
    return dict(spent)


def log_command(command: str, audit_id: str, steps: Mapping[str, StepSummary]) -> None:
    """One line in data/runs.jsonl for a command that called Claude, as styrke/update.py logs its runs."""
    update.record_run(dict(steps), {"command": f"audit {command}", "audit": audit_id})


# --------------------------------------------------------------------------- report

def write_report(text: str) -> None:
    scrape.write_atomic(REPORT, text.encode())


# The report's first line, which .github/scripts/route-audit.sh reads to title the pull request.
APPLIED = "<!-- audit: applied -->"
UNFINISHED = "<!-- audit: unfinished -->"
CONTINUE = ("To continue, run the workflow “Audit rules” on this pull request's branch, or "
            "`uv run -m styrke.audit propose` and then `uv run -m styrke.audit apply` on it: the answers kept in "
            "data/audit/ are not paid again.")


def audit_report(document: dict, steps: Mapping[str, StepSummary], command: str, *, failures: Sequence[str] = (),
                 history: checks.HistoryCheck | None = None, score: Sequence[str] = ()) -> str:
    """Markdown for the pull request: the outcome and how to go on, what the audit cost, the ops (applied, not agreed,
    rejected), what each year shows before and after, and the answer key's scores. `command`: the one writing it."""
    applied = bool(document["applied"])
    agreed = [entry for entry in document["ops"] if entry["agreed"]]
    if failures:
        outcome = (f"**Not applied**: {'; '.join(failures)}. Nothing was written to data/ but the answers kept in "
                   f"data/audit/. {CONTINUE}")
    elif applied:
        outcome = ("**Review**: the audit applied the agreed ops below to data/ and rebuilt the pages. It changes what "
                   "earlier years show by design (In force by year): check the merged and split rules against the "
                   "minutes, then merge the pull request to publish it, or close it to discard it.")
    elif not document["complete"]:
        outcome = (f"**Unfinished**: the cost or time limit, or failed calls, left "
                   f"{', '.join(document['missing'])}. {CONTINUE}")
    else:
        outcome = f"**Unfinished**: {len(agreed)} agreed ops, not applied yet. {CONTINUE}"
    lines = [APPLIED if applied else UNFINISHED, f"# Rule audit {date.today().isoformat()}", "", outcome, "",
             *calls_section(document, steps, command)]
    lines += [f"Ops proposed for {', '.join(document['categories'])} by {document['model']} (effort "
              f"{document['effort']}), {RUNS} runs each; an op is applied when both propose it.", ""]
    rejected = [f"run {r['run']}, {r['call']}: {r['op'].get('op')} {', '.join(map(str, r['op'].get('rules', [])))}: "
                f"{r['why']}" for r in document["rejected"]]
    for heading, items in ((f"{'Applied' if applied else 'Agreed'} ops", [_describe(entry) for entry in agreed]),
                           ("Not agreed", [_describe(entry) for entry in document["ops"] if not entry["agreed"]]),
                           ("Rejected by code", rejected)):
        lines += [f"## {heading}: {len(items)}", "", *([f"- {item}" for item in items] or ["None."]), ""]
    if document["notes"]:
        lines += ["## Notes", "", *(f"- {note}" for note in document["notes"]), ""]
    if history is not None:
        lines += history_section(history)
    lines += score
    return "\n".join(lines).rstrip() + "\n"


def calls_section(document: dict, steps: Mapping[str, StepSummary], command: str) -> list[str]:
    """This command's Claude calls, and what the whole audit has cost at list price (ledger)."""
    lines = ["## Claude calls", ""]
    if steps:
        lines += ["| Step | Calls | Failed | Skipped | Tokens in | Tokens out | USD (list price) |",
                  "|---|--:|--:|--:|--:|--:|--:|"]
        lines += [f"| {name} | {s.calls} | {s.failed} | {s.skipped} | {s.usage.all_input_tokens} | "
                  f"{s.usage.output_tokens} | {s.usage.cost_usd:.2f} |" for name, s in steps.items()]
        lines.append("")
    spent = ledger(document["audit"], command, steps)
    cost = ", ".join(f"{name} {usd:.2f} USD" for name, usd in sorted(spent.items(), key=lambda kv: kv[0] != "propose"))
    return lines + [f"Cost of this audit at list price, every call counted (failed attempts and rejected answers "
                    f"too): {cost or 'nothing'}.", ""]


def _describe(entry: dict) -> str:
    what = f"{entry['op']} " + " + ".join(f"`{slug}`" for slug in entry["rules"])
    if entry["agreed"]:
        if entry["op"] == "merge":
            what += f" → `{entry['keeps']}` “{entry['title']}”"
            if entry["category"]:
                what += f" in {entry['category']}"
        elif entry["op"] == "split":
            what += " into " + ", ".join(f"“{part['title']}” ({_versions(len(part['refs']))}"
                                         f"{', keeps the slug' if part['keeps'] else ''})" for part in entry["parts"])
            if entry["applied"]:
                what += " → " + ", ".join(f"`{slug}`" for slug in entry["applied"]["slugs"])
        elif entry["op"] == "rename":
            what += f" to “{entry['title']}”"
        else:
            what += f" to {entry['category']}"
    reasons = "; ".join(f"run {q['run']} ({q['call']}): {q['reason']} [{', '.join(q['refs'])}]"
                        for q in entry["proposals"])
    return f"{what}. {reasons}"


def _versions(n: int) -> str:
    return f"{n} version{'' if n == 1 else 's'}"


def history_section(history: checks.HistoryCheck) -> list[str]:
    """Per rule and run of years, the decision in force before and after the audit (and the one it confirms)."""
    lines = ["## In force by year", "",
             "What the year pages show for every rule whose version in force changed. Rules merged into one count "
             "together (`a, b → a`); a split-off part counts as a new rule.", ""]
    changes = sorted((c for c in history.changes if c.kind == "history"),
                     key=lambda c: (c.title.lower(), c.slugs, c.cutoff))
    if not changes:
        lines.append("No rule in force changed in any year.")
    else:
        lines += ["| Rule | Years | Before | After |", "|---|---|---|---|"]
        for (title, slugs, was, now), run in groupby(changes, key=lambda c: (c.title, c.slugs, update._state(c.before),
                                                                             update._state(c.after))):
            lines.append(f"| {update._cell(title)} (`{slugs}`) | {checks.years(c.cutoff for c in run)} | "
                         f"{update._cell(was)} | {update._cell(now)} |")
    wording = {c.title for c in history.changes if c.kind == "history-text"}
    if wording:
        lines += ["", f"Only the wording in force changed for {len(wording)} rules (rewritten texts): "
                      f"{', '.join(sorted(wording))}."]
    return lines + [""]


# --------------------------------------------------------------------------- command line

def _categories(text: str | None) -> list[str]:
    chosen = [c for c in (text or "").split(",") if c] or list(CATEGORIES)
    if unknown := [c for c in chosen if c not in CATEGORIES]:
        raise SystemExit(f"Unknown categories: {', '.join(unknown)}; the categories are {', '.join(CATEGORIES)}")
    return [c for c in CATEGORIES if c in chosen]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="uv run -m styrke.audit", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("propose", help="Opus proposes ops per category, in two runs; writes data/regler_ops.json")
    pr.add_argument("--categories", help="comma-separated categories (default: all)")
    ap = sub.add_parser("apply", help="apply the agreed ops to data/ and rewrite the merged and split rules' texts")
    for paid in (pr, ap):
        paid.add_argument("--max-cost", type=float, required=True, metavar="USD",
                          help="start no new Claude calls once this command has used this much at list price")
        paid.add_argument("--workers", type=int, default=4, help="parallel Claude calls (default: 4)")
        paid.add_argument("--time-budget", type=float, default=DEFAULT_TIME_BUDGET, metavar="MIN",
                          help=f"start no new Claude calls after this many minutes (default: {DEFAULT_TIME_BUDGET}; "
                               f"the workflow gives apply what propose left of it)")
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.command == "propose":
        raise SystemExit(propose(_categories(args.categories), args.max_cost, args.workers, args.time_budget))
    raise SystemExit(apply(args.max_cost, args.workers, args.time_budget))


if __name__ == "__main__":
    main()
