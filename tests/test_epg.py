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


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


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


def test_extract_playlist_channel_names_reads_extinf_case_insensitively(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#extinf:-1,Lowercase Marker\n"
        "http://provider.invalid/secret-token\n",
        encoding="utf-8",
    )

    names = epg.extract_playlist_channel_names(playlist)

    assert names == ["Lowercase Marker"]
    assert "provider.invalid" not in repr(names)


def test_extract_playlist_channel_names_ignores_commas_inside_quoted_attrs(
    tmp_path: Path,
):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        '#EXTINF:-1 group-title="News, US",Channel One\n'
        "http://provider.invalid/secret-token\n",
        encoding="utf-8",
    )

    names = epg.extract_playlist_channel_names(playlist)

    assert names == ["Channel One"]
    assert "News, US" not in names
    assert "provider.invalid" not in repr(names)


def test_extract_playlist_channel_names_ignores_non_extinf_prefixes(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINFRA:-1,Wrong\n"
        "http://provider.invalid/secret-token\n",
        encoding="utf-8",
    )

    assert epg.extract_playlist_channel_names(playlist) == []


def test_normalize_channel_name_handles_case_punctuation_and_whitespace():
    assert epg.normalize_channel_name("  Кино-UHD!!  ") == epg.normalize_channel_name(
        "кино uhd"
    )
    assert epg.normalize_channel_name("Channel   One") == "channel one"


def test_normalize_channel_name_handles_compatibility_and_combining_forms():
    assert epg.normalize_channel_name("Ｃａｆｅ\u0301 ＯＮＥ") == epg.normalize_channel_name(
        "Café One"
    )


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
    output = tmp_path / "epg.xml"
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
        output_xmltv_path=output,
    )

    text = read_text(output)
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
    output = tmp_path / "epg.xml"
    write_gzip(
        source,
        "<tv><channel id='one'><display-name>Channel One</display-name></channel></tv>",
    )

    summary = epg.trim_xmltv_to_playlist_channels(source, playlist, output)

    assert output.exists()
    assert summary.matched_channel_count == 0
    assert summary.programme_count == 0
    assert summary.unmatched_playlist_names == ("Missing Channel",)


