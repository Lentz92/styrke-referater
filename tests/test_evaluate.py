"""evaluate.py: extraction runs, scoring against the answer key and the incremental consolidation's candidate gate,
on a tiny corpus in tmp_path; Claude calls go to the fake `claude` from conftest.py."""

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pytest
from conftest import extracted, rule

import analyze
import evaluate
import scrape
from analyze import DocWords, quote_fields
from evaluate import DocScore
from matching import quote_span
from scrape import Doc

FILLER = " ".join(["mødet drøftede andre sager"] * 35)  # 140 words
TEXTS = {
    "rep2010": "Repræsentantskabsmøde 2010. Budgettet blev vedtaget. Licensgebyret er hævet til 150 kr. pr. løfter. "
               "Startgebyret er 150 kr. pr. start.",
    "rep2013": "Repræsentantskabsmøde 2013. Diverse takster: Licens: kr. 200 (uændret). Årsafgift: kr. 1000 "
               "(uændret). Startgebyret hæves til 200 kr. pr. start. Licensen betales ved første stævne i året.",
    "best2015": "Bestyrelsesmøde 2015. Kassereren sender mail til klubberne om licens. Klubskifte blev drøftet.",
    "rep2016": f"Repræsentantskabsmøde 2016. Licensgebyret er hævet til 250 kr. pr. løfter. {FILLER} Licens: kr. 250 "
               f"(uændret) i budgettet. {FILLER} {FILLER[:300]} Licens: kr. 275 i næste budget. {FILLER[:200]}",
    "rep2019": "Repræsentantskabsmøde 2019. Takster: Licens: kr. 200 og startgebyr kr. 300.",
    "rep2024": "Repræsentantskabsmøde 2024. Forslag fra bestyrelsen: Licensgebyret hæves fra 200 kr. til 300 kr. "
               "Forslaget blev vedtaget. Klubskifte kræver tre måneders karantæne.",
}
ORGANS = {"best2015": "bestyrelse"}
YEARS = {"best2015": "2015-05-01"}


STORED = {
    "rep2010": [extracted("Licensgebyr", "Licensgebyret er 150 kr. pr. løfter.",
                          citat="Licensgebyret er hævet til 150 kr. pr. løfter"),
                extracted("Startgebyr", citat="Startgebyret er 150 kr. pr. start")],
    "rep2013": [extracted("Licensgebyr", "Licensgebyret er 200 kr.", handling="bekraeftelse",
                          citat="Licens: kr. 200 (uændret)"),
                extracted("Startgebyr", handling="aendring", citat="Startgebyret hæves til 200 kr. pr. start"),
                extracted("Betaling af licens", handling="bekraeftelse",
                          citat="Licensen betales ved første stævne i året")],
    "best2015": [],
    "rep2016": [extracted("Gebyrer 2016", "Licensgebyret er 250 kr.", handling="aendring",
                          citat="Licensgebyret er hævet til 250 kr")],
    "rep2019": [extracted("Takster 2019", "Gebyr: licens 200 kr. og start 300 kr.", handling="bekraeftelse",
                          citat="Licens: kr. 200 og startgebyr")],
    "rep2024": [extracted("Licensgebyr", "Licensgebyret er 300 kr.", handling="aendring",
                          citat="Licensgebyret hæves fra 200 kr. til 300 kr"),
                extracted("Klubskifte", kategori="medlemskab", citat="Klubskifte kræver tre måneders karantæne")],
}
RULES = {"okonomi": [("Licensgebyr", "licensgebyr", [("rep2010#1", "indfoert"), ("rep2013#3", "bekraeftet"),
                                                     ("rep2013#1", "bekraeftet")]),
                     ("Licensgebyr 2024", "licensgebyr-2024", [("rep2024#1", "aendret")]),
                     ("Startgebyr", "startgebyr", [("rep2010#2", "indfoert"), ("rep2013#2", "aendret")])],
         "medlemskab": [("Klubskifte", "klubskifte", [("rep2024#2", "indfoert")])]}


