import json

import pytest

import analyze
from analyze import DocWords, load_decisions, locate_quote, quote_fields
from scrape import Doc

TEXT = (
    "[Side 1]\nReferat af repræsentantskabsmødet.\nPunkt 4: Forslag fra bestyrelsen om at "
    "licensopkrævningen hæves til 300 kr. pr. løfter.\n"
    "[Side 2]\nAfstemning: Licensopkrævningen hæves til 300 kr. pr. løfter. Vedtaget med 42 for.\n"
    "Punkt 5: Divisionsturneringen får nyt navn.\n"
)
QUOTE = "Licensopkrævningen hæves til 300 kr. pr. løfter"


def test_page_markers_are_not_words():
    words = DocWords.of(TEXT)
    assert "side" not in words.words
    assert words.pages[0] == 1 and words.pages[-1] == 2


def test_repeated_quote_prefers_the_page_claude_cited():
    words = DocWords.of(TEXT)
    assert words.pages[locate_quote(QUOTE, words, page=2)] == 2
    assert words.pages[locate_quote(QUOTE, words, page=1)] == 1


def test_repeated_quote_without_a_matching_page_takes_the_first():
    words = DocWords.of(TEXT)
    assert words.pages[locate_quote(QUOTE, words, page=7)] == 1


def test_line_break_hyphenation_still_locates_the_quote():
    text = "[Side 3]\nDet blev besluttet at licens-\nopkrævningen hæves til 300 kr. pr. løfter fra næste sæson."
    fields = quote_fields("licensopkrævningen hæves til 300 kr. pr. løfter fra næste sæson", DocWords.of(text), None)
    assert fields["citat_pos"] is not None
    assert fields["citat_side"] == 3


def test_quote_not_in_document():
    fields = quote_fields("klubber skal stille med to dommere", DocWords.of(TEXT), 1)
    assert fields == {"citat_fundet": False, "citat_pos": None, "citat_side": None}


def test_html_without_page_markers_has_no_page():
    fields = quote_fields("Divisionsturneringen får nyt navn", DocWords.of("Punkt 5: Divisionsturneringen får nyt navn."),
                          None)
    assert fields["citat_pos"] is not None and fields["citat_side"] is None


@pytest.fixture
def load(tmp_path, monkeypatch):
    """Write one document's cached decisions and load them back."""
    monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path)

    def run(raw: list[dict]):
        (tmp_path / "doc.json").write_text(json.dumps({"moededato": "2024-03-24", "beslutninger": raw}))
        return load_decisions([Doc("doc", "repraesentantskab", "Referat", "2024", "doc.pdf", None, "sha")])

    return run


def test_decisions_load_in_reading_order(load):
    # Twelve decisions listed in reverse page order; string order would put doc#10 before doc#2.
    raw = [_raw(f"emne {n}", pos=100 - n) for n in range(1, 13)]
    raw[4]["citat_pos"] = None  # not located: stays right after the decision listed before it
    order = [d.emne for d in load(raw)]
    assert order == [f"emne {n}" for n in [12, 11, 10, 9, 8, 7, 6, 4, 5, 3, 2, 1]]


def test_page_found_by_code_overrides_claudes(load):
    (d,) = load([_raw("x", pos=10, side=4, located_page=3)])
    assert d.side == 3 and d.side_rettet


def test_claudes_page_is_kept_when_the_quote_was_not_located(load):
    (d,) = load([_raw("x", pos=None, side=4, located_page=None)])
    assert d.side == 4 and not d.side_rettet


def _raw(emne: str, pos: int | None, side: int | None = 1, located_page: int | None = 1) -> dict:
    return {
        "emne": emne, "kategori": "okonomi", "udfald": "vedtaget", "forslagsstiller": None, "handling": "ny",
        "niveau": "staevneregel", "tekst": emne, "citat": emne, "side": side, "stemmer": None,
        "gaelder_fra": None, "gaelder_til": None, "citat_fundet": True, "citat_pos": pos, "citat_side": located_page,
    }


def test_extraction_stores_where_each_quote_is(tmp_path, monkeypatch):
    claude_output = {"moededato": "2024-03-24", "beslutninger": [
        {**_raw("Licensgebyr", pos=None, side=1), "citat": QUOTE},
        {**_raw("Navn", pos=None, side=2), "citat": "Divisionsturneringen får nyt navn"},
    ]}
    for d in claude_output["beslutninger"]:
        for key in ("citat_fundet", "citat_pos", "citat_side"):
            del d[key]
    monkeypatch.setattr(analyze, "DECISIONS_DIR", tmp_path)
    monkeypatch.setattr(analyze, "document_text", lambda doc: TEXT)
    monkeypatch.setattr(analyze, "ask_claude", lambda *args, **kwargs: (claude_output, 0.0))

    analyze._extract_one(Doc("doc", "repraesentantskab", "Referat", "2024", "doc.pdf", None, "sha"), "sonnet", None, None)

    saved = json.loads((tmp_path / "doc.json").read_text())["beslutninger"]
    assert [(d["citat_fundet"], d["citat_side"]) for d in saved] == [(True, 1), (True, 2)]
