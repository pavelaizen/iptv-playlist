from __future__ import annotations

from pathlib import Path

from app.admin_m3u import import_playlist_entries, render_channel_entry, render_playlist
from app.admin_models import ChannelSnapshot


def test_import_playlist_entries_extracts_structured_fields(tmp_path: Path) -> None:
    playlist = tmp_path / "playlist.m3u8"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:0 tvg-id=\"chan-1\" tvg-name=\"Channel 1\" tvg-logo=\"logo1\" tvg-rec=\"3\",Channel One\n"
        "#EXTGRP:News\n"
        "http://provider.invalid/one\n",
        encoding="utf-8",
    )

    rows = import_playlist_entries(playlist)

    assert rows == [
        {
            "name": "Channel One",
            "group_name": "News",
            "stream_url": "http://provider.invalid/one",
            "tvg_id": "chan-1",
            "tvg_name": "Channel 1",
            "tvg_logo": "logo1",
            "tvg_rec": "3",
        }
    ]


def test_import_playlist_entries_preserves_group_title_attribute(tmp_path: Path) -> None:
    playlist = tmp_path / "playlist.m3u8"
    playlist.write_text(
        "#EXTM3U\n"
        '#EXTINF:0 tvg-id="chan-1" group-title="News",Channel One\n'
        "http://provider.invalid/one\n",
        encoding="utf-8",
    )

    rows = import_playlist_entries(playlist)

    assert rows[0]["group_name"] == "News"


def test_render_channel_entry_with_logo_and_group() -> None:
    snapshot = ChannelSnapshot(
        name="Channel Two",
        group_name="Sports",
        stream_url="http://provider.invalid/two",
        tvg_id="chan-2",
        tvg_name="Channel Two",
        tvg_logo="logo2",
        tvg_rec="4",
        validated_version=3,
    )

    assert render_channel_entry(snapshot) == [
        '#EXTINF:0 tvg-id="chan-2" tvg-rec="4" tvg-logo="logo2",Channel Two',
        "#EXTGRP:Sports",
        "http://provider.invalid/two",
    ]


def test_render_channel_entry_minimal() -> None:
    snapshot = ChannelSnapshot(
        name="Channel Three",
        group_name="",
        stream_url="http://provider.invalid/three",
        tvg_id="",
        tvg_name="",
        tvg_logo="",
        tvg_rec="",
        validated_version=1,
    )

    assert render_channel_entry(snapshot) == [
        "#EXTINF:0,Channel Three",
        "http://provider.invalid/three",
    ]


def test_render_playlist_uses_structured_fields_and_order() -> None:
    snapshots = [
        ChannelSnapshot(
            name="Channel Two",
            group_name="Sports",
            stream_url="http://provider.invalid/two",
            tvg_id="chan-2",
            tvg_name="Channel Two",
            tvg_logo="logo2",
            tvg_rec="4",
            validated_version=3,
        ),
        ChannelSnapshot(
            name="Channel Three",
            group_name="News",
            stream_url="http://provider.invalid/three",
            tvg_id="",
            tvg_name="",
            tvg_logo="",
            tvg_rec="",
            validated_version=1,
        ),
    ]

    playlist = render_playlist(snapshots)

    assert playlist == (
        "#EXTM3U\n"
        '#EXTINF:0 tvg-id="chan-2" tvg-rec="4" tvg-logo="logo2",Channel Two\n'
        "#EXTGRP:Sports\n"
        "http://provider.invalid/two\n"
        "#EXTINF:0,Channel Three\n"
        "#EXTGRP:News\n"
        "http://provider.invalid/three\n"
    )
