"""Incremental consolidation (incremental.py, candidates.py) against the fake `claude` from conftest.py.

Calls run with one worker, so the fake answers them in order: three votes, a tie-break when the votes split, then
one update per touched rule (existing rules by category and slug, then new rules)."""

import json
import re
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import analyze
import candidates
import checks
import incremental
import render
import update
from analyze import RunBudget, decision_hash
from candidates import CandidateIndex, Query, RuleProfile
from incremental import NEW, ONE_OFF, Choice, Settings
from matching import FormerSlug, SlugRegistry
from scrape import Doc

SONNET, OPUS = "claude-sonnet-5-5", "claude-opus-5-5"
NOW = datetime(2026, 10, 9, tzinfo=timezone.utc)
TEXT = ("[Side 1]\nDagsorden og godkendelse af referat. " + "Mødet drøftede andre sager. " * 40 +
        "Licensgebyret hæves til 350 kr. pr. løfter fra næste år. Vedtaget med 30 stemmer. " +
        "Startgebyret er 200 kr. pr. stævne. " + "Eventuelt intet. " * 80)


def _decision(emne: str, tekst: str, kategori: str = "okonomi", **fields) -> dict:
    return {"emne": emne, "kategori": kategori, "udfald": "vedtaget", "forslagsstiller": None, "handling": "ny",
            "niveau": "staevneregel", "tekst": tekst, "citat": tekst, "side": 1, "stemmer": None,
            "gaelder_fra": None, "gaelder_til": None, "citat_fundet": True, "citat_pos": None, "citat_side": 1,
            **fields}


class World:
    """data/ in tmp_path: documents, their decisions and rule files, written as the pipeline writes them."""

    def __init__(self, tmp_path, monkeypatch):
        self.root = tmp_path
        monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path / "beslutninger")
        monkeypatch.setattr(analyze, "RULES_DIR", tmp_path / "regler")
        monkeypatch.setattr(analyze, "document_text", lambda doc: TEXT)
        analyze.DECISIONS_DIR.mkdir()
        analyze.RULES_DIR.mkdir()
        self.docs: list[Doc] = []

    def document(self, doc_id: str, dato: str, *decisions: dict, retired: tuple[str, ...] = ()) -> None:
        self.docs = [d for d in self.docs if d.id != doc_id] + [
            Doc(doc_id, "repraesentantskab", "Referat", dato, f"{doc_id}.pdf", None, f"sha-{doc_id}")]
        numbered = [{"id": d.pop("id", f"{doc_id}#{n}"), **d} for n, d in enumerate(decisions, start=1)]
        (analyze.DECISIONS_DIR / f"{doc_id}.json").write_text(json.dumps({
            "doc_id": doc_id, "sha256": f"sha-{doc_id}", "version": analyze.EXTRACT_VERSION,
            "model": update.EXTRACT_MODEL,
            "provenance": {"model": update.EXTRACT_MODEL, "cli": "2.1.294", "prompt": "x", "effort": "default"},
            "moededato": dato, "next_number": 20,
            "retired": [{"id": ref, "emne": "x", "reason": "no match in a re-extraction", "date": "2026-10-01"}
                        for ref in retired],
            "beslutninger": numbered}))

    def decisions(self):
        return analyze.load_decisions(self.docs)

    def rules(self, category: str, *rules: tuple, udeladt: tuple[str, ...] = ()) -> None:
        """Rules as (titel, slug, [(ref, effekt), ...]), each version built from its decision as it is now."""
        by_ref = {d.ref: d for d in self.decisions()}
        regler = [{"titel": titel, "slug": slug, "vigtig": True, "note": None,
                   "versioner": [{"ref": ref, "effekt": effekt, "tekst": f"Tekst {ref}", "kort": f"kort {ref}",
                                  "kort_regel": f"regel {ref}", "dhash": decision_hash(by_ref[ref])}
                                 for ref, effekt in versions]}
                  for titel, slug, versions in rules]
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
        return incremental.consolidate(self.docs, self.decisions(), Settings(workers=1, **settings), RunBudget(),
                                       now=NOW, known=known)

    def known(self) -> incremental.Known:
        """What the rule files reflect now, as update.py takes it before the extraction."""
        return incremental.known_inputs(self.decisions(), incremental.RuleBook.load(), self.docs)


@pytest.fixture
def world(tmp_path, monkeypatch):
    """Licensgebyr 2010 -> 2015 -> 2020, Startgebyr 2015 (okonomi) and Klubskifte 2020 (medlemskab), consolidated."""
    w = World(tmp_path, monkeypatch)
    w.document("rep2010", "2010-03-01", _decision("Licensgebyr", "Licensgebyret er 200 kr. pr. løfter."))
    w.document("rep2015", "2015-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 250 kr. pr. løfter."),
               _decision("Startgebyr", "Startgebyret er 150 kr. pr. stævne."))
    w.document("rep2020", "2020-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 300 kr. pr. løfter."),
               _decision("Klubskifte", "Et klubskifte kræver tre måneders karantæne.", "medlemskab"))
    w.rules("okonomi", ("Licensgebyr", "licensgebyr", [("rep2010#1", "indfoert"), ("rep2015#1", "aendret"),
                                                        ("rep2020#1", "aendret")]),
            ("Startgebyr", "startgebyr", [("rep2015#2", "indfoert")]))
    w.rules("medlemskab", ("Klubskifte", "klubskifte", [("rep2020#2", "indfoert")]))
    w.consolidated()
    return w


def _votes(*choices: dict) -> dict:
    """One vote: {ref: choice} or {ref: (NEW, title)}."""
    answers = []
    for vote in choices:
        for ref, choice in vote.items():
            target, title = choice if isinstance(choice, tuple) else (choice, None)
            answers.append({"ref": ref, "choice": target, "title": title})
    return {"answers": answers}


def _update(*refs: str, vigtig: bool = True, note: str | None = None, misfiled: tuple = ()) -> dict:
    """An update answer; `misfiled` lists refs, or (ref, suggested title) pairs."""
    return {"versioner": [{"ref": ref, "effekt": "aendret", "tekst": f"Ny tekst {ref}", "kort": f"ny {ref}",
                           "kort_regel": f"ny regel {ref}"} for ref in refs], "vigtig": vigtig, "note": note,
            "misfiled": [{"ref": m, "title": None} if isinstance(m, str) else {"ref": m[0], "title": m[1]}
                         for m in misfiled]}


