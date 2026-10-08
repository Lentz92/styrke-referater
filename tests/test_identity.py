"""Decision ids and rule slugs through the Claude steps, against the fake `claude` from conftest.py."""

import json
from collections import Counter
from datetime import date

import pytest
from conftest import decision

import analyze
import checks
import render
import website
from analyze import decision_hash
from matching import FormerSlug, SlugRegistry
from scrape import Doc

TEXT = (
    "[Side 1]\nPunkt 1: Licensgebyret hæves til 300 kr. pr. løfter. Vedtaget.\n"
    "Punkt 2: Et klubskifte kræver tre måneders karantæne. Vedtaget.\n"
    "Punkt 3: Forslaget om to dommere pr. stævne blev forkastet.\n"
    "[Side 2]\nPunkt 4: Startgebyret er 200 kr. pr. stævne. Vedtaget.\n"
)
DOC = Doc("rep2024", "repraesentantskab", "Referat", "2024-03-24", "rep2024.pdf", None, "sha")


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path / "beslutninger")
    monkeypatch.setattr(analyze, "RULES_DIR", tmp_path / "regler")
    monkeypatch.setattr(analyze, "document_text", lambda doc: TEXT)
    analyze.DECISIONS_DIR.mkdir()
    analyze.RULES_DIR.mkdir()
    return tmp_path


def _extracted(emne: str, tekst: str, citat: str, udfald: str = "vedtaget") -> dict:
    return {"emne": emne, "kategori": "okonomi", "udfald": udfald, "forslagsstiller": None, "handling": "ny",
            "niveau": "staevneregel", "tekst": tekst, "citat": citat, "side": 1, "stemmer": None,
            "gaelder_fra": None, "gaelder_til": None}


LICENS = _extracted("Licensgebyr", "Licensgebyret er 300 kr. pr. løfter.",
                    "Licensgebyret hæves til 300 kr. pr. løfter")
KLUBSKIFTE = _extracted("Klubskifte", "Et klubskifte kræver tre måneders karantæne.",
                        "Et klubskifte kræver tre måneders karantæne")
DOMMERE = _extracted("Dommerforpligtelse", "Klubberne skal stille to dommere pr. stævne.",
                     "Forslaget om to dommere pr. stævne blev forkastet", "forkastet")
START = _extracted("Startgebyr", "Startgebyret er 200 kr. pr. stævne.", "Startgebyret er 200 kr. pr. stævne")


def _extract(fake_claude, *decisions: dict) -> dict:
    fake_claude.answer({"moededato": "2024-03-24", "beslutninger": list(decisions)})
    analyze._extract_one(DOC, model="claude-sonnet-5-5", effort=None, cli="2.1.294")
    return json.loads((analyze.DECISIONS_DIR / f"{DOC.id}.json").read_text())


def _ids(saved: dict) -> dict[str, str]:
    return {d["emne"]: d["id"] for d in saved["beslutninger"]}


# ---------------------------------------------------------------- decision ids

def test_re_extraction_keeps_ids_and_never_reuses_a_number(fake_claude, data):
    first = _extract(fake_claude, LICENS, KLUBSKIFTE, DOMMERE)
    assert _ids(first) == {"Licensgebyr": "rep2024#1", "Klubskifte": "rep2024#2", "Dommerforpligtelse": "rep2024#3"}
    assert (first["next_number"], first["retired"]) == (4, [])

    # Reordered, reworded with a shorter quote, one decision gone and one new.
    reworded = {**LICENS, "tekst": "Licensen koster 300 kr. pr. løfter.", "citat": "hæves til 300 kr. pr. løfter"}
    second = _extract(fake_claude, START, DOMMERE, reworded)
    assert _ids(second) == {"Startgebyr": "rep2024#4", "Dommerforpligtelse": "rep2024#3", "Licensgebyr": "rep2024#1"}
    assert second["next_number"] == 5
    assert [(r["id"], r["emne"]) for r in second["retired"]] == [("rep2024#2", "Klubskifte")]
    assert second["retired"][0]["date"] == date.today().isoformat()

    # The retired decision comes back: a new number, not its old one.
    third = _extract(fake_claude, LICENS, KLUBSKIFTE)
    assert _ids(third) == {"Licensgebyr": "rep2024#1", "Klubskifte": "rep2024#5"}
    assert third["next_number"] == 6
    assert [r["id"] for r in third["retired"]] == ["rep2024#2", "rep2024#4", "rep2024#3"]
    assert [d.ref for d in analyze.load_decisions([DOC])] == ["rep2024#1", "rep2024#5"]
    assert analyze.load_retired_ids([DOC]) == {"rep2024#2", "rep2024#3", "rep2024#4"}
    assert fake_claude.invocations("call") == 3


