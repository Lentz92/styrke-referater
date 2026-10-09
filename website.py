# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "httpx>=0.27",
#   "beautifulsoup4>=4.12",
#   "pymupdf>=1.24",
# ]
# ///
"""Build the website (published on GitHub Pages) from the data in data/.

    uv run website.py     # writes _site/index.html; open it in a browser

The page is static: website/template.html with all rules embedded as JSON. In-force state per year
is computed here with the same logic as the Markdown pages, so both always agree.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import date
from pathlib import Path

import analyze
import render
import scrape
from analyze import Decision
from scrape import ROOT, Doc

SRC_DIR = ROOT / "website"
OUT_DIR = ROOT / "_site"
DATA_MARKER = "/*DATA*/null"
ASSETS = ("logo.png", "search.js", "vendor/minisearch.js")  # copied next to the page as-is


def build(docs: list[Doc], decisions: list[Decision], raw_rules: list[dict], today: date) -> Path:
    """Write _site/ and return the path of the page."""
    template = (SRC_DIR / "template.html").read_text()
    if template.count(DATA_MARKER) != 1:
        raise ValueError(f"website/template.html must contain {DATA_MARKER} exactly once")
    data = site_data({d.id: d for d in docs}, decisions, raw_rules, today)
    data["synonyms"] = json.loads((SRC_DIR / "synonyms.json").read_text())["groups"]
    # "</" inside a JSON string would close the <script> element early.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    OUT_DIR.mkdir(exist_ok=True)
    page = OUT_DIR / "index.html"
    page.write_text(template.replace(DATA_MARKER, payload))
    for asset in ASSETS:
        (OUT_DIR / asset).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(SRC_DIR / asset, OUT_DIR / asset)
    return page


def site_data(docs: dict[str, Doc], decisions: list[Decision], raw_rules: list[dict], today: date) -> dict:
    rules = render.build_rules(raw_rules, {d.ref: d for d in decisions})
    area_of = {c: i for i, area in enumerate(render.AREAS) for c in area.categories}
    years = render.covered_years(decisions, today)
    slugs: dict[str, int] = {}

    def slug(title: str) -> str:
        base = re.sub(r"[^\w]+", "-", title.lower()).strip("-")
        slugs[base] = slugs.get(base, 0) + 1
        return base if slugs[base] == 1 else f"{base}-{slugs[base]}"

    out_rules = []
    for rule in rules:
        # A rule is international when the decision behind its latest content came from IPF/EPF/DIF/ADD.
        content = [v for v in rule.versions if v.effekt in render.CONTENT_EFFECTS] or list(rule.versions)
        origin = rule.adopted(content[-1]).decision
        international = origin.niveau == "eksternt_krav"
        out_rules.append({
            "title": rule.titel,
            "category": rule.kategori,
            "area": area_of[rule.kategori],
            "central": rule.vigtig,
            "note": rule.note,
            "international": international,
            "origin": _origin_label(origin, docs) if international else "DSF",
            "slug": slug(rule.titel),
            "versions": [_version(v, docs) for v in rule.versions],
        })

    in_force = {}
    for year in years:
        cutoff = render.year_cutoff(year, today)
        rows = []
        for i, rule in enumerate(rules):
            v = rule.in_force(cutoff)
            if v:
                rows.append([i, rule.versions.index(v), rule.versions.index(rule.adopted(v))])
        in_force[year] = rows  # [rule, version in force, version that adopted its content]

    return {
        "today": today.isoformat(),
        "years": years,
        "areas": [area.title for area in render.AREAS],
        "documents": len(docs),
        "rules": out_rules,
        "inForce": in_force,
    }


def _version(v: render.Version, docs: dict[str, Doc]) -> dict:
    d = v.decision
    doc = docs.get(d.doc_id)
    url = None
    if doc and doc.url:
        url = doc.url + (f"#page={d.side}" if d.side and doc.path.lower().endswith(".pdf") else "")
    return {
        "date": d.dato,
        "effective": v.effective,
        "from": d.gaelder_fra,
        "effect": v.effekt,
        "change": v.change,
        "essence": v.kort_regel,
        "text": v.text if v.effekt in render.CONTENT_EFFECTS else None,
        "organ": _organ(d, docs),
        "congress": bool(doc and doc.organ == "dif_internationalt"),
        "votes": d.stemmer,
        "proposer": d.forslagsstiller,
        "doc": d.doc_id,
        "docUrl": doc.url if doc else None,
        "source": d.doc_id + (f" s. {d.side}" if d.side else ""),
        "url": url,
    }


def _organ(d: Decision, docs: dict[str, Doc]) -> str:
    doc = docs.get(d.doc_id)
    if doc is None:
        return "?"
    # Congress and DIF reports: name the actual meeting ("EPF Kongres 2014") instead of the folder label.
    return doc.title if doc.organ == "dif_internationalt" else doc.organ_label


def _origin_label(d: Decision, docs: dict[str, Doc]) -> str:
    doc = docs.get(d.doc_id)
    if doc is not None and doc.organ == "dif_internationalt":
        return doc.title
    return f"videreformidlet af {_organ(d, docs)}"


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    docs = scrape.load_manifest()
    page = build(docs, analyze.load_decisions(docs), analyze.load_rules(), date.today())
    logging.info("Wrote %s (%d KB)", page.relative_to(ROOT), page.stat().st_size // 1024)


if __name__ == "__main__":
    main()