def test_trim_xmltv_preserves_matching_programmes_before_channels(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:-1,Channel One\n"
        "http://provider.invalid/one\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.xml.gz"
    output = tmp_path / "epg.xml"
    write_gzip(
        source,
        "<tv>"
        "  <programme channel='one'><title>Before Channel</title></programme>"
        "  <channel id='one'><display-name>Channel One</display-name></channel>"
        "</tv>",
    )

    summary = epg.trim_xmltv_to_playlist_channels(source, playlist, output)

    root = ET.fromstring(read_text(output))
    assert [channel.attrib["id"] for channel in root.findall("channel")] == ["one"]
    assert [programme.attrib["channel"] for programme in root.findall("programme")] == [
        "one"
    ]
    assert summary.matched_channel_count == 1
    assert summary.programme_count == 1


def test_trim_xmltv_does_not_build_elementtree_for_programmes(
    tmp_path: Path,
):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:-1,Channel One\n"
        "http://provider.invalid/one\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.xml.gz"
    output = tmp_path / "epg.xml"
    write_gzip(
        source,
        "<tv>"
        "  <channel id='one'><display-name>Channel One</display-name></channel>"
        "  <programme channel='one'><title>One</title></programme>"
        "</tv>",
    )

    summary = epg.trim_xmltv_to_playlist_channels(source, playlist, output)

    assert not hasattr(epg, "ET")
    root = ET.fromstring(read_text(output))
    assert [channel.attrib["id"] for channel in root.findall("channel")] == ["one"]
    assert [programme.attrib["channel"] for programme in root.findall("programme")] == [
        "one"
    ]
    assert summary.matched_channel_count == 1
    assert summary.programme_count == 1


def test_trim_xmltv_with_israeli_overrides_uses_primary_and_fallback_sources(
    tmp_path: Path,
):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:-1,Channel One\n"
        "http://provider.invalid/one\n"
        "#EXTINF:-1,Kan 11 HD IL\n"
        "http://provider.invalid/kan11\n"
        "#EXTINF:-1,Keshet 12 HD IL\n"
        "http://provider.invalid/keshet12\n"
        "#EXTINF:-1,Channel 14 FHD IL\n"
        "http://provider.invalid/ch14\n",
        encoding="utf-8",
    )
    default_source = tmp_path / "default.xml.gz"
    israel_primary_source = tmp_path / "primary.xml.gz"
    israel_fallback_source = tmp_path / "fallback.xml.gz"
    output = tmp_path / "epg.xml"

    write_gzip(
        default_source,
        "<tv>"
        "  <channel id='one'><display-name>Channel One</display-name></channel>"
        "  <programme channel='one'><title>One</title></programme>"
        "</tv>",
    )
    write_gzip(
        israel_primary_source,
        "<tv>"
        "  <channel id='channel-11-il'><display-name>Channel 11 [IL]</display-name></channel>"
        "  <channel id='channel-12-il'><display-name>Channel 12 [IL]</display-name></channel>"
        "  <programme channel='channel-12-il'><title>Keshet</title></programme>"
        "</tv>",
    )
    write_gzip(
        israel_fallback_source,
        "<tv>"
        "  <channel id='כאן11.il'><display-name>IL - כאן 11</display-name></channel>"
        "  <channel id='ערוץ14.il'><display-name>IL - ערוץ 14</display-name></channel>"
        "  <programme channel='כאן11.il'><title>Kan Fallback</title></programme>"
        "  <programme channel='ערוץ14.il'><title>Ch14</title></programme>"
        "</tv>",
    )

    summary = epg.trim_xmltv_to_playlist_channels_with_israeli_overrides(
        default_source_xmltv_gz_path=default_source,
        israel_primary_source_xmltv_gz_path=israel_primary_source,
        israel_fallback_source_xmltv_gz_path=israel_fallback_source,
        playlist_path=playlist,
        output_xmltv_path=output,
    )

    root = ET.fromstring(read_text(output))
    channel_ids = [channel.attrib["id"] for channel in root.findall("channel")]
    programme_ids = [programme.attrib["channel"] for programme in root.findall("programme")]

    assert channel_ids == ["one", "channel-12-il", "כאן11.il", "ערוץ14.il"]
    assert programme_ids == ["one", "channel-12-il", "כאן11.il", "ערוץ14.il"]
    assert summary.playlist_channel_count == 4
    assert summary.matched_channel_count == 4
    assert summary.programme_count == 4
    assert summary.unmatched_playlist_names == ()


def test_trim_xmltv_prefers_explicit_mapping_then_global_name_fallback(
    tmp_path: Path,
):
    primary = tmp_path / "primary.xml.gz"
    fallback = tmp_path / "fallback.xml.gz"
    output = tmp_path / "epg.xml"

    write_gzip(
        primary,
        "<tv>"
        "  <channel id='other'><display-name>Other</display-name></channel>"
        "  <programme channel='other'><title>Other</title></programme>"
        "</tv>",
    )
    write_gzip(
        fallback,
        "<tv>"
        "  <channel id='chan-12'><display-name>Keshet 12 FHD IL</display-name></channel>"
        "  <programme channel='chan-12'><title>Morning</title></programme>"
        "</tv>",
    )

    summary = epg.trim_xmltv_with_source_strategies(
        published_channels=[
            {
                "name": "Keshet 12 FHD IL",
                "mappings": [{"source_key": "primary", "channel_id": "missing-id"}],
            }
        ],
        sources={"primary": primary, "fallback": fallback},
        default_source_order=["primary", "fallback"],
        output_xmltv_path=output,
    )

    root = ET.fromstring(read_text(output))
    assert [channel.attrib["id"] for channel in root.findall("channel")] == ["chan-12"]
    assert [programme.attrib["channel"] for programme in root.findall("programme")] == [
        "chan-12"
    ]
    assert summary.matched_channel_count == 1
    assert summary.programme_count == 1
    assert summary.unmatched_playlist_names == ()
