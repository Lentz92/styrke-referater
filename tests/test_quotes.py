import pytest
from conftest import extracted

import analyze
from analyze import DocWords, locate_quote, quote_fields

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
    # The split word sits mid-quote, so neither half alone has enough matching trigrams.
    text = ("[Side 2]\nPunkt 4.\n[Side 3]\nDet blev på mødet besluttet at licens-\nopkrævningen for alle aktive "
            "løftere hæves til 300 kr fra næste sæson.")
    quote = ("Det blev på mødet besluttet at licensopkrævningen for alle aktive løftere hæves til 300 kr "
             "fra næste sæson")
    words = DocWords.of(text)
    fields = quote_fields(quote, words, None)
    assert fields["citat_pos"] == words.words.index("det")
    assert fields["citat_side"] == 3 and fields["citat_fundet"]


def test_page_header_inside_the_quote_still_locates_it():
    # As in rep2012: the page break puts the date header between "står" and "fast".
    text = ("[Side 2]\nforeslås følgende: At DSF etablerer et fast løftested, hvor der står\n"
            "[Side 3]\n2012-03-30\nfast pro.udstyr mm. til afholdelse af officielle stævner og kurser. Evt. Odense.")
    quote = ("At DSF etablerer et fast løftested, hvor der står fast pro.udstyr mm. til afholdelse af officielle "
             "stævner og kurser.")
    words = DocWords.of(text)
    fields = quote_fields(quote, words, 2)
    assert fields["citat_pos"] == words.words.index("at")
    assert fields["citat_side"] == 2 and fields["citat_fundet"]


def test_an_earlier_match_of_the_first_words_does_not_move_the_start():
    text = "[Side 1]\n4. The head coach.\n5. The head coach must ensure each of his assistant coaches receive a badge."
    words = DocWords.of(text)
    start = locate_quote("The head coach must ensure each of his assistant coaches receive a badge", words)
    assert words.words[start:start + 4] == ["the", "head", "coach", "must"]


def test_short_quote_is_located_verbatim():
    text = "[Side 1]\nAntidopingpolitikken drøftes.\n[Side 2]\nAntidopingpolitikken godkendes.\n"
    words = DocWords.of(text)
    assert quote_fields("Antidopingpolitikken godkendes.", words, 1) == {
        "citat_fundet": True, "citat_pos": 2, "citat_side": 2}
    assert quote_fields("Antidopingpolitikken", words, 2)["citat_pos"] == 2  # Claude's page wins
    assert quote_fields("Antidopingpolitikken", words, None)["citat_pos"] == 0  # else the first
    assert quote_fields("Politikken forkastes.", words, 2)["citat_fundet"] is False


def test_quote_not_in_document():
    fields = quote_fields("klubber skal stille med to dommere", DocWords.of(TEXT), 1)
    assert fields == {"citat_fundet": False, "citat_pos": None, "citat_side": None}


def test_html_without_page_markers_has_no_page():
    fields = quote_fields("Divisionsturneringen får nyt navn", DocWords.of("Punkt 5: Divisionsturneringen får nyt navn."),
                          None)
    assert fields["citat_pos"] is not None and fields["citat_side"] is None


@pytest.fixture
def load(world):
    """Store one document's decisions as an extraction does, and load them back."""
    def run(*decisions: dict):
        world.document("doc", "2024-03-24", *decisions)
        return world.decisions()

    return run


def test_decisions_load_in_reading_order(load):
    # Twelve decisions listed in reverse page order; string order would put doc#10 before doc#2.
    raw = [extracted(f"emne {n}", citat_pos=100 - n) for n in range(1, 13)]
    raw[4]["citat_pos"] = None  # not located: stays right after the decision listed before it
    order = [d.emne for d in load(*raw)]
    assert order == [f"emne {n}" for n in [12, 11, 10, 9, 8, 7, 6, 4, 5, 3, 2, 1]]


def test_page_found_by_code_overrides_claudes(load):
    (d,) = load(extracted("x", side=4, citat_pos=10, citat_side=3))
    assert d.side == 3 and d.side_rettet


def test_claudes_page_is_kept_when_the_quote_was_not_located(load):
    (d,) = load(extracted("x", side=4, citat_pos=None, citat_side=None))
    assert d.side == 4 and not d.side_rettet


def test_filling_in_a_page_claude_left_out_is_not_a_correction(load):
    (d,) = load(extracted("x", side=None, citat_pos=10, citat_side=3))
    assert d.side == 3 and not d.side_rettet


def test_extraction_stores_where_each_quote_is(world, fake_claude):
    world.texts["doc"] = TEXT
    fake_claude.answer({"moededato": "2024-03-24", "beslutninger": [
        extracted("Licensgebyr", citat=QUOTE), extracted("Navn", citat="Divisionsturneringen får nyt navn", side=2)]})

    analyze.extract([world.downloaded("doc")], model="claude-sonnet-5-5", effort=None, workers=1)

    saved = world.extraction("doc")["beslutninger"]
    assert [(d["citat_fundet"], d["citat_side"]) for d in saved] == [(True, 1), (True, 2)]


def test_opening_words_before_a_page_header_keep_claudes_page():
    # The quote starts at the bottom of page 1; a header opens page 2 before the rest of it.
    # Only two words stand before the header, so no trigram matches on page 1.
    text = ("[Side 1]\nPunkt 7. Det blev besluttet at klubberne\n[Side 2]\nReferat 2012-03-30\n"
            "skal stille med mindst en dommer ved hvert stævne i divisionsturneringen fra næste sæson.")
    quote = "at klubberne skal stille med mindst en dommer ved hvert stævne i divisionsturneringen fra næste sæson"
    fields = quote_fields(quote, DocWords.of(text), 1)
    assert fields["citat_fundet"] and fields["citat_side"] == 1