def test_previous_quotes_are_located_again_in_a_changed_document(fake_claude, data, monkeypatch):
    # The replaced document has ten words more at the top, so the licence quote now stands where the club-change
    # quote stood. Matched by their old offsets, the club-change decision would take the licence decision's id.
    body = "Licensgebyret hæves til 300 kr. pr. løfter. Punkt to tre. Et klubskifte kræver tre måneders karantæne."
    monkeypatch.setattr(analyze, "document_text", lambda doc: f"[Side 1]\n{body}")
    _extract(fake_claude, LICENS, KLUBSKIFTE)
    monkeypatch.setattr(analyze, "document_text",
                        lambda doc: f"[Side 1]\nTi ord indsat foran der flytter alle citater ti pladser. {body}")
    # Reworded beyond recognition, so only the quotes can tell which is which.
    licens = {**LICENS, "emne": "Årligt bidrag", "tekst": "Hver udøver betaler fremover et højere beløb."}
    klubskifte = {**KLUBSKIFTE, "emne": "Skift af forening", "tekst": "Den der skifter forening venter en periode."}
    assert _ids(_extract(fake_claude, licens, klubskifte)) == {"Årligt bidrag": "rep2024#1",
                                                               "Skift af forening": "rep2024#2"}


@pytest.mark.parametrize("highest_in", ["versioner", "udeladt", "ikke_tildelt", "slugs.json"])
def test_a_lost_decision_file_numbers_above_every_id_still_named(fake_claude, data, highest_in):
    # Each place names rep2024#2 and rep2024#4, and one of them also rep2024#7, the highest.
    def refs(place: str) -> list[str]:
        return ["rep2024#2", "rep2024#4"] + (["rep2024#7"] if place == highest_in else [])

    (analyze.RULES_DIR / "okonomi.json").write_text(json.dumps({
        "kategori": "okonomi", "regler": [_rule("Licensgebyr", "licensgebyr", *refs("versioner"))],
        "udeladt": refs("udeladt"), "ikke_tildelt": refs("ikke_tildelt")}))
    analyze._save_slugs(SlugRegistry.of({"rabat": FormerSlug("okonomi", "Rabat", frozenset(refs("slugs.json")))}))
    saved = _extract(fake_claude, LICENS, KLUBSKIFTE)  # no rep2024.json any more
    assert _ids(saved) == {"Licensgebyr": "rep2024#8", "Klubskifte": "rep2024#9"}
    assert saved["next_number"] == 10


def test_decisions_without_ids_fail_with_the_file_named(fake_claude, data):
    path = analyze.DECISIONS_DIR / f"{DOC.id}.json"
    path.write_text(json.dumps({"moededato": None, "beslutninger": [LICENS]}))
    with pytest.raises(ValueError, match=f"{path}.*without stored ids"):
        analyze.load_decisions([DOC])
    with pytest.raises(ValueError, match="without stored ids"):
        _extract(fake_claude, LICENS)
    assert fake_claude.invocations("call") == 0  # it fails before the call costs anything


# ---------------------------------------------------------------- rule slugs

def _rule(titel: str, slug: str | None, *refs: str) -> dict:
    rule = {"titel": titel, "vigtig": True, "note": None,
            "versioner": [{"ref": ref, "effekt": "indfoert", "tekst": None, "kort": "", "kort_regel": None}
                          for ref in refs]}
    return rule if slug is None else {"titel": titel, "slug": slug, **rule}


def _rule_file(category: str, *rules: dict) -> None:
    (analyze.RULES_DIR / f"{category}.json").write_text(json.dumps({"kategori": category, "regler": list(rules)}))


def _slugs(category: str) -> list[tuple[str, str]]:
    return [(r["titel"], r["slug"]) for r in json.loads((analyze.RULES_DIR / f"{category}.json").read_text())["regler"]]


def _consolidate(decisions, fake_claude, answer: dict) -> None:
    fake_claude.answer(answer)  # every category gets the same answer; each keeps the refs it was given
    organ = {d.doc_id: "Repræsentantskabet" for d in decisions}
    analyze.consolidate(decisions, organ, model="claude-opus-5-5", effort=None, workers=1)


