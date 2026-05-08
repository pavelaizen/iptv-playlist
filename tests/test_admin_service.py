from __future__ import annotations

import gzip
from pathlib import Path
from types import SimpleNamespace

from app.admin_service import AdminService, AdminServiceSettings, _parse_xmltv_channel_cache
from app.admin_store import AdminStore
from app.stream_stability import StreamStabilityResult


def seed_channel(store: AdminStore) -> int:
    store.import_channels(
        [
            {
                "name": "Channel One",
                "group_name": "News",
                "stream_url": "http://provider.invalid/one",
                "tvg_id": "chan-1",
                "tvg_name": "Channel One",
                "tvg_logo": "",
                "tvg_rec": "3",
            }
        ]
    )
    return store.list_channels()[0].id


def seed_channels_with_duplicate_url(store: AdminStore) -> tuple[int, int]:
    store.import_channels(
        [
            {
                "name": "Channel One",
                "group_name": "News",
                "stream_url": "http://provider.invalid/shared",
                "tvg_id": "chan-1",
                "tvg_name": "Channel One",
                "tvg_logo": "",
                "tvg_rec": "3",
            },
            {
                "name": "Channel Two",
                "group_name": "Sports",
                "stream_url": "http://provider.invalid/shared",
                "tvg_id": "chan-2",
                "tvg_name": "Channel Two",
                "tvg_logo": "",
                "tvg_rec": "3",
            },
        ]
    )
    channels = store.list_channels()
    return channels[0].id, channels[1].id


def test_validate_channel_success_promotes_draft_to_live_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    settings = AdminServiceSettings(
        output_dir=tmp_path / "published",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: True})
    monkeypatch.setattr(
        service,
        "_sync_epg",
        lambda: {"changed": False, "matched_channels": 1, "programmes": 2},
    )
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.validate_channel(channel_id)
    channel = store.list_channels()[0]

    assert result["status"] == "valid"
    assert channel.status == "valid"
    assert channel.draft_differs_from_live is False
    assert channel.live_snapshot is not None


def test_validate_stream_stability_persists_extended_result(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    variant = store.list_stream_variants(channel_id)[0]
    service = AdminService(
        store=store,
        settings=AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )

    monkeypatch.setattr(
        "app.admin_service.run_stream_stability_test",
        lambda url: StreamStabilityResult(
            status="WARN",
            frames=400,
            speed="0.42",
            issues="slow decode speed=0.42x",
            returncode=0,
        ),
    )

    result = service.validate_stream_stability(variant.id)
    updated = store.get_stream_variant(variant.id)

    assert result == {
        "status": "WARN",
        "stream_id": variant.id,
        "frames": 400,
        "speed": "0.42",
        "issues": "slow decode speed=0.42x",
        "returncode": 0,
    }
    assert updated.last_stability_status == "WARN"
    assert updated.last_stability_error == "slow decode speed=0.42x"
    assert updated.last_stability_speed == "0.42"
    assert updated.last_stability_frames == 400
    assert not (tmp_path / "published" / "playlist_emby_clean.m3u8").exists()


def test_validate_channel_failure_keeps_previous_live_snapshot(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    settings = AdminServiceSettings(
        output_dir=tmp_path / "published",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: True})
    monkeypatch.setattr(
        service,
        "_sync_epg",
        lambda: {"changed": False, "matched_channels": 1, "programmes": 2},
    )
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)
    assert service.validate_channel(channel_id)["status"] == "valid"

    store.update_channel(
        channel_id,
        {
            "name": "Channel One HD",
            "group_name": "News",
            "stream_url": "http://provider.invalid/broken",
            "tvg_id": "chan-1",
            "tvg_name": "Channel One HD",
            "tvg_logo": "",
            "tvg_rec": "3",
            "enabled": True,
        },
    )
    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: False})

    result = service.validate_channel(channel_id)
    channel = store.list_channels()[0]

    assert result["status"] == "invalid"
    assert channel.status == "invalid"
    assert channel.live_snapshot is not None
    assert channel.live_snapshot.stream_url == "http://provider.invalid/one"


