# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "pymupdf>=1.24",
#   "snowballstemmer>=2.2",
# ]
# ///
"""Scores extractions and consolidations against the answer key in eval/key/, and runs the incremental
consolidation's candidate gate.

    uv run evaluate.py extract --name migrated --from-data   # data/beslutninger copied as a run (no Claude)
    uv run evaluate.py extract --name opus-v3-1 --model claude-opus-5-5 --prompt v3 --max-cost 5
    uv run evaluate.py score --run migrated --run opus-v3-1 --run opus-v3-2   # decision scores, stability, the gate
    uv run evaluate.py score-rules                  # rule scores of data/regler (no Claude)
    uv run evaluate.py candidate-recall             # incremental consolidation: candidate ranking recall (no Claude)

Two extraction runs agree on only ~90% of decisions, so comparing runs cannot tell better from different: every
change (models, prompts, consolidation) is scored against the key instead. Opus judges built it once, for the
documents and recurring rules in eval/selection.json: each document's decisions (key/decisions/<doc>.json) and each
rule's timeline with what was in force every year (key/rules/<slug>.json). Errors a check against the minutes found
in it are in key/corrections.json, applied whenever a key is scored. eval/README.md says how the key was built and
where the retired commands are.

Everything is written under eval/: runs/<run>/<doc>.json, reports/<name>.md and runs.jsonl; nothing to data/ or
regelsaet/. extract calls Claude unless --from-data: it takes --max-cost, prints how many calls it plans, logs each
call's usage and adds a line to eval/runs.jsonl, and exits 1 when a call failed or was skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
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
from analyze import Decision, DocWords, RunBudget, StepSummary, Usage
from matching import Candidate
from scrape import Doc

EVAL_DIR = scrape.ROOT / "eval"

# The seed of the bootstrap.
SEED = 2026
# The gate's default baseline: today's data, data/beslutninger copied after the migration to Opus with prompt v3
# (extract --name migrated --from-data). It is one run of today's pipeline, whose runs differ on noise alone (two Opus
# v3 runs: 88.7% and 95.5% on the three fields), so a candidate need only be no worse than today's pipeline's worst run
# where that is looser than the baseline moved by its slack (gate_needs). The worst is taken over every scored run of
# today's pipeline, so one outlier run loosens that figure's bound; the report's row "today's pipeline, worst run per
# figure" lists the runs it comes from.
BASELINE_RUN = "migrated"
# Calls already running finish after the cost limit is reached, so a run can overshoot it by one call per worker;
# below this limit the default is one worker.
SMALL_BUDGET_USD = 10.0

BOOTSTRAP_SAMPLES = 2000
# The extraction gate's slack: how far a configuration's worse run may fall behind the baseline run, per figure (higher
# is better for all but over-split), unless today's pipeline's worst run allows more (gate_needs). Over-split is in it
# so a prompt that splits more than the key asks for cannot pass on recall.
GATE_SLACK = {"recall": 0.0, "precision": 0.02, "field_three": 0.0, "over_split": 0.02}
GATE_LOWER_IS_BETTER = frozenset({"over_split"})
# Gate figures are ratios in floating point: a run exactly at a bound computed another way (the baseline plus its
# slack) lands a rounding error past it, and must still pass.
GATE_TOLERANCE = 1e-9
# The candidate-recall gate: recall@K for these K, and the recall the chosen K must reach.
RECALL_KS = (3, 5, 8, 10, 15)
RECALL_TARGET = 0.98
# For the call estimate printed before a command starts: Danish text runs about this many characters per token.
CHARS_PER_TOKEN = 3.2

# The coded fields of a decision, whose agreement with the key is scored.
CODED_FIELDS = ("kategori", "udfald", "handling", "niveau")
# The coded fields one document decides: handling (new, change, confirmation) often needs the rule's history, so
# field accuracy is also given without it.
FIRM_FIELDS = ("kategori", "udfald", "niveau")
# The outcome a decision with this effect has, so a key event can be compared with extracted decisions.
EFFECT_OUTCOME = {"foreslaaet": "ikke_afgjort", "forkastet": "forkastet", "trukket": "trukket"}

log = logging.getLogger("evaluate")


# --------------------------------------------------------------------------- files

def write_text(path: Path, text: str) -> None:
    """Write under eval/ only: the evaluation must never touch data/ or regelsaet/."""
    if not path.resolve().is_relative_to(EVAL_DIR.resolve()):
        raise ValueError(f"{path} is outside {EVAL_DIR}; evaluate.py writes nowhere else")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def write_json(path: Path, value: object) -> None:
    write_text(path, analyze.json_text(value))


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


def report_path(name: str) -> Path:
    return EVAL_DIR / "reports" / f"{name}.md"


def runs_log_path() -> Path:
    return EVAL_DIR / "runs.jsonl"


class Texts:
    """Each document's words, read once per command (reading PDFs is the slow part)."""

    def __init__(self) -> None:
        self._words: dict[str, DocWords] = {}

    def words(self, doc: Doc) -> DocWords:
        if doc.id not in self._words:
            self._words[doc.id] = DocWords.of(analyze.document_text(doc))
        return self._words[doc.id]


