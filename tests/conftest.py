import json
import os
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analyze  # noqa: E402
import incremental  # noqa: E402
import render  # noqa: E402
import update  # noqa: E402
from analyze import Decision, RunBudget, decision_hash  # noqa: E402
from scrape import Doc  # noqa: E402

_BASE = Decision(
    ref="doc#1", doc_id="doc", dato="2020-03-01", emne="Licensgebyr", kategori="okonomi", udfald="vedtaget",
    handling="ny", niveau="staevneregel", tekst="Licensgebyret er 200 kr.", citat="licensgebyret er 200 kr",
    citat_fundet=True, side=1, side_rettet=False, rank=0, stemmer=None, forslagsstiller=None,
    gaelder_fra=None, gaelder_til=None,
)


def decision(**changes) -> Decision:
    return replace(_BASE, **changes)


def extracted(emne: str, tekst: str | None = None, kategori: str = "okonomi", **fields) -> dict:
    """A decision as the extraction answers it, adopting a new rule on page 1; its text and quote are `emne` unless
    given. `fields` override or add any field (udfald, citat, id, …)."""
    tekst = tekst or emne
    return {"emne": emne, "kategori": kategori, "udfald": "vedtaget", "forslagsstiller": None, "handling": "ny",
            "niveau": "staevneregel", "tekst": tekst, "citat": tekst, "side": 1, "stemmer": None, "gaelder_fra": None,
            "gaelder_til": None, **fields}


def rule(titel: str, slug: str | None, *versions, **fields) -> dict:
    """A rule as a rule file holds it, or as a consolidation answers it when `slug` is None. Each version is a decision,
    or a (decision, effekt[, version fields]) tuple: a ref, or a Decision, whose fingerprint (dhash) the version then
    records; it introduces the rule unless another effect is given, in the decision's own words (no texts of its own).
    `fields` override or add any key of the rule (kategori, vigtig, …)."""
    def version(d, effekt="indfoert", changes=None) -> dict:
        ref = d if isinstance(d, str) else d.ref
        fingerprint = {} if isinstance(d, str) else {"dhash": decision_hash(d)}
        return {"ref": ref, "effekt": effekt, "tekst": None, "kort": None, "kort_regel": None, **fingerprint,
                **(changes or {})}

    return {"titel": titel, **({} if slug is None else {"slug": slug}), "vigtig": True, "note": None,
            "versioner": [version(*v) if isinstance(v, tuple) else version(v) for v in versions], **fields}


def section(prompt: str, tag: str):
    """The JSON a prompt gives between <tag> and </tag>."""
    return json.loads(prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])


@pytest.fixture(autouse=True)
def _slug_registry(tmp_path_factory, monkeypatch):
    """Every test gets its own data/slugs.json, so none can write the real one; outside the test's tmp_path,
    which some tests use as data/regler/ itself."""
    monkeypatch.setattr(analyze, "SLUGS_PATH", tmp_path_factory.mktemp("registry") / "slugs.json")


# ---------------------------------------------------------------- data/ in tmp_path

NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
TEXT = ("[Side 1]\nDagsorden og godkendelse af referat. " + "Mødet drøftede andre sager. " * 40 +
        "Licensgebyret hæves til 350 kr. pr. løfter fra næste år. Vedtaget med 30 stemmer. " +
        "Startgebyret er 200 kr. pr. stævne. " + "Eventuelt intet. " * 80)


