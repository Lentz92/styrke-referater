import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from analyze import Decision  # noqa: E402

_BASE = Decision(
    ref="doc#1", doc_id="doc", dato="2020-03-01", emne="Licensgebyr", kategori="okonomi", udfald="vedtaget",
    handling="ny", niveau="staevneregel", tekst="Licensgebyret er 200 kr.", citat="licensgebyret er 200 kr",
    citat_fundet=True, side=1, side_rettet=False, rank=0, stemmer=None, forslagsstiller=None,
    gaelder_fra=None, gaelder_til=None,
)


def decision(**changes) -> Decision:
    return replace(_BASE, **changes)