def _models(fake_claude) -> list[str]:
    return [args[args.index("--model") + 1] for args in fake_claude.calls()]


# ---------------------------------------------------------------- candidates

def test_words_are_stemmed_and_split_like_the_website_search():
    assert candidates.words("Og licensgebyret er 200 kr.") == ["licensgebyret", "200", "kr"]
    assert [candidates.stem(w) for w in ("licensgebyret", "landsholdsdragt", "stævner")] == \
        ["licensgebyr", "landsholdsdrag", "stævn"]
    vocab = candidates.vocabulary(["Dragt til landshold", "Licens og gebyr"])
    assert candidates.compound_parts("landsholdsdragt", vocab) == ["landshold", "dragt"]
    assert candidates.terms("licensgebyret", vocab) == ["licensgebyr"]  # "gebyret" is no word of its own here
    assert candidates.terms("licensgebyr", vocab) == ["licensgebyr", "lic", "gebyr"]  # Snowball: licens -> lic


def _profile(slug: str, category: str, title: str, text: str = "") -> RuleProfile:
    return RuleProfile(slug, category, title, text, None, ())


def _top(index: CandidateIndex, query: Query, k: int) -> list[str]:
    return [slug for slug, _ in index.rank(query)[:k]]


def test_compound_words_find_the_rule_named_by_their_parts():
    index = CandidateIndex.of([_profile("dragt", "landshold", "Dragt til landshold"),
                               _profile("licens", "okonomi", "Licens og gebyr"),
                               _profile("andet", "staevner", "Indvejning ved stævner")])
    assert _top(index, Query("Landsholdsdragt", "Ny landsholdsdragt fra 2025.", "andet"), 1) == ["dragt"]


def test_the_category_boosts_but_does_not_filter():
    rules = [_profile("start-a", "staevner", "Startgebyr ved stævner"),
             _profile("start-b", "okonomi", "Startgebyr ved stævner"),
             _profile("licens", "okonomi", "Licensgebyr for løftere")]
    index = CandidateIndex.of(rules)
    # Equally close: the decision's own category first; its category does not lift an unrelated rule above both.
    assert _top(index, Query("Startgebyr", "Startgebyr ved stævner", "okonomi"), 3)[:2] == ["start-b", "start-a"]
    assert _top(index, Query("Startgebyr", "Startgebyr ved stævner", "staevner"), 2) == ["start-a", "start-b"]
    # A clearly closer rule of another category still comes first.
    assert _top(index, Query("Licensgebyr", "Licensgebyr for løftere", "staevner"), 1) == ["licens"]


# ---------------------------------------------------------------- votes

OFFERED = {"a#1": ["licensgebyr", "startgebyr"], "a#2": ["startgebyr"]}


def test_a_vote_counts_only_choices_among_the_offered_candidates():
    vote = _votes({"a#1": "klubskifte", "a#2": (NEW, "")}, {"a#1": "startgebyr", "x#9": "licensgebyr"})
    assert incremental.read_vote(vote, OFFERED) == {}  # another rule, a new rule without title, an unknown ref
    vote = _votes({"a#1": "startgebyr", "a#2": ONE_OFF}, {"a#1": "licensgebyr"})
    assert incremental.read_vote(vote, OFFERED) == {"a#1": Choice("startgebyr"), "a#2": Choice(ONE_OFF)}


def test_two_of_three_votes_decide_and_a_split_is_left_open():
    votes = [{"a#1": Choice("licensgebyr"), "a#2": Choice(NEW, "Rabat")},
             {"a#1": Choice("startgebyr"), "a#2": Choice(NEW, "Klubrabat")},
             {"a#1": Choice("licensgebyr"), "a#2": Choice(NEW, "Klubrabat")}]
    assert incremental.tally(votes, ["a#1", "a#2"]) == ({"a#1": Choice("licensgebyr"),
                                                         "a#2": Choice(NEW, "Klubrabat")}, [])
    split = [{"a#1": Choice("licensgebyr")}, {"a#1": Choice(ONE_OFF)}, {}]  # the third vote was invalid
    assert incremental.tally(split, ["a#1"]) == ({}, ["a#1"])


# ---------------------------------------------------------------- the work queue

def test_todays_data_has_no_work(world):
    assert incremental.work_queue(world.decisions(), incremental.RuleBook.load(), world.docs) == []
    before = world.files()
    world.consolidate()
    assert world.files() == before


def test_the_queue_has_new_changed_and_retired_decisions_per_document_in_date_order(world):
    world.document("rep2015", "2015-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 260 kr. pr. løfter."),
                   retired=("rep2015#2",))  # #1 re-read with another amount, #2 gone
    world.document("udateret", None, _decision("Dopingregel", "Doping straffes.", "antidoping"))
    world.document("rep2024", "2024-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 350 kr."))
    queue = incremental.work_queue(world.decisions(), incremental.RuleBook.load(), world.docs)
    assert [(w.doc_id, w.new, w.changed, w.retired) for w in queue] == [
        ("rep2015", (), ("rep2015#1",), ("rep2015#2",)), ("rep2024", ("rep2024#1",), (), ()),
        ("udateret", ("udateret#1",), (), ())]
    assert update.pending_work(world.docs, "incremental").categories == {"okonomi", "antidoping"}
    assert update.pending_work(world.docs, "incremental").lost == {"antidoping"}


# ---------------------------------------------------------------- assign and update

