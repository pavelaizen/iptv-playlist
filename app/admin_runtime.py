from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.admin_service import AdminService, AdminServiceSettings
from app.admin_store import AdminStore, bootstrap_from_playlist
from app.admin_web import serve
from app.epg_worker import parse_run_time, seconds_until_next_run_time


@dataclass(frozen=True)
class RuntimeSettings:
    db_path: Path
    raw_playlist_path: Path
    fallback_playlist_path: Path
    output_dir: Path
    diagnostics_dir: Path
    epg_work_dir: Path
    run_time: tuple[int, int]
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
            run_time=parse_run_time(os.getenv("EPG_RUN_TIME", "04:00")),
            bind_host=os.getenv("ADMIN_BIND_HOST", "0.0.0.0"),
            bind_port=int(os.getenv("ADMIN_BIND_PORT", "8780")),
        )


def main() -> None:
    settings = RuntimeSettings.from_env()
    store = AdminStore(settings.db_path)
    store.initialize()

    bootstrap_from_playlist(
        store,
        playlist_path=settings.raw_playlist_path,
        fallback_playlist_path=settings.fallback_playlist_path,
    )
    store.seed_default_epg_sources(
        [
            ("Default", os.getenv("EPG_SOURCE_URL", "http://epg.one/epg2.xml.gz")),
            ("Israel primary", os.getenv("EPG_ISRAEL_PRIMARY_URL", "https://iptvx.one/EPG")),
            ("Israel fallback", os.getenv("EPG_ISRAEL_FALLBACK_URL", "https://iptv-epg.org/files/epg-il.xml.gz")),
        ]
    )

    service = AdminService(
        store=store,
        settings=AdminServiceSettings(
            output_dir=settings.output_dir,
            diagnostics_dir=settings.diagnostics_dir,
            epg_work_dir=settings.epg_work_dir,
        ),
    )

    scheduler_thread = threading.Thread(
        target=_scheduler_loop,
        args=(service, settings.run_time),
        daemon=True,
        name="playlist-admin-scheduler",
    )
    scheduler_thread.start()

    serve(
        bind_host=settings.bind_host,
        bind_port=settings.bind_port,
        store=store,
        service=service,
    )


def _scheduler_loop(service: AdminService, run_time: tuple[int, int]) -> None:
    while True:
        sleep_seconds = seconds_until_next_run_time(
            datetime.now().astimezone(),
            run_time,
        )
        time.sleep(sleep_seconds)
        service.validate_all(trigger_type="scheduled")


if __name__ == "__main__":
    main()
