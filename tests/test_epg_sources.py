from __future__ import annotations

import base64
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from app.epg_sources import download_epg_source, effective_epg_source_url, search_epgpw_channels


class ResponseBytes(BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


def test_epgpw_effective_url_replaces_stale_date_with_current_timezone_date() -> None:
    source_url = (
        "https://epg.pw/api/epg.xml?lang=en&date=20260606"
        "&channel_id=493395&timezone=Asia/Jerusalem"
    )

    assert effective_epg_source_url(
        source_url,
        now=datetime(2026, 5, 6, 4, 0, tzinfo=timezone.utc),
    ) == (
        "https://epg.pw/api/epg.xml?channel_id=493395&lang=en"
        "&timezone=QXNpYS9KZXJ1c2FsZW0%3D&date=20260506"
    )


def test_download_epg_source_fetches_effective_epgpw_url_and_accepts_plain_xml(
    tmp_path: Path, monkeypatch
) -> None:
    payload = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<tv><channel id='493395'><display-name>Eye Oscar</display-name></channel></tv>"
    ).encode("utf-8")
    captured_urls: list[str] = []

    def fake_urlopen(source_url: str, timeout: int):
        captured_urls.append(source_url)
        assert timeout == 180
        return ResponseBytes(payload)

    monkeypatch.setattr("app.epg_sources.request.urlopen", fake_urlopen)

    destination = tmp_path / "source-1.xmltv"
    effective_url = download_epg_source(
        "https://epg.pw/last/493395.html?lang=en&timezone=QXNpYS9KZXJ1c2FsZW0%3D",
        destination,
    )

    assert captured_urls == [effective_url]
    assert "date=" in effective_url
    assert "timezone=QXNpYS9KZXJ1c2FsZW0%3D" in effective_url
    assert destination.read_bytes() == payload


def test_search_epgpw_channels_empty_query() -> None:
    assert search_epgpw_channels("") == []
    assert search_epgpw_channels("   ") == []


def test_search_epgpw_channels_single_result(monkeypatch) -> None:
    html = """<tr>
<td><a class="is-text button" href="/last/7479.html?lang=en" target="_blank"
title="View channel">BCU Catastrophe HD[Russian Federation]</a></td>
<td></td>
<td>Russian Federation</td>"""

    def fake_urlopen(url: str, timeout: int):
        return ResponseBytes(html.encode("utf-8"))

    monkeypatch.setattr("app.epg_sources.request.urlopen", fake_urlopen)

    results = search_epgpw_channels("BCU Catastrophe HD")
    assert len(results) == 1
    assert results[0]["channel_id"] == "7479"
    assert results[0]["display_name"] == "BCU Catastrophe HD"
    assert results[0]["country"] == "Russian Federation"


def test_search_epgpw_channels_multi_result(monkeypatch) -> None:
    html = """<tr>
<td><a class="is-text button" href="/last/55749.html?lang=en" target="_blank"
title="View channel">BBC NEWS[France]</a></td></tr>
<tr>
<td><a class="is-text button" href="/last/76650.html?lang=en" target="_blank"
title="View channel">BBC News[Germany]</a></td></tr>"""

    def fake_urlopen(url: str, timeout: int):
        return ResponseBytes(html.encode("utf-8"))

    monkeypatch.setattr("app.epg_sources.request.urlopen", fake_urlopen)

    results = search_epgpw_channels("BBC NEWS")
    assert len(results) == 2
    assert results[0]["channel_id"] == "55749"
    assert results[0]["display_name"] == "BBC NEWS"
    assert results[0]["country"] == "France"
    assert results[1]["channel_id"] == "76650"
    assert results[1]["display_name"] == "BBC News"
    assert results[1]["country"] == "Germany"


def test_search_epgpw_channels_no_results(monkeypatch) -> None:
    html = "<html><body>No channels found</body></html>"

    def fake_urlopen(url: str, timeout: int):
        return ResponseBytes(html.encode("utf-8"))

    monkeypatch.setattr("app.epg_sources.request.urlopen", fake_urlopen)

    results = search_epgpw_channels("ZZZZNOTFOUND")
    assert results == []


def test_search_epgpw_channels_network_error(monkeypatch) -> None:
    def fake_urlopen(url: str, timeout: int):
        raise OSError("Connection refused")

    monkeypatch.setattr("app.epg_sources.request.urlopen", fake_urlopen)

    results = search_epgpw_channels("BBC")
    assert results == []


def test_search_epgpw_channels_name_without_country_bracket(monkeypatch) -> None:
    html = """<tr>
<td><a class="is-text button" href="/last/12345.html?lang=en" target="_blank"
title="View channel">Simple Channel Name</a></td></tr>"""

    def fake_urlopen(url: str, timeout: int):
        return ResponseBytes(html.encode("utf-8"))

    monkeypatch.setattr("app.epg_sources.request.urlopen", fake_urlopen)

    results = search_epgpw_channels("Simple Channel")
    assert len(results) == 1
    assert results[0]["channel_id"] == "12345"
    assert results[0]["display_name"] == "Simple Channel Name"
    assert results[0]["country"] == ""


def test_search_epgpw_channels_url_construction(monkeypatch) -> None:
    captured_urls: list[str] = []

    def fake_urlopen(url: str, timeout: int):
        captured_urls.append(url)
        return ResponseBytes(b"<html></html>")

    monkeypatch.setattr("app.epg_sources.request.urlopen", fake_urlopen)
    search_epgpw_channels("BBC HD")

    assert len(captured_urls) == 1
    url = captured_urls[0]
    assert url.startswith("https://epg.pw/search/channel/")
    assert "lang=en" in url
    assert "timezone=" in url
    encoded_part = url.removeprefix("https://epg.pw/search/channel/").split("?")[0].removesuffix(".html")
    decoded = base64.b64decode(encoded_part).decode()
    assert decoded == "BBC HD"