def test_votes_file_new_decisions_and_only_the_touched_rule_changes(world, fake_claude):
    world.document("rep2024", "2024-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 350 kr. pr. løfter."),
                   _decision("Startgebyr", "Startgebyret er 200 kr. pr. stævne."))
    before = world.files()
    startgebyr = json.dumps(world.rule("okonomi", "startgebyr"), ensure_ascii=False)
    fake_claude.answers(
        _votes({"rep2024#1": "licensgebyr", "rep2024#2": "startgebyr"}),
        _votes({"rep2024#1": "licensgebyr", "rep2024#2": "licensgebyr-gammel"}),  # not offered: no vote
        _votes({"rep2024#1": "startgebyr", "rep2024#2": ONE_OFF}),
        _votes({"rep2024#2": ONE_OFF}),  # Opus breaks the tie on #2
        _update("rep2024#1"))
    step = world.consolidate()

    assert _models(fake_claude) == [SONNET, SONNET, SONNET, OPUS, OPUS]
    assert (step.calls, step.failed, step.skipped) == (5, 0, 0)  # Claude calls, not documents
    licens = world.rule("okonomi", "licensgebyr")
    assert [v["ref"] for v in licens["versioner"]] == ["rep2010#1", "rep2015#1", "rep2020#1", "rep2024#1"]
    old = json.loads(before["okonomi.json"])["regler"][0]["versioner"]
    assert licens["versioner"][:3] == old  # byte for byte: the history before the new decision is untouched
    new = licens["versioner"][3]
    assert new["dhash"] == decision_hash(next(d for d in world.decisions() if d.ref == "rep2024#1"))
    assert new["updated"]["model"] == OPUS and new["updated"]["time"] == "2026-10-09T00:00:00+00:00"
    assert json.dumps(world.rule("okonomi", "startgebyr"), ensure_ascii=False) == startgebyr
    assert world.stored("okonomi")["udeladt"] == ["rep2024#2"]
    assert world.files()["medlemskab.json"] == before["medlemskab.json"]
    args = fake_claude.calls()
    assert [a[a.index("--system-prompt") + 1] for a in args] == [incremental.ASSIGN_SYSTEM] * 4 + [
        incremental.UPDATE_SYSTEM]
    assert "rep2024#1" not in fake_claude.prompts()[3]  # the tie-break is asked about the split decision only
    # The category reflects all its decisions again: full sees nothing to do, the checks nothing pending; the file
    # records the fingerprint of each decision it reflects.
    assert analyze.consolidation_todo(world.decisions(), {d.id: d.organ_label for d in world.docs}) == []
    assert set(world.stored("okonomi")["inputs"]) == {"rep2010#1", "rep2015#1", "rep2015#2", "rep2020#1",
                                                      "rep2024#1", "rep2024#2"}


def _section(prompt: str, tag: str):
    return json.loads(prompt.split(f"<{tag}>\n", 1)[1].split(f"\n</{tag}>", 1)[0])


def test_the_votes_see_the_ranked_candidates_and_the_update_the_minutes(world, fake_claude):
    world.document("rep2024", "2024-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 350 kr. pr. løfter"))
    fake_claude.answers(*[_votes({"rep2024#1": "licensgebyr"})] * 3, _update("rep2024#1"))
    world.consolidate(k=3)
    votes, rule_update = fake_claude.prompts()[0], fake_claude.prompts()[-1]
    (asked,) = _section(votes, "beslutninger")
    a, b, c = asked["kandidater"]
    assert a == "licensgebyr" and [r["slug"] for r in _section(votes, "regler")] == [a, b, c]
    # Each vote reads the candidates starting a third further on.
    assert [_section(p, "beslutninger")[0]["kandidater"] for p in fake_claude.prompts()[1:3]] == [[b, c, a], [c, a, b]]
    statuses = [(v["ref"], v["status"]) for v in _section(rule_update, "regel")["versioner"]]
    assert statuses == [("rep2010#1", "keep"), ("rep2015#1", "keep"), ("rep2020#1", "keep"), ("rep2024#1", "new")]
    (source,) = _section(rule_update, "kilder")
    # 100 words before the quote, as written in the document (the agenda before them is left out), and on to the end.
    quote = "Licensgebyret hæves til 350 kr. pr. løfter"
    before, _, after = source["passage"].partition(quote)
    assert len(analyze._words(before)) == 100 and after.endswith("Eventuelt intet")


def test_a_new_rule_gets_a_title_slug_that_no_live_or_former_rule_has(world, fake_claude):
    analyze._save_slugs(SlugRegistry.of({"juniorlicens": FormerSlug("okonomi", "Juniorlicens", frozenset({"z#1"}))}))
    world.document("rep2024", "2024-03-01", _decision("Licensgebyr for juniorer", "Juniorer betaler 100 kr."),
                   _decision("Licensgebyr for juniorer", "Juniorlicensen gælder fra 1. januar."))
    title = (NEW, "Juniorlicens")
    fake_claude.answers(_votes({"rep2024#1": title, "rep2024#2": title}), _votes({"rep2024#1": title,
                                                                                  "rep2024#2": title}),
                        _votes({"rep2024#1": ONE_OFF, "rep2024#2": title}),
                        _update("rep2024#1", "rep2024#2", vigtig=False, note="Kun juniorer."))
    world.consolidate()
    rule = world.stored("okonomi")["regler"][-1]
    assert (rule["titel"], rule["slug"], rule["vigtig"], rule["note"]) == ("Juniorlicens", "juniorlicens-2", False,
                                                                          "Kun juniorer.")
    assert [v["ref"] for v in rule["versioner"]] == ["rep2024#1", "rep2024#2"]
    assert checks.identity_problems(world.decisions(), set(), analyze.load_rules(), analyze.load_slugs()) == []


# ---------------------------------------------------------------- late, changed and retired decisions

def test_a_late_decision_rewrites_from_where_it_belongs_on(world, fake_claude):
    world.document("rep2017", "2017-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 275 kr. pr. løfter."))
    before = world.rule("okonomi", "licensgebyr")["versioner"]
    plan = incremental.plan_update(before, {d.ref: d for d in world.decisions()}, set(), set(), ["rep2017#1"])
    assert (list(plan.keep), plan.point) == (before[:2], 2)
    assert [(e.ref, e.status) for e in plan.entries] == [("rep2010#1", "keep"), ("rep2015#1", "keep"),
                                                    ("rep2017#1", "new"), ("rep2020#1", "rewrite")]

    fake_claude.answers(*[_votes({"rep2017#1": "licensgebyr"})] * 3, _update("rep2017#1", "rep2020#1"))
    world.consolidate()
    after = world.rule("okonomi", "licensgebyr")["versioner"]
    assert after[:2] == before[:2]
    assert [(v["ref"], v["tekst"]) for v in after[2:]] == [("rep2017#1", "Ny tekst rep2017#1"),
                                                          ("rep2020#1", "Ny tekst rep2020#1")]


