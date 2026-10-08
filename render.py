"""Render the rule overview as Markdown: a short page per year, a full page per area, and an index."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import quote

from analyze import CATEGORIES, Decision
from scrape import ROOT, Doc

OUT_DIR = ROOT / "regelsaet"
AREA_DIR = "regler"  # subfolder of OUT_DIR with one full page per area


@dataclass(frozen=True)
class Area:
    title: str
    file: str
    categories: tuple[str, ...]


# Broad areas shown on the pages; each groups one or more extraction categories.
AREAS = (
    Area("Medlemskab, licens og gebyrer", "medlemskab", ("medlemskab", "okonomi")),
    Area("Stævner og konkurrenceregler", "staevner", ("staevner", "udstyr")),
    Area("Dommere", "dommere", ("dommere",)),
    Area("Elite og landshold", "elite", ("landshold", "internationalt", "uddannelse")),
    Area("Master", "master", ("master",)),
    Area("Antidoping", "antidoping", ("antidoping",)),
    Area("Forbund og organisation", "organisation", ("organisation", "andet")),
)
assert {c for area in AREAS for c in area.categories} == set(CATEGORIES), "every category needs an area"

EFFEKT_LABELS = {
    "indfoert": "indført",
    "aendret": "ændret",
    "bekraeftet": "bekræftet",
    "ophaevet": "ophævet",
    "forkastet": "forslag forkastet",
    "trukket": "forslag trukket",
}
NIVEAU_LABELS = {
    "vedtaegt": "Vedtægt",
    "staevneregel": "Stævneregel",
    "bestyrelsesbeslutning": "Bestyrelsesbeslutning",
    "udvalgsbeslutning": "Udvalgsbeslutning",
    "eksternt_krav": "Eksternt krav",
}
CONTENT_EFFECTS = ("indfoert", "aendret", "bekraeftet")
PROPOSAL_EFFECTS = ("forkastet", "trukket")
MONTH_NAMES = ["januar", "februar", "marts", "april", "maj", "juni", "juli", "august",
               "september", "oktober", "november", "december"]
WARNING = "⚠"
STALE_AFTER_YEARS = 5


@dataclass(frozen=True)
class Version:
    decision: Decision
    effekt: str
    tekst: str | None  # consolidated rule text after this decision, if Claude produced one
    kort: str | None  # what this decision did, in a few words
    kort_regel: str | None  # the rule's essence after this decision, in a few words

    @property
    def effective(self) -> str:
        """ISO date (or year) the version takes effect; string order works because a bare year sorts first."""
        return self.decision.gaelder_fra or self.decision.dato or ""

    @property
    def text(self) -> str:
        return self.tekst or self.decision.tekst

    @property
    def change(self) -> str:
        return self.kort or self.decision.tekst

    @property
    def essence(self) -> str:
        return self.kort_regel or self.kort or self.text


@dataclass(frozen=True)
class Rule:
    titel: str
    kategori: str
    vigtig: bool  # central rule vs. internal routine or detail
    note: str | None
    versions: tuple[Version, ...]  # ordered by effective date

    def in_force(self, cutoff: str) -> Version | None:
        """The newest statement of the rule in force on the cutoff date, or None if it did not apply.

        Confirmations count: they restate the full rule, often with the current wording.
        """
        state: Version | None = None
        for v in self.versions:
            if v.effective > cutoff:
                break
            if (v.decision.dato or "") > cutoff:  # retroactive, but not yet decided at the cutoff
                continue
            if v.effekt in CONTENT_EFFECTS:
                state = v
            elif v.effekt == "ophaevet":
                state = None
        expires = state.decision.gaelder_til if state else None
        return None if expires and expires < cutoff else state

    def adopted(self, current: Version) -> Version:
        """The decision that last changed the rule's content, as of `current`."""
        if current.effekt != "bekraeftet":
            return current
        origin = current
        for v in self.versions:
            if v is current:
                break
            if v.effekt in ("indfoert", "aendret"):
                origin = v
            elif v.effekt == "ophaevet":
                origin = current
        return origin

    def history(self, cutoff: str) -> list[Version]:
        return [v for v in self.versions if (v.decision.dato or "") <= cutoff]


