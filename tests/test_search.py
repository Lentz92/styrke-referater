"""The website's rule search (website/search.js), run in Node."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
SEARCH_JS = Path(__file__).resolve().parent.parent / "website" / "search.js"

# Indexes argv[3] (JSON docs) with the thesaurus argv[4] and prints, for each query in argv[5], the hits as
# [id, excerpt of the doc's summary].
PROBE = r"""
const [searchJs, docs, groups, queries] = process.argv.slice(2);
const all = JSON.parse(docs), index = require(searchJs).create(all, JSON.parse(groups));
console.log(JSON.stringify(JSON.parse(queries).map((q) =>
  index.search(q).map((hit) => [hit.id, index.excerpt([all[hit.id].summary], hit.terms)]))));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_synonym_phrase_is_a_concept_as_a_whole_on_both_sides(tmp_path):
    docs = [{"id": 0, "title": "Kost og logi", "summary": "Løfteren betaler selv for kost og logi."},
            {"id": 1, "title": "Startgebyr", "summary": "Klubben skal betale startgebyret."},
            {"id": 2, "title": "Udtagelse", "summary": "Udtagelse til nordisk mesterskab sker i maj."},
            {"id": 3, "title": "Rejser", "summary": "Rejsen er med selvbetaling."}]
    groups = [["nm", "nordisk mesterskab"], ["egenbetaling", "selvbetaling", "betaler selv"]]
    script = tmp_path / "search.js"
    script.write_text(PROBE)
    result = subprocess.run([NODE, str(script), str(SEARCH_JS), json.dumps(docs), json.dumps(groups),
                             json.dumps(["egenbetaling", "nm", "betaler selv"])],
                            capture_output=True, text=True, check=True, timeout=30)
    own_payment, nm, phrase_in_query = (dict(hits) for hits in json.loads(result.stdout))

    # The concept is the phrase "betaler selv", not "betale" alone ("selv" is a stop word).
    assert sorted(own_payment) == [0, 3]
    # A phrase in a rule counts as its concept, so the short form finds it.
    assert list(nm) == [2]
    # A phrase in the query, stop word and all, finds the rules using its synonyms.
    assert 3 in phrase_in_query
    # The passage shown for a phrase match marks the phrase.
    assert own_payment[0] == "Løfteren <mark>betaler</mark> <mark>selv</mark> for kost og logi."
    assert nm[2] == "Udtagelse til <mark>nordisk</mark> <mark>mesterskab</mark> sker i maj."
