from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.admin_service import AdminService, AdminServiceSettings
from app.admin_store import AdminStore, bootstrap_from_playlist


@dataclass(frozen=True)
class RuntimeSettings:
    db_path: Path
    raw_playlist_path: Path
    fallback_playlist_path: Path
    output_dir: Path
    diagnostics_dir: Path
    epg_work_dir: Path
    bind_host: str
    bind_port: int

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        output_dir = Path(os.getenv("OUTPUT_DIR", "/data/output"))
        return cls(
            db_path=Path(os.getenv("ADMIN_DB_PATH", "/data/state/admin/playlist.db")),
            raw_playlist_path=Path(os.getenv("RAW_PLAYLIST_PATH", "/data/input/playlist.m3u")),
            fallback_playlist_path=output_dir / "playlist_emby_clean.m3u8",
            output_dir=output_dir,
            diagnostics_dir=Path(os.getenv("DIAGNOSTICS_DIR", "/data/state/diagnostics")),
            epg_work_dir=Path(os.getenv("EPG_WORK_DIR", "/data/state/epg")),
            bind_host=os.getenv("ADMIN_BIND_HOST", "0.0.0.0"),
            bind_port=int(os.getenv("ADMIN_BIND_PORT", "8780")),
        )


@dataclass(frozen=True)
class AdminContext:
    settings: RuntimeSettings
    store: AdminStore
    service: AdminService


def build_admin_context(settings: RuntimeSettings | None = None) -> AdminContext:
    settings = settings or RuntimeSettings.from_env()
    store = AdminStore(settings.db_path)
    store.initialize()

    bootstrap_from_playlist(
        store,
        playlist_path=settings.raw_playlist_path,
        fallback_playlist_path=settings.fallback_playlist_path,
    )
    store.seed_default_epg_sources(default_epg_sources())

    service = AdminService(
        store=store,
        settings=AdminServiceSettings(
            output_dir=settings.output_dir,
            diagnostics_dir=settings.diagnostics_dir,
            epg_work_dir=settings.epg_work_dir,
        ),
    )
    return AdminContext(settings=settings, store=store, service=service)


def default_epg_sources() -> list[tuple[str, str]]:
    return [
        ("Default", os.getenv("EPG_SOURCE_URL", "http://epg.one/epg2.xml.gz")),
        ("Israel primary", os.getenv("EPG_ISRAEL_PRIMARY_URL", "https://iptvx.one/EPG")),
        ("Israel fallback", os.getenv("EPG_ISRAEL_FALLBACK_URL", "https://iptv-epg.org/files/epg-il.xml.gz")),
    ]
