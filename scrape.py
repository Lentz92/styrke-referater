"""Download minutes and rule documents from styrke.dk and keep data/manifest.json in sync."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass, replace
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx
import pymupdf
from bs4 import BeautifulSoup
from bs4.dammit import EncodingDetector

ROOT = Path(__file__).parent
PDF_ROOT = ROOT / "referater"
DATA_DIR = ROOT / "data"
MANIFEST = DATA_DIR / "manifest.json"

INDEX_URL = "https://styrke.dk/?page=referater"
HEADERS = {"User-Agent": "styrke-referater-scraper/1.0 (Nicki Lentz; nickilentz@hotmail.com)"}
DOC_SUFFIXES = (".pdf", ".htm")
# Obituaries are linked next to the minutes but never contain decisions.
SKIP_PREFIXES = ("mindeord",)

# Section heading on the index page -> organ key (also the local folder name).
SECTIONS = {
    "Bestyrelsesmøder": "bestyrelse",
    "Eliteudvalgsmøder": "eliteudvalg",
    "Stævneudvalg": "staevneudvalg",
    "Medieudvalg": "medieudvalg",
    "Masterudvalget": "masterudvalget",
    "Dommerudvalg": "dommerudvalg",
    "Repræsentantskabsmøder": "repraesentantskab",
    "DIF og Internationalt arbejde": "dif_internationalt",
    "Udstyrsgruppen": "udstyrsgruppen",
    "Andet": "andet",
}
ORGAN_LABELS = {
    "repraesentantskab": "Repræsentantskabet",
    "bestyrelse": "Bestyrelsen",
    "eliteudvalg": "Eliteudvalget",
    "staevneudvalg": "Stævneudvalget",
    "dommerudvalg": "Dommerudvalget",
    "masterudvalget": "Masterudvalget",
    "medieudvalg": "Medieudvalget",
    "udstyrsgruppen": "Udstyrsgruppen",
    "dif_internationalt": "DIF/IPF/EPF",
    "andet": "Andet",
}
MONTHS = {
    name: number
    for number, name in enumerate(
        ["januar", "februar", "marts", "april", "maj", "juni", "juli",
         "august", "september", "oktober", "november", "december"],
        start=1,
    )
}

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Doc:
    id: str
    organ: str
    title: str
    date: str | None  # ISO date, or just the year, from the index page or filename
    path: str  # relative to ROOT
    url: str | None  # None when styrke.dk no longer links to the file
    sha256: str

    @property
    def organ_label(self) -> str:
        return ORGAN_LABELS.get(self.organ, self.organ)


@dataclass(frozen=True)
class Link:
    url: str
    title: str
    organ: str

    @property
    def filename(self) -> str:
        return unquote(urlparse(self.url).path.rsplit("/", maxsplit=1)[-1])


def load_manifest() -> list[Doc]:
    if not MANIFEST.exists():
        raise SystemExit(f"{MANIFEST} is missing; run without --offline first.")
    return [Doc(**entry) for entry in json.loads(MANIFEST.read_text())]


def sync() -> list[Doc]:
    """Download new and replaced documents, index everything under referater/ and write the manifest."""
    previous = {doc.id: doc for doc in load_manifest()} if MANIFEST.exists() else {}
    local = {
        p.name.lower(): p
        for p in PDF_ROOT.rglob("*")
        if _is_document(p.name) and not p.name.lower().startswith(SKIP_PREFIXES)
    }

    with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=30) as client:
        response = client.get(INDEX_URL)
        response.raise_for_status()
        links = parse_index(response.content)
        log.info("Found %d document links on %s", len(links), INDEX_URL)

        docs: dict[str, Doc] = {}
        for link in links:
            path = local.get(link.filename.lower())
            url: str | None
            if path is None:
                downloaded = _download(client, link)
                if downloaded is None:
                    continue
                path, url = downloaded
            else:
                old = previous.get(path.stem)
                url = _refresh(client, link.url, path, fallback=old.url if old and old.url else link.url)
            local[path.name.lower()] = path
            doc = _make_doc(path, link.title, link.organ, url)
            if doc.date is None:
                # Standing rule documents (e.g. Adfaerdskodeks) carry no date in name or title.
                doc = replace(doc, date=_last_modified(client, url or link.url))
            docs.setdefault(doc.id, doc)

        # Files we still have but styrke.dk no longer links to (e.g. last year's deadlines) keep
        # their history value, so they stay in the manifest, citing their old URL while it works.
        linked = {doc.path for doc in docs.values()}
        for path in sorted(local.values()):
            rel = str(path.relative_to(ROOT))
            if rel in linked:
                continue
            old = previous.get(path.stem)
            url = old.url if old else None
            if url:
                working = _working_url(client, url)
                if working is None:
                    log.warning("The link to %s no longer works; using the copy in %s", url, rel)
                url = working
            doc = _make_doc(path, old.title if old else path.stem, old.organ if old else path.parent.name, url)
            if old and doc.date is None:
                doc = replace(doc, date=old.date)
            if doc.id in docs:
                log.warning("Duplicate id %s: ignoring %s", doc.id, rel)
                continue
            docs[doc.id] = doc

    result = sorted(docs.values(), key=lambda d: (d.organ, d.date or "", d.id))
    DATA_DIR.mkdir(exist_ok=True)
    MANIFEST.write_text(json.dumps([asdict(d) for d in result], ensure_ascii=False, indent=1) + "\n")
    log.info("Manifest: %d documents", len(result))
    return result


def parse_index(html: bytes) -> list[Link]:
    # The page declares both iso-8859-1 and utf-8; the bytes are utf-8.
    soup = BeautifulSoup(html, "html.parser", from_encoding="utf-8")
    content = soup.find(id="dbcontent")
    links: list[Link] = []
    section = "Andet"
    for el in content.find_all(["b", "a"]):
        if el.name == "b":
            section = " ".join(el.get_text(" ", strip=True).split())
            if section not in SECTIONS:
                log.warning("Unknown section on the minutes page: %r", section)
        elif _is_document(el.get("href", "")):
            organ = SECTIONS.get(section) or re.sub(r"\W+", "_", section.lower()).strip("_")
            links.append(_link(el, organ))

    # Rule documents (masterudvalg requirements, deadlines) live in the site menu.
    in_content = {id(a) for a in content.find_all("a")}
    for a in soup.find_all("a", href=True):
        if id(a) not in in_content and _is_document(a["href"]):
            links.append(_link(a, "masterudvalget" if "/masterudvalg/" in a["href"] else "andet"))

    return [link for link in links if not link.filename.lower().startswith(SKIP_PREFIXES)]


def document_text(doc: Doc) -> str:
    """Plain text with [Side N] markers so extracted decisions can cite pages."""
    path = ROOT / doc.path
    if path.suffix.lower() == ".htm":
        return BeautifulSoup(_decode_html(path.read_bytes()), "html.parser").get_text("\n")
    with pymupdf.open(path) as pdf:
        return "\n".join(f"[Side {number}]\n{page.get_text()}" for number, page in enumerate(pdf, start=1))


def _decode_html(data: bytes) -> str:
    """The declared charset, else UTF-8 if the bytes are valid UTF-8, else windows-1252.

    Given bytes, BeautifulSoup asks whichever charset detector happens to be importable (chardet on GitHub's
    runner read undeclared UTF-8 as windows-1252), so we decode here and hand it text: no detector is consulted.
    """
    if declared := EncodingDetector.find_declared_encoding(data, is_html=True):
        try:
            return data.decode(declared, errors="replace")
        except LookupError:  # a charset Python does not know
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("windows-1252", errors="replace")


def parse_date(title: str, filename: str) -> str | None:
    """Meeting date from link text ("Referat fra d. 8 marts 2025") or filename (refbest_080325)."""
    text = title.lower()
    if m := re.search(r"(\d{1,2})\.?\s+(" + "|".join(MONTHS) + r")\s+(\d{4})", text):
        return _iso(int(m[3]), MONTHS[m[2]], int(m[1]))
    stem = filename.lower()
    if m := re.search(r"(?<!\d)(\d{2})(\d{2})((?:19|20)\d{2})(?!\d)", stem):
        return _iso(int(m[3]), int(m[2]), int(m[1]))
    if m := re.search(r"(?<!\d)(\d{2})(\d{2})(\d{2})(?!\d)", stem):
        return _iso(2000 + int(m[3]), int(m[2]), int(m[1]))
    if m := re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", f"{text} {stem}"):
        return m[1]
    return None


def _iso(year: int, month: int, day: int) -> str | None:
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _last_modified(client: httpx.Client, url: str) -> str | None:
    try:
        header = client.head(url).headers.get("last-modified")
        return parsedate_to_datetime(header).date().isoformat() if header else None
    except (httpx.HTTPError, ValueError, TypeError):
        return None


def _is_document(href: str) -> bool:
    return href.lower().endswith(DOC_SUFFIXES)


def _link(a, organ: str) -> Link:
    url = urljoin(INDEX_URL, a["href"])
    parsed = urlparse(url)
    # Re-encode so non-ASCII filenames (dif_aarsmøde_2025.pdf) become valid URLs.
    url = parsed._replace(path=quote(unquote(parsed.path), safe="/")).geturl()
    return Link(url=url, title=" ".join(a.get_text(" ", strip=True).split()), organ=organ)


def _make_doc(path: Path, title: str, organ: str, url: str | None) -> Doc:
    return Doc(
        id=path.stem,
        organ=organ,
        title=title,
        date=parse_date(title, path.stem),
        path=str(path.relative_to(ROOT)),
        url=url,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def _variants(url: str) -> list[str]:
    # The file server is case-sensitive and some links say .pdf for files named .PDF.
    if url.endswith(".pdf"):
        return [url, url[:-4] + ".PDF"]
    if url.endswith(".PDF"):
        return [url, url[:-4] + ".pdf"]
    return [url]


def _head(client: httpx.Client, url: str) -> httpx.Response | None:
    """HEAD response for the first variant of the URL that exists; None when all answer 404.

    The response's URL is the variant that answered, which is the one to cite.
    Raises httpx.HTTPError when styrke.dk can't be reached.
    """
    for variant in _variants(url):
        response = client.head(variant)
        if response.status_code != 404:
            return response
    return None


def _working_url(client: httpx.Client, url: str) -> str | None:
    """The variant of the URL that answers, or None when every variant answers 404.

    An unreachable server is not the same as a gone file, so then the URL is kept as it is.
    """
    try:
        response = _head(client, url)
    except httpx.HTTPError:
        return url
    return str(response.url) if response else None


def _refresh(client: httpx.Client, url: str, path: Path, fallback: str) -> str | None:
    """Check a file we already have against styrke.dk and return the URL to cite for it.

    A file with another size than ours has been replaced there, so it is downloaded again. A link
    that answers 404 is dead: the copy in referater/ stays, and the document gets no URL. When
    styrke.dk can't be reached, `fallback` (the URL cited so far) is kept.
    """
    cited = fallback
    try:
        response = _head(client, url)
        if response is None:
            log.warning("The link to %s no longer works; using the copy in %s", url, path.relative_to(ROOT))
            return None
        cited = str(response.url)
        size = _plain_size(response)
        if response.is_success and size is not None and size != path.stat().st_size:
            fresh = client.get(cited)
            if not fresh.is_success:
                log.warning("HTTP %s for %s; keeping the copy", fresh.status_code, cited)
            elif len(fresh.content) != size:
                # A cut-off transfer must not replace a good copy.
                log.warning("%s returned %d bytes, not %d; keeping the copy", cited, len(fresh.content), size)
            else:
                _write_atomic(path, fresh.content)
                log.info("%s had changed on styrke.dk and was downloaded again", path.relative_to(ROOT))
    except httpx.HTTPError as exc:
        log.warning("Could not check %s: %s", url, exc)
    except OSError as exc:
        log.warning("Could not save %s: %s", path.relative_to(ROOT), exc)
    time.sleep(0.1)
    return cited


def _plain_size(response: httpx.Response) -> int | None:
    """The file's size from a HEAD response; None when unknown or when it is the compressed size."""
    size = response.headers.get("content-length", "")
    if "content-encoding" in response.headers or not size.isdigit():
        return None
    return int(size)


def _write_atomic(path: Path, content: bytes) -> None:
    """Write a file whole: to a temporary file next to it, then renamed over it, so a run killed meanwhile leaves the
    old file or the new one, never part of one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_bytes(content)
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def _download(client: httpx.Client, link: Link) -> tuple[Path, str] | None:
    """Download a new document; returns where it was saved and the URL that answered."""
    for url in _variants(link.url):
        try:
            response = client.get(url)
        except httpx.HTTPError as exc:
            log.error("Could not download %s: %s", url, exc)
            return None
        if response.status_code == 404:
            continue
        if response.is_error:
            log.warning("HTTP %s for %s", response.status_code, url)
            return None
        dest = PDF_ROOT / link.organ / unquote(url.rsplit("/", maxsplit=1)[-1])
        try:
            _write_atomic(dest, response.content)
        except OSError as exc:
            log.error("Could not save %s: %s", dest.relative_to(ROOT), exc)
            return None
        log.info("Downloaded %s (%.0f KB)", dest.relative_to(ROOT), len(response.content) / 1024)
        time.sleep(0.5)
        return dest, str(response.url)

    log.warning("404 for %s", link.url)
    return None
