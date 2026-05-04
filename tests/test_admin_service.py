from __future__ import annotations

from pathlib import Path

from app.admin_service import AdminService, AdminServiceSettings
from app.admin_store import AdminStore


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

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {"http://provider.invalid/one": True})
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
    assert (tmp_path / "published" / "playlist_emby_clean.m3u8").exists()


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

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {"http://provider.invalid/one": True})
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
    monkeypatch.setattr(service, "_probe_urls", lambda channels: {"http://provider.invalid/broken": False})

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