# --------------------------------------------------------------------------- selection

def load_selection() -> dict:
    path = selection_path()
    if not path.exists():
        raise SystemExit(f"{path} is missing; it was made with the answer key (eval/README.md)")
    return read_json(path)


def selected_docs(selection: dict, pilot: int | None = None, named: Sequence[str] | None = None) -> list[Doc]:
    """The selected documents in selection order: all, the first `pilot`, or those `named`, which must be selected.
    Extractions are kept per document, so a pilot's are reused by the full run."""
    order = [entry["id"] for entry in selection["documents"]]
    if named:
        unknown = [doc_id for doc_id in named if doc_id not in order]
        if unknown:
            raise SystemExit(f"Not in eval/selection.json (documents): {', '.join(unknown)}")
        chosen = [doc_id for doc_id in order if doc_id in named]
    else:
        chosen = order[:pilot]
    docs = {doc.id: doc for doc in scrape.load_manifest()}
    missing = [doc_id for doc_id in chosen if doc_id not in docs]
    if missing:
        raise SystemExit(f"Selected documents not in the manifest: {', '.join(missing)}")
    return [docs[doc_id] for doc_id in chosen]


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


def log_run(command: str, details: dict, steps: Mapping[str, StepSummary]) -> None:
    """Append one line to eval/runs.jsonl: the command, its settings and, per step, what its Claude calls used (as
    data/runs.jsonl has it). update.py prices a rebuild from these lines."""
    line = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "command": command, **details,
            "cli": analyze.cli_version(), "steps": {name: update.step_json(step) for name, step in steps.items()}}
    path = runs_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as out:  # under eval/, like write_text
        out.write(json.dumps(line, ensure_ascii=False) + "\n")


def finish(steps: Mapping[str, StepSummary]) -> None:
    """Fail the command when a call failed or was skipped: run it again to resume."""
    failed = sum(step.failed + step.skipped for step in steps.values())
    spent = sum(step.usage.cost_usd for step in steps.values())
    print(f"Claude calls used {spent:.2f} USD at list price", flush=True)
    if failed:
        raise SystemExit(f"{failed} Claude calls failed or were skipped; run the command again to continue (answers "
                         f"so far are kept).")


def workers_for(args: argparse.Namespace) -> int:
    """--workers, else 1 under SMALL_BUDGET_USD (each worker's running call may overshoot the limit), else 4."""
    if args.workers is not None:
        return args.workers
    return 1 if args.max_cost is not None and args.max_cost < SMALL_BUDGET_USD else 4


# --------------------------------------------------------------------------- extraction runs

def check_run_name(name: str, what: str = "Run name") -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", name):
        raise SystemExit(f"{what} {name!r}: use lowercase letters, digits, '.', '_' and '-'")
    return name


def data_record(doc: Doc, run: str) -> dict:
    """A document's extraction in data/beslutninger, as it is, as the record of run `run`."""
    source = analyze.DECISIONS_DIR / f"{doc.id}.json"
    if not source.exists():
        raise SystemExit(f"{doc.id} has no extraction in {analyze.DECISIONS_DIR}; run update.py first")
    cached = read_json(source)
    if cached["sha256"] != doc.sha256:
        raise SystemExit(f"{doc.id}: the extraction in {analyze.DECISIONS_DIR} is of another version of the file")
    return {"doc_id": doc.id, "sha256": doc.sha256, "run": run, "model": cached["model"],
            "provenance": cached.get("provenance"), "usage": None, "moededato": cached["moededato"],
            "beslutninger": cached["beslutninger"]}


def copy_from_data(run: str, docs: list[Doc], force: bool) -> list[str]:
    """Copy the documents' extractions in data/beslutninger into run `run`; returns the documents copied. A different
    copy is replaced only with `force`: such a run is a baseline (the key was judged from `stored`, the gate measures
    against BASELINE_RUN), and replacing it moves what is measured against it."""
    records = {doc.id: data_record(doc, run) for doc in docs}
    differ = [doc_id for doc_id, record in records.items()
              if run_path(run, doc_id).exists() and read_json(run_path(run, doc_id)) != record]
    if differ and not force:
        raise SystemExit(f"Run {run} already has other extractions of {', '.join(differ)} than data/ has now; pass "
                         f"--force to replace them, which moves the baseline other runs are measured against")
    copied = [doc_id for doc_id, record in records.items() if not run_path(run, doc_id).exists() or doc_id in differ]
    for doc_id in copied:
        write_json(run_path(run, doc_id), records[doc_id])
    return copied


