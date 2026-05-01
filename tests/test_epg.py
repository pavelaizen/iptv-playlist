from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

from app import epg


def write_gzip(path: Path, text: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)


def read_gzip(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def test_extract_playlist_channel_names_reads_extinf_without_urls(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        '#EXTINF:-1 tvg-id="one",  Channel One  \n'
        "http://provider.invalid/secret-token\n"
        "#EXTGRP:News\n"
        "#EXTINF:0,Channel Two\n"
        "http://provider.invalid/another-token\n",
        encoding="utf-8",
    )

    names = epg.extract_playlist_channel_names(playlist)

    assert names == ["Channel One", "Channel Two"]
    assert "provider.invalid" not in repr(names)
    assert "secret-token" not in repr(names)


def test_normalize_channel_name_handles_case_punctuation_and_whitespace():
    assert epg.normalize_channel_name("  Кино-UHD!!  ") == epg.normalize_channel_name(
        "кино uhd"
    )
    assert epg.normalize_channel_name("Channel   One") == "channel one"


def test_trim_xmltv_keeps_only_matching_channels_and_programmes(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:-1,Channel One\n"
        "http://provider.invalid/one\n"
        "#EXTINF:-1,Channel Two\n"
        "http://provider.invalid/two\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.xml.gz"
    output = tmp_path / "epg.xml.gz"
    write_gzip(
        source,
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<tv source-info-name='unit-test'>\n"
        "  <channel id='one'><display-name> channel one </display-name></channel>\n"
        "  <channel id='two'><display-name>CHANNEL-TWO</display-name></channel>\n"
        "  <channel id='three'><display-name>Other</display-name></channel>\n"
        "  <programme channel='one' start='20260501040000 +0000' stop='20260501050000 +0000'><title>One</title></programme>\n"
        "  <programme channel='two' start='20260501050000 +0000' stop='20260501060000 +0000'><title>Two</title></programme>\n"
        "  <programme channel='three' start='20260501060000 +0000' stop='20260501070000 +0000'><title>Three</title></programme>\n"
        "</tv>\n",
    )

    summary = epg.trim_xmltv_to_playlist_channels(
        source_xmltv_gz_path=source,
        playlist_path=playlist,
        output_xmltv_gz_path=output,
    )

    text = read_gzip(output)
    root = ET.fromstring(text)
    assert root.tag == "tv"
    assert root.attrib["source-info-name"] == "unit-test"
    assert [channel.attrib["id"] for channel in root.findall("channel")] == ["one", "two"]
    assert [programme.attrib["channel"] for programme in root.findall("programme")] == [
        "one",
        "two",
    ]
    assert summary == epg.EpgTrimSummary(
        playlist_channel_count=2,
        source_channel_count=3,
        matched_channel_count=2,
        programme_count=2,
        unmatched_playlist_names=(),
    )


def test_trim_xmltv_reports_unmatched_playlist_names(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n#EXTINF:-1,Missing Channel\nhttp://provider.invalid/missing\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.xml.gz"
    output = tmp_path / "epg.xml.gz"
    write_gzip(
        source,
        "<tv><channel id='one'><display-name>Channel One</display-name></channel></tv>",
    )

    summary = epg.trim_xmltv_to_playlist_channels(source, playlist, output)

    assert output.exists()
    assert summary.matched_channel_count == 0
    assert summary.programme_count == 0
    assert summary.unmatched_playlist_names == ("Missing Channel",)