def test_an_answer_that_rewrites_earlier_versions_is_asked_again_then_left_for_the_next_run(world, fake_claude):
    world.document("rep2017", "2017-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 275 kr. pr. løfter."))
    before = world.files()
    fake_claude.answers(*[_votes({"rep2017#1": "licensgebyr"})] * 3,
                        _update("rep2010#1", "rep2017#1", "rep2020#1"))  # rewrites 2010: rejected, twice
    step = world.consolidate()
    assert _models(fake_claude) == [SONNET] * 3 + [OPUS] * 2
    assert (step.calls, step.failed) == (5, 1)
    assert world.files() == before


def test_an_update_answer_must_return_exactly_the_versions_from_the_insertion_point_on(world):
    world.document("rep2017", "2017-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 275 kr. pr. løfter."))
    by_ref = {d.ref: d for d in world.decisions()}
    versions = world.rule("okonomi", "licensgebyr")["versioner"]
    plan = incremental.plan_update(versions, by_ref, set(), set(), ["rep2017#1"])
    planned = incremental.RuleUpdate("okonomi", "licensgebyr", "Licensgebyr", True, None, plan, tuple(versions))
    with pytest.raises(incremental.UpdateRejected, match="before the insertion point .rep2010#1"):
        incremental.merge(planned, _update("rep2010#1", "rep2017#1", "rep2020#1"), by_ref, {})
    with pytest.raises(incremental.UpdateRejected, match="returned rep2017#1 instead of rep2017#1, rep2020#1"):
        incremental.merge(planned, _update("rep2017#1"), by_ref, {})
    # Render's order whatever Claude's; proposals get the effect their outcome gives.
    by_ref["rep2017#1"] = replace(by_ref["rep2017#1"], udfald="forkastet")
    merged = incremental.merge(planned, _update("rep2020#1", "rep2017#1"), by_ref, {"model": OPUS})
    assert [(v["ref"], v["effekt"]) for v in merged] == [("rep2010#1", "indfoert"), ("rep2015#1", "aendret"),
                                                          ("rep2017#1", "forkastet"), ("rep2020#1", "aendret")]


def test_a_changed_decision_stays_in_its_rule_without_a_vote(world, fake_claude):
    world.document("rep2015", "2015-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 260 kr. pr. løfter."),
                   _decision("Startgebyr", "Startgebyret er 150 kr. pr. stævne."))
    fake_claude.answers(_update("rep2015#1", "rep2020#1"))
    world.consolidate()
    assert _models(fake_claude) == [OPUS]
    versions = world.rule("okonomi", "licensgebyr")["versioner"]
    assert [v["ref"] for v in versions] == ["rep2010#1", "rep2015#1", "rep2020#1"]
    assert all(analyze.version_matches(v, d) for v, d in zip(versions, world.decisions()) if v["ref"] == d.ref)


def test_retired_decisions_leave_their_rules_and_an_empty_rule_is_retired(world, fake_claude):
    # rep2015#1 stood in the middle of Licensgebyr: the versions after it are rewritten. rep2015#2 was Startgebyr's
    # only decision: the rule goes, and its slug waits in the history for a successor.
    world.document("rep2015", "2015-03-01", _decision("Klubskifte", "Klubskifte kræver to måneders karantæne.",
                                                      "medlemskab", id="rep2015#3"),
                   retired=("rep2015#1", "rep2015#2"))
    fake_claude.answers(*[_votes({"rep2015#3": "klubskifte"})] * 3, _update("rep2015#3", "rep2020#2"),
                        _update("rep2020#1"))
    world.consolidate()
    assert _models(fake_claude) == [SONNET] * 3 + [OPUS] * 2
    assert [v["ref"] for v in world.rule("okonomi", "licensgebyr")["versioner"]] == ["rep2010#1", "rep2020#1"]
    assert [r["slug"] for r in world.stored("okonomi")["regler"]] == ["licensgebyr"]
    assert analyze.load_slugs().retired == {"startgebyr": FormerSlug("okonomi", "Startgebyr",
                                                                     frozenset({"rep2015#2"}))}
    assert [v["ref"] for v in world.rule("medlemskab", "klubskifte")["versioner"]] == ["rep2015#3", "rep2020#2"]


def test_a_rule_touched_by_one_document_also_drops_another_documents_retired_decision(world, fake_claude):
    # rep2020 was read again and lost rep2020#1; rep2017, filed first, lands in the same rule and brings it up to date.
    world.document("rep2020", "2020-03-01", _decision("Klubskifte", "Et klubskifte kræver tre måneders karantæne.",
                                                      "medlemskab", id="rep2020#2"), retired=("rep2020#1",))
    world.document("rep2017", "2017-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 275 kr. pr. løfter."))
    fake_claude.answers(*[_votes({"rep2017#1": "licensgebyr"})] * 3, _update("rep2017#1"))
    step = world.consolidate()
    assert (step.calls, step.failed, fake_claude.invocations("call")) == (4, 0, 4)  # rep2020 had nothing left
    assert [v["ref"] for v in world.rule("okonomi", "licensgebyr")["versioner"]] == ["rep2010#1", "rep2015#1",
                                                                                    "rep2017#1"]
    assert incremental.work_queue(world.decisions(), incremental.RuleBook.load(), world.docs) == []


def test_a_retired_last_version_needs_no_call(world, fake_claude):
    world.document("rep2020", "2020-03-01", _decision("Klubskifte", "Et klubskifte kræver tre måneders karantæne.",
                                                      "medlemskab", id="rep2020#2"), retired=("rep2020#1",))
    world.consolidate()
    assert fake_claude.calls() == []
    assert [v["ref"] for v in world.rule("okonomi", "licensgebyr")["versioner"]] == ["rep2010#1", "rep2015#1"]


# ---------------------------------------------------------------- a monthly run