def run_state(run: str, doc: Doc, model: str, effort: str | None, prompt: str | None = None) -> str:
    """"missing", "current" (this file version, the extraction prompt named `prompt` (None: the pipeline's) and this
    effort) or "stale". One made with another model would mix two configurations under one name, so it stops the
    command."""
    path = run_path(run, doc.id)
    if not path.exists():
        return "missing"
    stored = read_json(path)
    if stored["model"] != model:
        raise SystemExit(f"Run {run} was extracted with {stored['model']}, not {model}; use another --name")
    provenance = stored.get("provenance") or {}
    system = analyze.extract_prompt(prompt).system
    current = (stored["sha256"] == doc.sha256
               and provenance.get("prompt") == analyze.prompt_hash(system, analyze.EXTRACT_SCHEMA)
               and provenance.get("effort") == (effort or "default"))
    return "current" if current else "stale"


def extract_into_run(doc: Doc, run: str, *, model: str, effort: str | None, cli: str, budget: RunBudget,
                     prompt: str | None = None) -> tuple[str, Usage]:
    """One extraction with the pipeline's code (analyze.run_extraction) and the extraction prompt named `prompt`
    (None: the pipeline's), written to eval/runs/<run>/."""
    chosen = analyze.extract_prompt(prompt)
    extraction, usage = analyze.run_extraction(doc, model=model, effort=effort, budget=budget, prompt=prompt)
    with analyze.usage_kept(usage):
        write_json(run_path(run, doc.id), {
            "doc_id": doc.id, "sha256": doc.sha256, "run": run, "model": model, "prompt": chosen.name,
            "provenance": analyze.provenance(usage, cli, chosen.system, analyze.EXTRACT_SCHEMA, effort),
            "usage": usage_json(usage), "moededato": extraction.moededato, "beslutninger": extraction.decisions,
        })
    missing = sum(not d["citat_fundet"] for d in extraction.decisions)
    message = f"{doc.id}: {len(extraction.decisions)} decisions, {missing} quotes not found; {describe_usage(usage)}"
    return message, usage


# --------------------------------------------------------------------------- corrections

# What a correction may change, per kind. Corrections are kept in eval/key/corrections.json, each with its reason and
# evidence from the documents, and applied on top of the judges whenever a key is scored (the stored keys were written
# with those of their time applied).
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


_WORD = re.compile(r"\w+")


def tokens(text: str) -> list[str]:
    """Words as DocWords has them: lowercase runs of letters and digits."""
    return _WORD.findall(text.lower())


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


def rule_keys() -> list[dict]:
    """The rules key (eval/key/rules/), in slug order, with the corrections applied."""
    corrections = load_corrections()
    return [correct_rule_key(read_json(path), corrections) for path in sorted(key_dir("rules").glob("*.json"))]


def soft_rules() -> dict[str, str]:
    """Slug -> why, for the key rules eval/selection.json marks soft (loosely scoped, so which decisions are their
    events is arbitrary): scored and reported, but left out of the overall rules figures."""
    selection = load_selection() if selection_path().exists() else {"rules": []}
    return {r["slug"]: r.get("soft_reason", "soft") for r in selection["rules"] if r.get("soft")}


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


# --------------------------------------------------------------------------- scoring decisions

def decision_candidate(d: dict) -> Candidate:
    """What the matcher compares of an extracted or judged decision (its quote located as citat_pos)."""
    return Candidate(d["emne"] or "", d["tekst"] or "", d["udfald"] or "",
                     matching.quote_span(d.get("citat_pos"), d["citat"] or ""))


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


