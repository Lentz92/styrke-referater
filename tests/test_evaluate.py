"""evaluate.py: selection, candidates, judge agreement and scoring, on a tiny corpus in tmp_path; Claude calls go
to the fake `claude` from conftest.py."""

import hashlib
import json
from dataclasses import asdict, replace
from datetime import date

import pytest

import analyze
import evaluate
import scrape
from analyze import DocWords, quote_fields
from conftest import decision
from evaluate import DocChoice, DocScore, RuleChoice, Verdict
from matching import FormerSlug, SlugRegistry
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


def _decision(emne, kategori, udfald, handling, citat, niveau="staevneregel", tekst=None):
    return {"emne": emne, "kategori": kategori, "udfald": udfald, "forslagsstiller": None, "handling": handling,
            "niveau": niveau, "tekst": tekst or emne, "citat": citat, "side": None, "stemmer": None,
            "gaelder_fra": None, "gaelder_til": None}


STORED = {
    "rep2010": [_decision("Licensgebyr", "okonomi", "vedtaget", "ny", "Licensgebyret er hævet til 150 kr. pr. løfter",
                          tekst="Licensgebyret er 150 kr. pr. løfter."),
                _decision("Startgebyr", "okonomi", "vedtaget", "ny", "Startgebyret er 150 kr. pr. start")],
    "rep2013": [_decision("Licensgebyr", "okonomi", "vedtaget", "bekraeftelse", "Licens: kr. 200 (uændret)",
                          tekst="Licensgebyret er 200 kr."),
                _decision("Startgebyr", "okonomi", "vedtaget", "aendring", "Startgebyret hæves til 200 kr. pr. start"),
                _decision("Betaling af licens", "okonomi", "vedtaget", "bekraeftelse",
                          "Licensen betales ved første stævne i året")],
    "best2015": [],
    "rep2016": [_decision("Gebyrer 2016", "okonomi", "vedtaget", "aendring", "Licensgebyret er hævet til 250 kr",
                          tekst="Licensgebyret er 250 kr.")],
    "rep2019": [_decision("Takster 2019", "okonomi", "vedtaget", "bekraeftelse", "Licens: kr. 200 og startgebyr",
                          tekst="Gebyr: licens 200 kr. og start 300 kr.")],
    "rep2024": [_decision("Licensgebyr", "okonomi", "vedtaget", "aendring",
                          "Licensgebyret hæves fra 200 kr. til 300 kr", tekst="Licensgebyret er 300 kr."),
                _decision("Klubskifte", "medlemskab", "vedtaget", "ny", "Klubskifte kræver tre måneders karantæne")],
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
        monkeypatch.setattr(evaluate, "REQUIRED_DOCUMENTS", ("rep2024",))
        monkeypatch.setattr(evaluate, "TARGET_DOCUMENTS", 3)
        monkeypatch.setattr(evaluate, "MIN_VERSIONS", 2)
        monkeypatch.setattr(evaluate, "MIN_YEARS", 2)
        monkeypatch.setattr(evaluate, "MAX_KEYWORD_SHARE", 1.0)  # six documents: every word is common
        monkeypatch.setattr(evaluate, "today", lambda: date(2026, 10, 8))
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
            regler = [{"titel": titel, "slug": slug, "vigtig": True, "note": None,
                       "versioner": [{"ref": ref, "effekt": effekt, "tekst": None, "kort": None, "kort_regel": None,
                                      "dhash": analyze.decision_hash(by_ref[ref])} for ref, effekt in versions]}
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

def _rule(slug, kategori, versions, years, vigtig=True, parallel=False):
    return RuleChoice(slug, kategori, slug, versions, years, vigtig, parallel, frozenset())


def test_rules_are_taken_round_robin_over_categories_with_the_required_one_first(monkeypatch):
    monkeypatch.setattr(evaluate, "TARGET_RULES", 6)
    choices = [_rule(f"a{n}", "staevner", 10 - n, 9 - n) for n in range(5)] + [
        _rule("b1", "dommere", 6, 5), _rule("b2", "dommere", 4, 3), _rule("c1", "master", 5, 4),
        _rule("licensgebyr", "okonomi", 2, 2), _rule("kort", "okonomi", 3, 2),
        _rule("intern", "master", 9, 9, vigtig=False), _rule("parallel", "landshold", 24, 12, parallel=True)]
    chosen = evaluate.select_rules(sorted(choices, key=lambda r: r.rank))
    assert [r.slug for r, _ in chosen] == ["licensgebyr", "a0", "b1", "c1", "a1", "b2"]
    assert chosen[0][1].startswith("required") and "no. 1 of 5 recurring rules in staevner" in chosen[1][1]


def test_rules_are_ranked_by_real_versions_and_parallel_sub_rules_are_detected(corpus):
    decisions = {d.ref: d for d in analyze.load_decisions(corpus.docs)}
    raw = [{"slug": "forslag", "titel": "Forslag", "kategori": "okonomi", "vigtig": True,
            "versioner": [{"ref": "rep2010#1", "effekt": "indfoert"}, {"ref": "rep2013#1", "effekt": "forkastet"},
                          {"ref": "rep2024#1", "effekt": "foreslaaet"}]},
           {"slug": "per-mesterskab", "titel": "Per mesterskab", "kategori": "landshold", "vigtig": True,
            "versioner": [{"ref": "rep2013#1", "effekt": "aendret"}, {"ref": "rep2013#2", "effekt": "aendret"}]}]
    proposals, parallel = sorted(evaluate.rule_choices(raw, decisions), key=lambda r: r.slug)
    assert (proposals.versions, proposals.years, proposals.parallel) == (1, 1, False)
    assert parallel.parallel and not parallel.recurring


def test_documents_cover_strata_and_every_selected_rule(monkeypatch):
    monkeypatch.setattr(evaluate, "TARGET_DOCUMENTS", 9)
    monkeypatch.setattr(evaluate, "REQUIRED_DOCUMENTS", ("req",))
    infos = [DocChoice("req", "bestyrelse", "2020", "2018–22", 900, "large", ())]
    for organ in ("bestyrelse", "eliteudvalg", "dommerudvalg"):
        for era in ("2008–12", "2013–17", "2018–22", "2023–26"):
            for size in evaluate.SIZES:
                infos += [DocChoice(f"{organ}-{era}-{size}-{k}", organ, era[:4], era, 100, size, rules)
                          for k, rules in enumerate([(), ("licensgebyr",)])]
    infos += [DocChoice("rare", "andet", "2010", "2008–12", 100, "small", ()),
              DocChoice("only-big", "eliteudvalg", None, "unknown", 900, "large", ("sjaelden",)),
              DocChoice("only-small", "eliteudvalg", None, "unknown", 50, "small", ("sjaelden",))]
    chosen = evaluate.select_documents(infos, ["licensgebyr", "sjaelden"], seed=7)
    picked = [d for d, _ in chosen]
    assert picked[0].id == "req" and len(picked) == len({d.id for d in picked})
    assert {d.organ for d in picked} == {"bestyrelse", "eliteudvalg", "dommerudvalg", "andet"}
    assert {d.era for d in picked} >= {"2008–12", "2013–17", "2018–22", "2023–26"}
    assert {d.size for d in picked} == set(evaluate.SIZES)
    assert picked[-1].id == "only-small" and chosen[-1][1].startswith("covers sjaelden")
    assert evaluate.select_documents(infos, ["licensgebyr", "sjaelden"], seed=7) == chosen


def test_organ_quotas_give_every_organ_one_and_share_the_rest_by_size():
    quotas = evaluate.organ_quotas({"bestyrelse": 82, "eliteudvalg": 54, "andet": 2, "master": 10}, 10)
    assert quotas == {"andet": 1, "master": 1, "bestyrelse": 5, "eliteudvalg": 3}


def test_select_keeps_its_date_and_an_existing_selection(corpus, monkeypatch):
    evaluate.main(["select"])
    first = evaluate.selection_path().read_text()
    selection = json.loads(first)
    assert selection["as_of"] == "2026-10-08"
    assert [r["slug"] for r in selection["rules"]] == ["licensgebyr", "startgebyr"]
    assert selection["documents"][0]["id"] == "rep2024" and all(d["reason"] for d in selection["documents"])
    assert selection["summary"]["without_decisions"] == [d["id"] for d in selection["documents"]
                                                         if not d["decisions"]]
    monkeypatch.setattr(evaluate, "today", lambda: date(2026, 11, 1))  # a month later, same data
    evaluate.main(["select"])
    assert evaluate.selection_path().read_text() == first

    evaluate.write_json(evaluate.selection_path(), {**selection, "seed": 0})
    with pytest.raises(SystemExit, match="--force"):
        evaluate.main(["select"])
    evaluate.main(["select", "--force"])
    assert json.loads(evaluate.selection_path().read_text())["as_of"] == "2026-11-01"


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
    evaluate.main(["extract", "--name", "stored"])
    stored = json.loads(evaluate.run_path("stored", "rep2024").read_text())
    assert [d["id"] for d in stored["beslutninger"]] == ["rep2024#1", "rep2024#2"]
    assert fake_claude.invocations("call") == 0

    fake_claude.answer({"moededato": None, "beslutninger": [STORED["rep2024"][0]]})
    evaluate.main(["extract", "--name", "sonnet-1", "--model", "claude-sonnet-5-5", "--max-cost", "5",
                   "--docs", "rep2024"])
    argv = fake_claude.calls()[0]
    assert argv[argv.index("--system-prompt") + 1] == analyze.EXTRACT_SYSTEM
    assert argv[argv.index("--json-schema") + 1] == json.dumps(analyze.EXTRACT_SCHEMA)
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


def test_the_stored_run_is_not_replaced_silently(corpus):
    _selection(corpus, "rep2024")
    evaluate.main(["extract", "--name", "stored"])
    source = analyze.DECISIONS_DIR / "rep2024.json"
    changed = json.loads(source.read_text())
    changed["beslutninger"][0]["tekst"] = "Licensgebyret er 350 kr."
    source.write_text(json.dumps(changed))
    with pytest.raises(SystemExit, match="rep2024.*--force"):
        evaluate.main(["extract", "--name", "stored"])
    evaluate.main(["extract", "--name", "stored", "--force"])
    copied = json.loads(evaluate.run_path("stored", "rep2024").read_text())
    assert copied["beslutninger"][0]["tekst"] == "Licensgebyret er 350 kr."


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


def test_a_small_cost_limit_runs_one_worker():
    parse = evaluate.parser().parse_args
    assert evaluate.workers_for(parse(["key-rules", "--max-cost", "5"])) == 1
    assert evaluate.workers_for(parse(["key-rules", "--max-cost", "50"])) == 4
    assert evaluate.workers_for(parse(["key-rules", "--max-cost", "5", "--workers", "3"])) == 3


# ---------------------------------------------------------------- candidates and agreement

def _located(d: dict, doc_id: str = "rep2024", citat: str | None = None, **changes) -> dict:
    citat = citat or d["citat"]
    return {**d, **changes, "citat": citat, **quote_fields(citat, DocWords.of(TEXTS[doc_id]), None)}


def test_candidates_cluster_one_decision_per_run_in_reading_order():
    licens, klubskifte = _located(STORED["rep2024"][0]), _located(STORED["rep2024"][1])
    reworded = _located(licens, citat="hæves fra 200 kr. til 300 kr", tekst="Licensen koster nu 300 kr.")
    # b's decision also matches a's, but c split the decision in two, and b's is the same as c's second.
    clusters = evaluate.candidate_clusters({"a": [klubskifte, licens], "b": [reworded], "c": [licens, reworded]})
    assert [[(run, i) for run, i, _ in c.members] for c in clusters] == [[("a", 1), ("c", 0)], [("b", 0), ("c", 1)],
                                                                         [("a", 0)]]


def test_each_judge_sees_the_candidates_in_another_order_and_wording(corpus):
    doc = corpus.doc("rep2024")
    licens = STORED["rep2024"][0]
    corpus.extract(doc, STORED["rep2024"], evaluate.run_path("a", "rep2024"))
    corpus.extract(doc, [{**licens, "tekst": "Licensen koster nu 300 kr."}, STORED["rep2024"][1],
                         _decision("Møde", "organisation", "vedtaget", "ny", "Repræsentantskabsmøde 2024")],
                   evaluate.run_path("b", "rep2024"))
    task = evaluate.decisions_task(doc, ["a", "b"], evaluate.Texts())

    def shown(judge: int) -> list[dict]:
        block = task.prompt(judge).split("<candidates>\n")[1].split("\n</candidates>")[0]
        return [json.loads(line) for line in block.splitlines()]

    assert [c["candidate"] for c in shown(1)] == [1, 2, 3] and [c["candidate"] for c in shown(2)] == [2, 3, 1]
    assert shown(1)[1]["tekst"] == "Licensgebyret er 300 kr." and shown(2)[0]["tekst"] == "Licensen koster nu 300 kr."
    assert "run" not in task.prompt(1) and task.call(3).system == evaluate.DECISIONS_JUDGE_SYSTEM


def _kept(citat="Licensgebyret hæves fra 200 kr. til 300 kr", **changes):
    raw = {**_decision("Licensgebyr", "okonomi", "vedtaget", "aendring", citat), **changes}
    return Verdict("keep", decision=evaluate.judged_decision(raw, DocWords.of(TEXTS["rep2024"])))


def test_whether_a_decision_exists_is_judged_apart_from_its_fields():
    keep, reject = _kept(), Verdict("reject", reason="Ikke en regel")
    assert evaluate.resolve_candidate([reject, Verdict("reject")]).kind == "reject"
    assert evaluate.resolve_candidate([Verdict("duplicate", 2), Verdict("duplicate", 3)]).status == "open"
    assert evaluate.resolve_candidate([keep, Verdict("none")]).status == "open"
    assert evaluate.resolve_candidate([keep, reject, Verdict("reject")]).kind == "reject"
    assert evaluate.resolve_candidate([keep, reject, Verdict("duplicate", 1)]).status == "uncertain"

    other = _kept(handling="bekraeftelse", citat="Licensgebyret hæves fra 200 kr")
    decided = evaluate.resolve_candidate([keep, other])
    assert (decided.kind, decided.fields.uncertain, decided.fields.open) == ("keep", ("handling",), True)
    assert decided.quotes == ({"citat": other.decision["citat"], "citat_pos": other.decision["citat_pos"]},)
    settled = evaluate.resolve_candidate([keep, other, _kept(handling="bekraeftelse")])
    assert (settled.decision["handling"], settled.fields.uncertain) == ("bekraeftelse", ())
    split = evaluate.resolve_candidate([keep, other, _kept(handling="ophaevelse")])
    assert (split.kind, split.fields.uncertain, split.fields.open) == ("keep", ("handling",), False)


def test_added_decisions_need_two_judges_and_never_repeat_a_kept_one():
    karantaene, forslag = "Klubskifte kræver tre måneders karantæne", "Forslag fra bestyrelsen"

    def added(citat, **changes):
        return evaluate.judged_decision({**_decision("Klubskifte", "medlemskab", "vedtaget", "ny", citat),
                                         **changes}, DocWords.of(TEXTS["rep2024"]))

    both = evaluate.resolve_missing([[added(karantaene)], [added(karantaene, tekst="Karantæne")]])
    assert [(a.status, a.judges) for a in both] == [("certain", (1, 2))]
    assert [a.status for a in evaluate.resolve_missing([[added(karantaene)], []])] == ["open"]
    three = evaluate.resolve_missing([[added(karantaene)], [added(forslag, kategori="organisation")],
                                      [added(karantaene, niveau="vedtaegt")]])
    assert [(a.status, a.fields.uncertain) for a in three] == [("certain", ("niveau",)),
                                                              ("uncertain", evaluate.CODED_FIELDS)]
    kept = [evaluate.decision_candidate(added(karantaene))]
    assert evaluate.resolve_missing([[added(karantaene)], [added(karantaene)]], kept) == []


def _answer(candidates: list[dict], missing: list[dict] | None = None) -> dict:
    blank = {name: None for name in evaluate.JUDGED_FIELDS}
    return {"candidates": [{**blank, "duplicate_of": None, "reason": None, **c} for c in candidates],
            "missing": missing or []}


def _two_runs(corpus):
    doc = corpus.doc("rep2024")
    corpus.extract(doc, STORED["rep2024"], evaluate.run_path("stored", "rep2024"))
    extra = _decision("Møde", "organisation", "vedtaget", "ny", "Repræsentantskabsmøde 2024")
    corpus.extract(doc, [extra, STORED["rep2024"][0]], evaluate.run_path("other", "rep2024"))


def test_key_decisions_asks_a_blind_third_judge_where_two_disagree(corpus, fake_claude):
    _selection(corpus, "rep2024")
    _two_runs(corpus)
    licens = {**STORED["rep2024"][0], "verdict": "keep"}
    klub = {**STORED["rep2024"][1], "verdict": "keep"}
    first = _answer([{"candidate": 1, "verdict": "reject", "reason": "Intet"}, {"candidate": 2, **licens},
                     {"candidate": 3, **klub}])
    second = _answer([{"candidate": 1, "verdict": "reject"}, {"candidate": 2, **licens},
                      {"candidate": 3, **klub, "niveau": "bestyrelsesbeslutning"}],
                     [_decision("Forslag", "organisation", "vedtaget", "ny", "Forslag fra bestyrelsen")])
    third = _answer([{"candidate": 1, "verdict": "reject"}, {"candidate": 2, **licens},
                     {"candidate": 3, **klub, "niveau": "vedtaegt"}])
    fake_claude.answers(first, second, third)
    before = corpus.data_fingerprint()

    evaluate.main(["key-decisions", "--runs", "stored", "other", "--max-cost", "5"])
    calls = fake_claude.calls()
    assert len(calls) == 3
    assert all(c[c.index("--effort") + 1] == "max" and c[c.index("--model") + 1] == "claude-opus-5-5" for c in calls)
    assert {c[c.index("--system-prompt") + 1] for c in calls} == {evaluate.DECISIONS_JUDGE_SYSTEM}
    key = json.loads((evaluate.key_dir("decisions") / "rep2024.json").read_text())
    assert [(d["id"], d["status"], d["niveau"], d["uncertain_fields"]) for d in key["decisions"]] == \
        [("M1", "uncertain", "staevneregel", ["kategori", "udfald", "handling", "niveau"]),
         ("K2", "certain", "staevneregel", []), ("K3", "certain", "staevneregel", ["niveau"])]
    assert [(c["number"], c["role"], [m["run"] for m in c["members"]]) for c in key["candidates"]] == \
        [(1, "reject", ["other"]), (2, "decision", ["stored", "other"]), (3, "decision", ["stored"])]
    assert evaluate.solo_candidates([key]) == {"other": (0, 1), "stored": (1, 1)}

    evaluate.main(["key-decisions", "--runs", "stored", "other", "--max-cost", "5"])
    assert fake_claude.invocations("call") == 3  # every answer is stored
    assert corpus.data_fingerprint() == before


def test_judge_calls_stop_at_the_cost_limit(corpus, fake_claude):
    _selection(corpus, "rep2024")
    _two_runs(corpus)
    fake_claude.answer(_answer([]))
    with pytest.raises(SystemExit, match="skipped"):
        evaluate.main(["key-decisions", "--runs", "stored", "other", "--max-cost", "0.25"])
    assert fake_claude.invocations("call") == 1


# ---------------------------------------------------------------- rules key

def _rule_task(corpus, as_of=date(2026, 10, 8)) -> evaluate.RuleTask:
    decisions = analyze.load_decisions(corpus.docs)
    raw = next(r for r in analyze.load_rules() if r["slug"] == "licensgebyr")
    index = evaluate.KeywordIndex(corpus.docs, evaluate.Texts())
    return evaluate.rule_task({"slug": "licensgebyr"}, raw, decisions, {d.id: d for d in corpus.docs}, index,
                              evaluate.load_synonyms(), as_of)


def _pid(task, doc_id: str) -> str:
    return next(p.id for p in task.passages if p.doc.id == doc_id)


def test_rule_passages_take_related_decisions_and_trim_keyword_windows(corpus):
    task = _rule_task(corpus)
    decisions = analyze.load_decisions(corpus.docs)
    weights = evaluate.rule_keywords("Licensgebyr", task.emner, evaluate.load_synonyms(),
                                     evaluate.KeywordIndex(corpus.docs, evaluate.Texts()),
                                     evaluate.decision_vocabulary(decisions))
    assert weights[("licens",)] == 1.0 and weights[("årsafgift",)] == evaluate.SYNONYM_WEIGHT
    sources = {(p.doc.id, p.source) for p in task.passages}
    assert {("rep2010", "rule"), ("rep2013", "rule"), ("rep2019", "related"), ("rep2024", "related"),
            ("best2015", "keyword"), ("rep2016", "keyword")} <= sources
    # The keyword window around "Licens: kr. 250" overlaps the decision's window: trimmed, not dropped, and then
    # joined to it, as the two touch.
    assert [(p.start, p.end, p.source) for p in task.passages if p.doc.id == "rep2016"] == \
        [(0, 231, "related"), (261, 377, "keyword")]
    assert task.unread_amounts == ()
    assert "Today: 2026-10-08" in task.prompt


def test_amounts_the_word_budget_leaves_out_are_reported(corpus, monkeypatch):
    monkeypatch.setattr(evaluate, "PASSAGE_BUDGET", 0)
    task = _rule_task(corpus)
    assert {p.source for p in task.passages} == {"rule"}
    unread = task.unread_amounts
    assert {hit["doc"] for hit in unread} == {"rep2016", "rep2019", "rep2024"}
    assert any("Licens: kr. 275" in hit["text"] for hit in unread)


def test_keyword_passages_with_an_amount_come_first(corpus, monkeypatch):
    decided = sum(p.words for p in _rule_task(corpus).passages if p.source != "keyword")
    monkeypatch.setattr(evaluate, "PASSAGE_BUDGET", decided + 120)  # room for one of the two amounts in rep2016
    keyword = [p.doc.id for p in _rule_task(corpus).passages if p.source == "keyword"]
    assert keyword == ["rep2016"]  # not best2015, which mentions licens without an amount and is older


@pytest.mark.parametrize("gap, passages", [(0, 2), (200, 1)])
def test_decision_windows_a_few_words_apart_join(corpus, monkeypatch, gap, passages):
    monkeypatch.setattr(evaluate, "MERGE_GAP", gap)
    related = [decision(ref=f"rep2016#{n}", doc_id="rep2016", citat=citat, side=None)
               for n, citat in enumerate(["Licens: kr. 250 (uændret) i budgettet", "Licens: kr. 275 i næste budget"])]
    index = evaluate.KeywordIndex(corpus.docs, evaluate.Texts())
    gathered = evaluate.gather_passages([], related, {}, [], index, {d.id: d for d in corpus.docs})
    assert [p.source for p in gathered.passages] == ["related"] * passages


def test_a_passage_is_extended_to_its_proposal_heading_and_outcome(tmp_path):
    lines = ["Forslag 3 fra bestyrelsen", "Om licens", FILLER[:400], "Licensgebyret hæves til 400 kr.", FILLER[:500],
             "Forslag 3 blev vedtaget enstemmigt", "Forslag 4 fra Hvidovre", FILLER]
    path = tmp_path / "rep2017.htm"
    path.write_text("<p>" + "\n".join(lines) + "</p>")
    doc = Doc("rep2017", "repraesentantskab", "rep2017", "2017", str(path), None, "sha")
    texts = evaluate.Texts()
    words = texts.words(doc).words
    quote = analyze.locate_quote("Licensgebyret hæves til 400 kr", texts.words(doc))
    outcome = words.index("enstemmigt") + 1

    def extended(start: int, end: int) -> str:
        p = evaluate._extended(evaluate.Passage("", doc, None, start, end, "related", 0.0), texts)
        return texts.excerpt(doc, p.start, p.end)

    # From the middle of the proposal to its outcome: back to the heading. From the heading: on to the outcome.
    assert extended(quote - 8, outcome).startswith("Forslag 3 fra bestyrelsen")
    assert extended(0, quote + 14).endswith("Forslag 3 blev vedtaget enstemmigt")
    assert extended(0, outcome) == extended(0, outcome + 2)[:len(extended(0, outcome))]  # whole already
    assert evaluate._extended(evaluate.Passage("", doc, None, 0, outcome, "rule", 0.0), texts).end == outcome
    assert "Hvidovre" not in extended(0, quote + 14)


def _two_judges(task, first: list[tuple], second: list[tuple]):
    return evaluate.RuleTimeline.of(task, [_timeline(first, {}), _timeline(second, {})])


def test_events_cited_from_two_passages_are_one_decision(corpus):
    task = _rule_task(corpus)
    first, second = [p.id for p in task.passages if p.doc.id == "rep2016"]
    proposal = (first, "aendret", "Licensgebyret er hævet til 250 kr", "250 kr.")
    budget = (second, "aendret", "Licens: kr. 275 i næste budget", "250,00 kr. fra 1.1.2017")
    joined = _two_judges(task, [proposal], [budget])
    assert len(joined.clusters) == 1 and joined.events[0].status == "certain"
    other_amount = (second, "aendret", "Licens: kr. 275 i næste budget", "275 kr.")
    assert len(_two_judges(task, [proposal], [other_amount]).clusters) == 2
    one_decision = replace(task, extracted={"rep2016": [(0, 400)]})  # one extracted decision quotes both
    assert len(_two_judges(one_decision, [proposal], [other_amount]).clusters) == 1
    # Without amounts, two outcomes in one document stay two: they may be two proposals.
    rejected = [(first, "forkastet", "Licensgebyret er hævet til 250 kr", None)]
    other_rejection = [(second, "forkastet", "Licens: kr. 275 i næste budget", None)]
    assert len(_two_judges(task, rejected, other_rejection).clusters) == 2


def test_windows_merge_when_they_overlap_or_nearly_touch():
    assert evaluate._merge([(30, 40), (0, 10), (5, 12)]) == [(0, 12), (30, 40)]
    assert evaluate._merge([(30, 40), (0, 10)], gap=20) == [(0, 40)]
    assert evaluate._subtract((0, 100), [(20, 30), (90, 120)]) == [(0, 20), (30, 90)]


def _timeline(events: list[tuple], years: dict[int, int | str]) -> dict:
    return {"events": [{"passage": p, "effect": e, "quote": q, "value_after": v} for p, e, q, v in events],
            "years": [{"year": y, "state": "event" if isinstance(n, int) else n,
                       "event": n if isinstance(n, int) else None} for y, n in years.items()]}


def test_rule_timelines_agree_on_effect_value_and_year(corpus):
    task = _rule_task(corpus)
    e1 = (_pid(task, "rep2010"), "indfoert", "Licensgebyret er hævet til 150 kr. pr. løfter", "150 kr.")
    e2 = (_pid(task, "rep2013"), "aendret", "Licens: kr. 200 (uændret)", "200 kr.")
    years = {2010: 1, 2011: "unknown", 2012: "unknown"} | {y: 2 for y in range(2013, 2027)}
    first = _timeline([e1, e2], years)
    other_value = _timeline([e1, (*e2[:3], "250 kr.")], years)
    assert evaluate.RuleTimeline.of(task, [first, other_value]).open  # same event, other amount

    key = evaluate.rules_key(task, [first, other_value, first])
    assert [(e["id"], e["status"], e["doc"], e["value_after"]) for e in key["events"]] == \
        [("E1", "certain", "rep2010", "150 kr."), ("E2", "certain", "rep2013", "200 kr.")]
    states = {y["year"]: (y["status"], y["state"], y["event"]) for y in key["years"]}
    assert states[2010] == ("certain", "event", "E1") and states[2011] == ("certain", "unknown", None)
    assert states[2013] == ("certain", "event", "E2") and key["as_of"] == "2026-10-08"

    split = evaluate.rules_key(task, [first, other_value, _timeline([e1, (*e2[:3], "275 kr.")], years)])
    assert {y["year"]: y["status"] for y in split["years"]}[2013] == "uncertain"  # its event is uncertain


def test_years_agree_on_the_event_that_set_the_content(corpus):
    task = _rule_task(corpus)
    e1 = (_pid(task, "rep2010"), "indfoert", "Licensgebyret er hævet til 150 kr. pr. løfter", "150 kr. fra 1.1.2011")
    confirmed = (_pid(task, "rep2013"), "bekraeftet", "Licens: kr. 200 (uændret)", "150 kr.")
    with_confirmation = _timeline([e1, confirmed], {2010: 1, 2011: 1, 2012: 1, 2013: 2})
    without = _timeline([(*e1[:3], "150 kr. fra 1. januar 2011")], {2010: 1, 2011: 1, 2012: 1, 2013: 1})
    key = evaluate.rules_key(task, [with_confirmation, without])
    assert {y["year"]: (y["status"], y["adopted"], y["event"]) for y in key["years"]}[2013] == \
        ("certain", "E1", "E2")
    assert [(e["id"], e["status"]) for e in key["events"]] == [("E1", "certain"), ("E2", "uncertain")]


def test_an_event_quoted_from_outside_its_passage_is_void(corpus):
    task = _rule_task(corpus)
    elsewhere = (_pid(task, "rep2010"), "aendret", "Licens: kr. 200 (uændret)", "200 kr.")
    later = (_pid(task, "rep2016"), "aendret", "Licens: kr. 275 i næste budget", "275 kr.")  # same document
    inside = (_pid(task, "rep2016"), "aendret", "Licensgebyret er hævet til 250 kr", "250 kr.")
    judged = evaluate.events_of(_timeline([elsewhere, later, inside], {}), {p.id: p for p in task.passages},
                                task.texts)
    assert judged[:2] == [None, None] and judged[2].pos == 2
    timeline = evaluate.RuleTimeline.of(task, [_timeline([elsewhere], {2013: 1})] * 2)
    assert timeline.years[2013].status == "open"


def test_key_rules_judges_only_the_named_rules_and_reuses_their_answers(corpus, fake_claude):
    _selection(corpus, "rep2024", rules=("licensgebyr", "startgebyr"))
    fake_claude.answer(_timeline([("P1", "indfoert", "Startgebyret er 150 kr. pr. start", "150 kr.")],
                                 {year: 1 for year in range(2010, 2027)}))
    evaluate.main(["key-rules", "--max-cost", "5", "--rules", "startgebyr", "--judge-effort", "high"])
    assert fake_claude.invocations("call") == 2
    assert all(c[c.index("--effort") + 1] == "high" for c in fake_claude.calls())
    assert [p.name for p in evaluate.key_dir("rules").iterdir()] == ["startgebyr.json"]
    key = json.loads((evaluate.key_dir("rules") / "startgebyr.json").read_text())
    assert key["events"][0]["status"] == "certain" and key["years"][1]["event"] == "E1"
    assert key["as_of"] == "2026-01-15"  # the selection's date, whatever day the judges are asked

    evaluate.main(["key-rules", "--max-cost", "5", "--pilot", "2", "--judge-effort", "high"])
    assert fake_claude.invocations("call") == 4  # startgebyr's answers are reused; licensgebyr is new


def test_rederiving_uses_the_judged_passages_and_never_calls(corpus, fake_claude, monkeypatch):
    _selection(corpus, "rep2024")
    fake_claude.answer(_timeline([("P1", "indfoert", "Licensgebyret er hævet til 150 kr. pr. løfter", "150 kr.")],
                                 {year: 1 for year in range(2010, 2027)}))
    evaluate.main(["key-rules", "--max-cost", "5"])
    path = evaluate.key_dir("rules") / "licensgebyr.json"
    judged = json.loads(path.read_text())
    monkeypatch.setattr(evaluate, "PASSAGE_BUDGET", 0)  # the gathering has changed since
    with pytest.raises(SystemExit, match="--rejudge"):
        evaluate.main(["key-rules", "--max-cost", "5"])
    evaluate.write_json(evaluate.corrections_path(), [
        {"kind": "rule-year", "target": {"rule": "licensgebyr", "years": [2010]}, "change": {"status": "uncertain"},
         "reason": "Test.", "evidence": []}])
    evaluate.main(["key-rules", "--max-cost", "0", "--rederive"])
    assert fake_claude.invocations("call") == 2
    rederived = json.loads(path.read_text())
    assert rederived["passages"] == judged["passages"] and rederived["years"][0]["status"] == "uncertain"
    (evaluate.judge_path("rules", "licensgebyr", 2)).unlink()
    with pytest.raises(SystemExit, match="licensgebyr judge 2"):
        evaluate.main(["key-rules", "--max-cost", "5", "--rederive"])
    assert fake_claude.invocations("call") == 2


def test_some_rules_can_be_judged_again_while_the_others_keep_their_answers(corpus, fake_claude, monkeypatch):
    _selection(corpus, "rep2024", rules=("licensgebyr", "startgebyr"))
    fake_claude.answer(_timeline([], {}))
    evaluate.main(["key-rules", "--max-cost", "5"])
    kept = (evaluate.key_dir("rules") / "startgebyr.json").read_text()
    monkeypatch.setattr(evaluate, "PASSAGE_BUDGET", 0)  # the gathering changes for both rules
    evaluate.main(["key-rules", "--max-cost", "5", "--rejudge", "--rules", "licensgebyr"])
    assert fake_claude.invocations("call") == 6
    evaluate.main(["key-rules", "--max-cost", "0", "--rederive"])  # each on the passages it was judged on
    assert fake_claude.invocations("call") == 6
    assert (evaluate.key_dir("rules") / "startgebyr.json").read_text() == kept


@pytest.mark.parametrize("change", ["effort", "prompt", "model"])
def test_answers_for_other_input_are_asked_again_only_with_rejudge(corpus, fake_claude, monkeypatch, change):
    _selection(corpus, "rep2024")
    fake_claude.answer(_timeline([], {}))
    evaluate.main(["key-rules", "--max-cost", "5"])
    again = ["key-rules", "--max-cost", "5"]
    if change == "effort":
        again += ["--judge-effort", "high"]
    elif change == "model":
        again += ["--judge-model", "claude-sonnet-5-5"]
    else:
        monkeypatch.setattr(evaluate, "RULES_JUDGE_SYSTEM", evaluate.RULES_JUDGE_SYSTEM + " Be brief.")
    with pytest.raises(SystemExit, match="licensgebyr judge 1, licensgebyr judge 2.*--rejudge"):
        evaluate.main(again)
    assert fake_claude.invocations("call") == 2
    evaluate.main([*again, "--rejudge"])
    assert fake_claude.invocations("call") == 4


# ---------------------------------------------------------------- scoring decisions

def _probe(emne, udfald, start, length=4):
    return {"run": "stored", "index": 0, "emne": emne, "tekst": emne, "udfald": udfald,
            "span": [start, start + length]}


def _keyed(key_id, status, emne, pos, candidates=(), **changes):
    return {"id": key_id, "status": status, "candidates": list(candidates),
            **_decision(emne, "okonomi", "vedtaget", "ny", "a b c d"), "citat_pos": pos, "uncertain_fields": [],
            "quotes": [], **changes}


def _run(emne, pos, **changes):
    return {**_decision(emne, "okonomi", "vedtaget", "ny", "a b c d"), "citat_pos": pos, **changes}


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
    assert _runs_log(corpus)[-1]["metrics"]["a"]["recall_doc_avg"] == 1


# ---------------------------------------------------------------- scoring rules

def _event(event_id, doc, effect, value, quote, status="certain"):
    return {"id": event_id, "status": status, "doc": doc, "effect": effect, "value_after": value, "quote": quote}


EVENTS = [_event("E1", "rep2010", "indfoert", "150 kr.", "Licensgebyret er hævet til 150 kr. pr. løfter"),
          _event("E2", "rep2013", "bekraeftet", "200 kr.", "Licens: kr. 200 (uændret)"),
          _event("E3", "best2015", "bekraeftet", "200 kr.", "Kassereren sender mail til klubberne om licens"),
          _event("E4", "rep2024", "aendret", "300 kr.", "Licensgebyret hæves fra 200 kr. til 300 kr"),
          _event("E5", "rep2013", "bekraeftet", None, "Startgebyret hæves til 200 kr. pr. start", "uncertain")]


def _rule_key(corpus, events=EVENTS) -> dict:
    located = [{**e, "span": evaluate._span({"citat": e["quote"],
                                             **quote_fields(e["quote"], corpus.words(e["doc"]), None)})}
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
    raw = [{"titel": slug, "slug": slug, "kategori": "okonomi", "vigtig": True, "note": None,
            "versioner": [{"ref": ref, "effekt": effekt, "tekst": None, "kort": None, "kort_regel": None,
                           "dhash": analyze.decision_hash(by_ref[ref])} for ref, effekt in versions]}
           for by_ref in [{d.ref: d for d in analyze.load_decisions(corpus.docs)}] for slug, versions in rules]
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
    assert _runs_log(corpus)[-1]["soft"] == ["licensgebyr"]


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
    assert _runs_log(corpus)[-1]["metrics"]["content_share"] == 1.0
    assert corpus.data_fingerprint() == before


# ---------------------------------------------------------------- guards and prompts

def test_nothing_is_written_outside_eval(corpus):
    with pytest.raises(ValueError, match="outside"):
        evaluate.write_json(analyze.DECISIONS_DIR / "x.json", {})


def test_the_judges_get_the_extraction_criteria_verbatim():
    assert evaluate.DECISION_CRITERIA in analyze.EXTRACT_SYSTEM and evaluate.FIELD_RULES in analyze.EXTRACT_SYSTEM
    assert evaluate.DECISION_CRITERIA.startswith("Extract every decision")
    assert f"{evaluate.DECISION_CRITERIA}\n\n{evaluate.FIELD_RULES}" in evaluate.DECISIONS_JUDGE_SYSTEM
    assert evaluate.DECISION_CRITERIA in evaluate.RULES_JUDGE_SYSTEM
    assert "budget that sets three fees is three decisions" in evaluate.DECISIONS_JUDGE_SYSTEM.replace("\n", " ")


def test_numbers_and_word_positions():
    assert evaluate.numbers("Licens: kr. 1.000,- og 2,5 timer") == evaluate.numbers("2,5 timer, 1000 kr.")
    assert evaluate.numbers("300 kr. fra 1.1.2015") == evaluate.numbers("kr. 300,00 fra 1. januar 2015") == ("300",)
    assert evaluate.numbers("2000 kr. fra 2016") == ("2000",)
    text = "[Side 1]\nLicens: kr. 200,-\n[Side 2]\nStart-gebyr 300 kr."
    spans = evaluate.word_spans(text)
    assert [text[s:e] for s, e in spans] == ["Licens", "kr", "200", "Start", "gebyr", "300", "kr"]
    assert len(spans) == len(DocWords.of(text).words)


def test_a_judge_that_times_out_is_given_up_after_two_attempts(corpus, fake_claude, monkeypatch, caplog):
    _selection(corpus, "rep2024")
    monkeypatch.setattr(evaluate, "JUDGE_TIMEOUT", 0.5)
    fake_claude.plan("sleep")
    with pytest.raises(SystemExit, match="failed"):
        evaluate.main(["key-rules", "--max-cost", "5", "--workers", "2"])
    assert fake_claude.invocations("call") == 2 * evaluate.JUDGE_ATTEMPTS
    assert "licensgebyr judge 1 timed out" in caplog.text and "report no cost" in caplog.text


# ---------------------------------------------------------------- incremental consolidation gates

def test_candidate_recall_hides_each_decision_from_its_rule_and_picks_the_smallest_k(corpus):
    evaluate.main(["candidate-recall"])
    report = (evaluate.EVAL_DIR / "reports" / "candidate-recall.md").read_text()
    # Licensgebyr's three decisions and Startgebyr's two; the two one-decision rules are left out.
    assert "| **all** | 5 | 100.0% |" in report and "2 decisions are their rule's only one" in report
    assert "Chosen K: 3" in report and "| @3 relabelled |" in report
    assert _runs_log(corpus)[-1]["k"] == 3 and set(_runs_log(corpus)[-1]["relabelled"]) == {"@3", "@5", "@8", "@10",
                                                                                              "@15"}
    # Relabelled, the decision ranks with another category's boost: here every rule is okonomi's, so it still finds
    # its rule among the three.
    hidden = evaluate.hide_one_ranks(analyze.load_rules(), analyze.load_decisions(corpus.docs))
    assert evaluate.relabel("okonomi") == "antidoping" and all(h.relabelled <= 3 for h in hidden)


def test_recall_at_k_and_the_chosen_k():
    hidden = [evaluate.Hidden(f"d#{i}", "r", "okonomi", rank) for i, rank in enumerate([1] * 97 + [4, 9, 12])]
    assert [evaluate.recall_at(hidden, k) for k in (3, 5, 10)] == [0.97, 0.98, 0.99]
    assert evaluate.choose_k(hidden) == 5
    assert evaluate.choose_k(hidden[:97] + [evaluate.Hidden("x#1", "r", "okonomi", 99)] * 3) is None


def test_a_holdout_takes_the_newest_documents_then_seeded_draws_from_the_rest(corpus):
    decisions = analyze.load_decisions(corpus.docs)
    assert evaluate.holdout_parts("newest:2", decisions, 1) == (("newest:2", ("rep2024", "rep2019")),)
    (newest, drawn) = evaluate.holdout_parts("newest:1,random:2", decisions, 7)
    assert newest == ("newest:1", ("rep2024",)) and drawn[0] == "random:2"
    assert len(set(drawn[1])) == 2 and "rep2024" not in drawn[1]
    assert (newest, drawn) == evaluate.holdout_parts("newest:1,random:2", decisions, 7)
    with pytest.raises(SystemExit, match="newest:20"):
        evaluate.holdout_parts("oldest:2", decisions, 1)


def test_withholding_removes_decisions_their_empty_rules_and_slugs_that_stood_for_nothing_else():
    files = {"okonomi": {"kategori": "okonomi", "regler": [
        {"titel": "A", "slug": "a", "versioner": [{"ref": "x#1"}, {"ref": "y#1"}]},
        {"titel": "B", "slug": "b", "versioner": [{"ref": "y#2"}]}], "udeladt": ["y#3", "x#2"], "ikke_tildelt": []}}
    registry = SlugRegistry.of({"old-b": FormerSlug("okonomi", "B", frozenset({"y#2"}), "b"),
                                "old-a": FormerSlug("okonomi", "A", frozenset({"x#1", "y#1"}), "a")})
    out, kept = evaluate.withhold(files, registry, {"y#1", "y#2", "y#3"})
    assert [(r["slug"], [v["ref"] for v in r["versioner"]]) for r in out["okonomi"]["regler"]] == [("a", ["x#1"])]
    assert out["okonomi"]["udeladt"] == ["x#2"]
    assert set(kept.former()) == {"old-a"}


def _reflect_unassigned(corpus) -> None:
    """rep2016 and rep2019 left out as one-offs, so the corpus has no work but what a replay withholds."""
    path = analyze.RULES_DIR / "okonomi.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "udeladt": ["rep2016#1", "rep2019#1"]}))


REPLAY = ["replay", "--holdout", "newest:1", "--name", "r1", "--max-cost", "5", "--workers", "1"]


def test_a_replay_files_the_withheld_documents_again_on_a_copy_and_never_writes_data(corpus, fake_claude):
    _reflect_unassigned(corpus)
    before = corpus.data_fingerprint()
    vote = {"answers": [{"ref": "rep2024#1", "choice": "licensgebyr", "title": None},
                        {"ref": "rep2024#2", "choice": "new", "title": "Klubskifte"}]}

    def updated(ref):
        return {"versioner": [{"ref": ref, "effekt": "aendret", "tekst": "x", "kort": "x", "kort_regel": "x"}],
                "vigtig": True, "note": None}

    fake_claude.answers(vote, vote, vote, updated("rep2024#1"), updated("rep2024#2"))
    evaluate.main(REPLAY)

    assert corpus.data_fingerprint() == before
    replayed = evaluate.replay_dir("r1") / "data" / "regler"
    rules = {r["slug"]: [v["ref"] for v in r["versioner"]] for r in analyze.load_rules(replayed)}
    assert rules == {"licensgebyr": ["rep2010#1", "rep2013#3", "rep2013#1", "rep2024#1"],
                     "startgebyr": ["rep2010#2", "rep2013#2"], "klubskifte": ["rep2024#2"]}
    stored = evaluate.read_json(evaluate.replay_dir("r1") / "holdout.json")
    assert (stored["documents"], stored["parts"]) == (["rep2024"], {"newest:1": ["rep2024"]})
    assert _runs_log(corpus)[-1]["command"] == "replay"

    evaluate.main(REPLAY)  # continues on the copy: nothing is left, so nothing is asked
    assert fake_claude.invocations("call") == 5
    with pytest.raises(SystemExit, match="another holdout or mode; pass --force"):
        evaluate.main([*REPLAY[:2], "newest:2", *REPLAY[3:]])
    assert corpus.data_fingerprint() == before


def test_a_full_replay_consolidates_the_withheld_documents_categories_anew(corpus, fake_claude):
    _reflect_unassigned(corpus)
    before = corpus.data_fingerprint()
    fake_claude.answer({"regler": [{"titel": "Alt", "vigtig": True, "note": None, "versioner": [
        {"ref": ref, "effekt": "indfoert", "tekst": None, "kort": "x", "kort_regel": None}
        for ref in ("rep2010#1", "rep2024#1", "rep2024#2")]}], "udeladt": []})
    evaluate.main([*REPLAY[:4], "f1", *REPLAY[5:], "--mode", "full"])
    assert fake_claude.invocations("call") == 2  # okonomi and medlemskab, the categories rep2024 has decisions in
    assert corpus.data_fingerprint() == before


def test_b_cubed_compares_two_groupings_per_decision():
    a = {"x": frozenset("xy"), "y": frozenset("xy")}  # z in no rule: a cluster of its own
    b = {"y": frozenset("yz"), "z": frozenset("yz")}
    assert evaluate.bcubed(a, b, "xyz") == pytest.approx((2 / 3, 2 / 3, 2 / 3))
    assert evaluate.bcubed(a, a, "xyz") == (1.0, 1.0, 1.0)
    assert evaluate.bcubed(a, b, []) == (None, None, None)


def _rules_dir(corpus, name: str, rules: dict):
    directory = corpus.root / name
    directory.mkdir()
    corpus.rules(rules, directory)
    return directory


SPLIT = {"okonomi": [("Licensgebyr", "licensgebyr", [("rep2010#1", "indfoert"), ("rep2013#1", "bekraeftet")]),
                     ("Licensgebyr 2024", "licensgebyr-2024", [("rep2024#1", "indfoert")])]}
MERGED = {"okonomi": [("Licensgebyr", "licensgebyr", [("rep2010#1", "indfoert"), ("rep2013#1", "bekraeftet"),
                                                      ("rep2024#1", "aendret")])]}


def test_two_consolidations_compare_on_grouping_years_in_force_and_effects(corpus):
    decisions = analyze.load_decisions(corpus.docs)
    split, merged = (analyze.load_rules(_rules_dir(corpus, name, rules))
                     for name, rules in (("split", SPLIT), ("merged", MERGED)))
    c = evaluate.compare_rules(split, merged, decisions, decisions, date(2026, 10, 8), {"random:1": {"rep2024"}})
    # Split puts 2024 apart: precision 1, recall (2/3 + 2/3 + 1/3) / 3.
    assert c.overall.bcubed == pytest.approx((1.0, 5 / 9, 10 / 14))
    # 2010-2023 agree; from 2024 split also shows 2010's licence next to 2024's.
    years = c.overall.years
    assert years["2023-12-31"] == ((1, 1), (1, 1)) and years["2024-12-31"] == ((1, 2), (1, 2))
    shares = c.overall.shares()
    assert (shares["event_agreement"], shares["content_agreement"]) == (17 / 20, 17 / 20)
    assert c.overall.effects == (2, 3)
    # The held-out document alone: rep2024#1 sits apart in split; in force, only the rules holding it count, so
    # merged's licence of 2010-2023 is in force where split's rule of 2024 is not yet.
    held = c.parts["random:1"]
    assert held.decisions == 1 and held.bcubed == pytest.approx((1.0, 1 / 3, 0.5))
    assert held.shares()["event_agreement"] == 3 / 17 and held.effects == (0, 1)
    report = evaluate.comparison_report("split", "merged", c, None, {})
    assert "## Held out: random:1 (late insertion: mostly older documents, 1 decisions)" in report


def test_compare_rules_needs_the_key_or_no_key_and_writes_a_report(corpus):
    split, merged = (_rules_dir(corpus, name, rules) for name, rules in (("split", SPLIT), ("merged", MERGED)))
    with pytest.raises(SystemExit, match="No rules key in .*key-rules.*--no-key"):
        evaluate.main(["compare-rules", str(split), str(merged)])
    evaluate.main(["compare-rules", str(split), str(merged), "--no-key", "--as-of", "2026-10-08"])
    report = (evaluate.EVAL_DIR / "reports" / f"compare-{corpus.root.name}-split-vs-{corpus.root.name}-merged.md"
              ).read_text()
    assert "B-cubed precision / recall / F1 (A against B): 100.0% / 55.6% / 71.4%" in report
    assert "| 2024 | 1/2 | 1/2 |" in report and "Against the rules key" not in report

    evaluate.write_json(evaluate.key_dir("rules") / "licensgebyr.json", _rule_key(corpus))
    evaluate.main(["compare-rules", str(split), str(merged), "--as-of", "2026-10-08", "--report", "with-key"])
    report = (evaluate.EVAL_DIR / "reports" / "with-key.md").read_text()
    assert "## Against the rules key" in report and "| Fragmented rules | 1 | 0 |" in report
    assert "| licensgebyr | 2/4 found, 2 rules, " in report and " | 3/4 found, 1 rules, " in report