@pytest.fixture
def run(world, fake_claude, monkeypatch):
    monkeypatch.setattr(update, "RUNS_LOG", world.root / "runs.jsonl")
    monkeypatch.setattr(update, "RUN_REPORT", world.root / "run-report.md")
    monkeypatch.setattr(update.website, "build", lambda *args: world.root / "index.html")
    monkeypatch.setattr(render, "OUT_DIR", world.root / "regelsaet")
    monkeypatch.setattr(update.scrape, "ROOT", world.root)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)

    def main(*args: str) -> None:
        monkeypatch.setattr(update.scrape, "load_manifest", lambda: world.docs)
        monkeypatch.setattr(sys, "argv", ["update.py", "--offline", "--workers", "1", *args])
        update.main()

    return main


def test_an_incremental_month_passes_the_history_check_and_renders(world, fake_claude, run):
    world.document("rep2024", "2024-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 350 kr. pr. løfter."),
                   _decision("Startgebyr", "Startgebyret er 200 kr. pr. stævne."))
    fake_claude.answers(_votes({"rep2024#1": "licensgebyr", "rep2024#2": "startgebyr"}),
                        _votes({"rep2024#1": "licensgebyr", "rep2024#2": "startgebyr"}),
                        _votes({"rep2024#1": "licensgebyr", "rep2024#2": ONE_OFF}),
                        _update("rep2024#1"), _update("rep2024#2"))
    run()  # incremental is the default; exits normally: the checks found no errors

    report = (world.root / "run-report.md").read_text()
    assert "**Ready to publish**" in report and "No rule in force there changed." in report
    assert "Klubskifte" in (world.root / "regelsaet" / "regler" / "medlemskab.md").read_text()
    year = (world.root / "regelsaet" / "2024.md").read_text()
    assert "ny rep2024#1" in year and "ny rep2024#2" in year
    assert checks.find_problems(checks.Data.load(world.docs)) == []


def test_a_category_left_for_a_full_consolidation_is_listed_in_the_run_report(world, fake_claude, run):
    # styrke.dk dated rep2015 anew before this run: nothing reflects the old date, so okonomi cannot be settled.
    world.document("rep2015", "2016-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 250 kr. pr. løfter."),
                   _decision("Startgebyr", "Startgebyret er 150 kr. pr. stævne."))
    run()
    report = (world.root / "run-report.md").read_text()
    assert f"## {incremental.UNSEEN}: 1\n\n- okonomi may reflect decisions that changed unseen" in report
    assert "run `update.py --consolidate-mode full` for it" in report


def test_the_run_report_lists_what_a_step_asks_to_have_looked_at():
    step = analyze.StepSummary("Consolidate", 9, 0, 0, analyze.Usage(), 1.0,
                               {incremental.MISFILED_TWICE: ("x#1 (Emne): misfiled in `a`, `b`; filed as the new rule "
                                                             "Emne (`emne`)",)})
    report = update.run_report(0, {"consolidate": step}, [], None, [], datetime(2026, 10, 9).date())
    assert (f"## {incremental.MISFILED_TWICE}: 1\n\n- x#1 (Emne): misfiled in `a`, `b`; filed as the new rule Emne "
            f"(`emne`)\n") in report


def test_incremental_is_the_default_mode_and_full_is_asked_for(run, world, fake_claude, monkeypatch):
    called = []
    monkeypatch.setattr(analyze, "consolidate", lambda *args, **kwargs: called.append("full") or
                        analyze.StepSummary("Consolidate", 0, 0, 0, analyze.Usage(), 0))
    monkeypatch.setattr(incremental, "consolidate", lambda *args, **kwargs: called.append("incremental") or
                        analyze.StepSummary("Consolidate", 0, 0, 0, analyze.Usage(), 0))
    run()
    run("--consolidate-mode", "full")
    assert called == ["incremental", "full"]


def test_a_new_consolidation_version_is_migrated_in_full_only(run, world, fake_claude, monkeypatch):
    monkeypatch.setattr(analyze, "CONSOLIDATE_VERSION", analyze.CONSOLIDATE_VERSION + 1)
    for args in ((), ("--allow-rebuild",)):  # incremental cannot migrate it, allowed or not
        with pytest.raises(SystemExit, match="only a full consolidation migrates: medlemskab, okonomi"):
            run(*args)
        assert update.MIGRATE in (world.root / "run-report.md").read_text()
    with pytest.raises(SystemExit, match="2 of 2 categories need consolidating again") as refused:
        run("--consolidate-mode", "full")  # a full run stops at the guard too, until it is allowed
    assert update.HOW_TO_PROCEED in str(refused.value) and fake_claude.calls() == []
    fake_claude.answer(_same_rules(world))
    run("--consolidate-mode", "full", "--allow-rebuild")
    assert fake_claude.invocations("call") == 2  # both categories consolidated anew under the new version
    assert update.pending_work(world.docs).reasons() == []  # the following monthly runs go on incrementally


def _same_rules(world) -> dict:
    """A full consolidation answer that gives every rule of both categories again as it is: the past does not
    change."""
    rules = [{k: rule[k] for k in ("titel", "vigtig", "note")} | {"versioner": [
        {k: v[k] for k in ("ref", "effekt", "tekst", "kort", "kort_regel")} for v in rule["versioner"]]}
        for category in ("okonomi", "medlemskab") for rule in world.stored(category)["regler"]]
    return {"regler": rules, "udeladt": []}