def decisions_report(scores: Mapping[str, list[DocScore]], doc_ids: list[str], keys: Mapping[str, dict],
                     seed: int, warnings: Sequence[str] = (), stabilities: Mapping[str, float | None] | None = None,
                     gate: Sequence[str] = ()) -> str:
    """Markdown: per run the metrics pooled and averaged per document and the stability of its configuration
    (`stabilities`), median and spread over runs, the gate's lines, a paired bootstrap of each run against the
    first, and how often the judges kept decisions only one run found."""
    stabilities = stabilities or {}
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
             "three fields without it are given too. Doc avg: averaged over documents. Stability: for runs of the "
             "same configuration (model, prompt and effort), the share of their decisions matched one to one "
             "between them, per document and pooled.", ""]
    if outside:
        lines += [f"Not candidate runs: {', '.join(outside)}. The judges never saw their decisions, so those no "
                  f"candidate run had are unjudged, and their precision covers only the rest.", ""]
    lines += [f"Warning: {w}" for w in warnings] + ([""] if warnings else [])
    lines += ["| Run | Recall | Recall (doc avg) | Precision | Precision (doc avg) | Over-split | Kategori | Udfald | "
              "Handling | Niveau | All four | Three (no handling) | Found | False | Extra | Ignored | Unjudged | "
              "Stability |",
              "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|"]
    totals = {run: total(found) for run, found in scores.items()}
    for run, s in totals.items():
        values = [_pct(METRICS["recall"](s)), _pct(per_document(scores[run], METRICS["recall"])),
                  _pct(METRICS["precision"](s)), _pct(per_document(scores[run], METRICS["precision"]))]
        values += [_pct(METRICS[m](s)) for m in METRICS if m not in ("recall", "precision")]
        lines.append(f"| {run} | {' | '.join(values)} | {s.found} | {s.false} | {s.extra} | {s.ignored} | "
                     f"{s.unjudged} | {_pct(stabilities.get(run))} |")
    if len(totals) > 1:
        cells = []
        for m in ("recall", "recall_doc", "precision", "precision_doc", *list(METRICS)[2:]):
            if m.endswith("_doc"):
                values = [v for run in scores if (v := per_document(scores[run], METRICS[m[:-4]])) is not None]
            else:
                values = [v for s in totals.values() if (v := METRICS[m](s)) is not None]
            cells.append(f"{_pct(statistics.median(values))} ({_pct(min(values))}–{_pct(max(values))})"
                         if values else "–")
        lines.append(f"| median (min–max) | {' | '.join(cells)} | | | | | | |")
    lines += ["", *gate] if gate else []
    if len(totals) > 1:
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


# --------------------------------------------------------------------------- stability and the gate

@dataclass(frozen=True)
class Configuration:
    """What produced an extraction run: the model that answered, the prompt's fingerprint and the effort."""
    model: str
    prompt: str  # analyze.prompt_hash of the system prompt and the schema
    effort: str

    def label(self) -> str:
        names = {analyze.prompt_hash(text, analyze.EXTRACT_SCHEMA): name
                 for name, text in analyze.EXTRACT_PROMPTS.items()}
        return f"{self.model}, prompt {names.get(self.prompt, self.prompt)}, effort {self.effort}"


def configuration(records: Iterable[dict]) -> Configuration | None:
    """A run's configuration from its documents' provenance; None when a document has none (the stored run was
    extracted before provenance was kept) or they differ: then the run is no other run's repeat."""
    found = set()
    for record in records:
        p = record.get("provenance")
        if not p:
            return None
        found.add(Configuration(p["model"], p["prompt"], p["effort"]))
    return found.pop() if len(found) == 1 else None


def pipeline_configuration() -> Configuration:
    """Today's pipeline: update.py's extraction model and prompt, at Claude Code's default effort."""
    return Configuration(update.EXTRACT_MODEL, analyze.prompt_hash(analyze.EXTRACT_SYSTEM, analyze.EXTRACT_SCHEMA),
                         "default")


def stability(pairs: Iterable[tuple[list[dict], list[dict]]]) -> float | None:
    """How alike two runs are: the share of their decisions matched one to one between them (matching.match_decisions,
    which also carries decision ids over), per document and pooled: 2 × matched / (decisions of both runs). The key
    cannot show this: two runs may score alike on it and still find different decisions."""
    matched = decisions = 0
    for a, b in pairs:
        matched += len(matching.match_decisions([decision_candidate(d) for d in a], [decision_candidate(d) for d in b]))
        decisions += len(a) + len(b)
    return _ratio(2 * matched, decisions)


def repeated_configurations(configurations: Mapping[str, Configuration | None]) -> dict[Configuration, list[str]]:
    """The configurations scored with two or more runs, each with its runs in the order given."""
    groups: dict[Configuration, list[str]] = defaultdict(list)
    for run, config in configurations.items():
        if config is not None:
            groups[config].append(run)
    return {config: runs for config, runs in groups.items() if len(runs) > 1}


@dataclass(frozen=True)
class GateResult:
    """One configuration against the gate: its worse run's figures, its stability and the criteria it misses."""
    configuration: Configuration
    runs: tuple[str, ...]
    worse: Mapping[str, float | None]  # each GATE_SLACK figure of its worse run on it
    stability: float | None
    failed: tuple[str, ...]