def test_validate_all_rejects_overlapping_run(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    settings = AdminServiceSettings(
        output_dir=tmp_path / "published",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    service = AdminService(store=store, settings=settings)

    service._job_lock.acquire()
    try:
        result = service.validate_all(trigger_type="manual")
    finally:
        service._job_lock.release()

    assert result["status"] == "already_running"


def test_validate_all_duplicate_urls_do_not_alias_probe_results(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    first_id, second_id = seed_channels_with_duplicate_url(store)
    settings = AdminServiceSettings(
        output_dir=tmp_path / "published",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {first_id: True, second_id: False})
    monkeypatch.setattr(
        service,
        "_sync_epg",
        lambda: {"changed": False, "matched_channels": 1, "programmes": 2},
    )
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.validate_all(trigger_type="manual")
    channels = {channel.id: channel for channel in store.list_channels()}

    assert result["status"] == "ok"
    assert result["valid_count"] == 1
    assert result["invalid_count"] == 1
    assert channels[first_id].status == "valid"
    assert channels[second_id].status == "invalid"


def test_rebuild_playlist_publishes_multiple_enabled_variants(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    hd = store.add_stream_variant(channel_id, {"label": "HD", "url": "http://provider.invalid/hd"})
    disabled = store.add_stream_variant(channel_id, {"label": "Broken", "url": "http://provider.invalid/broken"})
    store.update_stream_variant(disabled.id, {"enabled": False})
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(service, "_probe_variants", lambda variants: {variant.id: True for variant in variants})
    assert service.validate_all(trigger_type="manual")["status"] == "ok"
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 0, "programmes": 0})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.rebuild_playlist(trigger_type="manual")

    playlist = (tmp_path / "published" / "playlist_emby_clean.m3u8").read_text(encoding="utf-8")
    assert result["status"] == "ok"
    assert "Channel One\n" in playlist
    assert "Channel One HD" in playlist
    assert "Broken" not in playlist
    assert playlist.count("Channel One") == 2
    assert "http://provider.invalid/hd" in playlist
    assert hd.id


def test_rebuild_playlist_keeps_live_primary_url_when_draft_url_is_unvalidated(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    store.update_channel(
        channel_id,
        {
            "name": "Channel One Edited",
            "group_name": "News",
            "stream_url": "http://provider.invalid/broken",
            "tvg_id": "chan-1-edited",
            "tvg_name": "Channel One Edited",
            "tvg_logo": "",
            "tvg_rec": "3",
            "enabled": True,
        },
    )
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 0, "programmes": 0})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.rebuild_playlist(trigger_type="manual")

    playlist = (tmp_path / "published" / "playlist_emby_clean.m3u8").read_text(encoding="utf-8")
    assert result["status"] == "ok"
    assert "Channel One\n" in playlist
    assert "Channel One Edited" not in playlist
    assert "http://provider.invalid/one" in playlist
    assert "http://provider.invalid/broken" not in playlist


def test_rebuild_playlist_keeps_valid_variant_url_when_variant_edit_is_unvalidated(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    hd = store.add_stream_variant(channel_id, {"label": "HD", "url": "http://provider.invalid/hd"})
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(service, "_probe_variants", lambda variants: {variant.id: True for variant in variants})
    assert service.validate_all(trigger_type="manual")["status"] == "ok"
    store.update_stream_variant(
        hd.id,
        {
            "label": "HD",
            "url": "http://provider.invalid/broken-hd",
            "enabled": True,
        },
    )
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 0, "programmes": 0})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.rebuild_playlist(trigger_type="manual")

    playlist = (tmp_path / "published" / "playlist_emby_clean.m3u8").read_text(encoding="utf-8")
    assert result["status"] == "ok"
    assert "Channel One HD" in playlist
    assert "http://provider.invalid/hd" in playlist
    assert "http://provider.invalid/broken-hd" not in playlist