def test_a_cut_off_migration_is_finished_by_its_command_which_skips_the_categories_it_did(world, fake_claude, run,
                                                                                         monkeypatch):
    monkeypatch.setattr(analyze, "CONSOLIDATE_VERSION", analyze.CONSOLIDATE_VERSION + 1)
    fake_claude.answer(_same_rules(world))
    fake_claude.plan("ok", "error")  # okonomi is migrated, then medlemskab's call fails: the migration is cut off
    with pytest.raises(SystemExit, match="1 Claude calls failed"):
        run("--consolidate-mode", "full", "--allow-rebuild")
    new, old = analyze.CONSOLIDATE_VERSION, analyze.CONSOLIDATE_VERSION - 1
    assert (world.stored("okonomi")["version"], world.stored("medlemskab")["version"]) == (new, old)
    report = (world.root / "run-report.md").read_text()
    assert "**Unfinished rebuild**" in report and update.RERUN in report

    # A plain run (the monthly one) refuses and says how to go on.
    with pytest.raises(SystemExit, match="only a full consolidation migrates: medlemskab") as refused:
        run()
    assert update.HOW_TO_PROCEED in str(refused.value)
    calls = fake_claude.invocations("call")
    fake_claude.plan("ok")
    run("--consolidate-mode", "full", "--allow-rebuild")  # the same command again: medlemskab only
    assert fake_claude.invocations("call") == calls + 1 and world.stored("medlemskab")["version"] == new
    run()  # and the next plain run is incremental, with nothing to do
    assert fake_claude.invocations("call") == calls + 1
    assert update.pending_work(world.docs).reasons() == [] and update.pending_work(world.docs).outdated == set()


def test_decisions_a_cut_off_month_left_in_most_categories_are_filed_by_the_next_plain_run(world, fake_claude, run):
    # A big meeting with new decisions in every category, extracted by a run whose filing was then cut off.
    world.document("rep2024", "2024-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 350 kr. pr. løfter."),
                   _decision("Klubskifte", "Et klubskifte kræver fire måneders karantæne.", "medlemskab"))
    fake_claude.plan("error")
    with pytest.raises(SystemExit, match="Claude calls failed"):
        run()
    assert update.pending_work(world.docs).categories == {"okonomi", "medlemskab"}  # 2 of 2: ordinary work all the same
    calls = fake_claude.invocations("call")
    fake_claude.plan("ok")
    vote = _votes({"rep2024#1": "licensgebyr", "rep2024#2": "klubskifte"})
    fake_claude.answers(*[{}] * calls, vote, vote, vote, _update("rep2024#2"), _update("rep2024#1"))
    run()  # the next monthly run, without --allow-rebuild
    assert fake_claude.invocations("call") == calls + 5
    assert [v["ref"] for v in world.rule("okonomi", "licensgebyr")["versioner"]][-1] == "rep2024#1"
    assert [v["ref"] for v in world.rule("medlemskab", "klubskifte")["versioner"]][-1] == "rep2024#2"


def test_changed_decisions_in_most_categories_still_stop_a_plain_run(world, fake_claude, run):
    # rep2020 read again: its decision in each category changed, so every category's rules are to be redone.
    world.document("rep2020", "2020-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 325 kr. pr. løfter."),
                   _decision("Klubskifte", "Et klubskifte kræver fire måneders karantæne.", "medlemskab"))
    with pytest.raises(SystemExit):
        run()
    report = (world.root / "run-report.md").read_text()
    assert "Stopped before any Claude call: 2 of 2 categories need consolidating again" in report
    assert fake_claude.calls() == []


def test_the_workflow_runs_the_default_mode_unless_full_is_ticked():
    workflow = (Path(update.__file__).parent / ".github" / "workflows" / "update.yml").read_text()
    full = workflow[workflow.index("\n      full:\n"):workflow.index("\npermissions:")]
    label = re.search(r'description: "(.*)"', full).group(1)
    assert f"'{label}'" in update.HOW_TO_PROCEED and "type: boolean" in full and "default: false" in full
    step = workflow[workflow.index("- name: Download new minutes and update the rule overview"):]
    assert "FULL: ${{ inputs.full }}" in step and "REBUILD: ${{ inputs.rebuild }}" in step
    assert 'if [ "$FULL" = "true" ]; then args+=(--consolidate-mode full); fi' in step
    assert 'uv run update.py "${args[@]}"' in step and "--consolidate-mode incremental" not in workflow


# ---------------------------------------------------------------- review fixes

def _full(world, fake_claude, answer: dict) -> None:
    fake_claude.answer(answer)
    analyze.consolidate(world.decisions(), {d.id: d.organ_label for d in world.docs}, model=OPUS, effort=None,
                        workers=1)


def test_a_decision_filed_under_another_categorys_rule_is_never_consolidated_twice(world, fake_claude):
    # Incremental files a medlemskab decision into okonomi's Licensgebyr; a later full run of medlemskab must not
    # take it again: it is consolidated with the rule that holds it.
    world.document("rep2024", "2024-03-01", _decision("Licens ved klubskifte", "Licensen følger med ved klubskifte.",
                                                      "medlemskab"))
    fake_claude.answers(*[_votes({"rep2024#1": "licensgebyr"})] * 3, _update("rep2024#1"))
    world.consolidate()
    assert analyze.consolidation_todo(world.decisions(), {d.id: d.organ_label for d in world.docs}) == []

    world.document("rep2025", "2025-03-01", _decision("Klubskifte", "Klubskifte kræver to måneders karantæne.",
                                                      "medlemskab"))
    todo = analyze.consolidation_todo(world.decisions(), {d.id: d.organ_label for d in world.docs})
    assert [(job.category, [i["ref"] for i in job.items]) for job in todo] == [("medlemskab", ["rep2020#2",
                                                                                               "rep2025#1"])]
    _full(world, fake_claude, {"regler": [{"titel": "Klubskifte", "vigtig": True, "note": None, "versioner": [
        {"ref": ref, "effekt": "indfoert", "tekst": None, "kort": "x", "kort_regel": None}
        for ref in ("rep2020#2", "rep2024#1", "rep2025#1")]}], "udeladt": []})
    held = [r["slug"] for r in analyze.load_rules() if "rep2024#1" in {v["ref"] for v in r["versioner"]}]
    assert held == ["licensgebyr"]
    problems = checks.identity_problems(world.decisions(), set(), analyze.load_rules(), analyze.load_slugs())
    assert not [p for p in problems if "rules" in p.message]