def render(docs: list[Doc], decisions: list[Decision], raw_rules: list[dict], missing: list[str],
           today: date) -> list[str]:
    """Write all pages to regelsaet/ and return their paths. `missing` = docs not yet analysed."""
    pages = build_pages({d.id: d for d in docs}, decisions, raw_rules, missing, today)
    for stale in OUT_DIR.rglob("*.md"):
        if stale.relative_to(OUT_DIR).as_posix() not in pages:
            stale.unlink()
    for name, content in pages.items():
        path = OUT_DIR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return sorted(pages)


def build_pages(docs: dict[str, Doc], decisions: list[Decision], raw_rules: list[dict], missing: list[str],
                today: date) -> dict[str, str]:
    rules = _build_rules(raw_rules, {d.ref: d for d in decisions})
    # Includes next year when documents for it exist already (e.g. deadlines for 2027).
    known_years = {int(d.dato[:4]) for d in decisions if d.dato} | {today.year}
    years = list(range(min(known_years), max(known_years) + 1))

    pages: dict[str, str] = {}
    targets: dict[int, str] = {}  # id(rule) -> link from a year page to the rule on its area page
    for area in AREAS:
        area_rules = [rule for rule in rules if rule.kategori in area.categories]
        page, anchors = _area_page(area, area_rules, today, _Links(docs, "../../"))
        pages[f"{AREA_DIR}/{area.file}.md"] = page
        targets.update({key: f"{AREA_DIR}/{area.file}.md#{anchor}" for key, anchor in anchors.items()})
    for year in years:
        pages[f"{year}.md"] = _year_page(year, years, rules, today, _Links(docs, "../"), targets)
    pages["README.md"] = _index_page(years, rules, decisions, raw_rules, docs, missing, today)
    return pages


def _build_rules(raw_rules: list[dict], by_ref: dict[str, Decision]) -> list[Rule]:
    rules = []
    for raw in raw_rules:
        versions = [
            Version(by_ref[v["ref"]], v["effekt"], v["tekst"], v.get("kort"), v.get("kort_regel"))
            for v in raw["versioner"] if v["ref"] in by_ref
        ]
        if not versions:
            continue
        versions.sort(key=lambda v: (v.effective, v.decision.dato or "", v.decision.ref))
        rules.append(Rule(raw["titel"], raw["kategori"], raw.get("vigtig", True), raw["note"], tuple(versions)))
    category_order = list(CATEGORIES)
    return sorted(rules, key=lambda r: (category_order.index(r.kategori), r.titel.lower()))


# --------------------------------------------------------------------------- year pages

