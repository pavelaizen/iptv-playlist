from __future__ import annotations

from app import admin_runtime


def test_runtime_main_starts_web_server_without_scheduler(monkeypatch) -> None:
    calls: list[str] = []

    class Context:
        settings = type("Settings", (), {"bind_host": "127.0.0.1", "bind_port": 8780})()
        store = object()
        service = object()

    monkeypatch.setattr(admin_runtime, "build_admin_context", lambda: Context())
    monkeypatch.setattr(
        admin_runtime,
        "serve",
        lambda **kwargs: calls.append(f"{kwargs['bind_host']}:{kwargs['bind_port']}"),
    )

    admin_runtime.main()

    assert calls == ["127.0.0.1:8780"]