def test_a_redated_document_reaches_its_rules_although_its_decisions_did_not_change(world, fake_claude):
    known = world.known()  # update.py takes it before the extraction
    world.document("rep2015", "2016-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 250 kr. pr. løfter."),
                   _decision("Startgebyr", "Startgebyret er 150 kr. pr. stævne."))  # re-read: another date only
    fake_claude.answers(_update("rep2015#1", "rep2020#1"), _update("rep2015#2"))
    world.consolidate(known)
    assert _models(fake_claude) == [OPUS, OPUS]  # no vote: they stay where they are
    assert [v["tekst"] for v in world.rule("okonomi", "licensgebyr")["versioner"]] == [
        "Tekst rep2010#1", "Ny tekst rep2015#1", "Ny tekst rep2020#1"]
    assert analyze.consolidation_todo(world.decisions(), {d.id: d.organ_label for d in world.docs}) == []


def test_a_category_that_may_have_changed_unseen_is_not_settled(world, fake_claude, caplog):
    stale = world.stored("okonomi")["input_hash"]
    world.document("rep2015", "2016-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 250 kr. pr. løfter."),
                   _decision("Startgebyr", "Startgebyret er 150 kr. pr. stævne."))
    world.consolidate()  # no baseline from before the change, and the file records no fingerprints
    assert fake_claude.calls() == [] and world.stored("okonomi")["input_hash"] == stale
    assert "okonomi may reflect decisions that changed unseen" in caplog.text
    todo = analyze.consolidation_todo(world.decisions(), {d.id: d.organ_label for d in world.docs})
    assert [job.category for job in todo] == ["okonomi"]  # still pending for full and for the checks


def test_a_one_off_counts_only_in_its_own_category(world):
    world.document("rep2012", "2012-03-01", _decision("Jubilæum", "Forbundet fejrer jubilæum.", "medlemskab"))
    path = analyze.RULES_DIR / "okonomi.json"
    path.write_text(json.dumps({**world.stored("okonomi"), "udeladt": ["rep2012#1"]}))  # left out under okonomi
    queue = incremental.work_queue(world.decisions(), incremental.RuleBook.load(), world.docs)
    assert [(w.doc_id, w.new) for w in queue] == [("rep2012", ("rep2012#1",))]


def test_the_check_before_the_insertion_point_compares_with_the_stored_versions(world):
    world.document("rep2017", "2017-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 275 kr. pr. løfter."))
    by_ref = {d.ref: d for d in world.decisions()}
    versions = world.rule("okonomi", "licensgebyr")["versioner"]
    plan = incremental.plan_update(versions, by_ref, set(), set(), ["rep2017#1"])
    tampered = replace(plan, keep=({**plan.keep[0], "tekst": "Ændret"}, plan.keep[1]))
    planned = incremental.RuleUpdate("okonomi", "licensgebyr", "Licensgebyr", True, None, tampered, tuple(versions))
    with pytest.raises(AssertionError, match="not those stored"):
        incremental.merge(planned, _update("rep2017#1", "rep2020#1"), by_ref, {})


def test_a_removed_version_is_placed_by_its_documents_date_not_its_place_in_the_file(world):
    by_ref = {d.ref: d for d in world.decisions()}
    licens = {v["ref"]: v for v in world.rule("okonomi", "licensgebyr")["versioner"]}
    versions = [licens["rep2010#1"], licens["rep2020#1"], licens["rep2015#1"]]  # file order is not render's order
    del by_ref["rep2020#1"]  # retired
    plan = incremental.plan_update(versions, by_ref, {"rep2020#1"}, set(), [], {"rep2020#1": "2020-03-01"})
    assert plan.entries is None and [v["ref"] for v in plan.keep] == ["rep2010#1", "rep2015#1"]  # it was the last
    plan = incremental.plan_update(versions, by_ref, {"rep2020#1"}, set(), [], {"rep2020#1": "2012-03-01"})
    assert [(e.ref, e.status) for e in plan.entries] == [("rep2010#1", "keep"), ("rep2020#1", "removed"),
                                                         ("rep2015#1", "rewrite")]


def test_a_slug_retired_by_a_document_is_not_given_to_a_new_rule_of_the_same_document(world, fake_claude):
    # Startgebyr's only decision is read again as a new one, which the votes and the tie-break file as a new rule.
    world.document("rep2015", "2015-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 250 kr. pr. løfter."),
                   _decision("Startgebyr", "Startgebyret er 150 kr. pr. start.", id="rep2015#3"),
                   retired=("rep2015#2",))
    new = _votes({"rep2015#3": (NEW, "Startgebyr")})
    fake_claude.answers(new, new, new, new, _update("rep2015#3"))
    world.consolidate()
    assert [r["slug"] for r in world.stored("okonomi")["regler"]] == ["licensgebyr", "startgebyr-2"]
    assert analyze.load_slugs().targets() == {"startgebyr": "startgebyr-2"}


def test_new_decisions_with_the_same_new_title_form_one_rule_whatever_their_category(world, fake_claude):
    world.document("rep2024", "2024-03-01", _decision("Klubrabat", "Klubber får rabat på licens."),
                   _decision("Klubrabat", "Rabatten gælder nye klubber.", "medlemskab"))
    fake_claude.answers(*[_votes({"rep2024#1": (NEW, "Klubrabat"), "rep2024#2": (NEW, "klubrabat")})] * 3,
                        _update("rep2024#1", "rep2024#2"))
    world.consolidate()
    assert [r["slug"] for r in world.stored("okonomi")["regler"]] == ["licensgebyr", "startgebyr", "klubrabat"]
    assert [r["slug"] for r in world.stored("medlemskab")["regler"]] == ["klubskifte"]


def test_a_new_rule_named_like_a_live_one_goes_to_the_tie_break_with_that_rule_offered(world, fake_claude):
    world.document("rep2024", "2024-03-01", _decision("Juniorkontingent", "Juniorer betaler halv pris."))
    fake_claude.answers(*[_votes({"rep2024#1": (NEW, "Licensgebyr")})] * 3, _votes({"rep2024#1": "licensgebyr"}),
                        _update("rep2024#1"))
    world.consolidate(k=1)
    assert _models(fake_claude) == [SONNET] * 3 + [OPUS] * 2
    assert "licensgebyr" in _section(fake_claude.prompts()[3], "beslutninger")[0]["kandidater"]
    assert [r["slug"] for r in world.stored("okonomi")["regler"]] == ["licensgebyr", "startgebyr"]
    assert "rep2024#1" in {v["ref"] for v in world.rule("okonomi", "licensgebyr")["versioner"]}