def test_slugs_survive_renames_merges_splits_and_moves_between_categories(fake_claude, data):
    okonomi = [decision(ref=ref) for ref in ("a#1", "b#1", "c#1", "e#1", "f#1", "x#1")]
    master = [decision(ref=ref, kategori="master") for ref in ("d#1", "g#1", "m#1")]  # d#1 moved here from okonomi
    _rule_file("okonomi", _rule("Licensgebyr", "licensgebyr", "a#1", "b#1"), _rule("Startgebyr", "startgebyr", "c#1"),
               _rule("Klubskifte", "klubskifte", "d#1"), _rule("Gebyrer", "gebyrer", "e#1", "f#1"))
    _rule_file("master", _rule("Masterudtagelse", "masterudtagelse", "m#1"),
               _rule("Gebyr for dommere", "gebyr-for-dommere", "g#1"))
    analyze._save_slugs(SlugRegistry.of({
        "licens": FormerSlug("okonomi", "Licens", frozenset({"c#1"}), to="startgebyr"),  # merged long ago
        "gebyr-for-dommere-2": FormerSlug("okonomi", "Dommerhonorar", frozenset({"z#1"})),
    }))
    _consolidate(okonomi + master, fake_claude, {"regler": [
        _rule("Licens- og startgebyr", None, "a#1", "b#1", "c#1"),  # renamed, and Startgebyr merged into it
        _rule("Gebyr for dommere", None, "x#1"),  # new; its slug is live in master and was used once before
        _rule("Rabat", None, "f#1"), _rule("Gebyrer i alt", None, "e#1"),  # split in equal parts: the first keeps it
        _rule("Masterudtagelse", None, "m#1"), _rule("Dommergebyr", None, "g#1"),
        _rule("Klubskifte for masters", None, "d#1"),
    ], "udeladt": []})

    assert _slugs("okonomi") == [("Licens- og startgebyr", "licensgebyr"), ("Gebyr for dommere", "gebyr-for-dommere-3"),
                                 ("Rabat", "gebyrer"), ("Gebyrer i alt", "gebyrer-i-alt")]
    # okonomi ran first and orphaned "klubskifte"; master's rule holding d#1 took it up again.
    assert _slugs("master") == [("Masterudtagelse", "masterudtagelse"), ("Dommergebyr", "gebyr-for-dommere"),
                                ("Klubskifte for masters", "klubskifte")]
    registry = analyze.load_slugs()
    assert registry.targets() == {"startgebyr": "licensgebyr", "licens": "licensgebyr"}
    assert set(registry.retired) == {"gebyr-for-dommere-2"}
    raw_rules = analyze.load_rules()
    assert checks.identity_problems(okonomi + master, set(), raw_rules, registry) == []
    assert fake_claude.invocations("call") == 2


def test_a_category_left_without_decisions_leads_its_slugs_to_where_they_went(fake_claude, data):
    _rule_file("master", _rule("Masterudtagelse", "masterudtagelse", "m#1"), _rule("Masterrabat", "masterrabat", "r#1"))
    _rule_file("landshold", _rule("Landshold", "landshold", "l#1"))
    _consolidate([decision(ref="m#1", kategori="landshold"), decision(ref="l#1", kategori="landshold")], fake_claude,
                 {"regler": [_rule("Udtagelse", None, "m#1", "l#1")], "udeladt": []})
    assert not (analyze.RULES_DIR / "master.json").exists()
    assert _slugs("landshold") == [("Udtagelse", "landshold")]
    registry = analyze.load_slugs()
    assert registry.targets() == {"masterudtagelse": "landshold"}  # followed m#1 into landshold
    assert registry.retired == {"masterrabat": FormerSlug("master", "Masterrabat", frozenset({"r#1"}))}


def test_a_rule_split_out_again_takes_its_former_slug_back(fake_claude, data):
    decisions = [decision(ref="a#1"), decision(ref="b#1")]
    _rule_file("okonomi", _rule("Gebyrer", "gebyrer", "a#1", "b#1"))
    analyze._save_slugs(SlugRegistry.of({"tilskud": FormerSlug("okonomi", "Tilskud", frozenset({"a#1"}), "gebyrer")}))
    _consolidate(decisions, fake_claude, {"regler": [_rule("Gebyrer", None, "b#1"), _rule("Klubtilskud", None, "a#1")],
                                          "udeladt": []})
    assert _slugs("okonomi") == [("Gebyrer", "gebyrer"), ("Klubtilskud", "tilskud")]
    assert analyze.load_slugs() == SlugRegistry()