class Corpus:
    """data/ and eval/ in tmp_path: six documents, their stored extractions, and rule files."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        self.data = tmp_path / "data"
        monkeypatch.setattr(scrape, "MANIFEST", self.data / "manifest.json")
        monkeypatch.setattr(analyze, "DECISIONS_DIR", self.data / "beslutninger")
        monkeypatch.setattr(analyze, "RULES_DIR", self.data / "regler")
        monkeypatch.setattr(evaluate, "EVAL_DIR", tmp_path / "eval")
        (tmp_path / "docs").mkdir()
        analyze.DECISIONS_DIR.mkdir(parents=True)
        analyze.RULES_DIR.mkdir(parents=True)
        self.docs = []
        for doc_id, text in TEXTS.items():
            path = tmp_path / "docs" / f"{doc_id}.htm"
            path.write_text(f"<p>{text}</p>")
            self.docs.append(Doc(doc_id, ORGANS.get(doc_id, "repraesentantskab"), doc_id,
                                 YEARS.get(doc_id, doc_id[3:]), str(path), None, f"sha-{doc_id}"))
        scrape.MANIFEST.write_text(json.dumps([asdict(d) for d in self.docs]))
        for doc in self.docs:
            self.extract(doc, STORED[doc.id], analyze.DECISIONS_DIR / f"{doc.id}.json", ids=True)
        self.rules(RULES)

    def doc(self, doc_id: str) -> Doc:
        return next(d for d in self.docs if d.id == doc_id)

    def words(self, doc_id: str) -> DocWords:
        return DocWords.of(TEXTS[doc_id])

    def extract(self, doc: Doc, decisions: list[dict], path, ids: bool = False, run: str | None = None) -> None:
        words = self.words(doc.id)
        located = [{**({"id": f"{doc.id}#{n}"} if ids else {}), **d, **quote_fields(d["citat"], words, None)}
                   for n, d in enumerate(decisions, start=1)]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"doc_id": doc.id, "sha256": doc.sha256, "version": analyze.EXTRACT_VERSION,
                                    "run": run, "model": "sonnet", "moededato": None,
                                    "next_number": len(decisions) + 1, "retired": [], "beslutninger": located}))

    def rules(self, categories: dict, directory=None) -> None:
        by_ref = {d.ref: d for d in analyze.load_decisions(self.docs)}
        for kategori, rules in categories.items():
            regler = [rule(titel, slug, *((by_ref[ref], effekt) for ref, effekt in versions))
                      for titel, slug, versions in rules]
            ((directory or analyze.RULES_DIR) / f"{kategori}.json").write_text(json.dumps(
                {"kategori": kategori, "version": analyze.CONSOLIDATE_VERSION, "input_hash": "x", "model": "opus",
                 "regler": regler, "udeladt": [], "ikke_tildelt": []}))

    def data_fingerprint(self) -> str:
        files = sorted(p for p in self.data.rglob("*") if p.is_file())
        return hashlib.sha256(b"".join(p.name.encode() + p.read_bytes() for p in files)).hexdigest()


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    return Corpus(tmp_path, monkeypatch)


def _selection(corpus, *doc_ids: str, rules: tuple[str, ...] = ("licensgebyr",)) -> None:
    evaluate.write_json(evaluate.selection_path(), {
        "as_of": "2026-01-15", "seed": 1, "rules": [{"slug": slug} for slug in rules],
        "documents": [{"id": doc_id} for doc_id in doc_ids]})


def _runs_log(corpus) -> list[dict]:
    return [json.loads(line) for line in evaluate.runs_log_path().read_text().splitlines()]


# ---------------------------------------------------------------- selection

def test_items_are_named_in_selection_order_and_exclude_a_pilot(corpus):
    _selection(corpus, "rep2024", "rep2010", "rep2013")
    selection = evaluate.load_selection()
    assert [d.id for d in evaluate.selected_docs(selection, named=["rep2013", "rep2024"])] == ["rep2024", "rep2013"]
    with pytest.raises(SystemExit, match="rep1999"):
        evaluate.selected_docs(selection, named=["rep1999"])
    with pytest.raises(SystemExit):
        evaluate.main(["extract", "--name", "x", "--pilot", "1", "--docs", "rep2024"])


# ---------------------------------------------------------------- extraction runs

def test_extraction_runs_use_the_pipeline_prompt_and_write_only_under_eval(corpus, fake_claude):
    _selection(corpus, "rep2024", "rep2010")
    before = corpus.data_fingerprint()
    evaluate.main(["extract", "--name", "stored", "--from-data"])
    stored = json.loads(evaluate.run_path("stored", "rep2024").read_text())
    assert [d["id"] for d in stored["beslutninger"]] == ["rep2024#1", "rep2024#2"]
    assert fake_claude.invocations("call") == 0
    with pytest.raises(SystemExit, match="--model and --max-cost"):  # without --from-data, every name calls Claude
        evaluate.main(["extract", "--name", "stored"])
    with pytest.raises(SystemExit, match="without calling Claude, so it takes no --model, --max-cost"):
        evaluate.main(["extract", "--name", "x", "--from-data", "--model", "claude-sonnet-5-5", "--max-cost", "5"])
    assert not evaluate.run_dir("x").exists()

    fake_claude.answer({"moededato": None, "beslutninger": [STORED["rep2024"][0]]})
    evaluate.main(["extract", "--name", "sonnet-1", "--model", "claude-sonnet-5-5", "--max-cost", "5",
                   "--docs", "rep2024"])
    assert fake_claude.option("--system-prompt") == [analyze.EXTRACT_SYSTEM]
    assert fake_claude.option("--json-schema") == [json.dumps(analyze.EXTRACT_SCHEMA)]
    run = json.loads(evaluate.run_path("sonnet-1", "rep2024").read_text())
    assert run["beslutninger"][0]["citat_fundet"] and run["beslutninger"][0]["citat_pos"] is not None
    assert run["provenance"]["prompt"] == analyze.prompt_hash(analyze.EXTRACT_SYSTEM, analyze.EXTRACT_SCHEMA)

    evaluate.main(["extract", "--name", "sonnet-1", "--model", "claude-sonnet-5-5", "--max-cost", "5"])
    assert fake_claude.invocations("call") == 2  # the pilot's document is not extracted again
    with pytest.raises(SystemExit, match="another --name"):
        evaluate.main(["extract", "--name", "sonnet-1", "--model", "claude-haiku-5-5", "--max-cost", "5"])
    assert corpus.data_fingerprint() == before
    assert [line["documents"] for line in _runs_log(corpus)] == [["rep2024"], ["rep2010"]]


@pytest.mark.parametrize("change", ["prompt", "effort", "file"])
def test_an_extraction_of_other_input_is_replaced_only_with_force(corpus, fake_claude, change):
    _selection(corpus, "rep2024")
    fake_claude.answer({"moededato": None, "beslutninger": []})
    command = ["extract", "--name", "r", "--model", "claude-sonnet-5-5", "--max-cost", "5"]
    evaluate.main(command)
    path = evaluate.run_path("r", "rep2024")
    stored = json.loads(path.read_text())
    if change == "prompt":
        stored["provenance"]["prompt"] = "0" * 12
    elif change == "file":
        stored["sha256"] = "older"
    path.write_text(json.dumps(stored))
    if change == "effort":
        command += ["--effort", "high"]
    with pytest.raises(SystemExit, match="--force"):
        evaluate.main(command)
    assert fake_claude.invocations("call") == 1
    evaluate.main([*command, "--force"])
    assert fake_claude.invocations("call") == 2


def test_a_run_copied_from_data_is_not_replaced_silently(corpus):
    _selection(corpus, "rep2024")
    command = ["extract", "--name", "migrated", "--from-data"]
    evaluate.main(command)
    source = analyze.DECISIONS_DIR / "rep2024.json"
    changed = json.loads(source.read_text())
    changed["beslutninger"][0]["tekst"] = "Licensgebyret er 350 kr."
    source.write_text(json.dumps(changed))
    with pytest.raises(SystemExit, match="rep2024.*--force"):
        evaluate.main(command)
    evaluate.main([*command, "--force"])
    copied = json.loads(evaluate.run_path("migrated", "rep2024").read_text())
    assert copied["run"] == "migrated" and copied["beslutninger"][0]["tekst"] == "Licensgebyret er 350 kr."


def test_pilot_and_cost_limit_are_respected(corpus, fake_claude):
    _selection(corpus, "rep2024", "rep2010", "rep2013")
    fake_claude.answer({"moededato": None, "beslutninger": []})
    evaluate.main(["extract", "--name", "pilot", "--model", "claude-sonnet-5-5", "--max-cost", "5", "--pilot", "2"])
    assert fake_claude.invocations("call") == 2
    assert not evaluate.run_path("pilot", "rep2013").exists()

    with pytest.raises(SystemExit, match="2 Claude calls failed or were skipped"):
        evaluate.main(["extract", "--name", "capped", "--model", "claude-sonnet-5-5", "--max-cost", "0.25"])
    assert fake_claude.invocations("call") == 3  # one worker under a small limit: the first call reached it
    line = _runs_log(corpus)[-1]
    assert line["steps"]["extract"]["skipped"] == 2 and line["documents"] == ["rep2024"]


def test_an_extraction_run_can_use_another_prompt_and_its_provenance_tells_them_apart(corpus, fake_claude):
    _selection(corpus, "rep2024")
    fake_claude.answer({"moededato": None, "beslutninger": []})
    command = ["extract", "--name", "sonnet-v3-1", "--model", "claude-sonnet-5-5", "--max-cost", "5"]
    evaluate.main([*command, "--prompt", "v3"])
    assert fake_claude.option("--system-prompt") == [analyze.EXTRACT_PROMPTS["v3"]]
    run = json.loads(evaluate.run_path("sonnet-v3-1", "rep2024").read_text())
    assert run["prompt"] == "v3"
    assert run["provenance"]["prompt"] == analyze.prompt_hash(analyze.EXTRACT_PROMPTS["v3"], analyze.EXTRACT_SCHEMA)
    assert run["provenance"]["prompt"] != analyze.prompt_hash(analyze.EXTRACT_PROMPTS["v2"], analyze.EXTRACT_SCHEMA)
    assert _runs_log(corpus)[-1]["prompt"] == "v3"

    evaluate.main([*command, "--prompt", "v3"])
    assert fake_claude.invocations("call") == 1  # current with v3
    with pytest.raises(SystemExit, match="--force"):
        evaluate.main([*command, "--prompt", "v2"])  # one name, one configuration


def test_a_small_cost_limit_runs_one_worker():
    parse = evaluate.parser().parse_args
    assert evaluate.workers_for(parse(["extract", "--name", "x", "--max-cost", "5"])) == 1
    assert evaluate.workers_for(parse(["extract", "--name", "x", "--max-cost", "50"])) == 4
    assert evaluate.workers_for(parse(["extract", "--name", "x", "--max-cost", "5", "--workers", "3"])) == 3


# ---------------------------------------------------------------- scoring decisions

def _probe(emne, udfald, start, length=4):
    return {"run": "stored", "index": 0, "emne": emne, "tekst": emne, "udfald": udfald,
            "span": [start, start + length]}


def _keyed(key_id, status, emne, pos, candidates=(), **changes):
    return {"id": key_id, "status": status, "candidates": list(candidates),
            **extracted(emne, citat="a b c d"), "citat_pos": pos, "uncertain_fields": [],
            "quotes": [], **changes}


def _run(emne, pos, **changes):
    return {**extracted(emne, citat="a b c d"), "citat_pos": pos, **changes}


KEY = {
    "runs": ["stored"],
    "decisions": [_keyed("K1", "certain", "Licensgebyr", 0, [1]),
                  _keyed("K2", "certain", "Startgebyr", 20, [2], uncertain_fields=["handling"]),
                  _keyed("M1", "uncertain", "Klubskifte", 60)],
    "candidates": [{"number": 1, "role": "decision", "members": [_probe("Licensgebyr", "vedtaget", 0)]},
                   {"number": 2, "role": "decision", "members": [_probe("Startgebyr", "vedtaget", 20)]},
                   {"number": 3, "role": "reject", "members": [_probe("Opgave", "vedtaget", 40)]}],
}


def test_a_run_is_scored_on_recall_precision_over_split_and_agreed_fields():
    run = [_run("Licensgebyr", 0), _run("Licensgebyr i år", 1, kategori="medlemskab"),
           _run("Startgebyr", 20, udfald="forkastet", handling="aendring"), _run("Opgave", 40),
           _run("Klubskifte", 60), _run("Noget helt andet", 80)]
    score = evaluate.score_document(KEY, run)
    assert (score.key_decisions, score.found, score.false, score.extra, score.split, score.ignored,
            score.unjudged) == (2, 2, 1, 1, 1, 1, 1)
    metrics = {name: metric(score) for name, metric in evaluate.METRICS.items()}
    assert metrics["recall"] == 1 and metrics["precision"] == pytest.approx(2 / 3)
    # The best match of K1 is right (the worse one has another kategori); K2's handling is not agreed, so not scored.
    assert metrics["over_split"] == 0.5 and metrics["field_kategori"] == 1 and metrics["field_udfald"] == 0.5
    assert metrics["field_handling"] == 1 and metrics["field_all"] == 1  # only K1 has all four agreed
    assert metrics["field_three"] == 0.5  # K2's udfald is wrong; its handling is left out anyway

    only_handling = evaluate.score_document(KEY, [_run("Startgebyr", 20, handling="aendring")])
    assert evaluate.METRICS["field_three"](only_handling) == 1  # K2's handling is not agreed: not held against it

    missed = evaluate.score_document(KEY, [_run("Startgebyr", 20)])
    assert evaluate.METRICS["recall"](missed) == 0.5 and evaluate.METRICS["precision"](missed) == 1


def test_the_bootstrap_is_paired_and_seeded():
    def scored(found):
        return DocScore(10, found, 0, 0, 0, 0, 0)

    better = [scored(f + 1) for f in range(10)]
    worse = [scored(f) for f in range(10)]  # one more found on every document, whatever the document
    paired = evaluate.bootstrap(better, worse, evaluate.METRICS["recall"], samples=300, seed=3)
    assert paired == evaluate.bootstrap(better, worse, evaluate.METRICS["recall"], samples=300, seed=3)
    assert paired["low"] == pytest.approx(0.1) and paired["high"] == pytest.approx(0.1) and paired["ahead"] == 1


def test_score_reports_pooled_and_per_document_figures_and_warns(corpus):
    evaluate.write_json(evaluate.run_path("a", "rep2024"), {"sha256": "sha-rep2024",
                                                           "beslutninger": [_run("Licensgebyr", 0)]})
    evaluate.write_json(evaluate.run_path("b", "rep2024"), {"sha256": "older", "beslutninger": []})
    evaluate.write_json(evaluate.key_dir("decisions") / "rep2024.json", {**KEY, "sha256": "sha-rep2024",
                                                                         "doc_id": "rep2024"})
    evaluate.write_json(evaluate.corrections_path(), [
        {"kind": "decision", "target": {"doc": "rep2024", "id": "K2"}, "change": {"status": "uncertain"},
         "reason": "Not a rule.", "evidence": [{"doc": "rep2024", "quote": "Startgebyr"}]}])
    evaluate.main(["score", "--run", "a", "--run", "b"])
    report = (evaluate.EVAL_DIR / "reports" / "decisions-a+b.md").read_text()
    assert "| a | 100.0% | 100.0% | 100.0% | 100.0% |" in report and "median (min–max)" in report  # K2 is out
    assert "decision doc rep2024, id K2 (K2)" in report
    assert "Not candidate runs: a, b" in report and "run b extracted another version of rep2024" in report
    assert "- stored: 2 of 3 kept" in report


def test_stability_is_the_share_of_two_runs_decisions_matched_one_to_one_pooled_over_documents():
    first = [[_run("Licensgebyr", 0), _run("Startgebyr", 20)], [_run("Klubskifte", 0)]]
    second = [[_run("Licensgebyr", 0)], [_run("Klubskifte", 0), _run("Noget helt andet", 40)]]
    assert evaluate.stability(zip(first, second)) == pytest.approx(2 * 2 / 6)
    assert evaluate.stability([([_run("Licensgebyr", 0)], [_run("Licensgebyr", 0), _run("Licensgebyr", 0)])]) == \
        pytest.approx(2 / 3)  # one to one: a decision split in two matches only once
    assert evaluate.stability([([], [])]) is None


def _configured_run(run: str, decisions: list[dict], model: str | None = None, prompt: str | None = None) -> None:
    """A run of rep2024 with the provenance of `model` and the extraction prompt named `prompt` (default: the
    pipeline's); the stored run has none."""
    pipeline = evaluate.pipeline_configuration()
    provenance = None if run == "stored" else {
        "model": model or pipeline.model, "cli": "2.1.294", "effort": "default",
        "prompt": analyze.prompt_hash(analyze.extract_prompt(prompt).system, analyze.EXTRACT_SCHEMA)}
    evaluate.write_json(evaluate.run_path(run, "rep2024"),
                        {"sha256": "sha-rep2024", "provenance": provenance, "beslutninger": decisions})


def test_todays_pipeline_is_opus_with_prompt_v3():
    assert evaluate.pipeline_configuration() == evaluate.Configuration(
        "claude-opus-5-5", analyze.prompt_hash(analyze.EXTRACT_PROMPTS["v3"], analyze.EXTRACT_SCHEMA), "default")
    assert evaluate.pipeline_configuration().label() == "claude-opus-5-5, prompt v3, effort default"


def test_score_reports_stability_and_the_gate_for_configurations_run_twice(corpus, monkeypatch):
    # The gate as it ran to choose v3: today's pipeline was then Sonnet with prompt v2.
    monkeypatch.setattr(evaluate.update, "EXTRACT_MODEL", "claude-sonnet-5-5")
    monkeypatch.setattr(analyze, "EXTRACT_VERSION", 2)
    monkeypatch.setattr(analyze, "EXTRACT_SYSTEM", analyze.EXTRACT_PROMPTS["v2"])
    evaluate.write_json(evaluate.key_dir("decisions") / "rep2024.json", {**KEY, "sha256": "sha-rep2024",
                                                                         "doc_id": "rep2024"})
    both = [_run("Licensgebyr", 0), _run("Startgebyr", 20)]
    _configured_run("stored", [_run("Licensgebyr", 0)])  # recall 50%, precision and three fields 100%, no over-split
    _configured_run("sonnet-1", [_run("Licensgebyr", 0)])
    _configured_run("sonnet-2", both)  # today's pipeline twice: one of three decisions not matched, 66.7%
    _configured_run("opus-v3-1", both, "claude-opus-5-5", "v3")
    _configured_run("opus-v3-2", both, "claude-opus-5-5", "v3")
    _configured_run("haiku-v3-1", [*both, _run("Opgave", 40)], "claude-haiku-5-5", "v3")  # a rejected candidate
    _configured_run("haiku-v3-2", [_run("Startgebyr", 20), _run("Klubskifte", 60)], "claude-haiku-5-5", "v3")
    split = [_run("Licensgebyr", 0), _run("Licensgebyr i år", 1), _run("Startgebyr", 20)]  # K1 twice
    _configured_run("sonnet-v3-1", split, prompt="v3")
    _configured_run("sonnet-v3-2", split, prompt="v3")
    runs = ["stored", "sonnet-1", "sonnet-2", "opus-v3-1", "opus-v3-2", "haiku-v3-1", "haiku-v3-2", "sonnet-v3-1",
            "sonnet-v3-2"]
    evaluate.main(["score", *(arg for run in runs for arg in ("--run", run)), "--baseline", "stored", "--report",
                   "gate"])

    report = (evaluate.EVAL_DIR / "reports" / "gate.md").read_text()
    assert "worse run is at least the run stored, less a margin, on recall" in report
    assert "| 1 | 0 | 0 | 0 | 0 | – |" in report  # the stored run is no other run's repeat
    assert report.count("| 2 | 0 | 0 | 0 | 0 | 100.0% |") == 2  # the opus runs found the same decisions
    # Today's pipeline found one and two of the key's two decisions: its recall spreads 50 points, so the floor drops
    # by that much below the stored run's 50%.
    assert "| needs | | ≥ 0.0% | ≥ 98.0% | ≥ 100.0% | ≤ 2.0% | ≥ 66.7% | |" in report
    assert "| spread of today's pipeline (points) | sonnet-1, sonnet-2 | 50.0 | 0.0 | 0.0 | 0.0 | | |" in report
    pipeline = "claude-sonnet-5-5, prompt v2, effort default (today's pipeline)"
    assert f"| {pipeline} | sonnet-1, sonnet-2 | 50.0% | 100.0% | 100.0% | 0.0% | 66.7% | yes |" in report
    assert ("| claude-opus-5-5, prompt v3, effort default | opus-v3-1, opus-v3-2 | 100.0% | 100.0% | 100.0% | 0.0% | "
            "100.0% | yes |") in report
    assert ("| claude-haiku-5-5, prompt v3, effort default | haiku-v3-1, haiku-v3-2 | 50.0% | 66.7% | 100.0% | 0.0% | "
            "40.0% | no: precision, stability |") in report
    assert ("| claude-sonnet-5-5, prompt v3, effort default | sonnet-v3-1, sonnet-v3-2 | 100.0% | 100.0% | 100.0% | "
            "50.0% | 100.0% | no: over_split |") in report

    # By default the gate measures against the run migrated, and only when it is scored.
    evaluate.main(["score", "--run", "stored", "--run", "opus-v3-1", "--run", "opus-v3-2", "--report", "no-baseline"])
    assert "## Gate" not in (evaluate.EVAL_DIR / "reports" / "no-baseline.md").read_text()
    _configured_run("migrated", [_run("Licensgebyr", 0), _run("Startgebyr", 20)])  # recall 100%
    evaluate.main(["score", "--run", "migrated", "--run", "opus-v3-1", "--run", "opus-v3-2", "--report", "unpaired"])
    report = (evaluate.EVAL_DIR / "reports" / "unpaired.md").read_text()
    assert "at least the run migrated, less a margin" in report and "| needs | | ≥ 100.0% |" in report
    assert "today's pipeline was not run twice here, so it is the slack alone" in report
    assert "were not scored, so no configuration passes" in report and "| no: stability |" in report
    # A baseline named on purpose that is not scored, or mistyped, stops the command instead of dropping the gate.
    for name, why in (("migrate", "--baseline migrate: also pass --run migrate"), ("Migrated", "Run name 'Migrated'")):
        with pytest.raises(SystemExit, match=why):
            evaluate.main(["score", "--run", "migrated", "--run", "opus-v3-1", "--baseline", name])


def test_a_worse_run_within_the_spread_of_todays_pipeline_passes_and_one_outside_it_fails():
    def run(found: int) -> DocScore:  # found of 100 key decisions, nothing false or split, every field right
        return DocScore(100, found, 0, 0, 0, 0, 0, {"three": found}, {"three": found})

    totals = {"today-1": run(90), "today-2": run(94), "within-1": run(87), "within-2": run(95), "outside-1": run(85),
              "outside-2": run(95)}
    within, outside = (evaluate.Configuration("claude-haiku-5-5", prompt, "default") for prompt in ("a", "b"))
    groups = {within: ["within-1", "within-2"], outside: ["outside-1", "outside-2"]}
    stable = {within: 0.95, outside: 0.95}
    # Today's pipeline ran at 90% and 94% recall: 4 points of noise below the baseline's 90% are allowed.
    needs = evaluate.gate_needs(totals["today-1"], 0.9, evaluate.spread([totals["today-1"], totals["today-2"]]))
    assert [r.failed for r in evaluate.gate(totals, groups, stable, needs)] == [(), ("recall",)]
    # Run once, today's pipeline shows no noise, and the slack alone (none on recall) fails both.
    needs = evaluate.gate_needs(totals["today-1"], 0.9, evaluate.spread([totals["today-1"]]))
    assert [r.failed for r in evaluate.gate(totals, groups, stable, needs)] == [("recall",)] * 2


# ---------------------------------------------------------------- scoring rules

def _event(event_id, doc, effect, value, quote, status="certain"):
    return {"id": event_id, "status": status, "doc": doc, "effect": effect, "value_after": value, "quote": quote}


EVENTS = [_event("E1", "rep2010", "indfoert", "150 kr.", "Licensgebyret er hævet til 150 kr. pr. løfter"),
          _event("E2", "rep2013", "bekraeftet", "200 kr.", "Licens: kr. 200 (uændret)"),
          _event("E3", "best2015", "bekraeftet", "200 kr.", "Kassereren sender mail til klubberne om licens"),
          _event("E4", "rep2024", "aendret", "300 kr.", "Licensgebyret hæves fra 200 kr. til 300 kr"),
          _event("E5", "rep2013", "bekraeftet", None, "Startgebyret hæves til 200 kr. pr. start", "uncertain")]


def _rule_key(corpus, events=EVENTS) -> dict:
    located = [{**e, "span": quote_span(quote_fields(e["quote"], corpus.words(e["doc"]), None)["citat_pos"],
                                        e["quote"])}
               for e in events]
    value = {"E1": "150 kr.", "E4": None}  # the key has no value for E4: still not "not in force"
    years = [{"year": y, "status": "certain", "state": "event", "event": event, "adopted": adopted,
              "value": value[adopted]} for y in range(2010, 2027)
             for event, adopted in [("E1", "E1") if y < 2013 else ("E2", "E1") if y < 2024 else ("E4", "E4")]]
    years += [{"year": 2009, "status": "certain", "state": "unknown", "event": None, "adopted": None, "value": None},
              {"year": 2008, "status": "uncertain", "state": None, "event": None, "adopted": None, "value": None}]
    return {"slug": "licensgebyr", "kategori": "okonomi", "titel": "Licensgebyr", "as_of": "2026-10-08",
            "events": located, "years": years}


def _score(corpus, rules) -> evaluate.RuleScore:
    by_ref = {d.ref: d for d in analyze.load_decisions(corpus.docs)}
    raw = [rule(slug, slug, *((by_ref[ref], effekt) for ref, effekt in versions), kategori="okonomi")
           for slug, versions in rules]
    return evaluate.score_rule(_rule_key(corpus), analyze.load_decisions(corpus.docs), raw,
                               {d.id: d for d in corpus.docs}, evaluate.Texts())


def test_rules_are_scored_on_events_fragmentation_and_the_rule_in_force_each_year(corpus):
    score = evaluate.score_rule(_rule_key(corpus), analyze.load_decisions(corpus.docs), analyze.load_rules(),
                                {d.id: d for d in corpus.docs}, evaluate.Texts())
    # E1 and E2 are in licensgebyr, E4 in licensgebyr-2024, E3 was never extracted, E5 is uncertain and not
    # counted; rep2013#3 is no key event. Unknown and uncertain years are not scored.
    assert (score.home, score.events, score.found, score.elsewhere, score.missing, score.extra) == \
        ("licensgebyr", 4, 2, 1, 1, 1)
    assert (score.effects, score.rules) == (2, 2)
    assert (score.years, score.same_event, score.same_content) == (17, 14, 14)
    assert score.disagreements[0] == "2024: key E4 (no value), pipeline rep2013#1"


@pytest.mark.parametrize("rules, expected", [
    # The first event sits alone in "a"; "b" holds most, shows nothing before 2013, and its 2013 confirmation was
    # adopted by nothing it holds, so only the event agrees in 2013-2023; it calls the 2024 change an introduction.
    ([("a", [("rep2010#1", "indfoert")]), ("b", [("rep2013#1", "bekraeftet"), ("rep2024#1", "indfoert")])],
     ("b", 2, 1, 2, 1, 17, 14, 3)),
    # "b" lacks the 2013 confirmation: it shows 2010's decision until 2024, the same content as the key's.
    ([("a", [("rep2013#1", "bekraeftet")]), ("b", [("rep2010#1", "indfoert"), ("rep2024#1", "indfoert")])],
     ("b", 2, 1, 2, 1, 17, 6, 17)),
])
def test_the_home_rule_holds_most_events_and_years_compare_event_and_content(corpus, rules, expected):
    score = _score(corpus, rules)
    assert (score.home, score.found, score.elsewhere, score.rules, score.effects, score.years, score.same_event,
            score.same_content) == expected


def _one_document(corpus, monkeypatch, doc_id: str, text: str, decisions: list[dict]) -> dict:
    """The document's text and stored decisions replaced; its decisions as map_events takes them."""
    monkeypatch.setitem(TEXTS, doc_id, text)
    Path(corpus.doc(doc_id).path).write_text(f"<p>{text}</p>")
    corpus.extract(corpus.doc(doc_id), decisions, analyze.DECISIONS_DIR / f"{doc_id}.json", ids=True)
    return {doc_id: [d for d in analyze.load_decisions(corpus.docs) if d.doc_id == doc_id]}


def _map(corpus, by_doc: dict, titel: str, keywords: list[str], events: list[dict]) -> dict[str, str]:
    key = {**_rule_key(corpus, events), "titel": titel, "keywords": keywords}
    return evaluate.map_events(key, by_doc, {d.id: d for d in corpus.docs}, evaluate.Texts())


BUDGET_LINE = "Årsafgift: kr. 1.000,- (Uændret) - Licens: kr. 200,- (Uændret). Startgebyrer: kr. 200,- (Ændret)."
FEES = [  # the line as prompt v3 extracts it: one decision per fee, in the line's order
    extracted("Årsafgift", "Klubbernes årsafgift til DSF fastholdes uændret på 1.000 kr.", handling="bekraeftelse",
              citat="Årsafgift: kr. 1.000,- (Uændret)"),
    extracted("Licensgebyr", "Licensgebyret fastholdes uændret på 200 kr.", handling="bekraeftelse",
              citat="Licens: kr. 200,- (Uændret)"),
    extracted("Startgebyr", "Startgebyret ændres til 200 kr.", handling="aendring",
              citat="Startgebyrer: kr. 200,- (Ændret)")]
# The real keys' keywords, which name the other fees too.
FEE_KEYS = {
    "licence": ("Licensgebyr", ["licensgebyr", "afgift", "koster", "kontingent", "gebyr", "årsafgift", "betaling",
                                "licens", "pris"], "200 kr. pr. løfter pr. år"),
    "club": ("Årsafgift", ["gebyrer", "afgift", "koster", "kontingent", "startgebyrer", "gebyr", "årsafgift",
                           "startgebyr", "års", "betaling", "licens", "pris"], "1.000 kr. pr. klub pr. år"),
    "start": ("Startgebyr ved stævner", ["andel", "arrangørklub", "afgift", "koster", "kontingent", "klubbernes",
                                         "gebyr", "årsafgift", "trekamp", "startgebyr", "fordeling", "betaling", "tre",
                                         "pris", "arrangør"], "200 kr. pr. start"),
}


@pytest.mark.parametrize("fee, expected", [("licence", "rep2013#2"), ("club", "rep2013#1"), ("start", "rep2013#3")])
def test_a_key_quote_spanning_one_decision_per_fee_maps_to_the_fee_of_its_rule(corpus, monkeypatch, fee, expected):
    # The key quotes the whole line for each fee's rule. Every fee's decision lies inside the quote, and the licence's
    # and start fee's values share no word trigram with their decisions: the matcher alone gives the line's first.
    by_doc = _one_document(corpus, monkeypatch, "rep2013", f"Repræsentantskabsmøde 2013. {BUDGET_LINE} Slut.", FEES)
    titel, keywords, value = FEE_KEYS[fee]
    assert _map(corpus, by_doc, titel, keywords, [_event("E1", "rep2013", "bekraeftet", value, BUDGET_LINE)]) == \
        {"E1": expected}


def test_a_decision_another_event_holds_is_not_taken(corpus, monkeypatch):
    # Two licence events, each covering the licence decision and one other: the matcher gives the first the
    # årsafgift (the first decision) and the second the licence, which the first may not take from it.
    by_doc = _one_document(corpus, monkeypatch, "rep2013", f"Repræsentantskabsmøde 2013. {BUDGET_LINE} Slut.", FEES)
    titel, keywords, value = FEE_KEYS["licence"]
    events = [_event("E1", "rep2013", "bekraeftet", value, "Årsafgift: kr. 1.000,- (Uændret) - Licens: kr. 200,-"),
              _event("E2", "rep2013", "bekraeftet", value, "Licens: kr. 200,- (Uændret). Startgebyrer: kr. 200,-")]
    assert _map(corpus, by_doc, titel, keywords, events) == {"E1": "rep2013#1", "E2": "rep2013#2"}


MEDALS = ("Startgebyret hæves til kr. 300. Den arrangerende klub bestiller og betaler selv medaljerne. "
          "Pokaler betales af forbundet.")


def test_containment_decides_which_decisions_compete_not_which_wins(corpus, monkeypatch):
    # As in rep2015: the start fee's quote runs a sentence too long and lies wholly inside the key's quote, the
    # medals' runs a sentence past it (12 of its 17 words inside). The medals are what the rule is about.
    by_doc = _one_document(corpus, monkeypatch, "rep2016", f"{MEDALS} Der udleveres medaljer til alle.", [
        extracted("Startgebyr", "Startgebyret hæves til 300 kr.", handling="aendring",
                  citat="Startgebyret hæves til kr. 300. Den arrangerende klub bestiller og betaler selv medaljerne."),
        extracted("Medaljer og pokaler",
                  "Den arrangerende klub bestiller og betaler selv medaljerne, mens forbundet betaler pokaler.",
                  "staevner", handling="aendring",
                  citat="Den arrangerende klub bestiller og betaler selv medaljerne. Pokaler betales af forbundet. Der "
                        "udleveres medaljer til alle.")])
    medal_event = _event("E1", "rep2016", "aendret", "medaljer købes af arrangøren; forbundet betaler pokaler",
                         MEDALS)
    assert _map(corpus, by_doc, "Bestilling og betaling af medaljer og pokaler",
                ["medaljer", "pokaler", "startgebyr", "gebyr", "betaling"], [medal_event]) == {"E1": "rep2016#2"}


def test_of_decisions_as_much_about_the_rule_the_one_most_inside_the_quote_wins(corpus, monkeypatch):
    # A licence decision extracted twice, once quoted from one word earlier: the one wholly inside the key's quote
    # wins over the first.
    by_doc = _one_document(corpus, monkeypatch, "rep2019", "Takster: Licens: kr. 200,- (Uændret). Slut.", [
        extracted("Licensgebyr", "Licensgebyret er uændret 200 kr.", handling="bekraeftelse",
                  citat="Takster: Licens: kr. 200,-"),
        extracted("Licensgebyr", "Licensgebyret er uændret 200 kr.", handling="bekraeftelse",
                  citat="Licens: kr. 200,- (Uændret)")])
    titel, keywords, value = FEE_KEYS["licence"]
    licence_event = _event("E1", "rep2019", "bekraeftet", value, "Licens: kr. 200,- (Uændret).")
    assert _map(corpus, by_doc, titel, keywords, [licence_event]) == {"E1": "rep2019#2"}


CORRECTIONS = [
    {"kind": "rule-event", "target": {"rule": "licensgebyr", "doc": "rep2010", "quote": "hævet til 150 kr"},
     "change": {"status": "uncertain"}, "reason": "Doubtful.", "evidence": [{"doc": "rep2013", "quote": "uændret"}]},
    {"kind": "rule-year", "target": {"rule": "licensgebyr", "years": [2010, 2011, 2012]},
     "change": {"status": "uncertain"}, "reason": "Not shown.", "evidence": []},
    {"kind": "rule-event", "target": {"rule": "licensgebyr", "doc": "rep2013", "quote": "Licens: kr. 200"},
     "change": {"effect_status": "uncertain"}, "reason": "Either.", "evidence": []},
    {"kind": "rule-event", "target": {"rule": "licensgebyr", "doc": "rep2099", "quote": "x"},
     "change": {"status": "uncertain"}, "reason": "Gone.", "evidence": []},
]


def test_corrections_apply_on_top_of_the_judges_when_scoring(corpus):
    corrected = evaluate.correct_rule_key(_rule_key(corpus), CORRECTIONS)
    assert [c["matched"] for c in corrected["corrections"]] == [["E1"], [2010, 2011, 2012], ["E2"], []]
    assert evaluate.correct_rule_key(corrected, CORRECTIONS) == corrected
    score = evaluate.score_rule(corrected, analyze.load_decisions(corpus.docs), analyze.load_rules(),
                                {d.id: d for d in corpus.docs}, evaluate.Texts())
    # E1 is no longer counted, nor 2010-2012; E2's effect is not compared.
    assert (score.events, score.found, score.effects, score.effect_events, score.years) == (3, 1, 0, 0, 14)
    with pytest.raises(SystemExit, match="correction 1"):
        evaluate.write_json(evaluate.corrections_path(), [{**CORRECTIONS[0], "change": {"titel": "x"}}])
        evaluate.load_corrections()


def test_soft_rules_and_corrections_are_reported_apart(corpus):
    evaluate.write_json(evaluate.key_dir("rules") / "licensgebyr.json", _rule_key(corpus))
    evaluate.write_json(evaluate.key_dir("rules") / "startgebyr.json", {
        **_rule_key(corpus, []), "slug": "startgebyr", "titel": "Startgebyr", "years": []})
    evaluate.write_json(evaluate.selection_path(), {"as_of": "2026-01-15", "rules": [
        {"slug": "licensgebyr", "soft": True, "soft_reason": "loosely scoped"}, {"slug": "startgebyr"}],
        "documents": []})
    evaluate.write_json(evaluate.corrections_path(), CORRECTIONS[3:])
    evaluate.main(["score-rules"])
    report = (evaluate.EVAL_DIR / "reports" / "rules-regler.md").read_text()
    assert "| licensgebyr (soft) | licensgebyr | 4 |" in report and "| **all** | | 0 | 0 |" in report
    assert "left out of the totals and the summary: licensgebyr: loosely scoped" in report
    assert "(NOTHING: the key has changed, check the correction)" in report


def test_score_rules_reads_another_rules_directory_and_writes_a_report(corpus):
    evaluate.write_json(evaluate.key_dir("rules") / "licensgebyr.json", _rule_key(corpus))
    before = corpus.data_fingerprint()
    other = corpus.root / "other-rules"
    other.mkdir()
    corpus.rules({"okonomi": [("Licensgebyr", "licensgebyr", [("rep2010#1", "indfoert"), ("rep2013#3", "bekraeftet"),
                                                              ("rep2013#1", "bekraeftet"), ("rep2024#1", "aendret")])]},
                 other)
    evaluate.main(["score-rules", "--rules-dir", str(other)])
    report = (evaluate.EVAL_DIR / "reports" / "rules-other-rules.md").read_text()
    assert "| licensgebyr | licensgebyr | 4 | 3 | 0 | 1 | 1 | 3 | 1 | 17 | 17 | 17 |" in report
    assert corpus.data_fingerprint() == before


# ---------------------------------------------------------------- guards and prompts

def test_nothing_is_written_outside_eval(corpus):
    with pytest.raises(ValueError, match="outside"):
        evaluate.write_json(analyze.DECISIONS_DIR / "x.json", {})


def test_word_positions_lead_back_to_the_text_past_page_markers():
    text = "[Side 1]\nLicens: kr. 200,-\n[Side 2]\nStart-gebyr 300 kr."
    spans = analyze.word_spans(text)
    assert [text[s:e] for s, e in spans] == ["Licens", "kr", "200", "Start", "gebyr", "300", "kr"]
    assert len(spans) == len(DocWords.of(text).words)


# ---------------------------------------------------------------- incremental consolidation gates

def test_candidate_recall_hides_each_decision_from_its_rule_and_picks_the_smallest_k(corpus):
    evaluate.main(["candidate-recall"])
    report = (evaluate.EVAL_DIR / "reports" / "candidate-recall.md").read_text()
    # Licensgebyr's three decisions and Startgebyr's two; the two one-decision rules are left out.
    assert "| **all** | 5 | 100.0% |" in report and "2 decisions are their rule's only one" in report
    assert "Chosen K: 3" in report and "| @3 relabelled |" in report
    # Relabelled, the decision ranks with another category's boost: here every rule is okonomi's, so it still finds
    # its rule among the three.
    hidden = evaluate.hide_one_ranks(analyze.load_rules(), analyze.load_decisions(corpus.docs))
    assert evaluate.relabel("okonomi") == "antidoping" and all(h.relabelled <= 3 for h in hidden)


def test_recall_at_k_and_the_chosen_k():
    hidden = [evaluate.Hidden(f"d#{i}", "r", "okonomi", rank) for i, rank in enumerate([1] * 97 + [4, 9, 12])]
    assert [evaluate.recall_at(hidden, k) for k in (3, 5, 10)] == [0.97, 0.98, 0.99]
    assert evaluate.choose_k(hidden) == 5
    assert evaluate.choose_k(hidden[:97] + [evaluate.Hidden("x#1", "r", "okonomi", 99)] * 3) is None