def worst(runs: Sequence[DocScore]) -> dict[str, float | None]:
    """Each GATE_SLACK figure of the worse of the runs on it: the lowest, or the highest where lower is better; None
    when a run lacks it."""
    found: dict[str, float | None] = {}
    for m in GATE_SLACK:
        values = [METRICS[m](s) for s in runs]
        found[m] = None if None in values else max(values) if m in GATE_LOWER_IS_BETTER else min(values)
    return found


def gate_needs(baseline: DocScore, reference: float | None,
               today: Mapping[str, float | None]) -> dict[str, float | None]:
    """The bound on each gate figure: the looser of the baseline run's figure moved by its GATE_SLACK (down, or up
    where lower is better) and `today`, today's pipeline's worst run's figure (worst; empty when it was not run
    twice); for stability, `reference`, that of today's pipeline's runs (None: not scored). A figure with neither bound
    gets None, which fails."""
    needs: dict[str, float | None] = {}
    for m, slack in GATE_SLACK.items():
        value, lower = METRICS[m](baseline), m in GATE_LOWER_IS_BETTER
        moved = None if value is None else value + slack if lower else value - slack
        bounds = [b for b in (moved, today.get(m)) if b is not None]
        needs[m] = (max(bounds) if lower else min(bounds)) if bounds else None
    return {**needs, "stability": reference}


def _within(metric: str, value: float | None, bound: float | None) -> bool:
    if value is None or bound is None:
        return False
    if metric in GATE_LOWER_IS_BETTER:
        return value <= bound + GATE_TOLERANCE
    return value >= bound - GATE_TOLERANCE


def gate(totals: Mapping[str, DocScore], groups: Mapping[Configuration, list[str]],
         stabilities: Mapping[Configuration, float | None], needs: Mapping[str, float | None]) -> list[GateResult]:
    """Each repeated configuration against `needs` (gate_needs): its worse run on each figure must be within its
    bound, and its runs' stability reach that of today's pipeline. A figure missing on either side fails."""
    results = []
    for config, runs in groups.items():
        worse = worst([totals[run] for run in runs])
        figures = {**worse, "stability": stabilities[config]}
        failed = tuple(m for m, value in figures.items() if not _within(m, value, needs[m]))
        results.append(GateResult(config, tuple(runs), worse, stabilities[config], failed))
    return results


def gate_section(results: Sequence[GateResult], needs: Mapping[str, float | None], baseline: str,
                 today: Sequence[str], today_worst: Mapping[str, float | None]) -> list[str]:
    """Markdown lines: the gate, what it needs and why (the slack, and the worst of `today`, the runs of today's
    pipeline), and each repeated configuration against it."""
    pipeline = pipeline_configuration()
    reference = pipeline.label()
    unscored = "" if needs["stability"] is not None else "; they were not scored, so no configuration passes"
    if today:
        bound = (f"than the run {baseline} moved by its slack or than the worst run of today's pipeline, whichever is "
                 f"looser")
    else:
        bound = f"than the run {baseline} moved by its slack (today's pipeline was not run twice here)"
    noise =(f" The run {baseline} is one of today's pipeline's runs, which differ on noise alone, so a candidate as "
             f"good as their worst passes." if baseline in today else "")
    lines = ["## Gate", "",
             f"A configuration passes when its worse run is no worse, on recall, precision, the three fields and "
             f"over-split, {bound}, and its runs are at least as stable as the runs of today's pipeline "
             f"({reference}){unscored}.{noise}", "",
             "| Configuration | Runs | Recall (worse) | Precision (worse) | Three (worse) | Over-split (worse) | "
             "Stability | Passes |",
             "|---|---|--:|--:|--:|--:|--:|---|",
             "| needs | | " + " | ".join(f"{'≤' if m in GATE_LOWER_IS_BETTER else '≥'} {_pct(needs[m])}"
                                       for m in (*GATE_SLACK, "stability")) + " | |",
             "| slack (points) | | " + " | ".join(f"{100 * GATE_SLACK[m]:.1f}" for m in GATE_SLACK) + " | | |"]
    if today:
        lines.append(f"| today's pipeline, worst run per figure | {', '.join(today)} | "
                     + " | ".join(_pct(today_worst[m]) for m in GATE_SLACK) + " | | |")
    for r in results:
        passes = f"no: {', '.join(r.failed)}" if r.failed else "yes"
        if not r.failed and r.configuration == pipeline:
            passes += " (today's pipeline: the reference)"  # its worst run and its stability set the bounds
        label = r.configuration.label() + (" (today's pipeline)" if r.configuration == pipeline else "")
        figures = " | ".join(_pct(r.worse[m]) for m in GATE_SLACK)
        lines.append(f"| {label} | {', '.join(r.runs)} | {figures} | {_pct(r.stability)} | {passes} |")
    return lines