def _year_page(year: int, years: list[int], rules: list[Rule], today: date, links: _Links,
               targets: dict[int, str]) -> str:
    cutoff = _cutoff(year, today)
    if year == today.year:
        cutoff_text = _long_date(today)
    elif year > today.year:
        cutoff_text = f"31. december {year} – kun regler, der allerede er vedtaget"
    else:
        cutoff_text = f"31. december {year}"

    nav = ["[Oversigt](README.md)"]
    if year - 1 in years:
        nav.append(f"[← {year - 1}]({year - 1}.md)")
    if year + 1 in years:
        nav.append(f"[{year + 1} →]({year + 1}.md)")

    def link(rule: Rule) -> str:
        target = targets.get(id(rule))
        return f"[{rule.titel}]({target})" if target else rule.titel

    lines = [
        f"# DSF-regelsæt {year}",
        "",
        " · ".join(nav),
        "",
        f"Regler i kraft pr. {cutoff_text}. Klik på en regel for fuld tekst og historik, "
        "eller på kilden for referatet.",
        "",
    ]

    # Confirmations are not news, except the first time a rule shows up.
    events = [
        (v, rule) for rule in rules for i, v in enumerate(rule.versions)
        if (v.decision.dato or "")[:4] == str(year) and (v.effekt != "bekraeftet" or i == 0)
    ]
    changes = [(v, rule) for v, rule in events if v.effekt not in PROPOSAL_EFFECTS]
    proposals = [(v, rule) for v, rule in events if v.effekt in PROPOSAL_EFFECTS]

    lines += [f"## Nyt i {year}", ""]
    if not changes:
        lines += ["Ingen registrerede ændringer.", ""]
    for area in AREAS:
        items = sorted(((v, r) for v, r in changes if r.kategori in area.categories),
                       key=lambda item: (item[0].decision.dato or "", item[1].titel))
        if not items:
            continue
        lines += [f"**{area.title}**", ""]
        for v, rule in items:
            starts = f" (fra {_short_date(v.effective)})" if v.decision.gaelder_fra else ""
            lines.append(f"- {_day_month(v.decision.dato)} · {link(rule)} – {_labelled_change(v)}{starts} · "
                         f"{links.source(v.decision)}")
        lines.append("")
    if proposals:
        lines += ["**Forslag der ikke blev til noget**", ""]
        for v, rule in sorted(proposals, key=lambda item: (item[0].decision.dato or "", item[1].titel)):
            lines.append(f"- {_day_month(v.decision.dato)} · {rule.titel} – {v.change} · {links.source(v.decision)}")
        lines.append("")

    pending = [
        (v, rule) for rule in rules for v in rule.versions
        if (v.decision.dato or "") <= cutoff < v.effective and v.effekt in ("indfoert", "aendret", "ophaevet")
    ]
    if pending:
        lines += ["## På vej", "", "Vedtaget, men endnu ikke trådt i kraft.", ""]
        for v, rule in sorted(pending, key=lambda item: (item[0].effective, item[1].titel)):
            lines.append(f"- Fra {_short_date(v.effective)} · {link(rule)} – {v.change} · {links.source(v.decision)}")
        lines.append("")

    in_force = [(rule, v) for rule in rules if (v := rule.in_force(cutoff))]
    central = sum(1 for rule, _ in in_force if rule.vigtig)
    lines += [
        "## Gældende regler",
        "",
        f"{central} centrale regler og {len(in_force) - central} øvrige (interne procedurer og detaljer, "
        "nævnt til sidst under hvert område).",
        "",
    ]
    for area in AREAS:
        section = [(rule, v) for rule, v in in_force if rule.kategori in area.categories]
        if not section:
            continue
        lines += [f"### {area.title}", ""]
        for category in area.categories:
            items = [(rule, v) for rule, v in section if rule.kategori == category and rule.vigtig]
            if not items:
                continue
            if len(area.categories) > 1:
                lines += [f"*{CATEGORIES[category]}*", ""]
            for rule, v in items:
                origin = rule.adopted(v)
                stale = _stale_note(rule, cutoff)
                lines.append(f"- {link(rule)}: {v.essence} · siden {origin.effective[:4]} "
                             f"{links.source(origin.decision)}{f' · {stale}' if stale else ''}")
            lines.append("")
        minor = [rule for rule, _ in section if not rule.vigtig]
        if minor:
            lines += [f"Også i kraft: {', '.join(link(rule) for rule in minor)}.", ""]

    lines += [f"{WARNING} ved en kilde betyder, at Claudes citat ikke kunne genfindes ordret i referatet.", ""]
    return "\n".join(lines).rstrip() + "\n"


# --------------------------------------------------------------------------- area pages

def _area_page(area: Area, rules: list[Rule], today: date, links: _Links) -> tuple[str, dict[int, str]]:
    """Full text and history of every rule in the area; returns the page and each rule's anchor."""
    cutoff = today.isoformat()
    anchors = _Anchors()
    anchors.add(area.title)
    rule_anchors: dict[int, str] = {}
    lines = [
        f"# {area.title}",
        "",
        f"[Oversigt](../README.md) · [{today.year}](../{today.year}.md)",
        "",
        "Alle regler i området med fuld tekst og historik – også dem, der ikke længere gælder. "
        "Årssiderne viser, hvad der gjaldt i et bestemt år.",
        "",
    ]
    for category in area.categories:
        in_category = [rule for rule in rules if rule.kategori == category]
        if not in_category:
            continue
        if len(area.categories) > 1:
            anchors.add(CATEGORIES[category])
            lines += [f"## {CATEGORIES[category]}", ""]
        # Rules in force first, central before minor, then alphabetical.
        in_category.sort(key=lambda r: (r.in_force(cutoff) is None, not r.vigtig, r.titel.lower()))
        for rule in in_category:
            rule_anchors[id(rule)] = anchors.add(rule.titel)
            lines += _rule_entry(rule, cutoff, links)
    lines += [f"{WARNING} ved en kilde betyder, at Claudes citat ikke kunne genfindes ordret i referatet.", ""]
    return "\n".join(lines).rstrip() + "\n", rule_anchors


