from __future__ import annotations

from types import SimpleNamespace

from app import admin_jobs


def test_nightly_job_reloads_enabled_epg_sources_then_publishes_from_cache(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Store:
        def list_epg_sources(self):
            return [
                SimpleNamespace(id=1, enabled=True),
                SimpleNamespace(id=2, enabled=False),
                SimpleNamespace(id=3, enabled=True),
            ]

    class Service:
        def reload_epg_source(self, source_id: int) -> dict[str, object]:
            calls.append(("reload", source_id))
            return {"status": "ok", "source_id": source_id}

        def validate_all(self, trigger_type: str) -> dict[str, object]:
            calls.append(("validate", trigger_type))
            return {"status": "ok", "valid_count": 12, "invalid_count": 1}

        def publish_from_cache(self) -> dict[str, object]:
            calls.append(("publish-from-cache", None))
            return {"status": "ok"}

    monkeypatch.setattr(
        admin_jobs,
        "build_admin_context",
        lambda: SimpleNamespace(store=Store(), service=Service()),
    )

    result = admin_jobs.run_nightly()

    assert result["status"] == "ok"
    assert result["epg_reload"]["sources"] == [
        {"status": "ok", "source_id": 1},
        {"status": "ok", "source_id": 3},
    ]
    assert result["validation"]["status"] == "ok"
    assert result["publish"]["status"] == "ok"
    assert calls == [
        ("reload", 1),
        ("reload", 3),
        ("validate", "scheduled"),
        ("publish-from-cache", None),
    ]


def test_nightly_job_skips_publish_when_validation_does_not_finish_ok(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Store:
        def list_epg_sources(self):
            return [SimpleNamespace(id=1, enabled=True)]

    class Service:
        def reload_epg_source(self, source_id: int) -> dict[str, object]:
            calls.append(("reload", source_id))
            return {"status": "ok", "source_id": source_id}

        def validate_all(self, trigger_type: str) -> dict[str, object]:
            calls.append(("validate", trigger_type))
            return {"status": "already_running"}

        def publish_from_cache(self) -> dict[str, object]:
            raise AssertionError("publish must not run after failed validation")

    monkeypatch.setattr(
        admin_jobs,
        "build_admin_context",
        lambda: SimpleNamespace(store=Store(), service=Service()),
    )

    result = admin_jobs.run_nightly()

    assert result["status"] == "validation_skipped_publish"
    assert result["publish"] == {"status": "skipped"}
    assert calls == [("reload", 1), ("validate", "scheduled")]


def test_admin_jobs_main_runs_nightly_command(monkeypatch) -> None:
    monkeypatch.setattr(admin_jobs, "run_nightly", lambda: {"status": "ok"})

    assert admin_jobs.main(["nightly"]) == 0