# --------------------------------------------------------------------------- scoring rules

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
    """Key event id -> the pipeline decision it is, matched (match_decisions) among the decisions of its document.

    The key quotes a budget line whole ("Årsafgift: kr. 1.000,- (Uændret) - Licens: kr. 200,- (Uændret).") for each
    fee's rule, while extraction prompt v3 makes it one decision per fee. Every fee's decision then lies inside the
    event's quote, so the quote overlap ties, and the matcher's text part (word trigrams of the key's value against
    the decision's sentence) mostly shares nothing: the line's first decision won for every fee. So when an event's
    quote covers several decisions (more than half of each one's quoted words inside it) and the matcher picked one
    of them, the event gets the free one most about the key's rule (key_similarity), then the one most inside the
    quote, then the first. Containment decides which decisions compete, not which wins: in rep2015 the medal
    decision's quote runs a sentence past the key's, while the start fee's, quoted a sentence too long, lies wholly
    inside it. Each decision still goes to one event at most, and an event covering one decision or none keeps the
    matcher's pick."""
    vocabulary = candidates.vocabulary([key["titel"], *key.get("keywords", ()), *(
        t for decisions in decisions_by_doc.values() for d in decisions for t in (d.emne, d.tekst))])
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
        matches = matching.match_decisions(wanted, found)
        chosen = {m.old: m.new for m in matches}
        for m in sorted(matches, key=lambda m: (-m.score, m.old)):  # the matcher's own order
            inside = {j: share_inside(c.span, wanted[m.old].span) for j, c in enumerate(found)}
            covered = [j for j, share in inside.items() if share > 0.5]
            if len(covered) < 2 or chosen[m.old] not in covered:
                continue
            held = set(chosen.values()) - {chosen[m.old]}
            terms = key_terms(key, events[m.old], vocabulary)
            chosen[m.old] = max((j for j in covered if j not in held),
                                key=lambda j: (key_similarity(terms, pipeline[j], vocabulary), inside[j], -j))
        mapped |= {events[i]["id"]: pipeline[j].ref for i, j in chosen.items()}
    return mapped


def share_inside(span: tuple[int, int] | None, outer: tuple[int, int] | None) -> float:
    """The share of a decision's quoted words inside a key event's quote; 0 when either was not located."""
    if span is None or outer is None:
        return 0.0
    return max(min(span[1], outer[1]) - max(span[0], outer[0]), 0) / (span[1] - span[0])


def key_terms(key: dict, event: dict, vocabulary: Collection[str]) -> Counter[str]:
    """What a key event is about, as candidates.terms (Danish stems and compound parts): the rule's title, which
    names it and counts candidates.TITLE_WEIGHT times, its keywords and the event's value."""
    terms = Counter(candidates.terms(key["titel"], vocabulary) * candidates.TITLE_WEIGHT)
    for text in (*key.get("keywords", ()), event["value_after"]):
        terms.update(candidates.terms(text, vocabulary))
    return terms


def key_similarity(terms: Counter[str], decision: Decision, vocabulary: Collection[str]) -> float:
    """The weighted share of a key event's terms (key_terms) that the decision's emne and tekst use."""
    used = set(candidates.terms(f"{decision.emne} {decision.tekst}", vocabulary))
    total = sum(terms.values())
    return sum(n for term, n in terms.items() if term in used) / total if total else 0.0


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


def score_section(before: list[dict], after: list[dict], docs: list[Doc], decisions: list[Decision]) -> list[str]:
    """Markdown lines for audit.py's report: the rule scores (score_rule) of two sets of rule files over today's
    decisions, and the audit's gate; empty without a key."""
    keys, soft = rule_keys(), soft_rules()
    if not keys:
        return []
    texts, by_id = Texts(), {doc.id: doc for doc in docs}
    old = [score_rule(key, decisions, before, by_id, texts) for key in keys]
    new = [score_rule(key, decisions, after, by_id, texts) for key in keys]
    a = rules_summary([s for s in old if s.slug not in soft])
    b = rules_summary([s for s in new if s.slug not in soft])
    lines = ["## Answer key: before → after", "",
             "evaluate.py's rules scores (score-rules) over today's decisions. Rules: pipeline rules holding the key "
             "rule's certain events (more than one: fragmented); Found: in the rule holding most of them; Same "
             "content: years whose version in force was adopted by the key's adopting event.", "",
             "| Rule | Rules | Found | Elsewhere | Missing | Same content |", "|---|--:|--:|--:|--:|--:|"]
    for o, n in zip(old, new):
        name = f"{o.slug} (soft)" if o.slug in soft else o.slug
        lines.append(f"| {name} | {o.rules} → {n.rules} | {o.found} → {n.found} | {o.elsewhere} → "
                     f"{n.elsewhere} | {o.missing} → {n.missing} | {o.same_content} → {n.same_content} of "
                     f"{n.years} |")
    passes = (b["fragmented"] < a["fragmented"] and b["found"] >= a["found"] and b["same_content"] >= a["same_content"]
              and b["missing"] <= a["missing"])
    lines += ["", f"Without soft rules: fragmented rules {a['fragmented']} → {b['fragmented']}; events found in the "
                  f"right rule {_pct(a['found_share'])} → {_pct(b['found_share'])}; years with the same content in "
                  f"force {_pct(a['content_share'])} → {_pct(b['content_share'])}; missing events {a['missing']} → "
                  f"{b['missing']}.", "",
              f"Gate (fragmented rules fall, found and same content do not fall, no event goes missing): "
              f"{'passes' if passes else 'fails'}.", ""]
    return lines