def test_the_voters_get_the_extraction_criteria_for_one_offs_and_no_rule_categories(world):
    listed = incremental.NOT_DECISIONS
    assert listed in analyze.EXTRACT_SYSTEM and listed in incremental.ASSIGN_SYSTEM
    assert listed.startswith("- one-off tasks") and listed.endswith("competition results")
    prompt = incremental.ASSIGN_SYSTEM.replace("\n", " ")
    assert "These are not one-offs: a fee or rate set or confirmed with a budget" in prompt
    assert "Prefer an existing rule when the decision touches any part of what it regulates" in prompt
    by_ref = {d.ref: d for d in world.decisions()}
    assert "kategori" not in incremental.rule_view("okonomi", world.rule("okonomi", "licensgebyr"), by_ref)


def test_the_update_keeps_the_fields_when_the_minutes_contradict_them():
    prompt = incremental.UPDATE_SYSTEM.replace("\n", " ")
    assert "When the minutes contradict a decision's fields, keep the fields and say so in note." in prompt
    assert "the minutes decide" not in prompt


def test_the_passage_reaches_from_before_the_quote_to_the_vote():
    filler = "Debatten fortsatte længe. " * 150  # 450 words
    text = (f"[Side 1]\n{'Indledning. ' * 120}Forslag om at hæve licensgebyret til 400 kr. {filler}"
            f"Forslaget blev vedtaget med 31 stemmer for, 7 imod og 2 blanke. {'Eventuelt. ' * 600}")
    quote = "Forslag om at hæve licensgebyret til 400 kr"
    found = incremental.passage(text, quote, 1, "31 for, 7 imod, 2 blanke")
    assert found.startswith("Indledning.") and len(analyze._words(found.partition(quote)[0])) == 100
    assert found.endswith("7 imod og 2 blanke")
    plain = incremental.passage(text, quote, 1)
    assert len(analyze._words(plain.partition(quote)[2])) == 400 and "31 stemmer" not in plain


def test_a_misfiled_decision_is_voted_on_again_without_that_rule(world, fake_claude):
    world.document("rep2024", "2024-03-01", _decision("Startgebyr", "Startgebyret er 200 kr. pr. stævne."))
    fake_claude.answers(*[_votes({"rep2024#1": "licensgebyr"})] * 3, _update(misfiled=("rep2024#1",)),
                        *[_votes({"rep2024#1": "startgebyr"})] * 3, _update("rep2024#1"))
    step = world.consolidate(k=3)
    assert _models(fake_claude) == [SONNET] * 3 + [OPUS] + [SONNET] * 3 + [OPUS]
    assert (step.calls, step.failed) == (8, 0)
    assert "licensgebyr" in _section(fake_claude.prompts()[0], "beslutninger")[0]["kandidater"]
    assert "licensgebyr" not in _section(fake_claude.prompts()[4], "beslutninger")[0]["kandidater"]
    assert [v["ref"] for v in world.rule("okonomi", "startgebyr")["versioner"]] == ["rep2015#2", "rep2024#1"]
    assert "rep2024#1" not in {v["ref"] for v in world.rule("okonomi", "licensgebyr")["versioner"]}


def test_a_decision_misfiled_twice_is_filed_as_a_new_rule_and_listed_for_review(world, fake_claude, caplog):
    world.document("rep2024", "2024-03-01", _decision("Startgebyr", "Juniorer betaler halvt startgebyr."))
    fake_claude.answers(*[_votes({"rep2024#1": "licensgebyr"})] * 3, _update(misfiled=("rep2024#1",)),
                        *[_votes({"rep2024#1": "startgebyr"})] * 3,
                        _update(misfiled=(("rep2024#1", "Startgebyr for juniorer"),)), _update("rep2024#1"))
    step = world.consolidate(k=3)
    assert _models(fake_claude) == [SONNET] * 3 + [OPUS] + [SONNET] * 3 + [OPUS, OPUS]
    assert (step.calls, step.failed, step.skipped) == (9, 0, 0)  # filed, not left pending for every later run
    rule = world.stored("okonomi")["regler"][-1]
    assert (rule["titel"], rule["slug"], [v["ref"] for v in rule["versioner"]]) == (
        "Startgebyr for juniorer", "startgebyr-for-juniorer", ["rep2024#1"])
    note = ("rep2024#1 (Startgebyr): misfiled in `licensgebyr`, `startgebyr`; filed as the new rule Startgebyr for "
            "juniorer (`startgebyr-for-juniorer`)")
    assert step.notes == {incremental.MISFILED_TWICE: (note,)} and note in caplog.text
    assert incremental.work_queue(world.decisions(), incremental.RuleBook.load(), world.docs) == []


def test_a_new_rule_after_two_misfilings_is_checked_for_a_namesake_once(world, fake_claude):
    # No title suggested: the emne, "Klubskifte", names a live rule, which the tie-break picks; misfiled there too,
    # the decision gets its own rule.
    world.document("rep2024", "2024-03-01", _decision("Klubskifte", "Klubskiftegebyret er 100 kr."))
    fake_claude.answers(*[_votes({"rep2024#1": "licensgebyr"})] * 3, _update(misfiled=("rep2024#1",)),
                        *[_votes({"rep2024#1": "startgebyr"})] * 3, _update(misfiled=("rep2024#1",)),
                        _votes({"rep2024#1": "klubskifte"}), _update(misfiled=("rep2024#1",)), _update("rep2024#1"))
    step = world.consolidate(k=3)
    assert _models(fake_claude) == [SONNET] * 3 + [OPUS] + [SONNET] * 3 + [OPUS] * 4
    assert _section(fake_claude.prompts()[8], "beslutninger")[0]["kandidater"] == ["klubskifte"]
    assert [r["slug"] for r in world.stored("okonomi")["regler"]] == ["licensgebyr", "startgebyr", "klubskifte-2"]
    assert step.notes[incremental.MISFILED_TWICE] == (
        "rep2024#1 (Klubskifte): misfiled in `klubskifte`, `licensgebyr`, `startgebyr`; filed as the new rule "
        "Klubskifte (`klubskifte-2`)",)
