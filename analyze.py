"""Claude steps: extract decisions per document, then consolidate them into rule histories.

Both steps call the Claude Code CLI headless (`claude -p`), so they run on the logged-in
subscription and need no API key. Results are cached in data/ and only recomputed when the
input (document hash, decisions in a category) or the prompt version changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from scrape import DATA_DIR, Doc, document_text

# Bump when a prompt or schema changes so cached results are recomputed.
EXTRACT_VERSION = 1
CONSOLIDATE_VERSION = 6

DECISIONS_DIR = DATA_DIR / "beslutninger"
RULES_DIR = DATA_DIR / "regler"

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
EFFEKTER = ["indfoert", "aendret", "bekraeftet", "ophaevet", "forkastet", "trukket"]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Decision:
    ref: str  # "<doc id>#<n>"
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
    side: int | None
    stemmer: str | None
    gaelder_fra: str | None
    gaelder_til: str | None


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
- udfald: vedtaget (adopted or decided), forkastet (voted down or rejected), trukket \
(withdrawn or postponed without a decision).
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
                    "udfald": {"type": "string", "enum": ["vedtaget", "forkastet", "trukket"]},
                    "handling": {"type": "string", "enum": ["ny", "aendring", "bekraeftelse", "ophaevelse"]},
                    "niveau": {"type": "string", "enum": NIVEAUER},
                    "tekst": {"type": "string"},
                    "citat": {"type": "string"},
                    "side": {"type": ["integer", "null"]},
                    "stemmer": {"type": ["string", "null"]},
                    "gaelder_fra": {"type": ["string", "null"]},
                    "gaelder_til": {"type": ["string", "null"]},
                },
                "required": ["emne", "kategori", "udfald", "handling", "niveau", "tekst", "citat",
                             "side", "stemmer", "gaelder_fra", "gaelder_til"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["moededato", "beslutninger"],
    "additionalProperties": False,
}


def extract(docs: list[Doc], *, model: str, effort: str | None, workers: int) -> None:
    """Extract decisions from every document whose cached result is missing or outdated."""
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    todo = [doc for doc in docs if not _extraction_is_current(doc)]
    log.info("Udtræk: %d af %d dokumenter skal analyseres", len(todo), len(docs))
    cost = _run_parallel(todo, lambda doc: _extract_one(doc, model, effort), workers, "Udtræk")
    log.info("Udtræk færdig (%.2f USD listepris)", cost)


def missing_extractions(docs: list[Doc]) -> list[str]:
    """Ids of documents without a current extraction (new, changed, or a failed Claude call)."""
    return sorted(doc.id for doc in docs if not _extraction_is_current(doc))


def _extraction_is_current(doc: Doc) -> bool:
    path = DECISIONS_DIR / f"{doc.id}.json"
    if not path.exists():
        return False
    cached = json.loads(path.read_text())
    return cached.get("sha256") == doc.sha256 and cached.get("version") == EXTRACT_VERSION


def _extract_one(doc: Doc, model: str, effort: str | None) -> tuple[str, float]:
    text = document_text(doc)
    prompt = (
        f"Dokument-id: {doc.id}\nOrgan: {doc.organ_label}\nTitel på styrke.dk: {doc.title}\n"
        f"Dato ifølge styrke.dk: {doc.date or 'ukendt'}\n\n<dokument>\n{text}\n</dokument>"
    )
    output, cost = ask_claude(EXTRACT_SYSTEM, prompt, EXTRACT_SCHEMA, model=model, effort=effort)
    trigrams = _trigrams(_words(text))
    decisions = [{**d, "citat_fundet": _quote_found(d["citat"], trigrams)} for d in output["beslutninger"]]
    result = {
        "doc_id": doc.id,
        "sha256": doc.sha256,
        "version": EXTRACT_VERSION,
        "model": model,
        "moededato": output["moededato"],
        "beslutninger": decisions,
    }
    _write_json(DECISIONS_DIR / f"{doc.id}.json", result)
    missing = sum(not d["citat_fundet"] for d in decisions)
    note = f", {missing} citat ikke genfundet" if missing else ""
    return f"{doc.id}: {len(decisions)} beslutninger{note}", cost


def load_decisions(docs: list[Doc]) -> list[Decision]:
    """All cached decisions for the given documents, in chronological order."""
    decisions: list[Decision] = []
    for doc in docs:
        path = DECISIONS_DIR / f"{doc.id}.json"
        if not path.exists():
            continue
        cached = json.loads(path.read_text())
        meeting_date = _meeting_date(cached.get("moededato"), doc)
        for n, d in enumerate(cached["beslutninger"], start=1):
            decisions.append(Decision(
                ref=f"{doc.id}#{n}",
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
                side=d["side"],
                stemmer=d["stemmer"],
                gaelder_fra=_valid_date(d["gaelder_fra"]),
                gaelder_til=_valid_date(d["gaelder_til"]),
            ))
    return sorted(decisions, key=lambda d: (d.dato or "", d.ref))


def _meeting_date(extracted: str | None, doc: Doc) -> str | None:
    found = _valid_date(extracted)
    if found and doc.date and abs(int(found[:4]) - int(doc.date[:4])) > 1:
        log.warning("%s: mødedato %s passer ikke med styrke.dk (%s) – bruger sidstnævnte", doc.id, found, doc.date)
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


def _trigrams(words: list[str]) -> set[tuple[str, ...]]:
    return set(zip(words, words[1:], words[2:]))


def _quote_found(quote: str, doc_trigrams: set[tuple[str, ...]]) -> bool:
    """True when the quote appears (near-)verbatim in the document; tolerates line-break hyphenation."""
    quote_trigrams = list(_trigrams(_words(quote)))
    if not quote_trigrams:
        return False
    hits = sum(gram in doc_trigrams for gram in quote_trigrams)
    return hits / len(quote_trigrams) >= 0.8


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
(repeated, confirmed or clarified without changing content), ophaevet (abolished), forkastet \
(a proposal to change it was rejected – rule unchanged), trukket (a proposal was withdrawn – \
rule unchanged)
  - tekst: for aendret and bekraeftet, the complete rule text in force AFTER this decision, in \
Danish, merging earlier parts that still apply so the text stands alone, and covering only \
this rule (leave out other rules mentioned in the same decision, e.g. other fees in a budget \
confirmation). An amendment that rewrites only part of a rule (e.g. "Rettes til" for one \
paragraph, or a quoted text ending in "…") keeps the earlier clauses it does not replace or \
remove. For indfoert, null when the decision's own tekst states exactly this rule and \
nothing else (the usual case), otherwise the rule-specific text. For other effects null.
  - kort: at most 12 Danish words saying what this decision did, keeping key numbers and \
dates; no full sentence needed, e.g. "hævet fra 200 til 300 kr.", "ny klub anmoder via \
licens@styrke.dk", "forslag om 12 mdr. karantæne forkastet".
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


def consolidate(decisions: list[Decision], organ_of: dict[str, str], *, model: str,
                effort: str | None, workers: int) -> None:
    """Group decisions into rule histories, one Claude call per category whose input changed."""
    RULES_DIR.mkdir(parents=True, exist_ok=True)
    by_category: dict[str, list[dict]] = {}
    for d in decisions:
        by_category.setdefault(d.kategori, []).append(_consolidation_input(d, organ_of[d.doc_id]))

    for stale in RULES_DIR.glob("*.json"):
        if stale.stem not in by_category:
            stale.unlink()

    todo = []
    for category, items in by_category.items():
        input_hash = _hash(items, CONSOLIDATE_VERSION)
        path = RULES_DIR / f"{category}.json"
        if not path.exists() or json.loads(path.read_text()).get("input_hash") != input_hash:
            todo.append((category, items, input_hash))
    log.info("Konsolidering: %d af %d kategorier skal opdateres", len(todo), len(by_category))
    cost = _run_parallel(todo, lambda job: _consolidate_one(*job, model=model, effort=effort), workers, "Konsolidering")
    log.info("Konsolidering færdig (%.2f USD listepris)", cost)


def _consolidation_input(d: Decision, organ: str) -> dict:
    item = {
        "ref": d.ref, "dato": d.dato, "organ": organ, "udfald": d.udfald, "handling": d.handling,
        "niveau": d.niveau, "emne": d.emne, "tekst": d.tekst, "stemmer": d.stemmer,
        "gaelder_fra": d.gaelder_fra, "gaelder_til": d.gaelder_til,
    }
    return {k: v for k, v in item.items() if v is not None}


def _consolidate_one(category: str, items: list[dict], input_hash: str, model: str,
                     effort: str | None) -> tuple[str, float]:
    prompt = (
        f"Kategori: {CATEGORIES[category]}\n\n<beslutninger>\n"
        f"{json.dumps(items, ensure_ascii=False, indent=0)}\n</beslutninger>"
    )
    output, cost = ask_claude(CONSOLIDATE_SYSTEM, prompt, CONSOLIDATE_SCHEMA, model=model, effort=effort)

    known = {item["ref"] for item in items}
    seen: set[str] = set()
    rules = []
    for rule in output["regler"]:
        versions = [v for v in rule["versioner"] if v["ref"] in known and v["ref"] not in seen]
        seen.update(v["ref"] for v in versions)
        if versions:
            rules.append({**rule, "versioner": versions})
    skipped = [ref for ref in output["udeladt"] if ref in known and ref not in seen]
    unassigned = sorted(known - seen - set(skipped))
    _write_json(RULES_DIR / f"{category}.json", {
        "kategori": category,
        "version": CONSOLIDATE_VERSION,
        "input_hash": input_hash,
        "model": model,
        "regler": rules,
        "udeladt": skipped,
        "ikke_tildelt": unassigned,
    })
    note = f", {len(unassigned)} ikke tildelt" if unassigned else ""
    return f"{category}: {len(items)} beslutninger -> {len(rules)} regler{note}", cost


def load_rules() -> list[dict]:
    rules = []
    for path in sorted(RULES_DIR.glob("*.json")):
        cached = json.loads(path.read_text())
        rules.extend({**rule, "kategori": cached["kategori"]} for rule in cached["regler"])
    return rules


# --------------------------------------------------------------------------- claude CLI

class ClaudeError(RuntimeError):
    pass


def ask_claude(system: str, prompt: str, schema: dict, *, model: str, effort: str | None,
               attempts: int = 3) -> tuple[dict, float]:
    """Run one headless Claude Code call with structured output; returns (output, list-price USD)."""
    claude = shutil.which("claude")
    if claude is None:
        raise SystemExit("Claude Code CLI ('claude') blev ikke fundet på PATH.")
    cmd = [
        claude, "-p", "--output-format", "json", "--no-session-persistence",
        # No tools, plugins, hooks, CLAUDE.md or MCP servers: just the prompt and the schema.
        "--safe-mode", "--strict-mcp-config", "--disable-slash-commands", "--tools", "",
        "--model", model, "--system-prompt", system, "--json-schema", json.dumps(schema),
    ]
    if effort:
        cmd += ["--effort", effort]
    env = {**os.environ, "CLAUDE_CODE_MAX_OUTPUT_TOKENS": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS", "64000")}

    error = ""
    for attempt in range(1, attempts + 1):
        with tempfile.TemporaryDirectory() as cwd:
            try:
                proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, check=False,
                                      timeout=1800, cwd=cwd, env=env)
            except subprocess.TimeoutExpired:
                error = "timeout"
                continue
        try:
            result = json.loads(proc.stdout)
        except json.JSONDecodeError:
            result = {}
        output = result.get("structured_output")
        if proc.returncode == 0 and not result.get("is_error") and isinstance(output, dict):
            return output, float(result.get("total_cost_usd") or 0)
        error = str(result.get("result") or proc.stderr or proc.stdout)[-500:]
        if attempt < attempts:
            time.sleep(15 * attempt)
    raise ClaudeError(error)


def _run_parallel(jobs: list, fn, workers: int, label: str) -> float:
    """Run fn over jobs in a thread pool; log progress and failures, keep going on errors."""
    total_cost = 0.0
    failures = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, job): job for job in jobs}
        for done, future in enumerate(as_completed(futures), start=1):
            try:
                message, cost = future.result()
            except Exception as exc:  # one failed call must not stop the batch; rerun picks it up
                failures += 1
                log.error("%s [%d/%d] fejlede: %s", label, done, len(jobs), exc)
                continue
            total_cost += cost
            log.info("%s [%d/%d] %s", label, done, len(jobs), message)
    if failures:
        log.warning("%s: %d fejlede – kør igen for at prøve dem igen", label, failures)
    return total_cost


def _hash(value: object, version: int) -> str:
    return hashlib.sha256(json.dumps([version, value], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n")
