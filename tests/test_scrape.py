import httpx
import pytest

import scrape

URL = "https://filer.styrke.dk/referater/rep2024.pdf"


@pytest.fixture
def local_file(tmp_path, monkeypatch):
    monkeypatch.setattr(scrape, "ROOT", tmp_path)
    monkeypatch.setattr(scrape.time, "sleep", lambda _: None)
    path = tmp_path / "rep2024.pdf"
    path.write_bytes(b"old")
    return path


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_unchanged_file_is_kept(local_file):
    def handler(request):
        assert request.method == "HEAD"
        return httpx.Response(200, headers={"content-length": "3"})

    assert scrape._refresh(_client(handler), URL, local_file) == URL
    assert local_file.read_bytes() == b"old"


def test_replaced_file_is_downloaded_again(local_file):
    def handler(request):
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-length": "9"})
        return httpx.Response(200, content=b"corrected")

    assert scrape._refresh(_client(handler), URL, local_file) == URL
    assert local_file.read_bytes() == b"corrected"


def test_other_case_of_the_extension_is_tried(local_file):
    def handler(request):
        if str(request.url).endswith(".PDF"):
            return httpx.Response(200, headers={"content-length": "3"})
        return httpx.Response(404)

    assert scrape._refresh(_client(handler), URL, local_file) == URL


def test_dead_link_keeps_the_copy_without_url(local_file):
    assert scrape._refresh(_client(lambda request: httpx.Response(404)), URL, local_file) is None
    assert local_file.read_bytes() == b"old"


def test_unreachable_server_changes_nothing(local_file):
    def handler(request):
        raise httpx.ConnectError("no network")

    assert scrape._refresh(_client(handler), URL, local_file) == URL
    assert local_file.read_bytes() == b"old"
