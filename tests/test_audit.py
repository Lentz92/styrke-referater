"""The audit (audit.py) against the fake `claude` from conftest.py.

Calls run with one worker, so the fake answers them in order: two propose runs per category with rules (in category
order), the title choice, then one text rewrite per merged or split rule (merges first)."""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

import analyze
import audit
import checks
import evaluate
import incremental
import render
import scrape
import update
import website
from analyze import RunBudget, decision_hash
from incremental import UpdateRejected
from matching import FormerSlug, SlugRegistry
from test_incremental import World, _decision
from test_route_update import Repo

ROOT = Path(audit.__file__).parent
OPS = ("merge", "split", "rename", "move")


def _rules(world: World, category: str, *rules: tuple) -> None:
    """Rules as (titel, slug, [(ref, effekt), ...]), worded as a consolidation words them: the text of a version that
    introduces a rule is the decision's own."""
    by_ref = {d.ref: d for d in world.decisions()}
    regler = [{"titel": titel, "slug": slug, "vigtig": True, "note": None,
               "versioner": [{"ref": ref, "effekt": effekt,
                              "tekst": None if effekt == "indfoert" else by_ref[ref].tekst,
                              "kort": by_ref[ref].emne.lower(), "kort_regel": None, "dhash": decision_hash(by_ref[ref])}
                             for ref, effekt in versions]}
              for titel, slug, versions in rules]
    analyze._write_json(analyze.RULES_DIR / f"{category}.json", {
        "kategori": category, "version": analyze.CONSOLIDATE_VERSION, "input_hash": "x", "model": "opus",
        "regler": regler, "udeladt": [], "ikke_tildelt": []})


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A licence fee spread over okonomi and medlemskab, a rule on club transfers holding a referee requirement, an
    anti-doping rule among the competitions and a fee whose title is too short; consolidated."""
    w = World(tmp_path, monkeypatch)
    w.document("rep2010", "2010-03-01", _decision("Licensgebyr", "Licensgebyret er 200 kr. pr. løfter."))
    w.document("rep2015", "2015-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 250 kr. pr. løfter."),
               _decision("Startgebyr", "Startgebyret er 150 kr. pr. start ved stævner."))
    w.document("rep2016", "2016-03-01",
               _decision("Klubskifte", "Et klubskifte kræver tre måneders karantæne.", "medlemskab"))
    w.document("rep2018", "2018-03-01",
               _decision("Dommerkrav", "Hver klub skal stille med en dommer ved danske mesterskaber.", "medlemskab"),
               _decision("Antidopingkursus", "Alle landsholdsløftere skal gennemføre et antidopingkursus.",
                         "staevner"))
    w.document("rep2020", "2020-03-01",
               _decision("Licensgebyr", "Licensgebyret hæves til 300 kr. pr. løfter.", "medlemskab"),
               _decision("Klubskifte", "Karantænen ved klubskifte forkortes til en måned.", "medlemskab"))
    _rules(w, "okonomi", ("Licensgebyr", "licensgebyr", [("rep2010#1", "indfoert"), ("rep2015#1", "aendret")]),
           ("Startgebyr", "startgebyr", [("rep2015#2", "indfoert")]))
    _rules(w, "medlemskab", ("Licens for løftere", "licens-for-løftere", [("rep2020#1", "aendret")]),
           ("Klubskifte", "klubskifte", [("rep2016#1", "indfoert"), ("rep2018#1", "aendret"),
                                         ("rep2020#2", "aendret")]))
    _rules(w, "staevner", ("Antidopingkursus", "antidopingkursus", [("rep2018#2", "indfoert")]))
    w.consolidated()
    monkeypatch.setattr(scrape, "load_manifest", lambda: w.docs)
    monkeypatch.setattr(audit, "OPS_PATH", tmp_path / "regler_ops.json")
    monkeypatch.setattr(audit, "CACHE_DIR", tmp_path / "audit")
    monkeypatch.setattr(audit, "REPORT", tmp_path / "audit-report.md")
    monkeypatch.setattr(evaluate, "EVAL_DIR", tmp_path / "eval")  # no answer key: the report has no scores
    monkeypatch.setattr(render, "OUT_DIR", tmp_path / "regelsaet")
    monkeypatch.setattr(website, "OUT_DIR", tmp_path / "site")
    monkeypatch.setattr(update, "RUNS_LOG", tmp_path / "runs.jsonl")
    assert audit.unsettled(audit.Data.load()) == []
    return w


def _op(op: str, rules: list[str], refs: list[str], title: str | None = None, category: str | None = None,
        parts: tuple = ()) -> dict:
    return {"op": op, "rules": rules, "title": title, "category": category,
            "parts": [{"title": t, "refs": r} for t, r in parts], "reason": "Det er sådan.", "refs": refs}


CATEGORIES = ["medlemskab", "okonomi", "staevner"]  # those with rules, in the order they are called
MERGE = _op("merge", ["licens-for-løftere", "licensgebyr"], ["rep2020#1"])


def _split(second: str) -> dict:
    return _op("split", ["klubskifte"], ["rep2018#1"],
               parts=(("Klubskifte", ["rep2020#2", "rep2016#1"]), (second, ["rep2018#1"])))


MOVE = _op("move", ["antidopingkursus"], ["rep2018#2"], category="antidoping")
# The propose answers in call order: medlemskab, okonomi and staevner, two runs each.
PROPOSALS = (
    {"ops": [MERGE, _split("Dommerkrav for klubber")]},
    {"ops": [MERGE, _split("Klubbers dommerkrav")]},
    {"ops": [_op("rename", ["startgebyr"], ["rep2015#2"], title="Startgebyr ved stævner"),
             {**MERGE, "refs": ["rep2015#1"]}]},
    {"ops": [_op("rename", ["startgebyr"], ["rep2015#2"], title="Startgebyr for stævner"),
             _op("merge", ["licensgebyr", "licensgebyr-gammel"], ["rep2015#1"])]},
    {"ops": [MOVE]},
    {"ops": [MOVE, _op("rename", ["antidopingkursus"], ["rep2018#2"], title="Antidopingkurser")]},
)
# Agreed ops are numbered first, by kind: 1 merge, 2 split, 3 rename, 4 move; 5 is the rename only run 2 proposed.
TITLES = {"valg": [{"id": "2.2", "titel": "Klubbers dommerkrav"}, {"id": "3", "titel": "Startgebyr ved stævner"}]}


def _text(*refs: str) -> dict:
    return {"versioner": [{"ref": ref, "effekt": "aendret", "tekst": f"Samlet {ref}", "kort": f"kort {ref}",
                           "kort_regel": f"regel {ref}"} for ref in refs], "vigtig": False, "note": "Samlet.",
            "misfiled": []}


# The rewrites in call order: the merged licence fee, then the two parts of the split.
TEXTS = (_text("rep2010#1", "rep2015#1", "rep2020#1"), _text("rep2016#1", "rep2020#2"), _text("rep2018#1"))


def _stored(world: World) -> dict:
    return {path.name: path.read_bytes() for path in [*sorted(analyze.RULES_DIR.glob("*.json")), analyze.SLUGS_PATH]
            if path.exists()}


def _ops() -> dict:
    return json.loads(audit.OPS_PATH.read_text())


def _view(category: str, run: int = 1) -> audit.View:
    data = audit.Data.load()
    return next(v for v in audit.views(data, [category], audit.similar_rules(data)) if v.run == run)


# ---------------------------------------------------------------- candidates

def test_fragments_of_one_rule_are_found_across_categories(world):
    similar = audit.similar_rules(audit.Data.load())
    assert similar["licensgebyr"][0][0] == "licens-for-løftere"
    assert "antidopingkursus" not in dict(similar["licensgebyr"])
    view = _view("okonomi")
    assert "licens-for-løftere" in view.brief and "klubskifte" not in view.brief  # another category, in brief
    rules = json.loads(view.prompt.split("<regler>\n", 1)[1].split("\n</regler>", 1)[0])
    assert rules[0]["slug"] == "licensgebyr" and rules[0]["ligner"][0] == "licens-for-løftere"
    assert [v["ref"] for v in rules[0]["versioner"]] == ["rep2010#1", "rep2015#1"]
    assert _view("okonomi", 2).full == ("startgebyr", "licensgebyr")  # the second run reads them rotated


def test_the_threshold_is_the_highest_that_puts_enough_fragment_pairs_into_one_call():
    rows = [audit.Recall(t, None, share, 0, (0, 0)) for t, share in ((0.05, 1.0), (0.1, 0.96), (0.15, 0.9))]
    assert audit.choose_threshold(rows) == 0.1
    assert audit.choose_threshold(rows[2:]) is None


# ---------------------------------------------------------------- validation and agreement

@pytest.mark.parametrize(("raw", "why"), [
    (_op("merge", ["licensgebyr", "ukendt"], ["rep2020#1"]), "unknown slug ukendt"),
    (_op("merge", ["licensgebyr", "licens-for-løftere"], ["rep2010#1"]), "unknown ref rep2010#1"),
    (_op("split", ["klubskifte"], ["rep2018#1"], parts=(("A", ["rep2016#1"]), ("B", ["rep2018#1"]))),
     "the parts do not partition the versions of klubskifte (missing rep2020#2)"),
    (_op("split", ["klubskifte"], ["rep2018#1"], parts=(("A", ["rep2016#1", "rep2018#1"]),
                                                       ("B", ["rep2018#1", "rep2020#2"]))),
     "the parts do not partition the versions of klubskifte (twice rep2018#1)"),
    (_op("split", ["klubskifte"], ["rep2018#1"], parts=(("A", ["rep2016#1", "rep2020#2", "rep2018#1"]),)),
     "the parts do not partition the versions of klubskifte (fewer than two parts)"),
    (_op("split", ["klubskifte"], ["rep2018#1"], parts=(("A", ["rep2016#1", "rep2020#2"]),
                                                       ("B", ["rep2018#1", "rep2010#1"]))),
     "the parts name refs klubskifte does not hold: rep2010#1"),
    (_op("rename", ["klubskifte"], ["rep2016#1"], title="Klubskifte"), "a rename needs a new title"),
    (_op("move", ["klubskifte"], ["rep2016#1"], category="medlemskab"), "a move needs another category"),
    (_op("move", ["licensgebyr"], ["rep2020#1"], category="medlemskab"), "licensgebyr is not a rule of medlemskab"),
    (_op("rename", ["klubskifte"], [], title="Klubskifter"), "cites no decision"),
])
def test_code_rejects_ops_it_cannot_apply(world, raw, why):
    data = audit.Data.load()
    assert audit.validate(raw, _view("medlemskab"), data.located(), data.by_ref) == why


def test_a_merge_needs_a_rule_of_the_calls_category(world):
    data = audit.Data.load()
    view = audit.View("medlemskab", 1, ("klubskifte",), ("licensgebyr", "startgebyr"), frozenset({"rep2016#1"}), "")
    assert audit.validate(_op("merge", ["licensgebyr", "startgebyr"], ["rep2016#1"]), view, data.located(),
                          data.by_ref) == "a merge needs a rule of medlemskab"


def test_a_split_is_read_in_the_rules_order_whatever_the_answers_order(world):
    data = audit.Data.load()
    op = audit.validate(_split("Dommerkrav"), _view("medlemskab"), data.located(), data.by_ref)
    assert [part.refs for part in op.parts] == [("rep2016#1", "rep2020#2"), ("rep2018#1",)]


def test_ops_of_one_run_that_share_a_rule_conflict_and_are_not_applied():
    merge = audit.Op("merge", ("a", "b"))
    split = audit.Op("split", ("a",), parts=(audit.Part("x", ("a#1",)), audit.Part("y", ("a#2",))))
    rename, move = audit.Op("rename", ("c",), "C"), audit.Op("move", ("c",), category="master")
    proposals = [audit.Proposal(op, run, "okonomi", "", ("a#1",)) for run in (1, 2) for op in (merge, split, rename,
                                                                                               move)]
    proposed, rejected = audit.agree(proposals)
    # A rename and a move may go together; a merge or split shares its rule with no other op.
    assert [(p.op.kind, p.agreed) for p in proposed] == [("rename", True), ("move", True)]
    assert {(r.run, r.answer["op"]) for r in rejected} == {(1, "merge"), (1, "split"), (2, "merge"), (2, "split")}
    assert rejected[0].why == "conflicts with split on a in the same run"


def test_two_runs_must_propose_the_same_op(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES)
    assert audit.propose(CATEGORIES, 10, 1) == 0

    args = fake_claude.calls()
    models = [a[a.index("--model") + 1] for a in args]
    assert models == [audit.MODEL] * 7 and all(a[a.index("--effort") + 1] == "high" for a in args)
    assert [a[a.index("--system-prompt") + 1] for a in args] == [audit.PROPOSE_SYSTEM] * 6 + [audit.TITLE_SYSTEM]
    ops = _ops()
    assert ops["complete"] and ops["data"] == audit.data_fingerprint()
    assert [(e["id"], e["op"], e["rules"], e["agreed"]) for e in ops["ops"]] == [
        (1, "merge", ["licens-for-løftere", "licensgebyr"], True), (2, "split", ["klubskifte"], True),
        (3, "rename", ["startgebyr"], True), (4, "move", ["antidopingkursus"], True),
        (5, "rename", ["antidopingkursus"], False)]
    merge, split, rename, move, lone = ops["ops"]
    # Code keeps the rule with most versions; both runs gave no title, so it keeps its own.
    assert (merge["keeps"], merge["title"], merge["category"]) == ("licensgebyr", "Licensgebyr", None)
    assert [(p["run"], p["call"]) for p in merge["proposals"]] == [(1, "medlemskab"), (1, "okonomi"), (2, "medlemskab")]
    assert split["parts"] == [{"title": "Klubskifte", "refs": ["rep2016#1", "rep2020#2"], "keeps": True},
                              {"title": "Klubbers dommerkrav", "refs": ["rep2018#1"], "keeps": False}]
    assert rename["title"] == "Startgebyr ved stævner" and move["category"] == "antidoping"
    assert lone["applied"] is None and "title" not in lone
    (rejected,) = ops["rejected"]
    assert (rejected["run"], rejected["call"], rejected["why"]) == (2, "okonomi", "unknown slug licensgebyr-gammel")
    # The third call chooses only where the runs differ, and a rename always, between their titles and the old one.
    asked = json.loads(fake_claude.prompts()[-1].split("<regler>\n", 1)[1].split("\n</regler>", 1)[0])
    assert [(item["id"], item["titel"], item["muligheder"]) for item in asked] == [
        ("2.2", None, ["Dommerkrav for klubber", "Klubbers dommerkrav"]),
        ("3", "Startgebyr", ["Startgebyr ved stævner", "Startgebyr for stævner", "Startgebyr"])]
    report = audit.REPORT.read_text()
    assert report.startswith(f"{audit.UNFINISHED}\n") and "**Unfinished**: 4 agreed ops, not applied yet" in report
    assert "## Not agreed: 1" in report and "Cost of this audit at list price" in report


def test_kept_answers_are_never_paid_twice(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES)
    audit.propose(CATEGORIES, 10, 1)
    first = _ops()
    audit.propose(CATEGORIES, 10, 1)
    assert fake_claude.invocations("call") == 7
    assert {k: v for k, v in _ops().items() if k != "proposed"} == {k: v for k, v in first.items() if k != "proposed"}


def test_a_cut_off_proposal_is_incomplete_and_cannot_be_applied(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES)
    assert audit.propose(CATEGORIES, 0, 1) == 1  # the limit stops every call
    assert fake_claude.invocations("call") == 0 and not _ops()["complete"]
    with pytest.raises(SystemExit, match="incomplete"):
        audit.apply(10, 1)


def test_runs_that_differ_on_the_rules_the_parts_or_the_target_do_not_agree():
    def part(title: str, *refs: str) -> audit.Part:
        return audit.Part(title, refs)

    runs = {1: [audit.Op("merge", ("a", "b")),
                audit.Op("split", ("s",), parts=(part("x", "s#1"), part("y", "s#2", "s#3"))),
                audit.Op("move", ("m",), category="master")],
            2: [audit.Op("merge", ("a", "b", "c")),
                audit.Op("split", ("s",), parts=(part("x", "s#1", "s#2"), part("y", "s#3"))),
                audit.Op("move", ("m",), category="landshold")]}
    proposed, rejected = audit.agree([audit.Proposal(op, run, "okonomi", "", ("s#1",))
                                      for run, ops in runs.items() for op in ops])
    assert rejected == [] and len(proposed) == 6 and not any(p.agreed for p in proposed)
    # The same partition is agreed, whatever each run calls its parts (the third call chooses).
    same = [audit.Proposal(audit.Op("split", ("s",), parts=(part(x, "s#1"), part(y, "s#2", "s#3"))), run, "okonomi",
                           "", ("s#1",)) for run, (x, y) in ((1, ("x", "y")), (2, ("z", "w")))]
    assert [p.agreed for p in audit.agree(same)[0]] == [True]


def test_a_changed_question_is_asked_again_and_the_same_one_is_not(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES, PROPOSALS[0], PROPOSALS[1])
    audit.propose(CATEGORIES, 10, 1)
    medlemskab = world.stored("medlemskab")
    medlemskab["regler"][1]["titel"] = "Klubskifte og karantæne"
    analyze._write_json(analyze.RULES_DIR / "medlemskab.json", medlemskab)
    assert audit.propose(CATEGORIES, 10, 1) == 0
    asked = fake_claude.prompts()[7:]  # only medlemskab's two runs show the changed title
    assert len(asked) == 2 and all("Klubskifte og karantæne" in prompt for prompt in asked)


def test_a_cut_off_audit_keeps_what_it_paid_for_says_how_to_go_on_and_counts_every_call(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES)
    assert audit.propose(CATEGORIES, 0.5, 1) == 1  # each fake call costs 0.25: two calls, then the limit
    assert sorted(path.stem for path in audit.CACHE_DIR.glob("*.json")) == ["propose-medlemskab-1",
                                                                          "propose-medlemskab-2"]
    report = audit.REPORT.read_text()
    assert report.startswith(f"{audit.UNFINISHED}\n") and audit.CONTINUE in report
    assert "left propose okonomi run 1, propose okonomi run 2, propose staevner run 1" in report
    (line,) = [json.loads(line) for line in update.RUNS_LOG.read_text().splitlines()]
    assert (line["command"], line["audit"], line["steps"]["propose"]["cost_usd"]) == (
        "audit propose", _ops()["audit"], 0.5)

    assert audit.propose(CATEGORIES, 10, 1) == 0
    assert fake_claude.invocations("call") == 7  # the kept answers were not asked again
    assert "every call counted (failed attempts and rejected answers too): propose 1.75 USD." in \
        audit.REPORT.read_text()


def test_no_call_starts_once_the_time_budget_is_spent(world, fake_claude):
    assert audit.propose(CATEGORIES, 10, 1, minutes=0) == 1
    assert fake_claude.invocations("call") == 0 and not _ops()["complete"]
    assert audit.REPORT.read_text().startswith(audit.UNFINISHED)


# ---------------------------------------------------------------- apply

@pytest.fixture
def proposed(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES, *TEXTS)
    audit.propose(CATEGORIES, 10, 1)
    return world


def test_apply_merges_splits_moves_and_renames_and_keeps_every_link(proposed, fake_claude):
    world = proposed
    old_slugs = {rule["slug"] for rule in analyze.load_rules()}
    assert audit.apply(10, 1) == 0
    assert [a[a.index("--system-prompt") + 1] for a in fake_claude.calls()[7:]] == [audit.AUDIT_UPDATE_SYSTEM] * 3

    licens = world.rule("okonomi", "licensgebyr")  # most versions: it keeps its slug
    assert [v["ref"] for v in licens["versioner"]] == ["rep2010#1", "rep2015#1", "rep2020#1"]
    assert licens["versioner"][2]["tekst"] == "Samlet rep2020#1" and licens["note"] == "Samlet."
    assert licens["vigtig"] is True  # kept by code, whatever the answer says
    assert licens["versioner"][0]["updated"]["prompt"] == analyze.prompt_hash(audit.AUDIT_UPDATE_SYSTEM,
                                                                            incremental.UPDATE_SCHEMA)
    registry = analyze.load_slugs()
    assert registry.aliases["licens-for-løftere"].to == "licensgebyr"
    assert json.loads(analyze.SLUGS_PATH.read_text())["aliases"]["licens-for-løftere"] == {
        "to": "licensgebyr", "category": "medlemskab", "title": "Licens for løftere", "refs": ["rep2020#1"]}
    # The split's largest part keeps the slug; the other part gets a new one, from its title.
    medlemskab = world.stored("medlemskab")["regler"]
    assert [(r["slug"], r["titel"], [v["ref"] for v in r["versioner"]]) for r in medlemskab] == [
        ("klubskifte", "Klubskifte", ["rep2016#1", "rep2020#2"]),
        ("klubbers-dommerkrav", "Klubbers dommerkrav", ["rep2018#1"])]
    # The move changes the file, the rename only the title.
    assert world.stored("staevner")["regler"] == []
    assert [r["slug"] for r in world.stored("antidoping")["regler"]] == ["antidopingkursus"]
    assert world.rule("okonomi", "startgebyr")["titel"] == "Startgebyr ved stævner"
    assert world.rule("okonomi", "startgebyr")["versioner"][0].get("updated") is None  # renamed, not rewritten

    # No work for update.py, no check error, and every old slug leads to a rule.
    assert update.pending_work(world.docs).categories == frozenset()
    assert analyze.consolidation_todo(world.decisions(), {d.id: d.organ_label for d in world.docs}) == []
    assert checks.errors(checks.find_problems(checks.Data.load(world.docs))) == []
    live, targets = {rule["slug"] for rule in analyze.load_rules()}, registry.targets()
    assert all(slug in live or targets.get(slug) in live for slug in old_slugs)
    step = incremental.consolidate(world.docs, world.decisions(), incremental.Settings(workers=1), RunBudget(),
                                   known=incremental.known_inputs(world.decisions(), incremental.RuleBook.load(),
                                                                  world.docs))
    assert step.calls == 0 and fake_claude.invocations("call") == 10

    ops = _ops()
    assert ops["applied"]["data"] == audit.data_fingerprint()
    assert [e["applied"] for e in ops["ops"]] == [
        {"slug": "licensgebyr", "aliases": ["licens-for-løftere"], "category": "okonomi", "title": "Licensgebyr"},
        {"slugs": ["klubskifte", "klubbers-dommerkrav"], "category": "medlemskab"},
        {"title": "Startgebyr ved stævner", "from": "Startgebyr"},
        {"category": "antidoping", "from": "staevner"}, None]
    report = audit.REPORT.read_text()
    assert "**Review**" in report and "## In force by year" in report and "Licensgebyr (`licens-for-løftere, " \
        "licensgebyr → licensgebyr`) | 2020–" in report
    assert "Klubbers dommerkrav" in (render.OUT_DIR / "regler" / "medlemskab.md").read_text()
    with pytest.raises(SystemExit, match="applied already"):
        audit.apply(10, 1)
    with pytest.raises(SystemExit, match="was applied to exactly these rules"):  # e.g. a rerun on the audit's branch
        audit.propose(CATEGORIES, 10, 1)
    assert fake_claude.invocations("call") == 10


def test_a_merge_keeps_the_rule_with_most_versions_then_the_oldest(world):
    data = audit.Data.load()
    located = data.located()
    rules = [located["startgebyr"], located["licens-for-løftere"]]  # one version each: 2015 is older than 2020
    assert audit.survivor(rules, data.by_ref) == 0
    assert audit.survivor([located["licens-for-løftere"], located["klubskifte"]], data.by_ref) == 1


def test_the_rewrite_may_change_only_texts_and_never_the_versions_or_their_order(world):
    data = audit.Data.load()
    rule = {"titel": "Gebyrer", "slug": "gebyrer", "vigtig": True, "note": None,
            "versioner": [v for slug in ("licensgebyr", "startgebyr") for v in data.located()[slug][1]["versioner"]]}
    rule["versioner"] = incremental.ordered_versions(rule["versioner"], data.by_ref)
    u = audit.rule_update("okonomi", rule)
    assert [v["ref"] for v in audit.rewritten(u, _text("rep2010#1", "rep2015#1", "rep2015#2"), {}, data.by_ref)] == [
        "rep2010#1", "rep2015#1", "rep2015#2"]
    with pytest.raises(UpdateRejected, match="another order"):  # the same day: Claude's order would stand
        audit.rewritten(u, _text("rep2010#1", "rep2015#2", "rep2015#1"), {}, data.by_ref)
    with pytest.raises(UpdateRejected, match="instead of"):
        audit.rewritten(u, _text("rep2010#1", "rep2015#1"), {}, data.by_ref)


def test_a_rejected_rewrite_writes_nothing_and_the_rest_is_not_paid_again(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES, TEXTS[0], _text("rep2016#1"), _text("rep2016#1"), TEXTS[2], TEXTS[1])
    audit.propose(CATEGORIES, 10, 1)
    before, ops = _stored(world), audit.OPS_PATH.read_bytes()
    assert audit.apply(10, 1) == 1  # klubskifte's rewrite left out rep2020#2, twice
    assert _stored(world) == before and audit.OPS_PATH.read_bytes() == ops
    assert "**Not applied**: no accepted rewrite of klubskifte yet" in audit.REPORT.read_text()

    assert audit.apply(10, 1) == 0
    assert fake_claude.invocations("call") == 12  # only klubskifte's rewrite was asked again
    # The cost counts the rejected answers too: four calls in the first apply, one in the second.
    assert "propose 1.75 USD, apply 1.25 USD." in audit.REPORT.read_text()
    assert world.rule("medlemskab", "klubskifte")["versioner"][1]["tekst"] == "Samlet rep2020#2"




@pytest.mark.parametrize("failing", [(audit, "score_section"), (render, "build_pages"), (website, "page_html")])
def test_a_failure_before_the_writes_writes_nothing(proposed, monkeypatch, failing):
    def fail(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(*failing, fail)
    before, ops = _stored(proposed), audit.OPS_PATH.read_bytes()
    assert audit.apply(10, 1) == 1
    assert _stored(proposed) == before and audit.OPS_PATH.read_bytes() == ops
    assert not render.OUT_DIR.exists() and not website.OUT_DIR.exists()
    report = audit.REPORT.read_text()
    assert report.startswith(f"{audit.UNFINISHED}\n") and "**Not applied**: RuntimeError: boom" in report


def _recorded(monkeypatch) -> list[tuple[str, bytes]]:
    """What apply replaces, in order, besides the answers it keeps."""
    written: list[tuple[str, bytes]] = []
    replace = audit._replace

    def record(path: Path, content: bytes) -> None:
        if not path.name.startswith(("text-", "propose-", "titles")):
            written.append((path.name, content))
        replace(path, content)

    monkeypatch.setattr(audit, "_replace", record)
    return written


def test_apply_replaces_the_slugs_then_the_rule_files_then_the_ops_file(proposed, monkeypatch):
    written = _recorded(monkeypatch)
    assert audit.apply(10, 1) == 0
    assert [name for name, _ in written] == ["slugs.json", "antidoping.json", "medlemskab.json", "okonomi.json",
                                             "staevner.json", "regler_ops.json", "audit-report.md"]
    assert not list(proposed.root.rglob(".*.tmp"))


def test_a_slug_a_split_takes_up_again_stays_taken_until_the_rules_hold_it(world, fake_claude, monkeypatch):
    # A rule once split off and merged back: its slug leads to klubskifte, which holds its decision.
    analyze._save_slugs(SlugRegistry.of({"dommerkrav": FormerSlug("medlemskab", "Dommerkrav", frozenset({"rep2018#1"}),
                                                                  "klubskifte")}))
    fake_claude.answers(*PROPOSALS, TITLES, *TEXTS)
    audit.propose(CATEGORIES, 10, 1)
    written = _recorded(monkeypatch)
    assert audit.apply(10, 1) == 0
    assert [name for name, _ in written][:2] == ["slugs.json", "antidoping.json"] and written[5][0] == "slugs.json"
    assert "dommerkrav" in json.loads(written[0][1])["aliases"]  # until the rule files hold it
    assert "dommerkrav" not in analyze.load_slugs().former()
    assert [r["slug"] for r in world.stored("medlemskab")["regler"]] == ["klubskifte", "dommerkrav"]


def test_a_rejected_rewrite_is_not_asked_again_once_the_budget_is_spent(world, fake_claude, caplog):
    fake_claude.answers(*PROPOSALS, TITLES, _text("rep2010#1"))
    audit.propose(CATEGORIES, 10, 1)
    assert audit.apply(0.25, 1) == 1  # the first rewrite is rejected, and the 0.25 it cost is the limit
    assert fake_claude.invocations("call") == 8
    assert "not asked again because the cost limit of 0.25 USD is reached" in caplog.text
    assert "no accepted rewrite of licensgebyr, klubskifte, klubbers-dommerkrav yet" in audit.REPORT.read_text()


def test_apply_keeps_only_this_audits_answers(proposed):
    (audit.CACHE_DIR / "text-an-earlier-audits-rule.json").write_text("{}")
    assert audit.apply(10, 1) == 0
    assert sorted(path.stem for path in audit.CACHE_DIR.glob("*.json")) == [
        *(f"propose-{c}-{run}" for c in ("medlemskab", "okonomi", "staevner") for run in (1, 2)),
        "text-klubbers-dommerkrav", "text-klubskifte", "text-licensgebyr", "titles"]


def test_ops_proposed_for_other_rules_are_not_applied(proposed):
    rules = proposed.stored("okonomi")
    analyze._write_json(analyze.RULES_DIR / "okonomi.json", {**rules, "regler": rules["regler"][::-1]})
    with pytest.raises(SystemExit, match="changed since the ops were proposed"):
        audit.apply(10, 1)


def test_an_audit_waits_for_decisions_update_py_has_not_filed(world, fake_claude):
    world.document("rep2024", "2024-03-01", _decision("Licensgebyr", "Licensgebyret hæves til 350 kr. pr. løfter."))
    with pytest.raises(SystemExit, match="decisions to file in okonomi"):
        audit.propose(["okonomi"], 10, 1)
    assert fake_claude.calls() == []


# ---------------------------------------------------------------- the workflow

AUDIT_SCRIPT = ROOT / ".github" / "scripts" / "route-audit.sh"
TODAY = "2026-10-09"


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text())


def _job(workflow: dict) -> dict:
    (job,) = workflow["jobs"].values()
    return job


def _step(job: dict, found: str) -> dict:
    return next(step for step in job["steps"] if found in (step.get("name", ""), step.get("id", "")))


def _script(repo: Repo, mode: str, branch: str = "main", today: str = TODAY, data: str | None = None,
            report: str | None = None, files: dict[str, str | None] | None = None) -> subprocess.CompletedProcess:
    """route-audit.sh in a fresh checkout of `branch`, after audit.py wrote `data`, `report` and `files` (None:
    removed)."""
    work = repo.clone(branch)
    if data is not None:
        (work / "data.json").write_text(data + "\n")
    if report is not None:
        (work / "audit-report.md").write_text(report)
    for name, content in (files or {}).items():
        if content is None:
            (work / name).unlink()
        else:
            (work / name).parent.mkdir(parents=True, exist_ok=True)
            (work / name).write_text(content)
    return subprocess.run(["bash", str(AUDIT_SCRIPT), mode], cwd=work, env={**repo.env, "AUDIT_TODAY": today},
                          capture_output=True, text=True, check=False)


@pytest.fixture
def repo(tmp_path) -> Repo:
    repo = Repo(tmp_path)
    repo.commit_elsewhere("main", ".gitignore", "run-report.md\naudit-report.md\n")
    return repo


def test_the_audit_workflow_is_manual_reviewed_and_pinned_like_the_monthly_run():
    audit_yml, update_yml = _workflow("audit.yml"), _workflow("update.yml")
    assert set(audit_yml.get("on", audit_yml.get(True))) == {"workflow_dispatch"}  # PyYAML reads `on` as True
    job = _job(audit_yml)
    assert job["env"]["CLAUDE_CODE_VERSION"] == _job(update_yml)["env"]["CLAUDE_CODE_VERSION"]
    # Its own concurrency group: a queued audit never cancels a pending monthly update.
    assert audit_yml["concurrency"]["group"] != update_yml["concurrency"]["group"]
    names = [step.get("name", step.get("uses")) for step in job["steps"]]
    check, smoke, run = (names.index(name) for name in ("Check that no update runs and no other audit is open",
                                                          "Check Claude runs on the subscription", "Propose and apply"))
    assert check < smoke < run  # nothing is paid before the checks
    assert "gh run list --workflow update.yml" in job["steps"][check]["run"]
    assert "route-audit.sh check" in job["steps"][check]["run"]
    # The result is routed to review whatever the audit step ended with, timed out included, and never published.
    route = _step(job, "Send the result to a pull request")
    assert route["run"] == "bash .github/scripts/route-audit.sh route"
    assert route["if"] == "${{ !cancelled() && steps.audit.outcome != 'skipped' }}"
    assert not any("route-update.sh" in step.get("run", "") or "git push" in step.get("run", "")
                   for step in job["steps"])
    # The time budget, a call still running at its end and the routing fit into the job.
    budget = int(job["env"]["TIME_BUDGET"])
    assert budget == audit.DEFAULT_TIME_BUDGET
    assert budget + audit.PROPOSE_TIMEOUT / 60 < _step(job, "audit")["timeout-minutes"] < job["timeout-minutes"] - 5


@pytest.mark.parametrize(("propose_code", "budget", "applied", "code"), [
    (0, "75", True, 0), (1, "75", False, 1), (0, "0", False, 1)])
def test_the_workflow_applies_a_complete_proposal_within_the_time_left(tmp_path, propose_code, budget, applied, code):
    step = _step(_job(_workflow("audit.yml")), "audit")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(f'#!/bin/sh\necho "$*" >> {tmp_path}/uv.log\n'
                                f'[ "$3" = propose ] && exit "$PROPOSE_CODE"\nexit 0\n')
    (bin_dir / "uv").chmod(0o755)
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    env = {"PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin", "MAX_COST": "25", "CATEGORIES": "okonomi,dommere",
           "TIME_BUDGET": budget, "GITHUB_OUTPUT": str(tmp_path / "output"), "PROPOSE_CODE": str(propose_code)}
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)], env=env, check=True)  # as GitHub
    calls = (tmp_path / "uv.log").read_text().splitlines()
    assert calls[0] == f"run audit.py propose --max-cost 25 --time-budget {budget} --categories okonomi,dommere"
    assert calls[1:] == (["run audit.py apply --max-cost 25 --time-budget 75"] if applied else [])
    assert (tmp_path / "output").read_text() == f"code={code}\n"


def test_a_new_audit_waits_for_an_open_one_and_for_the_monthly_update(repo):
    assert _script(repo, "check").returncode == 0
    month_end = _script(repo, "check", today="2026-10-30")
    assert month_end.returncode == 1 and "The month ends within two days" in month_end.stderr
    assert _script(repo, "check", today="2026-10-29").returncode == 0
    repo.commit_elsewhere(f"auto/audit-{TODAY}", "answers.json", "{}\n")  # an audit cut off earlier today
    again = _script(repo, "check")
    assert again.returncode == 1 and f"An audit is open on auto/audit-{TODAY}" in again.stderr
    assert _script(repo, "check", branch=f"auto/audit-{TODAY}").returncode == 0  # a run that finishes it


@pytest.mark.skipif(subprocess.run(["which", "jq"], capture_output=True).returncode != 0,
                    reason="the fake gh applies --jq with jq")
def test_an_audit_goes_to_a_pull_request_and_never_to_main(repo):
    main, branch = repo.git("rev-parse", "main"), f"auto/audit-{TODAY}"
    cut_off = _script(repo, "route", data='{"audited": 1}', report=f"{audit.UNFINISHED}\n# Rule audit\n")
    assert cut_off.returncode == 0, cut_off.stderr
    assert repo.git("rev-parse", "main") == main and repo.file(branch) == '{"audited": 1}'
    (pr,) = repo.prs()
    assert (pr["headRefName"], pr["baseRefName"], pr["body"]) == (branch, "main", f"{audit.UNFINISHED}\n# Rule audit\n")
    assert repo.gh_calls()[-1]["args"][-3] == f"Rule audit {TODAY} (unfinished)"
    assert "audit-report.md" not in repo.git("ls-tree", "--name-only", branch)

    # A run on the audit's branch finishes it: it commits there and updates its pull request.
    first = repo.git("rev-parse", branch)
    finished = _script(repo, "route", branch=branch, today="2026-10-10", data='{"audited": 2}',
                       report=f"{audit.APPLIED}\n# Finished\n")
    assert finished.returncode == 0, finished.stderr
    assert repo.git("rev-parse", f"{branch}~1") == first and repo.file(branch) == '{"audited": 2}'
    (pr,) = repo.prs()
    assert (pr["baseRefName"], pr["body"]) == ("main", f"{audit.APPLIED}\n# Finished\n")
    edited = repo.gh_calls()[-1]["args"]
    assert edited[edited.index("--title") + 1] == f"Rule audit {TODAY}" and repo.git("rev-parse", "main") == main


def test_a_new_audit_does_not_start_when_origin_cannot_be_asked(repo):
    work = repo.clone("main")
    repo.git("remote", "set-url", "origin", str(repo.root / "gone.git"), cwd=work)
    result = subprocess.run(["bash", str(AUDIT_SCRIPT), "check"], cwd=work, env={**repo.env, "AUDIT_TODAY": TODAY},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "Cannot list origin's audit branches" in result.stderr


@pytest.mark.skipif(subprocess.run(["which", "jq"], capture_output=True).returncode != 0,
                    reason="the fake gh applies --jq with jq")
def test_an_unfinished_audit_commits_what_it_paid_for_and_no_rule(repo):
    work = repo.clone("main")
    for name, content in (("data/regler/okonomi.json", "{}\n"), ("data/slugs.json", "{}\n"),
                          ("regelsaet/2026.md", "# 2026\n")):
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(content)
    repo.git("add", "-A", cwd=work)
    repo.git("-c", "user.name=Someone", "-c", "user.email=someone@example.com", "commit", "-q", "-m", "data", cwd=work)
    repo.git("push", "-q", "origin", "HEAD:refs/heads/main", cwd=work)
    branch = f"auto/audit-{TODAY}"
    half = {"data/regler/okonomi.json": '{"half": 1}\n', "data/regler/antidoping.json": "{}\n",
            "data/slugs.json": '{"half": 1}\n', "regelsaet/2026.md": None,
            "data/regler_ops.json": '{"applied": {"time": "now"}}\n', "data/audit/propose-okonomi-1.json": "{}\n"}
    result = _script(repo, "route", report=f"{audit.UNFINISHED}\n# Cut off\n", files=half)
    assert result.returncode == 0, result.stderr
    committed = set(repo.git("ls-tree", "-r", "--name-only", branch).split())
    assert {"data/audit/propose-okonomi-1.json", "regelsaet/2026.md"} <= committed
    assert not {"data/regler/antidoping.json", "data/regler_ops.json"} & committed  # its ops file claims applied
    assert repo.file(branch, "data/regler/okonomi.json") == "{}" and repo.file(branch, "data/slugs.json") == "{}"