class World:
    """data/ in tmp_path: the documents styrke.dk lists, their decisions and rule files, written as the pipeline
    writes them. Every document reads as TEXT unless `texts` gives it another."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path / "beslutninger")
        monkeypatch.setattr(analyze, "RULES_DIR", tmp_path / "regler")
        monkeypatch.setattr(analyze, "document_text", lambda doc: self.texts.get(doc.id, TEXT))
        analyze.DECISIONS_DIR.mkdir()
        analyze.RULES_DIR.mkdir()
        self.docs: list[Doc] = []
        self.texts: dict[str, str] = {}

    def downloaded(self, doc_id: str, dato: str | None = "2024") -> Doc:
        """A document styrke.dk lists, not extracted yet (replacing one of that id)."""
        doc = Doc(doc_id, "repraesentantskab", "Referat", dato, f"{doc_id}.pdf", None, f"sha-{doc_id}")
        self.docs = [d for d in self.docs if d.id != doc_id] + [doc]
        return doc

    def document(self, doc_id: str, dato: str | None, *decisions: dict, retired: tuple[str, ...] = ()) -> None:
        """A document extracted by the pipeline's prompt and model: `decisions` (extracted) numbered from 1 unless
        they carry an id, each quote found on page 1 unless they say otherwise; `retired` lists the ids it lost."""
        self.downloaded(doc_id, dato)
        numbered = [{"id": f"{doc_id}#{n}", "citat_fundet": True, "citat_pos": None, "citat_side": 1, **d}
                    for n, d in enumerate(decisions, start=1)]
        used = [d["id"] for d in numbered] + list(retired)
        analyze._write_json(analyze.DECISIONS_DIR / f"{doc_id}.json", {
            "doc_id": doc_id, "sha256": f"sha-{doc_id}", "version": analyze.EXTRACT_VERSION,
            "model": update.EXTRACT_MODEL,
            "provenance": {"model": update.EXTRACT_MODEL, "cli": "2.1.294", "prompt": "x", "effort": "default"},
            "moededato": dato, "next_number": analyze._highest_number(doc_id, used) + 1,
            "retired": [{"id": ref, "emne": "x", "reason": "no match in a re-extraction", "date": "2026-10-01"}
                        for ref in retired],
            "beslutninger": numbered})

    def extraction(self, doc_id: str) -> dict:
        """The document's stored extraction."""
        return json.loads((analyze.DECISIONS_DIR / f"{doc_id}.json").read_text())

    def decisions(self) -> list[Decision]:
        return analyze.load_decisions(self.docs)

    def rules(self, category: str, *rules: dict, udeladt: tuple[str, ...] = ()) -> None:
        """A category's rule file (rules made by `rule`, with slugs), each version with the fingerprint of its decision
        as it is now."""
        by_ref = {d.ref: d for d in self.decisions()}
        regler = [{**r, "versioner": [{**v, "dhash": decision_hash(by_ref[v["ref"]])} for v in r["versioner"]]}
                  for r in rules]
        analyze._write_json(analyze.RULES_DIR / f"{category}.json", {
            "kategori": category, "version": analyze.CONSOLIDATE_VERSION, "input_hash": "x", "model": "opus",
            "regler": regler, "udeladt": list(udeladt), "ikke_tildelt": []})

    def consolidated(self) -> None:
        """Every rule file's input_hash as a full consolidation of today's decisions leaves it."""
        for job in analyze.consolidation_todo(self.decisions(), {d.id: d.organ_label for d in self.docs}):
            stored = self.stored(job.category)
            analyze._write_json(analyze.RULES_DIR / f"{job.category}.json", {**stored, "input_hash": job.input_hash})

    def stored(self, category: str) -> dict:
        return json.loads((analyze.RULES_DIR / f"{category}.json").read_text())

    def rule(self, category: str, slug: str) -> dict:
        return next(r for r in self.stored(category)["regler"] if r["slug"] == slug)

    def files(self) -> dict[str, bytes]:
        return {p.name: p.read_bytes() for p in sorted(analyze.RULES_DIR.glob("*.json"))}

    def consolidate(self, known: incremental.Known | None = None, **settings) -> analyze.StepSummary:
        """An incremental consolidation with one worker, so the fake answers its calls in order."""
        return incremental.consolidate(self.docs, self.decisions(), incremental.Settings(workers=1, **settings),
                                       RunBudget(), now=NOW, known=known)

    def known(self) -> incremental.Known:
        """What the rule files reflect now, as update.py takes it before the extraction."""
        return incremental.known_inputs(self.decisions(), incremental.RuleBook.load(), self.docs)


@pytest.fixture
def world(tmp_path, monkeypatch) -> World:
    """An empty data/, which a test fills, or a module's own `world` fixture that builds on this one."""
    return World(tmp_path, monkeypatch)


@pytest.fixture
def run(world, fake_claude, monkeypatch):
    """update.main(), offline and with one worker (the fake answers its calls in order), on the world's documents; the
    run log, the report and the pages go to the world's root, and the website is not built."""
    monkeypatch.setattr(update, "RUNS_LOG", world.root / "runs.jsonl")
    monkeypatch.setattr(update, "RUN_REPORT", world.root / "run-report.md")
    monkeypatch.setattr(update.website, "build", lambda *args: world.root / "index.html")
    monkeypatch.setattr(render, "OUT_DIR", world.root / "regelsaet")
    monkeypatch.setattr(update.scrape, "ROOT", world.root)
    monkeypatch.setattr(update.scrape, "load_manifest", lambda: world.docs)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def main(*args: str) -> None:
        monkeypatch.setattr(sys, "argv", ["update.py", "--offline", "--workers", "1", *args])
        update.main()

    return main


# ---------------------------------------------------------------- the fake `claude`

