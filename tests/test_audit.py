"""The audit (styrke/audit.py) against the fake `claude` from conftest.py.

Calls run with one worker, so the fake answers them in order: two propose runs per category with rules (in category
order), the title choice, then one text rewrite per merged or split rule (merges first)."""

import json
from pathlib import Path

import pytest
from conftest import extracted, rule, section

from styrke import analyze, audit, checks, claude, evaluate, incremental, render, scrape, update, website
from styrke.incremental import UpdateRejected
from styrke.matching import FormerSlug, SlugRegistry


@pytest.fixture
def world(world, tmp_path, monkeypatch):
    """A licence fee spread over okonomi and medlemskab, a rule on club transfers holding a referee requirement, an
    anti-doping rule among the competitions and a fee whose title is too short; consolidated."""
    world.document("rep2010", "2010-03-01", extracted("Licensgebyr", "Licensgebyret er 200 kr. pr. løfter."))
    world.document("rep2015", "2015-03-01", extracted("Licensgebyr", "Licensgebyret hæves til 250 kr. pr. løfter."),
                   extracted("Startgebyr", "Startgebyret er 150 kr. pr. start ved stævner."))
    world.document("rep2016", "2016-03-01",
                   extracted("Klubskifte", "Et klubskifte kræver tre måneders karantæne.", "medlemskab"))
    world.document("rep2018", "2018-03-01",
                   extracted("Dommerkrav", "Hver klub skal stille med en dommer ved danske mesterskaber.",
                             "medlemskab"),
                   extracted("Antidopingkursus", "Alle landsholdsløftere skal gennemføre et antidopingkursus.",
                             "staevner"))
    world.document("rep2020", "2020-03-01",
                   extracted("Licensgebyr", "Licensgebyret hæves til 300 kr. pr. løfter.", "medlemskab"),
                   extracted("Klubskifte", "Karantænen ved klubskifte forkortes til en måned.", "medlemskab"))
    world.rules("okonomi", rule("Licensgebyr", "licensgebyr", "rep2010#1", ("rep2015#1", "aendret")),
                rule("Startgebyr", "startgebyr", "rep2015#2"))
    world.rules("medlemskab", rule("Licens for løftere", "licens-for-løftere", ("rep2020#1", "aendret")),
                rule("Klubskifte", "klubskifte", "rep2016#1", ("rep2018#1", "aendret"), ("rep2020#2", "aendret")))
    world.rules("staevner", rule("Antidopingkursus", "antidopingkursus", "rep2018#2"))
    world.consolidated()
    monkeypatch.setattr(scrape, "load_manifest", lambda: world.docs)
    monkeypatch.setattr(audit, "OPS_PATH", tmp_path / "regler_ops.json")
    monkeypatch.setattr(audit, "CACHE_DIR", tmp_path / "audit")
    monkeypatch.setattr(audit, "REPORT", tmp_path / "audit-report.md")
    monkeypatch.setattr(evaluate, "EVAL_DIR", tmp_path / "eval")  # no answer key: the report has no scores
    monkeypatch.setattr(render, "OUT_DIR", tmp_path / "regelsaet")
    monkeypatch.setattr(website, "OUT_DIR", tmp_path / "site")
    monkeypatch.setattr(update, "RUNS_LOG", tmp_path / "runs.jsonl")
    assert audit.unsettled(audit.Data.load()) == []
    return world


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


def _stored(world) -> dict:
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
    rules = section(view.prompt, "regler")
    assert rules[0]["slug"] == "licensgebyr" and rules[0]["ligner"][0] == "licens-for-løftere"
    assert [v["ref"] for v in rules[0]["versioner"]] == ["rep2010#1", "rep2015#1"]
    assert _view("okonomi", 2).full == ("startgebyr", "licensgebyr")  # the second run reads them rotated


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

    assert fake_claude.option("--model") == [audit.MODEL] * 7 and set(fake_claude.option("--effort")) == {"high"}
    assert fake_claude.option("--system-prompt") == [audit.PROPOSE_SYSTEM] * 6 + [audit.TITLE_SYSTEM]
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
    asked = section(fake_claude.prompts()[-1], "regler")
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
    assert fake_claude.option("--system-prompt")[7:] == [audit.AUDIT_UPDATE_SYSTEM] * 3

    licens = world.rule("okonomi", "licensgebyr")  # most versions: it keeps its slug
    assert [v["ref"] for v in licens["versioner"]] == ["rep2010#1", "rep2015#1", "rep2020#1"]
    assert licens["versioner"][2]["tekst"] == "Samlet rep2020#1" and licens["note"] == "Samlet."
    assert licens["vigtig"] is True  # kept by code, whatever the answer says
    assert licens["versioner"][0]["updated"]["prompt"] == claude.prompt_hash(audit.AUDIT_UPDATE_SYSTEM,
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

    # No work for styrke/update.py, no check error, and every old slug leads to a rule.
    assert update.pending_work(world.docs).categories == frozenset()
    assert analyze.consolidation_todo(world.decisions(), {d.id: d.organ_label for d in world.docs}) == []
    assert checks.errors(checks.find_problems(checks.Data.load(world.docs))) == []
    live, targets = {rule["slug"] for rule in analyze.load_rules()}, registry.targets()
    assert all(slug in live or targets.get(slug) in live for slug in old_slugs)
    assert world.consolidate(world.known()).calls == 0 and fake_claude.invocations("call") == 10

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


@pytest.mark.parametrize("failing", [(evaluate, "score_section"), (render, "build_pages"), (website, "page_html")])
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
    replace = scrape.write_atomic

    def record(path: Path, content: bytes) -> None:
        if not path.name.startswith(("text-", "propose-", "titles")):
            written.append((path.name, content))
        replace(path, content)

    monkeypatch.setattr(scrape, "write_atomic", record)
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
    world.document("rep2024", "2024-03-01", extracted("Licensgebyr", "Licensgebyret hæves til 350 kr. pr. løfter."))
    with pytest.raises(SystemExit, match="decisions to file in okonomi"):
        audit.propose(["okonomi"], 10, 1)
    assert fake_claude.calls() == []
