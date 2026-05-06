from __future__ import annotations

import gzip
from pathlib import Path

from app.admin_epg import sync_epg


def test_sync_epg_returns_selected_channel_icons(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "published" / "epg.xml"
    work_dir = tmp_path / "epg"
    payload = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<tv>"
        "<channel id='chan-one'>"
        "<display-name>Channel One</display-name>"
        "<icon src='http://epg.example/icon.png'/>"
        "</channel>"
        "<programme channel='chan-one' start='20260504000000 +0000' stop='20260504010000 +0000'>"
        "<title>Show</title>"
        "</programme>"
        "</tv>"
    ).encode("utf-8")

    def fake_download_epg(source_url: str, destination: Path) -> None:
        del source_url
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(gzip.compress(payload))

    monkeypatch.setattr("app.admin_epg.download_epg_source", fake_download_epg)

    result = sync_epg(
        published_channels=[
            {
                "channel_id": 1,
                "name": "Channel One",
                "mappings": [
                    {
                        "source_key": "source-1",
                        "channel_id": "chan-one",
                    }
                ],
            }
        ],
        epg_sources=[{"id": 1, "source_url": "http://epg.example/source.xml.gz", "enabled": True}],
        output_path=output_path,
        work_dir=work_dir,
    )

    assert result.channel_icons == {1: "http://epg.example/icon.png"}


def test_sync_epg_accepts_plain_xml_epgpw_source(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "published" / "epg.xml"
    work_dir = tmp_path / "epg"
    captured: list[tuple[str, str]] = []
    payload = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<tv>"
        "<channel id='493395'>"
        "<display-name>Eye Oscar</display-name>"
        "<icon src='https://epg.pw/media/eye-oscar.png'/>"
        "</channel>"
        "<programme channel='493395' start='20260506010300 +0300' stop='20260506030500 +0300'>"
        "<title>Movie</title>"
        "</programme>"
        "</tv>"
    ).encode("utf-8")

    def fake_download_epg(source_url: str, destination: Path) -> None:
        captured.append((source_url, destination.name))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    monkeypatch.setattr("app.admin_epg.download_epg_source", fake_download_epg)

    result = sync_epg(
        published_channels=[
            {
                "channel_id": 1,
                "name": "Eye Oscar",
                "mappings": [
                    {
                        "source_key": "source-1",
                        "channel_id": "493395",
                    }
                ],
            }
        ],
        epg_sources=[
            {
                "id": 1,
                "source_url": "https://epg.pw/api/epg.xml?channel_id=493395&date=20260606&lang=en&timezone=Asia/Jerusalem",
                "enabled": True,
            }
        ],
        output_path=output_path,
        work_dir=work_dir,
    )

    assert captured == [
        (
            "https://epg.pw/api/epg.xml?channel_id=493395&date=20260606&lang=en&timezone=Asia/Jerusalem",
            "source-1.xmltv",
        )
    ]
    assert result.matched_channels == 1
    assert result.programmes == 1
    assert result.channel_icons == {1: "https://epg.pw/media/eye-oscar.png"}
    assert 'channel id="493395"' in output_path.read_text(encoding="utf-8")


def test_sync_epg_uses_cached_legacy_source_when_download_fails(
    tmp_path: Path, monkeypatch
) -> None:
    output_path = tmp_path / "published" / "epg.xml"
    work_dir = tmp_path / "epg"
    work_dir.mkdir(parents=True, exist_ok=True)
    work_dir.joinpath("source.xml.gz").write_bytes(
        gzip.compress(
            (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<tv>"
                "<channel id='chan-one'>"
                "<display-name>Channel One</display-name>"
                "<icon src='http://epg.example/cached-icon.png'/>"
                "</channel>"
                "<programme channel='chan-one' start='20260504000000 +0000' stop='20260504010000 +0000'>"
                "<title>Show</title>"
                "</programme>"
                "</tv>"
            ).encode("utf-8")
        )
    )

    def failing_download_epg(source_url: str, destination: Path) -> None:
        del source_url, destination
        raise OSError("temporary name resolution failure")

    monkeypatch.setattr("app.admin_epg.download_epg_source", failing_download_epg)

    result = sync_epg(
        published_channels=[
            {
                "channel_id": 1,
                "name": "Channel One",
                "mappings": [
                    {
                        "source_key": "source-1",
                        "channel_id": "chan-one",
                    }
                ],
            }
        ],
        epg_sources=[{"id": 1, "source_url": "http://epg.one/epg2.xml.gz", "enabled": True}],
        output_path=output_path,
        work_dir=work_dir,
    )

    assert result.failed_sources == ["http://epg.one/epg2.xml.gz"]
    assert result.matched_channels == 1
    assert result.programmes == 1
    assert result.channel_icons == {1: "http://epg.example/cached-icon.png"}


