from __future__ import annotations

import json
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
    monkeypatch.setattr(service, "validate_all", lambda trigger_type: {"status": "ok", "trigger": trigger_type})
    app = build_test_server(store, service)

    status, headers, body = app("POST", "/api/channels/validate", None)

    assert status == 202
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload == {"status": "ok", "trigger": "manual"}


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