def _rule_entry(rule: Rule, cutoff: str, links: _Links) -> list[str]:
    current = rule.in_force(cutoff)
    content = [v for v in rule.versions if v.effekt in CONTENT_EFFECTS]
    upcoming = [v for v in rule.versions if (v.decision.dato or "") <= cutoff < v.effective]
    shown = current or (content[-1] if content else rule.versions[-1])

    if current:
        status = "**I kraft**"
    elif upcoming:
        status = f"**Vedtaget, gælder fra {_short_date(upcoming[0].effective)}**"
    elif content:
        status = "**Ikke længere i kraft**"
    else:
        status = "**Kun forslag – aldrig vedtaget**"
    if not rule.vigtig:
        status += " · detalje"

    lines = [f"### {rule.titel}", "", status, "", shown.text if content else shown.decision.tekst, ""]
    if content:
        lines += [f"*{' · '.join(_meta(rule, shown, links))}*", ""]
    for v in rule.versions:
        starts = f" (fra {_short_date(v.effective)})" if v.decision.gaelder_fra else ""
        lines.append(f"- {_short_date(v.decision.dato)} · {_labelled_change(v)}{starts} · {links.source(v.decision)}")
    lines.append("")
    if rule.note:
        lines += [f"> **Bemærk:** {rule.note}", ""]
    return lines


def _meta(rule: Rule, v: Version, links: _Links) -> list[str]:
    origin = rule.adopted(v)
    d = origin.decision
    verb = "fastslået" if origin.effekt == "bekraeftet" else "vedtaget"
    adopted = f"{verb} {_short_date(d.dato)} af {links.organ(d)}" + (f" ({d.stemmer})" if d.stemmer else "")
    meta = [NIVEAU_LABELS[d.niveau], adopted]
    if v is not origin:
        meta.append(f"senest bekræftet {_short_date(v.decision.dato)}")
    if v.decision.gaelder_til:
        meta.append(f"gælder til {_short_date(v.decision.gaelder_til)}")
    return meta


def _labelled_change(v: Version) -> str:
    """'ændret: hævet til 300 kr.' – without repeating the label when Claude's summary starts with it."""
    label = EFFEKT_LABELS[v.effekt]
    text = v.change
    if text.lower().startswith(label):
        text = text[len(label):].lstrip(" :–-") or text
    return f"{label}: {text}"


def _stale_note(rule: Rule, cutoff: str) -> str | None:
    """Rules are rarely repealed explicitly, so flag those nobody has mentioned for years."""
    last_mentioned = max((v.decision.dato or "")[:4] for v in rule.history(cutoff))
    if last_mentioned and int(cutoff[:4]) - int(last_mentioned) >= STALE_AFTER_YEARS:
        return f"sidst nævnt {last_mentioned}"
    return None


# --------------------------------------------------------------------------- index

