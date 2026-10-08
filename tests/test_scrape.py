from pathlib import Path

import httpx
import pytest

import scrape

URL = "https://filer.styrke.dk/referater/rep2024.pdf"
UPPER = URL[:-4] + ".PDF"


@pytest.fixture
def local_file(tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "ROOT", tmp_path)
    monkeypatch.setattr(scrape, "PDF_ROOT", tmp_path)
    monkeypatch.setattr(scrape.time, "sleep", lambda _: None)
    path = tmp_path / "rep2024.pdf"
    path.write_bytes(b"old")
    return path


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def _only_upper_case(request):
    """The server is case-sensitive and only has the file as .PDF."""
    if str(request.url).endswith(".PDF"):
        return httpx.Response(200, headers={"content-length": "3"}, content=b"new" if request.method == "GET" else b"")
    return httpx.Response(404)


def test_unchanged_file_is_kept(local_file):
    def handler(request):
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"content-length": "3"})

    assert scrape._refresh(_client(handler), URL, local_file, URL) == URL
    assert local_file.read_bytes() == b"old"


def test_replaced_file_is_downloaded_again(local_file):
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "9"})
        return httpx.Response(200, content=b"corrected")

    assert scrape._refresh(_client(handler), URL, local_file, URL) == URL
    assert local_file.read_bytes() == b"corrected"
    assert [p.name for p in local_file.parent.iterdir()] == ["rep2024.pdf"]


def test_cut_off_download_keeps_the_copy(local_file):
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "9"})
        return httpx.Response(200, content=b"corr")

    assert scrape._refresh(_client(handler), URL, local_file, URL) == URL
    assert local_file.read_bytes() == b"old"


def test_compressed_length_is_not_compared(local_file):
    def handler(request):
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"content-length": "2", "content-encoding": "gzip"})

    assert scrape._refresh(_client(handler), URL, local_file, URL) == URL
    assert local_file.read_bytes() == b"old"


def test_failed_write_keeps_the_copy(local_file, monkeypatch):
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "9"})
        return httpx.Response(200, content=b"corrected")

    def disk_full(self, target):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "replace", disk_full)
    assert scrape._refresh(_client(handler), URL, local_file, URL) == URL
    assert local_file.read_bytes() == b"old"
    assert [p.name for p in local_file.parent.iterdir()] == ["rep2024.pdf"]


def test_the_case_that_answers_is_cited(local_file):
    assert scrape._refresh(_client(_only_upper_case), URL, local_file, URL) == UPPER


def test_new_file_is_cited_with_the_case_that_answers(local_file):
    link = scrape.Link(URL, "Referat", "repraesentantskab")
    path, url = scrape._download(_client(_only_upper_case), link)
    assert url == UPPER and path.read_bytes() == b"new"


def test_unlinked_file_is_cited_with_the_case_that_answers():
    assert scrape._working_url(_client(_only_upper_case), URL) == UPPER
    assert scrape._working_url(_client(lambda request: httpx.Response(404)), URL) is None


def test_dead_link_keeps_the_copy_without_url(local_file):
    assert scrape._refresh(_client(lambda request: httpx.Response(404)), URL, local_file, URL) is None
    assert local_file.read_bytes() == b"old"


def test_unreachable_server_changes_nothing(local_file):
    def handler(request):
        raise httpx.ConnectError("no network")

    assert scrape._refresh(_client(handler), URL, local_file, UPPER) == UPPER  # keeps the URL cited so far
    assert scrape._working_url(_client(handler), URL) == URL
    assert local_file.read_bytes() == b"old"


def test_new_file_that_cannot_be_saved_is_skipped(local_file, monkeypatch):
    def disk_full(self, content):
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "write_bytes", disk_full)
    assert scrape._download(_client(_only_upper_case), scrape.Link(URL, "Referat", "repraesentantskab")) is None