def test_rebuild_playlist_does_not_publish_unvalidated_new_variant(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    store.add_stream_variant(channel_id, {"label": "HD", "url": "http://provider.invalid/hd"})
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 0, "programmes": 0})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.rebuild_playlist(trigger_type="manual")

    playlist = (tmp_path / "published" / "playlist_emby_clean.m3u8").read_text(encoding="utf-8")
    assert result["status"] == "ok"
    assert "Channel One\n" in playlist
    assert "Channel One HD" not in playlist
    assert "http://provider.invalid/hd" not in playlist


def test_sync_epg_uses_base_live_name_for_original_variant(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    seed_channel(store)
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    captured: dict[str, object] = {}

    def fake_sync_epg(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            changed=True,
            matched_channels=0,
            programmes=0,
            failed_sources=[],
            channel_icons={},
        )

    monkeypatch.setattr("app.admin_service.sync_epg", fake_sync_epg)

    result = service._sync_epg()

    assert result["changed"] is True
    assert captured["published_channels"][0]["name"] == "Channel One"


def test_validate_all_updates_status_without_rebuilding_outputs(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: True})

    result = service.validate_all(trigger_type="manual")

    assert result["status"] == "ok"
    assert store.list_channels()[0].status == "valid"
    assert not (tmp_path / "published" / "playlist_emby_clean.m3u8").exists()


def test_validate_channel_unknown_id_returns_not_found(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    settings = AdminServiceSettings(
        output_dir=tmp_path / "published",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    service = AdminService(store=store, settings=settings)

    result = service.validate_channel(99999)

    assert result == {"status": "not_found", "channel_id": 99999}


def test_validate_all_continues_when_one_epg_source_download_fails(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    store.seed_default_epg_sources(
        [
            ("Main", "http://good.invalid/epg.xml.gz"),
            ("Bad", "http://bad.invalid/epg.xml.gz"),
        ]
    )
    settings = AdminServiceSettings(
        output_dir=tmp_path / "published",
        diagnostics_dir=tmp_path / "diagnostics",
    )
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: True})
    monkeypatch.setattr(
        service,
        "_sync_epg",
        lambda: {
            "changed": True,
            "matched_channels": 1,
            "programmes": 2,
            "failed_sources": ["http://bad.invalid/epg.xml.gz"],
        },
    )
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.validate_all(trigger_type="manual")

    assert result["status"] == "ok"
    assert result["publish"] == {"status": "not_run"}


def test_delete_epg_source_invalidates_channels_without_remaining_valid_mapping(
    tmp_path: Path, monkeypatch
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    source = store.add_epg_source({"display_name": "Only", "source_url": "https://example.com/only.xml"})
    store.replace_epg_channel_cache(source.id, [{"id": "only-id", "display_name": "Only"}])
    store.add_channel_epg_mapping(channel_id, source.id, 0, "only-id")
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 0, "programmes": 0})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.delete_epg_source_and_rebuild(source.id)

    assert result["invalidated_channel_ids"] == [channel_id]
    assert store.get_channel(channel_id).status == "invalid"


def test_apply_epg_logo_for_mapping_sets_blank_channel_logo_from_cached_source(
    tmp_path: Path,
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    store.add_channel_epg_mapping(channel_id, source.id, 0, "chan-1")
    epg_work_dir = tmp_path / "epg"
    epg_work_dir.mkdir()
    (epg_work_dir / f"source-{source.id}.xmltv").write_text(
        '<tv><channel id="chan-1"><display-name>Channel One</display-name>'
        '<icon src="https://img.example/one.png"/></channel></tv>',
        encoding="utf-8",
    )
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
            epg_work_dir=epg_work_dir,
        ),
    )

    result = service.apply_epg_logo_for_mapping(channel_id, source.id, "chan-1")

    assert result == "https://img.example/one.png"
    assert store.get_channel(channel_id).tvg_logo == "https://img.example/one.png"


