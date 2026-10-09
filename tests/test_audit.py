"""The audit (audit.py) against the fake `claude` from conftest.py.

Calls run with one worker, so the fake answers them in order: two propose runs per category with rules (in category
order), the title choice, then one text rewrite per merged or split rule (merges first)."""

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

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
    assert audit.unsettled(audit.Data.load()) == []
    return w


def _op(op: str, rules: list[str], refs: list[str], title: str | None = None, category: str | None = None,
        parts: tuple = ()) -> dict:
    return {"op": op, "rules": rules, "title": title, "category": category,
            "parts": [{"title": t, "refs": r} for t, r in parts], "reason": "Det er sådan.", "refs": refs}


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
    assert audit.propose(["medlemskab", "okonomi", "staevner"], 10, 1) == 0

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
    assert "**Proposed**: 4 agreed ops" in report and "## Not agreed: 1" in report


def test_kept_answers_are_never_paid_twice(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES)
    audit.propose(["medlemskab", "okonomi", "staevner"], 10, 1)
    first = _ops()
    audit.propose(["medlemskab", "okonomi", "staevner"], 10, 1)
    assert fake_claude.invocations("call") == 7
    assert {k: v for k, v in _ops().items() if k != "proposed"} == {k: v for k, v in first.items() if k != "proposed"}


def test_a_cut_off_proposal_is_incomplete_and_cannot_be_applied(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES)
    assert audit.propose(["medlemskab", "okonomi", "staevner"], 0, 1) == 1  # the limit stops every call
    assert fake_claude.invocations("call") == 0 and not _ops()["complete"]
    with pytest.raises(SystemExit, match="incomplete"):
        audit.apply(10, 1)


# ---------------------------------------------------------------- apply

@pytest.fixture
def proposed(world, fake_claude):
    fake_claude.answers(*PROPOSALS, TITLES, *TEXTS)
    audit.propose(["medlemskab", "okonomi", "staevner"], 10, 1)
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
        audit.propose(["medlemskab", "okonomi", "staevner"], 10, 1)
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
    audit.propose(["medlemskab", "okonomi", "staevner"], 10, 1)
    before, ops = _stored(world), audit.OPS_PATH.read_bytes()
    assert audit.apply(10, 1) == 1  # klubskifte's rewrite left out rep2020#2, twice
    assert _stored(world) == before and audit.OPS_PATH.read_bytes() == ops
    assert "**Not applied**: no accepted rewrite of klubskifte yet" in audit.REPORT.read_text()

    assert audit.apply(10, 1) == 0
    assert fake_claude.invocations("call") == 12  # only klubskifte's rewrite was asked again
    assert world.rule("medlemskab", "klubskifte")["versioner"][1]["tekst"] == "Samlet rep2020#2"


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


def _route(repo: Repo, data: str, report: str, branch: str = "main") -> subprocess.CompletedProcess:
    work = repo.clone(branch)
    (work / "data.json").write_text(data + "\n")
    (work / "audit-report.md").write_text(report)
    return subprocess.run(["bash", str(AUDIT_SCRIPT)], cwd=work, env=repo.env, capture_output=True, text=True,
                          check=False)


@pytest.mark.skipif(subprocess.run(["which", "jq"], capture_output=True).returncode != 0,
                    reason="the fake gh applies --jq with jq")
def test_an_audit_goes_to_a_pull_request_and_never_to_main(tmp_path):
    repo = Repo(tmp_path)
    repo.commit_elsewhere("main", ".gitignore", "run-report.md\naudit-report.md\n")
    main = repo.git("rev-parse", "main")
    branch = f"auto/audit-{datetime.now(timezone.utc).date().isoformat()}"

    result = _route(repo, '{"audited": 1}', "# Rule audit\n")
    assert result.returncode == 0, result.stderr
    assert repo.git("rev-parse", "main") == main and repo.file(branch) == '{"audited": 1}'
    (pr,) = repo.prs()
    assert (pr["headRefName"], pr["baseRefName"], pr["body"]) == (branch, "main", "# Rule audit\n")
    assert "audit-report.md" not in repo.git("ls-tree", "--name-only", branch)

    # A run on the audit's branch (finishing a cut-off audit) commits there and updates its pull request.
    first = repo.git("rev-parse", branch)
    again = _route(repo, '{"audited": 2}', "# Finished\n", branch=branch)
    assert again.returncode == 0, again.stderr
    assert repo.git("rev-parse", f"{branch}~1") == first and repo.file(branch) == '{"audited": 2}'
    (pr,) = repo.prs()
    assert (pr["baseRefName"], pr["body"]) == ("main", "# Finished\n") and repo.git("rev-parse", "main") == main


def test_the_audit_workflow_proposes_applies_and_routes_to_review_with_the_monthly_runs_claude():
    audit_yml = (ROOT / ".github" / "workflows" / "audit.yml").read_text()
    update_yml = (ROOT / ".github" / "workflows" / "update.yml").read_text()
    pinned = [re.search(r'CLAUDE_CODE_VERSION: "([^"]+)"', text)[1] for text in (audit_yml, update_yml)]
    assert pinned[0] == pinned[1]
    assert 'uv run audit.py propose "${propose[@]}"' in audit_yml and "propose=(--max-cost" in audit_yml
    assert 'uv run audit.py apply --max-cost "$MAX_COST"' in audit_yml
    assert "bash .github/scripts/route-audit.sh" in audit_yml and "route-update.sh" not in audit_yml
    assert "group: update" in audit_yml and "workflow_dispatch:" in audit_yml and "schedule:" not in audit_yml