def _index_page(years: list[int], rules: list[Rule], decisions: list[Decision], raw_rules: list[dict],
                docs: dict[str, Doc], missing: list[str], today: date) -> str:
    lines = [
        "# DSF-regelsæt – oversigt",
        "",
        f"Opdateret {_long_date(today)} ud fra {len(docs)} referater og regeldokumenter fra styrke.dk.",
        "",
        "## År",
        "",
        "Hver årsside starter med, hvad der er nyt det år, og viser derefter de regler, der var i kraft "
        "ved årets udgang (for indeværende år: i dag) – én linje pr. regel med kilde.",
        "",
        "| År | Ændringer i året | Centrale regler i kraft | Øvrige regler i kraft |",
        "|---|---|---|---|",
    ]
    for year in reversed(years):
        active = [rule for rule in rules if rule.in_force(_cutoff(year, today))]
        central = sum(1 for rule in active if rule.vigtig)
        changes = sum(
            1 for rule in rules for i, v in enumerate(rule.versions)
            if (v.decision.dato or "")[:4] == str(year)
            and v.effekt not in PROPOSAL_EFFECTS and (v.effekt != "bekraeftet" or i == 0)
        )
        status = " (indeværende)" if year == today.year else " (kommende)" if year > today.year else ""
        lines.append(f"| [{year}]({year}.md){status} | {changes} | {central} | {len(active) - central} |")

    cutoff = today.isoformat()
    lines += ["", "## Områder", "", "Fuld tekst og historik for hver regel:", ""]
    for area in AREAS:
        area_rules = [rule for rule in rules if rule.kategori in area.categories]
        active = sum(1 for rule in area_rules if rule.in_force(cutoff))
        lines.append(f"- [{area.title}]({AREA_DIR}/{area.file}.md) – {active} regler i kraft, "
                     f"{len(area_rules)} i alt")

    with_decisions = {d.doc_id for d in decisions}
    without_decisions = sum(1 for doc_id in docs if doc_id not in with_decisions and doc_id not in missing)
    assigned = {v["ref"] for raw in raw_rules for v in raw["versioner"]}
    lines += [
        "",
        "## Sådan læses reglerne",
        "",
        "Hjemmel, fra højeste til laveste vægt:",
        "",
        "1. **Vedtægt** – vedtaget af Repræsentantskabet med 2/3 flertal.",
        "2. **Stævneregel** – vedtaget af Repræsentantskabet med simpelt flertal (inkl. gebyrer i budgettet).",
        "3. **Bestyrelsesbeslutning** – bestyrelsens politikker og præciseringer.",
        "4. **Udvalgsbeslutning** – udvalgenes retningslinjer (elite, stævne, dommer, master …).",
        "5. **Eksternt krav** – krav fra IPF, EPF, DIF eller Anti Doping Danmark.",
        "",
        "Regler ophæves sjældent formelt, så en regel gælder her, indtil et senere referat ændrer eller "
        "ophæver den. «Sidst nævnt» markerer regler, som ingen referater har nævnt i mindst "
        f"{STALE_AFTER_YEARS} år – de kan være gået i glemmebogen.",
        "",
        "## Datakvalitet",
        "",
        f"- Dokumenter der mangler analyse (kør `uv run update.py` igen): {len(missing)}"
        + (f" ({', '.join(missing)})" if missing else ""),
        f"- Dokumenter uden regelbeslutninger (fx budgetmøder): {without_decisions}",
        f"- Beslutninger hvor citatet ikke kunne genfindes ordret ({WARNING}): "
        f"{sum(1 for d in decisions if not d.citat_fundet)}",
        f"- Engangsbeslutninger uden for reglerne (kun i data/beslutninger): "
        f"{sum(1 for d in decisions if d.ref not in assigned)}",
        "",
        "Udtrækket er lavet automatisk af Claude og kan indeholde fejl. Referatet er altid den gældende kilde.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- helpers

class _Links:
    def __init__(self, docs: dict[str, Doc], to_root: str):
        self.docs = docs
        self.to_root = to_root  # relative path from the page to the project root

    def source(self, d: Decision) -> str:
        doc = self.docs.get(d.doc_id)
        label = d.doc_id + (f" s. {d.side}" if d.side else "")
        if doc is None:
            target = None
        elif doc.url:
            target = doc.url + (f"#page={d.side}" if d.side and doc.path.lower().endswith(".pdf") else "")
        else:
            target = self.to_root + quote(doc.path)
        warning = f" {WARNING}" if not d.citat_fundet else ""
        return (f"[{label}]({target})" if target else label) + warning

    def organ(self, d: Decision) -> str:
        doc = self.docs.get(d.doc_id)
        return doc.organ_label if doc else "?"


class _Anchors:
    """Heading anchors as GitHub generates them: lowercase, punctuation dropped, each space a
    hyphen, and repeated headings suffixed -1, -2 … in document order."""

    def __init__(self) -> None:
        self.seen: dict[str, int] = {}

    def add(self, heading: str) -> str:
        base = re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
        count = self.seen.get(base, 0)
        self.seen[base] = count + 1
        return base if count == 0 else f"{base}-{count}"


def _cutoff(year: int, today: date) -> str:
    """The date a year page shows rules for: today for the current year, else 31 December."""
    return today.isoformat() if year == today.year else f"{year}-12-31"


def _short_date(value: str | None) -> str:
    if not value:
        return "ukendt dato"
    if len(value) == 10:
        return f"{value[8:10]}.{value[5:7]}.{value[:4]}"
    return value


def _day_month(value: str | None) -> str:
    """Date within a year page, where the year is implied."""
    if value and len(value) == 10:
        return f"{value[8:10]}.{value[5:7]}"
    return value or "?"


def _long_date(value: date) -> str:
    return f"{value.day}. {MONTH_NAMES[value.month - 1]} {value.year}"