# --------------------------------------------------------------------------- incremental consolidation: candidates

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


# --------------------------------------------------------------------------- commands

def cmd_extract(args: argparse.Namespace) -> None:
    run = check_run_name(args.name)
    docs = selected_docs(load_selection(), args.pilot, args.docs)
    if args.from_data:
        calling = [flag for flag, value in (("--model", args.model), ("--max-cost", args.max_cost),
                                            ("--prompt", args.prompt), ("--effort", args.effort),
                                            ("--workers", args.workers)) if value is not None]
        if calling:
            raise SystemExit(f"--from-data copies data/beslutninger without calling Claude, so it takes no "
                             f"{', '.join(calling)}")
        copied = copy_from_data(run, docs, args.force)
        print(f"Copied {len(copied)} of {len(docs)} extractions from {analyze.DECISIONS_DIR} to {run_dir(run)}")
        return
    if not args.model or args.max_cost is None:
        raise SystemExit("extract calls Claude: give --model and --max-cost")
    prompt = analyze.extract_prompt(args.prompt)
    states = {doc.id: run_state(run, doc, args.model, args.effort, args.prompt) for doc in docs}
    stale = [doc_id for doc_id, state in states.items() if state == "stale"]
    if stale and not args.force:
        raise SystemExit(f"Run {run} has extractions of {', '.join(stale)} from another file version, prompt or "
                         f"effort; pass --force to extract them again (paid), or use another --name")
    todo = [doc for doc in docs if states[doc.id] != "current"]
    tokens_in = sum(estimate_tokens(prompt.system, analyze.document_prompt(doc, analyze.document_text(doc)),
                                    analyze.EXTRACT_SCHEMA) for doc in todo)
    print(f"Extract {run}: {len(todo)} Claude calls to {args.model} (effort {args.effort or 'default'}), about "
          f"{tokens_in / 1000:.0f}K input tokens, prompt {prompt.name}; {len(docs) - len(todo)} of {len(docs)} "
          f"documents extracted already", flush=True)
    if not todo:
        return
    budget = RunBudget(max_cost_usd=args.max_cost)
    cli = analyze.cli_version()
    step = analyze.run_parallel(
        todo, lambda doc: extract_into_run(doc, run, model=args.model, effort=args.effort, cli=cli, budget=budget,
                                           prompt=args.prompt),
        workers_for(args), f"Extract {run}", budget)
    done = [doc.id for doc in todo if run_state(run, doc, args.model, args.effort, args.prompt) == "current"]
    log_run("extract", {"run": run, "model": args.model, "prompt": prompt.name, "effort": args.effort,
                        "documents": done}, {"extract": step})
    finish({"extract": step})


def cmd_score(args: argparse.Namespace) -> None:
    runs = [check_run_name(run) for run in args.run]
    # A baseline named on purpose must be scored; the default's gate is left out when it is not.
    if args.baseline is not None and check_run_name(args.baseline, "--baseline") not in runs:
        raise SystemExit(f"--baseline {args.baseline}: also pass --run {args.baseline}")
    baseline = args.baseline or BASELINE_RUN
    corrections = load_corrections()
    keys = {path.stem: correct_decisions_key(read_json(path), corrections)
            for path in sorted(key_dir("decisions").glob("*.json"))}
    if not keys:
        raise SystemExit(f"No decisions key in {key_dir('decisions')}; it was built once (eval/README.md)")
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
    groups = repeated_configurations({run: configuration(stored[run, doc_id] for doc_id in doc_ids) for run in runs})
    by_config = {config: stability((stored[a, doc_id]["beslutninger"], stored[b, doc_id]["beslutninger"])
                                   for a, b in combinations(members, 2) for doc_id in doc_ids)
                 for config, members in groups.items()}
    by_run = {run: by_config[config] for config, members in groups.items() for run in members}
    gate_lines: list[str] = []
    if args.baseline is not None and not groups:
        log.warning("No configuration was run twice, so no gate was computed against %s", baseline)
    if baseline in runs and groups:
        totals = {run: total(s) for run, s in scores.items()}
        today = groups.get(pipeline_configuration(), [])  # the baseline among them when it has today's configuration
        today_worst = worst([totals[run] for run in today]) if today else {}
        needs = gate_needs(totals[baseline], by_config.get(pipeline_configuration()), today_worst)
        gate_lines = gate_section(gate(totals, groups, by_config, needs), needs, baseline, today, today_worst)
    report = decisions_report(scores, doc_ids, {doc_id: keys[doc_id] for doc_id in doc_ids}, args.seed, warnings,
                              by_run, gate_lines)
    if left_out:
        report += f"\nLeft out, not extracted by every run: {', '.join(left_out)}\n"
    name = args.report or f"decisions-{'+'.join(runs)}"
    write_text(report_path(name), report)
    print(report)


