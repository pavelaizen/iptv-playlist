from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

from app.admin_service import AdminService, AdminServiceSettings
from app.admin_store import AdminStore
from app.admin_web import build_test_server


def test_get_channels_api_returns_json(tmp_path: Path) -> None:
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
                "tvg_rec": "3",
            }
        ]
    )
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, headers, body = app("GET", "/api/channels", None)

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["channels"][0]["name"] == "Channel One"


def test_channels_ui_renders_validate_button(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, headers, body = app("GET", "/ui/channels", None)

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "Validate all channels" in body
    assert "EPG Sources" in body


def test_channels_ui_exposes_add_channel_form(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", "/ui/channels", None)

    assert status == 200
    assert 'href="/ui/channels/new"' in body
    assert ">Add channel</a>" in body
    assert 'id="channel-create-form"' not in body


def test_new_channel_page_renders_create_form_and_stability_redirect(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", "/ui/channels/new", None)

    assert status == 200
    assert 'id="channel-create-form"' in body
    assert 'name="name" placeholder="Name" required' in body
    assert 'name="stream_url" placeholder="Stream URL" required' in body
    assert ">Add channel</button>" in body
    assert "api('/api/channels', {method:'POST'" in body
    assert "stability_job" in body
    assert "stability_job=" in body


def test_channels_ui_uses_drag_handles_for_ordering(tmp_path: Path) -> None:
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
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", "/ui/channels", None)

    assert status == 200
    assert 'draggable="true"' in body
    assert 'class="drag-handle"' in body
    assert 'class="order-input"' not in body
    assert "<th>Order</th>" not in body
    assert "document.querySelectorAll('#channels-table tbody tr')" in body
    assert ".order-input" not in body


def test_create_channel_api_starts_rebuild_and_extended_test_jobs(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(
        service,
        "start_rebuild_playlist_job",
        lambda trigger_type: {"job_id": "rebuild-job", "status": "queued", "trigger_type": trigger_type},
    )
    monkeypatch.setattr(
        service,
        "start_stream_stability_job",
        lambda stream_id: {"job_id": f"stability-{stream_id}", "status": "queued"},
    )
    app = build_test_server(store, service)

    status, _, body = app(
        "POST",
        "/api/channels",
        {"name": "New Channel", "stream_url": "http://provider.invalid/new"},
    )

    assert status == 201
    payload = json.loads(body)
    assert payload["channel"]["name"] == "New Channel"
    assert payload["job"]["job_id"] == "rebuild-job"
    assert payload["stability_job"]["job_id"].startswith("stability-")


def test_validate_all_endpoint_returns_accepted(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(service, "start_validate_all_job", lambda trigger_type: {"job_id": "job-1", "status": "queued"})
    app = build_test_server(store, service)

    status, headers, body = app("POST", "/api/jobs/validate-all", None)

    assert status == 202
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload == {"job_id": "job-1", "status": "queued"}


def test_system_status_endpoint_returns_ok(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, headers, body = app("GET", "/api/system/status", None)

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["status"] == "ok"


def test_get_channel_detail_is_read_only(tmp_path: Path, monkeypatch) -> None:
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
                "tvg_rec": "3",
            }
        ]
    )
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    called = False

    def fail_if_called(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("GET must not mutate")

    monkeypatch.setattr(service, "validate_channel", fail_if_called)
    monkeypatch.setattr(service, "rebuild_playlist", fail_if_called)
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/api/channels/{store.list_channels()[0].id}", None)

    assert status == 200
    assert json.loads(body)["channel"]["name"] == "Channel One"
    assert called is False


def test_epg_source_channel_search_uses_persisted_cache(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    store.replace_epg_channel_cache(source.id, [{"id": "kino", "display_name": "Кино UHD"}])
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/api/epg-sources/{source.id}/channels?q=кино", None)

    assert status == 200
    assert json.loads(body)["channels"] == [
        {"epg_channel_id": "kino", "display_name": "Кино UHD"}
    ]


def test_channel_editor_mapping_form_refreshes_without_page_reload(tmp_path: Path) -> None:
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
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    channel_id = store.list_channels()[0].id
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/ui/channels/{channel_id}", None)

    assert status == 200
    assert 'id="epg-mappings-body"' in body
    assert 'data-action="delete-mapping"' in body
    assert 'id="epg-preview-body"' in body
    assert "refreshChannelEditor" in body
    assert "programmeRowsHtml" in body
    assert "/api/channels/' + channelId + '/mappings" in body

    status, _, response = app(
        "POST",
        f"/api/channels/{channel_id}/mappings",
        {"epg_source_id": source.id, "epg_channel_id": "chan-1"},
    )

    assert status == 201
    payload = json.loads(response)
    assert payload["mapping"]["epg_channel_id"] == "chan-1"
    assert payload["job"]["job_id"]


def test_channel_editor_exposes_extended_stream_test(tmp_path: Path) -> None:
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
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/ui/channels/{store.list_channels()[0].id}", None)

    assert status == 200
    assert "<th>Stability</th>" in body
    assert "<th>Speed</th>" in body
    assert "<th>Issues</th>" in body
    assert 'data-action="test-stream-stability"' in body
    assert ">Extended test</button>" in body
    assert "/api/jobs/validate-stream-extended/" in body
    assert "stability_job" in body


def test_extended_stream_test_endpoint_returns_accepted(tmp_path: Path, monkeypatch) -> None:
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
    variant = store.list_stream_variants()[0]
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(
        service,
        "start_stream_stability_job",
        lambda stream_id: {"job_id": f"stability-{stream_id}", "status": "queued"},
    )
    app = build_test_server(store, service)

    status, _, body = app("POST", f"/api/jobs/validate-stream-extended/{variant.id}", None)

    assert status == 202
    assert json.loads(body) == {"job_id": f"stability-{variant.id}", "status": "queued"}


def test_channel_detail_epg_preview_is_empty_without_mappings(tmp_path: Path) -> None:
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
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
            epg_work_dir=tmp_path / "epg",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/api/channels/{store.list_channels()[0].id}/epg-preview", None)

    assert status == 200
    assert json.loads(body) == {
        "items": [],
        "empty_message": "No EPG mappings attached.",
    }


def test_channel_detail_epg_preview_returns_next_five_programmes(tmp_path: Path) -> None:
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
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml.gz"})
    channel_id = store.list_channels()[0].id
    store.add_channel_epg_mapping(channel_id, source.id, 0, "chan-1")
    epg_work_dir = tmp_path / "epg"
    epg_work_dir.mkdir()
    source_path = epg_work_dir / f"source-{source.id}.xmltv"
    programmes = "\n".join(
        f'<programme start="2099010{day}100000 +0000" stop="2099010{day}110000 +0000" channel="chan-1">'
        f"<title>Show {day}</title><desc>Description {day}</desc></programme>"
        for day in range(1, 7)
    )
    with gzip.open(source_path, "wb") as fh:
        fh.write(
            (
                '<?xml version="1.0" encoding="UTF-8"?><tv>'
                '<channel id="chan-1"><display-name>Channel One</display-name></channel>'
                f"{programmes}</tv>"
            ).encode("utf-8")
        )
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
            epg_work_dir=epg_work_dir,
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/api/channels/{channel_id}/epg-preview", None)

    assert status == 200
    preview = json.loads(body)
    assert preview["empty_message"] == ""
    assert [item["title"] for item in preview["items"]] == [
        "Show 1",
        "Show 2",
        "Show 3",
        "Show 4",
        "Show 5",
    ]
    assert preview["items"][0]["channel_id"] == "chan-1"
    assert preview["items"][0]["source_id"] == source.id


def test_channel_editor_renders_epg_preview_card(tmp_path: Path) -> None:
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
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml.gz"})
    channel_id = store.list_channels()[0].id
    store.add_channel_epg_mapping(channel_id, source.id, 0, "chan-1")
    epg_work_dir = tmp_path / "epg"
    epg_work_dir.mkdir()
    (epg_work_dir / f"source-{source.id}.xmltv").write_text(
        '<?xml version="1.0" encoding="UTF-8"?><tv>'
        '<channel id="chan-1"><display-name>Channel One</display-name></channel>'
        '<programme start="20990101100000 +0000" stop="20990101110000 +0000" channel="chan-1">'
        "<title>Morning News</title></programme></tv>",
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
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/ui/channels/{channel_id}", None)

    assert status == 200
    assert "TV Guide" in body
    assert "loading" in body
    assert "<script>" in body
    assert 'id="epg-preview-body"' in body


def test_duplicate_channel_epg_mapping_returns_bad_request(tmp_path: Path) -> None:
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
    source = store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    channel_id = store.list_channels()[0].id
    store.add_channel_epg_mapping(channel_id, source.id, 0, "chan-1")
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app(
        "POST",
        f"/api/channels/{channel_id}/mappings",
        {"epg_source_id": source.id, "epg_channel_id": "chan-1"},
    )

    assert status == 400
    assert "duplicate EPG mapping" in json.loads(body)["error"]


def test_epg_sources_ui_exposes_delete_controls(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    store.add_epg_source({"display_name": "Main", "source_url": "https://example.com/epg.xml"})
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", "/ui/epg-sources", None)

    assert status == 200
    assert 'data-action="delete-epg-source"' in body


def test_epg_sources_ui_has_live_job_feedback_and_refresh_hooks(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", "/ui/epg-sources", None)

    assert status == 200
    assert 'id="epg-sources-table"' in body
    assert 'id="job-panel" aria-live="polite"' in body
    assert "refreshEpgSources" in body
    assert "Working..." in body
    assert "Action failed" in body


def test_locked_database_returns_controlled_unavailable_response(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    monkeypatch.setattr(store, "list_epg_sources", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")))
    app = build_test_server(store, service)

    status, headers, body = app("GET", "/api/epg-sources", None)

    assert status == 503
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == {"error": "database busy; retry shortly"}


def test_channel_ui_exposes_delete_control(tmp_path: Path) -> None:
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
                "tvg_rec": "3",
            }
        ]
    )
    service = AdminService(
        store,
        AdminServiceSettings(
            output_dir=tmp_path / "published",
            diagnostics_dir=tmp_path / "diagnostics",
        ),
    )
    app = build_test_server(store, service)

    status, _, body = app("GET", f"/ui/channels/{store.list_channels()[0].id}", None)

    assert status == 200
    assert 'data-action="delete-channel"' in body
