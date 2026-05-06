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
    assert [len(store.list_stream_variants(channel.id)) for channel in channels] == [1, 1, 1]


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


def test_stream_variants_publish_order_and_disabled_state_are_persisted(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    store.import_channels(
        [
            {
                "name": "Kan 11",
                "group_name": "Israel",
                "stream_url": "http://provider.invalid/orig",
                "tvg_id": "kan-11",
                "tvg_name": "Kan 11",
                "tvg_logo": "",
                "tvg_rec": "",
            }
        ]
    )
    channel = store.list_channels()[0]

    hd = store.add_stream_variant(channel.id, {"label": "HD", "url": "http://provider.invalid/hd"})
    four_k = store.add_stream_variant(channel.id, {"label": "4K", "url": "http://provider.invalid/4k"})
    store.update_stream_variant(hd.id, {"enabled": False})
    store.reorder_stream_variants(channel.id, [four_k.id, store.list_stream_variants(channel.id)[0].id, hd.id])

    variants = store.list_stream_variants(channel.id)

    assert [(variant.label, variant.enabled) for variant in variants] == [
        ("4K", True),
        ("Orig", True),
        ("HD", False),
    ]


def test_stream_stability_result_is_persisted_on_variant(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    store.import_channels(
        [
            {
                "name": "Kan 11",
                "group_name": "Israel",
                "stream_url": "http://provider.invalid/orig",
                "tvg_id": "kan-11",
                "tvg_name": "Kan 11",
                "tvg_logo": "",
                "tvg_rec": "",
            }
        ]
    )
    variant = store.list_stream_variants()[0]

    assert variant.last_stability_status == "new"
    assert variant.last_stability_speed == ""
    assert variant.last_stability_frames == 0

    store.mark_stream_variant_stability_result(
        variant.id,
        status="WARN",
        error_text="slow decode speed=0.42x",
        speed="0.42",
        frames=400,
    )

    updated = store.get_stream_variant(variant.id)
    assert updated.last_stability_status == "WARN"
    assert updated.last_stability_error == "slow decode speed=0.42x"
    assert updated.last_stability_speed == "0.42"
    assert updated.last_stability_frames == 400
    assert updated.last_stability_at is not None


def test_duplicate_epg_source_url_is_rejected_after_normalization(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()

    store.add_epg_source({"display_name": "One", "source_url": "HTTP://Example.COM/epg.xml?b=2&a=1"})

    with pytest.raises(ValueError, match="duplicate"):
        store.add_epg_source({"display_name": "Duplicate", "source_url": "http://example.com/epg.xml?a=1&b=2"})


def test_epgpw_source_url_is_canonicalized_without_stale_date(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()

    source = store.add_epg_source(
        {
            "display_name": "Eye Oscar",
            "source_url": "https://epg.pw/api/epg.xml?lang=en&date=20260606&channel_id=493395&timezone=Asia/Jerusalem",
        }
    )

    assert source.source_url == (
        "https://epg.pw/api/epg.xml?channel_id=493395&lang=en"
        "&timezone=QXNpYS9KZXJ1c2FsZW0%3D"
    )
    assert source.normalized_url == source.source_url

    with pytest.raises(ValueError, match="duplicate"):
        store.add_epg_source(
            {
                "display_name": "Eye Oscar duplicate",
                "source_url": "https://epg.pw/last/493395.html?timezone=QXNpYS9KZXJ1c2FsZW0%3D&lang=en",
            }
        )


def test_blank_epg_source_url_is_rejected_without_persisting(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()

    with pytest.raises(ValueError, match="EPG source URL is required"):
        store.add_epg_source({"display_name": "Blank", "source_url": ""})

    assert store.list_epg_sources() == []


def test_epg_cache_search_is_utf8_casefolded(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    store.replace_epg_channel_cache(
        source.id,
        [
            {"id": "kan11", "display_name": "כאן 11"},
            {"id": "kino", "display_name": "Кино UHD"},
        ],
    )

    assert [row["display_name"] for row in store.search_epg_channel_cache(source.id, "כאן")] == ["כאן 11"]
    assert [row["display_name"] for row in store.search_epg_channel_cache(source.id, "кино")] == ["Кино UHD"]


def test_epg_cache_replace_accepts_streaming_iterable(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})

    def channel_rows():
        yield {"id": "one", "display_name": "One"}
        yield {"id": "two", "display_name": "Two"}

    count = store.replace_epg_channel_cache_from_iterable(source.id, channel_rows())

    assert count == 2
    assert store.list_epg_sources()[0].channel_count == 2
    assert [row["display_name"] for row in store.search_epg_channel_cache(source.id, "")] == ["One", "Two"]


def test_epg_cache_replace_tolerates_duplicate_xmltv_ids(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})

    count = store.replace_epg_channel_cache_from_iterable(
        source.id,
        [
            {"id": "duplicate", "display_name": "First"},
            {"id": "duplicate", "display_name": "Second"},
        ],
    )

    assert count == 2
    assert store.search_epg_channel_cache(source.id, "duplicate") == [
        {"epg_channel_id": "duplicate", "display_name": "Second"}
    ]


def test_deleting_epg_source_identifies_affected_channels_and_remaining_mapping_validity(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    store.import_channels(
        [
            {
                "name": "Channel One",
                "group_name": "News",
                "stream_url": "http://provider.invalid/one",
                "tvg_id": "chan-1",
                "tvg_name": "Channel One",
                "tvg_logo": "",
                "tvg_rec": "",
            }
        ]
    )
    channel_id = store.list_channels()[0].id
    first = store.add_epg_source({"display_name": "First", "source_url": "https://example.com/first.xml"})
    second = store.add_epg_source({"display_name": "Second", "source_url": "https://example.com/second.xml"})
    store.replace_epg_channel_cache(first.id, [{"id": "first-id", "display_name": "First"}])
    store.replace_epg_channel_cache(second.id, [{"id": "second-id", "display_name": "Second"}])
    store.add_channel_epg_mapping(channel_id, first.id, 0, "first-id")
    store.add_channel_epg_mapping(channel_id, second.id, 1, "second-id")

    assert store.list_channel_ids_for_epg_source(first.id) == [channel_id]
    store.delete_epg_source(first.id)

    assert store.channel_has_valid_enabled_mapping(channel_id) is True


def test_channel_without_remaining_valid_mapping_can_be_marked_invalid(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    store.import_channels(
        [
            {
                "name": "Channel One",
                "group_name": "News",
                "stream_url": "http://provider.invalid/one",
                "tvg_id": "chan-1",
                "tvg_name": "Channel One",
                "tvg_logo": "",
                "tvg_rec": "",
            }
        ]
    )
    channel_id = store.list_channels()[0].id
    source = store.add_epg_source({"display_name": "Only", "source_url": "https://example.com/only.xml"})
    store.replace_epg_channel_cache(source.id, [{"id": "only-id", "display_name": "Only"}])
    store.add_channel_epg_mapping(channel_id, source.id, 0, "only-id")

    store.delete_epg_source(source.id)
    assert store.channel_has_valid_enabled_mapping(channel_id) is False
    store.mark_channels_invalid([channel_id], "EPG source deleted")

    channel = store.get_channel(channel_id)
    assert channel.status == "invalid"


def test_duplicate_channel_epg_mapping_is_rejected(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    store.import_channels(
        [
            {
                "name": "Channel One",
                "group_name": "News",
                "stream_url": "http://provider.invalid/one",
                "tvg_id": "chan-1",
                "tvg_name": "Channel One",
                "tvg_logo": "",
                "tvg_rec": "",
            }
        ]
    )
    channel_id = store.list_channels()[0].id
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})

    store.add_channel_epg_mapping(channel_id, source.id, 0, "chan-1")

    with pytest.raises(ValueError, match="duplicate EPG mapping"):
        store.add_channel_epg_mapping(channel_id, source.id, 1, "chan-1")