# A fake `claude` that prints what Claude Code 2.1.294 prints. plan.json lists what each call does, in order
# (the last step repeats): "ok"; "error" (exit 1, costs 0.10); "other" (answers with Haiku); "sleep";
# "helper-before"/"helper-after" (a helper model that writes less, sorting before/after the main one);
# "keyed" (modelUsage keyed by another name than canonicalModel, plus an entry that is not an object).
# output.json, if present, is the structured output; outputs.json, if present, lists one per call (the last
# repeats). Each call's arguments are appended to argv.jsonl, and its prompt (stdin) to prompts.jsonl.
FAKE_CLAUDE = """\
import json, sys, time
from pathlib import Path

here = Path(__file__).parent
with (here / "invocations").open("a") as log:
    log.write(("version" if sys.argv[1:] == ["--version"] else "call") + "\\n")
if sys.argv[1:] != ["--version"]:
    with (here / "argv.jsonl").open("a") as log:
        log.write(json.dumps(sys.argv[1:]) + "\\n")
    with (here / "prompts.jsonl").open("a") as log:
        log.write(json.dumps(sys.stdin.read()) + "\\n")
if sys.argv[1:] == ["--version"]:
    if (here / "version_fails").exists():
        sys.exit(1)
    if (here / "version_garbled").exists():
        sys.stdout.buffer.write(b"2.1.294 \\xff\\xfe (Claude Code)\\n")
        sys.exit(0)
    print("2.1.294 (Claude Code)")
    sys.exit(0)
calls = (here / "invocations").read_text().split().count("call")
plan = json.loads((here / "plan.json").read_text()) if (here / "plan.json").exists() else ["ok"]
step = plan[min(calls, len(plan)) - 1]
if step == "sleep":
    time.sleep(10)
requested = sys.argv[sys.argv.index("--model") + 1]
model = "claude-haiku-5-5" if step == "other" else {"sonnet": "claude-sonnet-5-5"}.get(requested, requested)
cost = 0.1 if step == "error" else 0.25
entry = {"inputTokens": 2, "outputTokens": 52, "cacheReadInputTokens": 967, "cacheCreationInputTokens": 2514,
         "costUSD": cost, "canonicalModel": model, "costBasis": "list"}
usage = {model: entry}
if step.startswith("helper"):
    helper = "claude-aaa-helper" if step == "helper-before" else "claude-zzz-helper"
    usage[helper] = {**entry, "outputTokens": 5, "canonicalModel": helper}
if step == "keyed":
    usage = {"us.anthropic." + model: entry, "junk": "not an object"}
result = {"type": "result", "is_error": step == "error", "total_cost_usd": cost, "duration_ms": 1200,
          "num_turns": 1, "modelUsage": usage}
if step == "error":
    print(json.dumps({**result, "result": "API Error: overloaded"}))
    sys.exit(1)
output = here / "output.json"
answer = json.loads(output.read_text()) if output.exists() else {"answer": "ok"}
if (here / "outputs.json").exists():
    outputs = json.loads((here / "outputs.json").read_text())
    answer = outputs[min(calls, len(outputs)) - 1]
print(json.dumps({**result, "result": "", "structured_output": answer}))
"""


class FakeClaude:
    def __init__(self, bin_dir: Path):
        self.bin_dir = bin_dir

    def plan(self, *steps: str) -> None:
        (self.bin_dir / "plan.json").write_text(json.dumps(steps))

    def answer(self, output: dict) -> None:
        (self.bin_dir / "output.json").write_text(json.dumps(output))

    def answers(self, *outputs: dict) -> None:
        """One structured output per call, in order; the last repeats."""
        (self.bin_dir / "outputs.json").write_text(json.dumps(outputs))

    def calls(self) -> list[list[str]]:
        """The arguments of every Claude call so far, in order."""
        log = self.bin_dir / "argv.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def option(self, name: str) -> list[str]:
        """The value every Claude call so far gave option `name` (e.g. "--model"), in order."""
        return [args[args.index(name) + 1] for args in self.calls()]

    def prompts(self) -> list[str]:
        """The prompt (stdin) of every Claude call so far, in order."""
        log = self.bin_dir / "prompts.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def fail_version(self) -> None:
        (self.bin_dir / "version_fails").touch()

    def garble_version(self) -> None:
        """`claude --version` prints bytes that are not UTF-8."""
        (self.bin_dir / "version_garbled").touch()

    def invocations(self, kind: str) -> int:
        """How often the CLI ran: "call" for a Claude call, "version" for `claude --version`."""
        log = self.bin_dir / "invocations"
        return log.read_text().split().count(kind) if log.exists() else 0


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(f"#!{sys.executable}\n{FAKE_CLAUDE}")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setattr(analyze.time, "sleep", lambda _: None)  # the back-off between attempts
    analyze.cli_version.cache_clear()
    yield FakeClaude(bin_dir)
    analyze.cli_version.cache_clear()
