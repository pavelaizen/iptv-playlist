from __future__ import annotations

import sqlite3
from pathlib import Path

from app.admin_m3u import import_playlist_entries
from app.admin_models import ChannelDraft, ChannelSnapshot, EpgSource

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_order INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    name TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    stream_url TEXT NOT NULL,
    tvg_id TEXT NOT NULL DEFAULT '',
    tvg_name TEXT NOT NULL DEFAULT '',
    tvg_logo TEXT NOT NULL DEFAULT '',
    tvg_rec TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL,
    draft_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS channel_live_snapshots (
    channel_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    group_name TEXT NOT NULL DEFAULT '',
    stream_url TEXT NOT NULL,
    tvg_id TEXT NOT NULL DEFAULT '',
    tvg_name TEXT NOT NULL DEFAULT '',
    tvg_logo TEXT NOT NULL DEFAULT '',
    tvg_rec TEXT NOT NULL DEFAULT '',
    validated_version INTEGER NOT NULL,
    validated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS channel_validation_states (
    channel_id INTEGER PRIMARY KEY,
    status TEXT NOT NULL,
    last_checked_at TEXT,
    last_error TEXT NOT NULL DEFAULT '',
    checked_version INTEGER NOT NULL DEFAULT 0,
    draft_differs_from_live INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS epg_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL,
    last_fetch_status TEXT NOT NULL DEFAULT '',
    last_fetch_error TEXT NOT NULL DEFAULT '',
    last_success_at TEXT
);

CREATE TABLE IF NOT EXISTS channel_epg_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    epg_source_id INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    channel_xmltv_id TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE,
    FOREIGN KEY(epg_source_id) REFERENCES epg_sources(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS validation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_type TEXT NOT NULL,
    status TEXT NOT NULL,
    valid_count INTEGER NOT NULL DEFAULT 0,
    invalid_count INTEGER NOT NULL DEFAULT 0,
    publish_changed INTEGER NOT NULL DEFAULT 0,
    epg_matched_channels INTEGER NOT NULL DEFAULT 0,
    epg_programmes INTEGER NOT NULL DEFAULT 0,
    error_summary TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT
);
"""


class AdminStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)

    def seed_default_epg_sources(self, defaults: list[tuple[str, str]]) -> None:
        with self._connect() as conn:
            existing = conn.execute("SELECT COUNT(*) FROM epg_sources").fetchone()[0]
            if existing:
                return
            conn.executemany(
                """
                INSERT INTO epg_sources (display_name, source_url, enabled, priority)
                VALUES (?, ?, 1, ?)
                """,
                [
                    (display_name, source_url, index)
                    for index, (display_name, source_url) in enumerate(defaults)
                ],
            )

    def channel_count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]

    def import_channels(self, imported_rows: list[dict[str, str]]) -> None:
        with self._connect() as conn:
            for index, row in enumerate(imported_rows):
                cursor = conn.execute(
                    """
                    INSERT INTO channels (
                        display_order, enabled, name, group_name, stream_url,
                        tvg_id, tvg_name, tvg_logo, tvg_rec, source_kind, draft_version
                    )
                    VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, 'migrated', 1)
                    """,
                    (
                        index,
                        row["name"],
                        row["group_name"],
                        row["stream_url"],
                        row["tvg_id"],
                        row["tvg_name"],
                        row["tvg_logo"],
                        row["tvg_rec"],
                    ),
                )
                channel_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    INSERT INTO channel_live_snapshots (
                        channel_id, name, group_name, stream_url,
                        tvg_id, tvg_name, tvg_logo, tvg_rec, validated_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        channel_id,
                        row["name"],
                        row["group_name"],
                        row["stream_url"],
                        row["tvg_id"],
                        row["tvg_name"],
                        row["tvg_logo"],
                        row["tvg_rec"],
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO channel_validation_states (
                        channel_id, status, checked_version, draft_differs_from_live
                    )
                    VALUES (?, 'new', 0, 0)
                    """,
                    (channel_id,),
                )

    def list_channels(self) -> list[ChannelDraft]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT
                    c.id,
                    c.display_order,
                    c.enabled,
                    c.name,
                    c.group_name,
                    c.stream_url,
                    c.tvg_id,
                    c.tvg_name,
                    c.tvg_logo,
                    c.tvg_rec,
                    c.draft_version,
                    vs.status,
                    vs.draft_differs_from_live,
                    ls.name AS live_name,
                    ls.group_name AS live_group_name,
                    ls.stream_url AS live_stream_url,
                    ls.tvg_id AS live_tvg_id,
                    ls.tvg_name AS live_tvg_name,
                    ls.tvg_logo AS live_tvg_logo,
                    ls.tvg_rec AS live_tvg_rec,
                    ls.validated_version AS live_validated_version
                FROM channels c
                JOIN channel_validation_states vs ON vs.channel_id = c.id
                LEFT JOIN channel_live_snapshots ls ON ls.channel_id = c.id
                ORDER BY c.display_order, c.id
                """
            ).fetchall()
        drafts: list[ChannelDraft] = []
        for row in rows:
            snapshot = None
            if row["live_stream_url"] is not None:
                snapshot = ChannelSnapshot(
                    name=row["live_name"],
                    group_name=row["live_group_name"],
                    stream_url=row["live_stream_url"],
                    tvg_id=row["live_tvg_id"],
                    tvg_name=row["live_tvg_name"],
                    tvg_logo=row["live_tvg_logo"],
                    tvg_rec=row["live_tvg_rec"],
                    validated_version=row["live_validated_version"],
                )
            drafts.append(
                ChannelDraft(
                    id=row["id"],
                    display_order=row["display_order"],
                    enabled=bool(row["enabled"]),
                    name=row["name"],
                    group_name=row["group_name"],
                    stream_url=row["stream_url"],
                    tvg_id=row["tvg_id"],
                    tvg_name=row["tvg_name"],
                    tvg_logo=row["tvg_logo"],
                    tvg_rec=row["tvg_rec"],
                    draft_version=row["draft_version"],
                    status=row["status"],
                    draft_differs_from_live=bool(row["draft_differs_from_live"]),
                    live_snapshot=snapshot,
                )
            )
        return drafts

    def list_epg_sources(self) -> list[EpgSource]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, display_name, source_url, enabled, priority FROM epg_sources ORDER BY priority, id"
            ).fetchall()
        return [
            EpgSource(
                id=row["id"],
                display_name=row["display_name"],
                source_url=row["source_url"],
                enabled=bool(row["enabled"]),
                priority=row["priority"],
            )
            for row in rows
        ]

    def get_channel(self, channel_id: int) -> ChannelDraft:
        channels = {channel.id: channel for channel in self.list_channels()}
        return channels[channel_id]

    def update_channel(self, channel_id: int, payload: dict[str, object]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channels
                SET
                    enabled = ?,
                    name = ?,
                    group_name = ?,
                    stream_url = ?,
                    tvg_id = ?,
                    tvg_name = ?,
                    tvg_logo = ?,
                    tvg_rec = ?,
                    draft_version = draft_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    int(bool(payload["enabled"])),
                    payload["name"],
                    payload["group_name"],
                    payload["stream_url"],
                    payload["tvg_id"],
                    payload["tvg_name"],
                    payload["tvg_logo"],
                    payload["tvg_rec"],
                    channel_id,
                ),
            )
            conn.execute(
                """
                UPDATE channel_validation_states
                SET status = 'new', draft_differs_from_live = 1
                WHERE channel_id = ?
                """,
                (channel_id,),
            )

    def replace_live_snapshot(self, channel_id: int, draft: ChannelDraft) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_live_snapshots (
                    channel_id, name, group_name, stream_url,
                    tvg_id, tvg_name, tvg_logo, tvg_rec, validated_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    name = excluded.name,
                    group_name = excluded.group_name,
                    stream_url = excluded.stream_url,
                    tvg_id = excluded.tvg_id,
                    tvg_name = excluded.tvg_name,
                    tvg_logo = excluded.tvg_logo,
                    tvg_rec = excluded.tvg_rec,
                    validated_version = excluded.validated_version,
                    validated_at = CURRENT_TIMESTAMP
                """,
                (
                    channel_id,
                    draft.name,
                    draft.group_name,
                    draft.stream_url,
                    draft.tvg_id,
                    draft.tvg_name,
                    draft.tvg_logo,
                    draft.tvg_rec,
                    draft.draft_version,
                ),
            )
            conn.execute(
                """
                UPDATE channel_validation_states
                SET
                    status = 'valid',
                    last_checked_at = CURRENT_TIMESTAMP,
                    last_error = '',
                    checked_version = ?,
                    draft_differs_from_live = 0
                WHERE channel_id = ?
                """,
                (draft.draft_version, channel_id),
            )

    def mark_channel_invalid(self, channel_id: int, draft_version: int, error_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_validation_states
                SET
                    status = 'invalid',
                    last_checked_at = CURRENT_TIMESTAMP,
                    last_error = ?,
                    checked_version = ?,
                    draft_differs_from_live = 1
                WHERE channel_id = ?
                """,
                (error_text, draft_version, channel_id),
            )

    def record_validation_run(
        self,
        *,
        trigger_type: str,
        status: str,
        valid_count: int,
        invalid_count: int,
        publish_changed: bool,
        epg_matched_channels: int,
        epg_programmes: int,
        error_summary: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO validation_runs (
                    trigger_type, status, valid_count, invalid_count,
                    publish_changed, epg_matched_channels, epg_programmes,
                    error_summary, finished_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                """,
                (
                    trigger_type,
                    status,
                    valid_count,
                    invalid_count,
                    int(publish_changed),
                    epg_matched_channels,
                    epg_programmes,
                    error_summary,
                ),
            )

    def list_channel_epg_mappings(self, channel_id: int) -> list[dict[str, object]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, channel_id, epg_source_id, priority, channel_xmltv_id, enabled
                FROM channel_epg_mappings
                WHERE channel_id = ?
                ORDER BY priority, id
                """,
                (channel_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_channel_epg_mapping(
        self,
        channel_id: int,
        epg_source_id: int,
        priority: int,
        channel_xmltv_id: str,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO channel_epg_mappings (
                    channel_id, epg_source_id, priority, channel_xmltv_id, enabled
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (channel_id, epg_source_id, priority, channel_xmltv_id),
            )

    def list_enabled_epg_sources_payload(self) -> list[dict[str, object]]:
        return [
            {
                "id": source.id,
                "display_name": source.display_name,
                "source_url": source.source_url,
                "enabled": source.enabled,
            }
            for source in self.list_epg_sources()
            if source.enabled
        ]

    def list_runs(self, limit: int = 20) -> list[dict[str, object]]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, trigger_type, status, valid_count, invalid_count,
                       publish_changed, epg_matched_channels, epg_programmes,
                       error_summary, started_at, finished_at
                FROM validation_runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def bootstrap_from_playlist(
    store: AdminStore,
    playlist_path: Path | None,
    fallback_playlist_path: Path | None,
) -> bool:
    if store.channel_count():
        return False

    source_paths: list[Path] = []
    if playlist_path and playlist_path.exists():
        source_paths.append(playlist_path)
    if (
        fallback_playlist_path
        and fallback_playlist_path.exists()
        and fallback_playlist_path not in source_paths
    ):
        source_paths.append(fallback_playlist_path)

    for source_path in source_paths:
        try:
            imported_rows = import_playlist_entries(source_path)
        except Exception:
            continue
        if not imported_rows:
            continue
        store.import_channels(imported_rows)
        return True

    return False