def cmd_score_rules(args: argparse.Namespace) -> None:
    keys = rule_keys()
    if not keys:
        raise SystemExit(f"No rules key in {key_dir('rules')}; it was built once (eval/README.md)")
    soft = soft_rules()
    rules_dir = args.rules_dir or analyze.RULES_DIR
    docs = scrape.load_manifest()
    decisions = analyze.load_decisions(docs, args.decisions_dir)
    raw_rules = analyze.load_rules(rules_dir)
    texts = Texts()
    by_id = {doc.id: doc for doc in docs}
    scores = [score_rule(key, decisions, raw_rules, by_id, texts) for key in keys]
    report = rules_report(scores, rules_dir, soft, [c for key in keys for c in key["corrections"]])
    name = args.report or f"rules-{rules_dir.name}"
    write_text(report_path(name), report)
    print(report)


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


def _ids(text: str) -> list[str]:
    return [item for item in text.split(",") if item]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    extract = sub.add_parser("extract", help="run today's extraction on the selected documents")
    extract.add_argument("--name", required=True, help="run name: eval/runs/<name>/")
    extract.add_argument("--from-data", action="store_true",
                         help="copy data/beslutninger's extractions as they are instead of calling Claude: a baseline "
                              "run, e.g. after a migration")
    extract.add_argument("--model", help="model id, e.g. claude-sonnet-5-5")
    extract.add_argument("--effort", choices=update.EFFORTS, help="effort (default: Claude Code's own)")
    extract.add_argument("--prompt", choices=list(analyze.EXTRACT_PROMPTS),
                         help=f"extraction prompt (default: the pipeline's, v{analyze.EXTRACT_VERSION})")
    extract.add_argument("--force", action="store_true",
                         help="replace extractions of another file version, prompt or effort (paid; with --from-data: "
                              "other copies)")
    extract.add_argument("--max-cost", type=float, metavar="USD",
                         help="start no new Claude calls once this much is used at list price")
    extract.add_argument("--workers", type=int,
                         help=f"parallel Claude calls; calls running when the limit is reached finish, so the cost can "
                              f"exceed it by one call per worker (default: 1 below {SMALL_BUDGET_USD:.0f} USD, else 4)")
    only = extract.add_mutually_exclusive_group()
    only.add_argument("--pilot", type=int, metavar="N", help="only the first N selected documents")
    only.add_argument("--docs", type=_ids, metavar="ID,...",
                      help="only these selected documents (their extractions are reused by the full run)")
    extract.set_defaults(func=cmd_extract)

    score = sub.add_parser("score", help="score extraction runs against the decisions key (no Claude)")
    score.add_argument("--run", action="append", required=True, help="a run to score; repeat to compare runs")
    score.add_argument("--baseline", metavar="RUN",
                       help=f"the run the gate measures against, which must be among the --run (default: "
                            f"{BASELINE_RUN}, today's data/ after the v3 migration; the gate is left out when it is "
                            f"not scored)")
    score.add_argument("--seed", type=int, default=SEED, help="bootstrap seed")
    score.add_argument("--report", help="report name under eval/reports/")
    score.set_defaults(func=cmd_score)

    score_rules = sub.add_parser("score-rules", help="score rule files against the rules key (no Claude)")
    score_rules.add_argument("--rules-dir", type=Path, help="default: data/regler")
    score_rules.add_argument("--decisions-dir", type=Path, help="default: data/beslutninger")
    score_rules.add_argument("--report", help="report name under eval/reports/")
    score_rules.set_defaults(func=cmd_score_rules)

    recall = sub.add_parser("candidate-recall",
                            help="incremental gate: how often the candidate ranking offers a decision's own rule (no "
                                 "Claude)")
    recall.set_defaults(func=cmd_candidate_recall)
    return p


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