def test_reload_epg_source_applies_missing_logos_for_existing_mappings(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    store.add_channel_epg_mapping(channel_id, source.id, 0, "chan-1")
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
            epg_work_dir=tmp_path / "epg",
        ),
    )

    def fake_download_epg_source(source_url: str, destination: Path, **kwargs) -> None:
        del source_url, kwargs
        destination.write_text(
            '<tv><channel id="chan-1"><display-name>Channel One</display-name>'
            '<icon src="https://img.example/reloaded.png"/></channel></tv>',
            encoding="utf-8",
        )

    monkeypatch.setattr("app.admin_service.download_epg_source", fake_download_epg_source)

    result = service.reload_epg_source(source.id)

    assert result["status"] == "ok"
    assert result["applied_logo_count"] == 1
    assert store.get_channel(channel_id).tvg_logo == "https://img.example/reloaded.png"


def test_reload_epg_source_does_not_overwrite_manual_channel_logo(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    store.update_channel(
        channel_id,
        {
            "name": "Channel One",
            "group_name": "News",
            "stream_url": "http://provider.invalid/one",
            "tvg_id": "chan-1",
            "tvg_name": "Channel One",
            "tvg_logo": "https://manual.example/logo.png",
            "tvg_rec": "3",
            "enabled": True,
        },
    )
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    store.add_channel_epg_mapping(channel_id, source.id, 0, "chan-1")
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
            epg_work_dir=tmp_path / "epg",
        ),
    )

    def fake_download_epg_source(source_url: str, destination: Path, **kwargs) -> None:
        del source_url, kwargs
        destination.write_text(
            '<tv><channel id="chan-1"><display-name>Channel One</display-name>'
            '<icon src="https://img.example/source.png"/></channel></tv>',
            encoding="utf-8",
        )

    monkeypatch.setattr("app.admin_service.download_epg_source", fake_download_epg_source)

    result = service.reload_epg_source(source.id)

    assert result["status"] == "ok"
    assert result["applied_logo_count"] == 0
    assert store.get_channel(channel_id).tvg_logo == "https://manual.example/logo.png"


def test_auto_add_epgpw_mapping_gets_logo_after_source_reload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
            epg_work_dir=tmp_path / "epg",
        ),
    )
    monkeypatch.setattr(
        service,
        "start_reload_epg_source_job",
        lambda source_id: {"job_id": f"reload-{source_id}", "status": "queued"},
    )

    def fake_download_epg_source(source_url: str, destination: Path, **kwargs) -> None:
        del source_url, kwargs
        destination.write_text(
            '<tv><channel id="12345"><display-name>Channel One</display-name>'
            '<icon src="https://img.example/epgpw.png"/></channel></tv>',
            encoding="utf-8",
        )

    monkeypatch.setattr("app.admin_service.download_epg_source", fake_download_epg_source)

    mapping_result = service.auto_add_epgpw_mapping(channel_id, "12345", "Channel One")
    reload_result = service.reload_epg_source(int(mapping_result["source_id"]))

    assert reload_result["status"] == "ok"
    assert reload_result["applied_logo_count"] == 1
    assert store.get_channel(channel_id).tvg_logo == "https://img.example/epgpw.png"


def test_xmltv_channel_cache_ignores_programmes_without_retaining_them(tmp_path: Path) -> None:
    source = tmp_path / "source.xml.gz"
    with gzip.open(source, "wt", encoding="utf-8") as fh:
        fh.write("<tv>")
        for index in range(200):
            fh.write(f"<programme channel='chan-{index}'><title>Show {index}</title></programme>")
        fh.write("<channel id='one'><display-name>One</display-name></channel>")
        fh.write("<channel id='two'><display-name>Two</display-name></channel>")
        for index in range(200, 400):
            fh.write(f"<programme channel='chan-{index}'><title>Show {index}</title></programme>")
        fh.write("</tv>")

    assert _parse_xmltv_channel_cache(source) == [
        {"id": "one", "display_name": "One"},
        {"id": "two", "display_name": "Two"},
    ]