def test_fetch_source_downloads_and_returns_channel_list(
    tmp_path: Path, monkeypatch
) -> None:
    from app.admin_epg import fetch_source

    work_dir = tmp_path / "epg"
    work_dir.mkdir()
    payload = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<tv>"
        "<channel id='chan-one'>"
        "<display-name>Channel One</display-name>"
        "<icon src='http://epg.example/icon.png'/>"
        "</channel>"
        "<channel id='chan-two'>"
        "<display-name>Channel Two</display-name>"
        "</channel>"
        "<programme channel='chan-one' start='20260504000000 +0000' stop='20260504010000 +0000'>"
        "<title>Show</title>"
        "</programme>"
        "</tv>"
    ).encode("utf-8")

    def fake_download_epg(source_url: str, destination: Path) -> None:
        del source_url
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(gzip.compress(payload))

    monkeypatch.setattr("app.admin_epg.download_epg_source", fake_download_epg)

    result = fetch_source(
        source_url="http://epg.example/source.xml.gz",
        source_id=1,
        work_dir=work_dir,
    )

    assert result["status"] == "ok"
    assert result["channel_count"] == 2
    assert result["channels"][0]["id"] == "chan-one"
    assert result["channels"][1]["id"] == "chan-two"


def test_fetch_source_uses_cached_file_when_download_fails(
    tmp_path: Path, monkeypatch
) -> None:
    from app.admin_epg import fetch_source

    work_dir = tmp_path / "epg"
    work_dir.mkdir()
    work_dir.joinpath("source-1.xml.gz").write_bytes(
        gzip.compress(
            (
                "<?xml version='1.0' encoding='UTF-8'?>"
                "<tv>"
                "<channel id='cached-one'>"
                "<display-name>Cached Channel</display-name>"
                "</channel>"
                "<programme channel='cached-one' start='20260504000000 +0000' stop='20260504010000 +0000'>"
                "<title>Show</title>"
                "</programme>"
                "</tv>"
            ).encode("utf-8")
        )
    )

    def failing_download(source_url: str, destination: Path) -> None:
        del source_url, destination
        raise OSError("download failed")

    monkeypatch.setattr("app.admin_epg.download_epg_source", failing_download)

    result = fetch_source(
        source_url="http://epg.one/epg2.xml.gz",
        source_id=1,
        work_dir=work_dir,
    )

    assert result["status"] == "ok"
    assert result["channel_count"] == 1
    assert result["channels"][0]["id"] == "cached-one"


def test_sync_epg_finds_icon_via_implicit_tvg_id_mapping_across_sources(
    tmp_path: Path, monkeypatch
) -> None:
    output_path = tmp_path / "published" / "epg.xml"
    work_dir = tmp_path / "epg"
    israel_channel_id = "\u05e2\u05e8\u05d5\u05e514.il"
    general_payload = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<tv>"
        "<channel id='some-other-id'>"
        "<display-name>Other Channel</display-name>"
        "</channel>"
        "<programme channel='some-other-id' start='20260504000000 +0000' stop='20260504010000 +0000'>"
        "<title>Other Show</title>"
        "</programme>"
        "</tv>"
    ).encode("utf-8")
    israel_payload = (
        "<?xml version='1.0' encoding='UTF-8'?>"
        "<tv>"
        f"<channel id='{israel_channel_id}'>"
        "<display-name>Channel 14</display-name>"
        "<icon src='http://epg.example/ch14-icon.png'/>"
        "</channel>"
        f"<programme channel='{israel_channel_id}' start='20260504000000 +0000' stop='20260504010000 +0000'>"
        "<title>Show</title>"
        "</programme>"
        "</tv>"
    ).encode("utf-8")

    def fake_download_epg(source_url: str, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if "epg.one" in source_url:
            destination.write_bytes(gzip.compress(general_payload))
        else:
            destination.write_bytes(gzip.compress(israel_payload))

    monkeypatch.setattr("app.admin_epg.download_epg_source", fake_download_epg)

    result = sync_epg(
        published_channels=[
            {
                "channel_id": 6,
                "name": "Channel 14 FHD IL",
                "mappings": [
                    {"source_key": "source-1", "channel_id": "ערוץ14.il"},
                    {"source_key": "source-2", "channel_id": "ערוץ14.il"},
                ],
            }
        ],
        epg_sources=[
            {"id": 1, "source_url": "http://epg.one/epg2.xml.gz", "enabled": True},
            {"id": 2, "source_url": "http://iptv-epg.example/epg-il.xml.gz", "enabled": True},
        ],
        output_path=output_path,
        work_dir=work_dir,
    )

    assert result.channel_icons == {6: "http://epg.example/ch14-icon.png"}
    assert result.matched_channels == 1
