from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.admin_store import AdminStore, bootstrap_from_playlist


def write_playlist(path: Path) -> None:
    path.write_text(
        "#EXTM3U\n"
        "#EXTINF:0 tvg-id=\"chan-1\" tvg-name=\"Channel 1\" tvg-logo=\"logo1\" tvg-rec=\"3\",Channel One\n"
        "#EXTGRP:News\n"
        "http://provider.invalid/one\n"
        "#EXTINF:0,Channel One\n"
        "#EXTGRP:News\n"
        "http://provider.invalid/one-backup\n"
        "#EXTINF:0,Channel Two\n"
        "#EXTGRP:Sports\n"
        "http://provider.invalid/two\n",
        encoding="utf-8",
    )


def test_bootstrap_from_playlist_imports_rows_and_preserves_duplicates(tmp_path: Path):
    db_path = tmp_path / "playlist.db"
    playlist_path = tmp_path / "original_playlist.m3u8"
    write_playlist(playlist_path)

    store = AdminStore(db_path)
    store.initialize()
    imported = bootstrap_from_playlist(store, playlist_path, fallback_playlist_path=None)

    channels = store.list_channels()
    assert imported is True
    assert [channel.display_order for channel in channels] == [0, 1, 2]
    assert [channel.name for channel in channels] == ["Channel One", "Channel One", "Channel Two"]
    assert [channel.stream_url for channel in channels] == [
        "http://provider.invalid/one",
        "http://provider.invalid/one-backup",
        "http://provider.invalid/two",
    ]
    assert [channel.status for channel in channels] == ["new", "new", "new"]
    assert all(channel.live_snapshot is not None for channel in channels)


def test_bootstrap_from_playlist_is_one_shot_when_channels_exist(tmp_path: Path):
    db_path = tmp_path / "playlist.db"
    playlist_path = tmp_path / "original_playlist.m3u8"
    write_playlist(playlist_path)

    store = AdminStore(db_path)
    store.initialize()
    assert bootstrap_from_playlist(store, playlist_path, fallback_playlist_path=None) is True
    assert bootstrap_from_playlist(store, playlist_path, fallback_playlist_path=None) is False
    assert len(store.list_channels()) == 3


def test_bootstrap_from_playlist_falls_back_when_primary_has_no_rows(tmp_path: Path):
    db_path = tmp_path / "playlist.db"
    primary_path = tmp_path / "primary.m3u8"
    fallback_path = tmp_path / "fallback.m3u8"

    primary_path.write_text("#EXTM3U\n#EXTINF:0,No URL Entry\n", encoding="utf-8")
    write_playlist(fallback_path)

    store = AdminStore(db_path)
    store.initialize()

    imported = bootstrap_from_playlist(store, primary_path, fallback_path)

    channels = store.list_channels()
    assert imported is True
    assert len(channels) == 3
    assert [channel.name for channel in channels] == ["Channel One", "Channel One", "Channel Two"]


def test_admin_store_enables_foreign_keys_on_method_connections(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen_foreign_key_flags: list[int] = []
    original_connect = sqlite3.connect

    class TrackingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):  # type: ignore[override]
            result = super().execute(sql, *args, **kwargs)
            if isinstance(sql, str) and sql.strip().upper() == "PRAGMA FOREIGN_KEYS = ON":
                seen_foreign_key_flags.append(super().execute("PRAGMA foreign_keys").fetchone()[0])
            return result

    def tracking_connect(*args, **kwargs):
        kwargs.setdefault("factory", TrackingConnection)
        return original_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracking_connect)

    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    store.channel_count()
    store.list_channels()

    assert seen_foreign_key_flags, "expected AdminStore methods to enable SQLite foreign keys"
    assert all(flag == 1 for flag in seen_foreign_key_flags)


def test_default_epg_sources_are_seeded_once(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()

    store.seed_default_epg_sources(
        [
            ("Main", "http://epg.one/epg2.xml.gz"),
            ("IL fallback", "https://iptv-epg.org/files/epg-il.xml.gz"),
        ]
    )
    store.seed_default_epg_sources([("Ignored", "http://example.invalid/other.xml")])

    sources = store.list_epg_sources()
    assert [(source.display_name, source.source_url, source.priority) for source in sources] == [
        ("Main", "http://epg.one/epg2.xml.gz", 0),
        ("IL fallback", "https://iptv-epg.org/files/epg-il.xml.gz", 1),
    ]