def test_a_revived_slug_stays_taken_when_its_rule_file_fails_to_write(fake_claude, data, monkeypatch):
    _rule_file("okonomi", _rule("Gebyrer", "gebyrer", "a#1", "b#1"))
    analyze._save_slugs(SlugRegistry.of({"tilskud": FormerSlug("okonomi", "Tilskud", frozenset({"a#1"}), "gebyrer")}))
    write = analyze._write_json

    def full_disk_for_rules(path, value):
        if path.parent == analyze.RULES_DIR:
            raise OSError("No space left on device")
        write(path, value)

    def consolidate(category: str, *decisions) -> None:
        items = [analyze._consolidation_input(d, "Repræsentantskabet") for d in decisions]
        analyze._consolidate_one(category, items, "hash", {d.ref: decision_hash(d) for d in decisions},
                                 model="claude-opus-5-5", effort=None, cli="2.1.294")

    fake_claude.answer({"regler": [_rule("Gebyrer", None, "b#1"), _rule("Klubtilskud", None, "a#1")], "udeladt": []})
    with monkeypatch.context() as m:
        m.setattr(analyze, "_write_json", full_disk_for_rules)
        with pytest.raises(analyze.ClaudeError, match="No space left"):
            consolidate("okonomi", decision(ref="a#1"), decision(ref="b#1"))
    assert "tilskud" in analyze.load_slugs().taken()

    # A later, unrelated rule of that name must not get it.
    fake_claude.answer({"regler": [_rule("Tilskud", None, "m#1")], "udeladt": []})
    consolidate("master", decision(ref="m#1", kategori="master"))
    assert _slugs("master") == [("Tilskud", "tilskud-2")]


def test_ties_between_categories_go_to_the_earlier_category_not_the_earlier_file():
    # data/regler/ lists andet.json first, but andet is the last category.
    raw = [{**_rule("Diverse", "diverse", "a#1"), "kategori": "andet"},
           {**_rule("Gebyr", "gebyr", "b#1"), "kategori": "okonomi"}]
    registry = SlugRegistry.of({"gammel": FormerSlug("okonomi", "Gammel", frozenset({"a#1", "b#1"}))})
    assert registry.resolved(analyze.live_rules(raw), {"a#1", "b#1"}).targets() == {"gammel": "gebyr"}


@pytest.mark.parametrize("slug", ["", None, 7])
def test_rules_without_a_usable_slug_fail_before_the_call(fake_claude, data, slug):
    _rule_file("okonomi", {**_rule("Licensgebyr", None, "a#1"), "slug": slug})
    with pytest.raises(ValueError, match="without a stored slug"):
        analyze._consolidate_one("okonomi", [], "hash", {}, model="claude-opus-5-5", effort=None, cli="2.1.294")
    assert fake_claude.invocations("call") == 0


def test_the_slug_history_round_trips_through_its_file(data):
    registry = SlugRegistry.of({"licens": FormerSlug("okonomi", "Licens", frozenset({"b#1", "a#1"}), "licensgebyr"),
                                "rabat": FormerSlug("master", "Rabat", frozenset())})
    analyze._save_slugs(registry)
    assert json.loads(analyze.SLUGS_PATH.read_text()) == {
        "aliases": {"licens": {"to": "licensgebyr", "category": "okonomi", "title": "Licens", "refs": ["a#1", "b#1"]}},
        "retired": {"rabat": {"category": "master", "title": "Rabat", "refs": []}}}
    assert analyze.load_slugs() == registry


# ---------------------------------------------------------------- website and Markdown

def test_an_alias_leads_to_its_rule_in_the_page_data():
    d = decision(ref="a#1")
    raw = {**_rule("Licens- og startgebyr", "licensgebyr", "a#1"), "kategori": "okonomi"}
    raw["versioner"][0]["dhash"] = decision_hash(d)
    data = website.site_data({}, [d], [raw], {"startgebyr": "licensgebyr"}, date(2026, 10, 8))
    (rule,) = data["rules"]
    assert rule["slug"] == "licensgebyr"
    assert data["aliases"]["startgebyr"] == rule["slug"]


def test_markdown_links_follow_the_slug_and_never_a_heading_anchor():
    # A renamed rule keeps "licensgebyr"; a new rule took its old title, so GitHub names that heading
    # "licensgebyr" too. The rules' own anchors carry a "/", which no heading anchor has.
    d1, d2 = decision(ref="a#1", dato="2024-03-24"), decision(ref="b#1", dato="2024-03-24")
    renamed = {**_rule("Licens (nyt navn)", "licensgebyr", "a#1"), "kategori": "okonomi"}
    new = {**_rule("Licensgebyr", "licensgebyr-2", "b#1"), "kategori": "okonomi"}
    renamed["versioner"][0]["dhash"], new["versioner"][0]["dhash"] = decision_hash(d1), decision_hash(d2)
    pages = render.build_pages({}, [d1, d2], [renamed, new], [], Counter(), date(2024, 10, 8))
    area = pages["regler/medlemskab.md"]
    assert '<a id="regel/licensgebyr"></a>\n### Licens (nyt navn)\n' in area
    assert '<a id="regel/licensgebyr-2"></a>\n### Licensgebyr\n' in area
    assert "[Licens (nyt navn)](regler/medlemskab.md#regel/licensgebyr)" in pages["2024.md"]
    assert "[Licensgebyr](regler/medlemskab.md#regel/licensgebyr-2)" in pages["2024.md"]
