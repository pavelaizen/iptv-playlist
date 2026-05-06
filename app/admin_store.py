from __future__ import annotations

from collections.abc import Iterable
import sqlite3
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.admin_m3u import import_playlist_entries
from app.admin_models import ChannelDraft, ChannelSnapshot, ChannelStreamVariant, EpgSource
from app.epg_sources import canonicalize_epg_source_url

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

CREATE TABLE IF NOT EXISTS channel_stream_variants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL,
    display_order INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_probe_status TEXT NOT NULL DEFAULT 'new',
    last_probe_error TEXT NOT NULL DEFAULT '',
    last_probe_at TEXT,
    last_stability_status TEXT NOT NULL DEFAULT 'new',
    last_stability_error TEXT NOT NULL DEFAULT '',
    last_stability_speed TEXT NOT NULL DEFAULT '',
    last_stability_frames INTEGER NOT NULL DEFAULT 0,
    last_stability_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS epg_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    display_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    normalized_url TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    priority INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    channel_count INTEGER NOT NULL DEFAULT 0,
    last_loaded_at TEXT,
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

CREATE TABLE IF NOT EXISTS epg_channel_cache (
    source_id INTEGER NOT NULL,
    epg_channel_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    normalized_search_text TEXT NOT NULL,
    PRIMARY KEY(source_id, epg_channel_id),
    FOREIGN KEY(source_id) REFERENCES epg_sources(id) ON DELETE CASCADE
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


def normalize_source_url(source_url: str) -> str:
    parts = urlsplit(canonicalize_epg_source_url(source_url))
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    netloc = parts.netloc.casefold()
    return urlunsplit((parts.scheme.casefold(), netloc, parts.path, query, ""))


def _require_source_url(value: object) -> str:
    source_url = canonicalize_epg_source_url(str(value or "").strip())
    if not source_url:
        raise ValueError("EPG source URL is required")
    return source_url


def _row_has_column(conn: sqlite3.Connection, table_name: str, column_name: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return any(str(row[1]) == column_name for row in rows)


class AdminStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_SQL)
            self._migrate_schema(conn)

    def _migrate_schema(self, conn: sqlite3.Connection) -> None:
        epg_source_columns = {
            "normalized_url": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT ''",
            "channel_count": "INTEGER NOT NULL DEFAULT 0",
            "last_loaded_at": "TEXT",
        }
        for column, ddl in epg_source_columns.items():
            if not _row_has_column(conn, "epg_sources", column):
                conn.execute(f"ALTER TABLE epg_sources ADD COLUMN {column} {ddl}")

        stream_variant_columns = {
            "last_stability_status": "TEXT NOT NULL DEFAULT 'new'",
            "last_stability_error": "TEXT NOT NULL DEFAULT ''",
            "last_stability_speed": "TEXT NOT NULL DEFAULT ''",
            "last_stability_frames": "INTEGER NOT NULL DEFAULT 0",
            "last_stability_at": "TEXT",
        }
        for column, ddl in stream_variant_columns.items():
            if not _row_has_column(conn, "channel_stream_variants", column):
                conn.execute(f"ALTER TABLE channel_stream_variants ADD COLUMN {column} {ddl}")

        rows = conn.execute("SELECT id, source_url, normalized_url FROM epg_sources").fetchall()
        for source_id, source_url, normalized_url in rows:
            canonical_url = _require_source_url(source_url)
            recalculated_normalized_url = normalize_source_url(canonical_url)
            if source_url != canonical_url or normalized_url != recalculated_normalized_url:
                conn.execute(
                    "UPDATE epg_sources SET source_url = ?, normalized_url = ? WHERE id = ?",
                    (canonical_url, recalculated_normalized_url, source_id),
                )

        existing_variant_count = conn.execute("SELECT COUNT(*) FROM channel_stream_variants").fetchone()[0]
        if existing_variant_count:
            return
        channel_rows = conn.execute(
            """
            SELECT id, stream_url
            FROM channels
            ORDER BY display_order, id
            """
        ).fetchall()
        for channel_id, stream_url in channel_rows:
            conn.execute(
                """
                INSERT INTO channel_stream_variants (
                    channel_id, label, url, display_order, enabled, last_probe_status
                )
                VALUES (?, 'Orig', ?, 0, 1, 'new')
                """,
                (channel_id, stream_url),
            )

    def seed_default_epg_sources(self, defaults: list[tuple[str, str]]) -> None:
        with self._connect() as conn:
            existing = conn.execute("SELECT COUNT(*) FROM epg_sources").fetchone()[0]
            if existing:
                return
            conn.executemany(
                """
                INSERT INTO epg_sources (display_name, source_url, normalized_url, enabled, priority)
                VALUES (?, ?, ?, 1, ?)
                """,
                [
                    (display_name, source_url, normalize_source_url(source_url), index)
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
                conn.execute(
                    """
                    INSERT INTO channel_stream_variants (
                        channel_id, label, url, display_order, enabled, last_probe_status
                    )
                    VALUES (?, 'Orig', ?, 0, 1, 'new')
                    """,
                    (channel_id, row["stream_url"]),
                )

    def add_channel(self, payload: dict[str, object]) -> ChannelDraft:
        with self._connect() as conn:
            next_order = conn.execute("SELECT COALESCE(MAX(display_order), -1) + 1 FROM channels").fetchone()[0]
            stream_url = str(payload.get("stream_url") or payload.get("url") or "")
            cursor = conn.execute(
                """
                INSERT INTO channels (
                    display_order, enabled, name, group_name, stream_url,
                    tvg_id, tvg_name, tvg_logo, tvg_rec, source_kind, draft_version
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'manual', 1)
                """,
                (
                    int(payload.get("display_order", next_order)),
                    int(bool(payload.get("enabled", True))),
                    str(payload["name"]),
                    str(payload.get("group_name") or payload.get("group_title") or ""),
                    stream_url,
                    str(payload.get("tvg_id", "")),
                    str(payload.get("tvg_name", "")),
                    str(payload.get("tvg_logo") or payload.get("logo") or ""),
                    str(payload.get("tvg_rec", "")),
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
                    str(payload["name"]),
                    str(payload.get("group_name") or payload.get("group_title") or ""),
                    stream_url,
                    str(payload.get("tvg_id", "")),
                    str(payload.get("tvg_name", "")),
                    str(payload.get("tvg_logo") or payload.get("logo") or ""),
                    str(payload.get("tvg_rec", "")),
                ),
            )
            conn.execute(
                """
                INSERT INTO channel_validation_states (
                    channel_id, status, checked_version, draft_differs_from_live
                )
                VALUES (?, 'new', 0, 1)
                """,
                (channel_id,),
            )
            if stream_url:
                conn.execute(
                    """
                    INSERT INTO channel_stream_variants (
                        channel_id, label, url, display_order, enabled, last_probe_status
                    )
                    VALUES (?, ?, ?, 0, 1, 'new')
                    """,
                    (channel_id, str(payload.get("variant_label", "Orig")), stream_url),
                )
        return self.get_channel(channel_id)

    def delete_channel(self, channel_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))

    def reorder_channels(self, channel_ids: list[int]) -> None:
        with self._connect() as conn:
            for display_order, channel_id in enumerate(channel_ids):
                conn.execute(
                    """
                    UPDATE channels
                    SET display_order = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (display_order, channel_id),
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
                    ls.validated_version AS live_validated_version,
                    COALESCE(
                        (SELECT COUNT(*) FROM channel_epg_mappings em
                         WHERE em.channel_id = c.id AND em.enabled = 1),
                        0
                    ) AS epg_mapping_count
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
                    epg_mapping_count=row["epg_mapping_count"],
                    live_snapshot=snapshot,
                )
            )
        return drafts

    def list_epg_sources(self) -> list[EpgSource]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, display_name, source_url, normalized_url, enabled, priority,
                       status, channel_count, last_loaded_at, last_fetch_error
                FROM epg_sources
                ORDER BY priority, id
                """
            ).fetchall()
        return [
            EpgSource(
                id=row["id"],
                display_name=row["display_name"],
                source_url=row["source_url"],
                enabled=bool(row["enabled"]),
                priority=row["priority"],
                normalized_url=row["normalized_url"],
                status=row["status"],
                channel_count=row["channel_count"],
                last_loaded_at=row["last_loaded_at"],
                last_error=row["last_fetch_error"],
            )
            for row in rows
        ]

    def get_epg_source(self, source_id: int) -> EpgSource:
        sources = {source.id: source for source in self.list_epg_sources()}
        return sources[source_id]

    def add_epg_source(self, payload: dict[str, object]) -> EpgSource:
        source_url = _require_source_url(payload.get("source_url"))
        normalized_url = normalize_source_url(source_url)
        display_name = str(payload.get("display_name") or payload.get("name") or source_url)
        with self._connect() as conn:
            duplicate = conn.execute(
                "SELECT id FROM epg_sources WHERE normalized_url = ?",
                (normalized_url,),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("duplicate normalized EPG source URL")
            next_priority = conn.execute("SELECT COALESCE(MAX(priority), -1) + 1 FROM epg_sources").fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO epg_sources (
                    display_name, source_url, normalized_url, enabled, priority, status
                )
                VALUES (?, ?, ?, ?, ?, 'new')
                """,
                (
                    display_name,
                    source_url,
                    normalized_url,
                    int(bool(payload.get("enabled", True))),
                    int(payload.get("priority", next_priority)),
                ),
            )
            source_id = int(cursor.lastrowid)
        return self.get_epg_source(source_id)

    def ensure_epg_source(
        self, source_url: str, display_name: str, *, enabled: bool = True
    ) -> EpgSource:
        source_url = _require_source_url(source_url)
        normalized_url = normalize_source_url(source_url)
        display_name = str(display_name or source_url)
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM epg_sources WHERE normalized_url = ?",
                (normalized_url,),
            ).fetchone()
            if existing is not None:
                if enabled:
                    conn.execute(
                        "UPDATE epg_sources SET enabled = 1 WHERE id = ? AND enabled = 0",
                        (existing[0],),
                    )
                return self.get_epg_source(int(existing[0]))
            next_priority = conn.execute(
                "SELECT COALESCE(MAX(priority), -1) + 1 FROM epg_sources"
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO epg_sources (
                    display_name, source_url, normalized_url, enabled, priority, status
                )
                VALUES (?, ?, ?, ?, ?, 'new')
                """,
                (
                    display_name,
                    source_url,
                    normalized_url,
                    int(enabled),
                    next_priority,
                ),
            )
            source_id = int(cursor.lastrowid)
        return self.get_epg_source(source_id)

    def set_channel_logo_url(self, channel_id: int, logo_url: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channels
                SET tvg_logo = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND (tvg_logo = '' OR tvg_logo IS NULL)
                """,
                (logo_url, channel_id),
            )

    def update_epg_source(self, source_id: int, payload: dict[str, object]) -> EpgSource:
        current = self.get_epg_source(source_id)
        source_url = _require_source_url(payload.get("source_url", current.source_url))
        normalized_url = normalize_source_url(source_url)
        with self._connect() as conn:
            duplicate = conn.execute(
                "SELECT id FROM epg_sources WHERE normalized_url = ? AND id != ?",
                (normalized_url, source_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("duplicate normalized EPG source URL")
            conn.execute(
                """
                UPDATE epg_sources
                SET display_name = ?, source_url = ?, normalized_url = ?, enabled = ?
                WHERE id = ?
                """,
                (
                    str(payload.get("display_name", current.display_name)),
                    source_url,
                    normalized_url,
                    int(bool(payload.get("enabled", current.enabled))),
                    source_id,
                ),
            )
        return self.get_epg_source(source_id)

    def delete_epg_source(self, source_id: int) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM epg_sources WHERE id = ?", (source_id,))

    def list_channel_ids_for_epg_source(self, source_id: int) -> list[int]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT channel_id
                FROM channel_epg_mappings
                WHERE epg_source_id = ?
                ORDER BY channel_id
                """,
                (source_id,),
            ).fetchall()
        return [int(row[0]) for row in rows]

    def channel_has_valid_enabled_mapping(self, channel_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM channel_epg_mappings mapping
                JOIN epg_sources source
                    ON source.id = mapping.epg_source_id
                JOIN epg_channel_cache cache
                    ON cache.source_id = mapping.epg_source_id
                   AND cache.epg_channel_id = mapping.channel_xmltv_id
                WHERE mapping.channel_id = ?
                  AND mapping.enabled = 1
                  AND source.enabled = 1
                  AND source.status = 'loaded'
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
        return row is not None

    def replace_epg_channel_cache(self, source_id: int, channels: list[dict[str, str]]) -> None:
        self.replace_epg_channel_cache_from_iterable(source_id, channels)

    def replace_epg_channel_cache_from_iterable(
        self,
        source_id: int,
        channels: Iterable[dict[str, str]],
    ) -> int:
        count = 0
        batch: list[tuple[object, ...]] = []
        with self._connect() as conn:
            conn.execute("DELETE FROM epg_channel_cache WHERE source_id = ?", (source_id,))

            def flush_batch() -> None:
                nonlocal count
                if not batch:
                    return
                conn.executemany(
                    """
                    INSERT OR REPLACE INTO epg_channel_cache (
                        source_id, epg_channel_id, display_name, normalized_search_text
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    batch,
                )
                count += len(batch)
                batch.clear()

            for channel in channels:
                batch.append(
                    (
                        source_id,
                        str(channel["id"]),
                        str(channel["display_name"]),
                        f"{channel['id']} {channel['display_name']}".casefold(),
                    )
                )
                if len(batch) >= 1000:
                    flush_batch()
            flush_batch()

            conn.execute(
                """
                UPDATE epg_sources
                SET status = 'loaded',
                    channel_count = ?,
                    last_loaded_at = CURRENT_TIMESTAMP,
                    last_fetch_status = 'ok',
                    last_fetch_error = '',
                    last_success_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (count, source_id),
            )
        return count

    def mark_epg_source_error(self, source_id: int, error_text: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE epg_sources
                SET status = 'error',
                    last_fetch_status = 'error',
                    last_fetch_error = ?
                WHERE id = ?
                """,
                (error_text[:300], source_id),
            )

    def search_epg_channel_cache(
        self,
        source_id: int,
        query: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, str]]:
        needle = query.casefold().strip()
        sql = """
            SELECT epg_channel_id, display_name
            FROM epg_channel_cache
            WHERE source_id = ?
        """
        params: list[object] = [source_id]
        if needle:
            sql += " AND normalized_search_text LIKE ?"
            params.append(f"%{needle}%")
        sql += " ORDER BY display_name, epg_channel_id LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def get_channel(self, channel_id: int) -> ChannelDraft:
        channels = {channel.id: channel for channel in self.list_channels()}
        return channels[channel_id]

    def update_channel(self, channel_id: int, payload: dict[str, object]) -> None:
        with self._connect() as conn:
            existing_stream_url = conn.execute(
                "SELECT stream_url FROM channels WHERE id = ?",
                (channel_id,),
            ).fetchone()
            stream_url = payload.get("stream_url")
            if stream_url is None and existing_stream_url is not None:
                stream_url = existing_stream_url[0]
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
                    stream_url,
                    payload["tvg_id"],
                    payload["tvg_name"],
                    payload["tvg_logo"],
                    payload["tvg_rec"],
                    channel_id,
                ),
            )
            if "stream_url" in payload:
                first_variant = conn.execute(
                    """
                    SELECT id
                    FROM channel_stream_variants
                    WHERE channel_id = ?
                    ORDER BY display_order, id
                    LIMIT 1
                    """,
                    (channel_id,),
                ).fetchone()
                if first_variant is not None:
                    conn.execute(
                        """
                        UPDATE channel_stream_variants
                        SET url = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (stream_url, first_variant[0]),
                    )
            conn.execute(
                """
                UPDATE channel_validation_states
                SET status = 'new', draft_differs_from_live = 1
                WHERE channel_id = ?
                """,
                (channel_id,),
            )

    def list_stream_variants(self, channel_id: int | None = None) -> list[ChannelStreamVariant]:
        sql = """
            SELECT id, channel_id, label, url, display_order, enabled,
                   last_probe_status, last_probe_error, last_probe_at,
                   last_stability_status, last_stability_error,
                   last_stability_speed, last_stability_frames, last_stability_at
            FROM channel_stream_variants
        """
        params: tuple[object, ...] = ()
        if channel_id is not None:
            sql += " WHERE channel_id = ?"
            params = (channel_id,)
        sql += " ORDER BY channel_id, display_order, id"
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
        return [
            ChannelStreamVariant(
                id=row["id"],
                channel_id=row["channel_id"],
                label=row["label"],
                url=row["url"],
                display_order=row["display_order"],
                enabled=bool(row["enabled"]),
                last_probe_status=row["last_probe_status"],
                last_probe_error=row["last_probe_error"],
                last_probe_at=row["last_probe_at"],
                last_stability_status=row["last_stability_status"],
                last_stability_error=row["last_stability_error"],
                last_stability_speed=row["last_stability_speed"],
                last_stability_frames=row["last_stability_frames"],
                last_stability_at=row["last_stability_at"],
            )
            for row in rows
        ]

    def get_stream_variant(self, stream_id: int) -> ChannelStreamVariant:
        variants = {variant.id: variant for variant in self.list_stream_variants()}
        return variants[stream_id]

    def add_stream_variant(self, channel_id: int, payload: dict[str, object]) -> ChannelStreamVariant:
        with self._connect() as conn:
            next_order = conn.execute(
                """
                SELECT COALESCE(MAX(display_order), -1) + 1
                FROM channel_stream_variants
                WHERE channel_id = ?
                """,
                (channel_id,),
            ).fetchone()[0]
            cursor = conn.execute(
                """
                INSERT INTO channel_stream_variants (
                    channel_id, label, url, display_order, enabled, last_probe_status
                )
                VALUES (?, ?, ?, ?, ?, 'new')
                """,
                (
                    channel_id,
                    str(payload.get("label") or "Variant"),
                    str(payload["url"]),
                    int(payload.get("display_order", next_order)),
                    int(bool(payload.get("enabled", True))),
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
            stream_id = int(cursor.lastrowid)
        return self.get_stream_variant(stream_id)

    def update_stream_variant(self, stream_id: int, payload: dict[str, object]) -> ChannelStreamVariant:
        current = self.get_stream_variant(stream_id)
        label = str(payload.get("label", current.label))
        url = str(payload.get("url", current.url))
        enabled = bool(payload.get("enabled", current.enabled))
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_stream_variants
                SET label = ?, url = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (label, url, int(enabled), stream_id),
            )
            if current.display_order == 0:
                conn.execute(
                    "UPDATE channels SET stream_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (url, current.channel_id),
                )
            conn.execute(
                """
                UPDATE channel_validation_states
                SET status = 'new', draft_differs_from_live = 1
                WHERE channel_id = ?
                """,
                (current.channel_id,),
            )
        return self.get_stream_variant(stream_id)

    def delete_stream_variant(self, stream_id: int) -> None:
        current = self.get_stream_variant(stream_id)
        with self._connect() as conn:
            conn.execute("DELETE FROM channel_stream_variants WHERE id = ?", (stream_id,))
            conn.execute(
                """
                UPDATE channel_validation_states
                SET status = 'new', draft_differs_from_live = 1
                WHERE channel_id = ?
                """,
                (current.channel_id,),
            )

    def reorder_stream_variants(self, channel_id: int, stream_ids: list[int]) -> None:
        with self._connect() as conn:
            for display_order, stream_id in enumerate(stream_ids):
                conn.execute(
                    """
                    UPDATE channel_stream_variants
                    SET display_order = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND channel_id = ?
                    """,
                    (display_order, stream_id, channel_id),
                )
            first = conn.execute(
                """
                SELECT url
                FROM channel_stream_variants
                WHERE channel_id = ?
                ORDER BY display_order, id
                LIMIT 1
                """,
                (channel_id,),
            ).fetchone()
            if first is not None:
                conn.execute(
                    "UPDATE channels SET stream_url = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (first[0], channel_id),
                )

    def mark_stream_variant_probe_result(
        self,
        stream_id: int,
        *,
        status: str,
        error_text: str = "",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_stream_variants
                SET last_probe_status = ?, last_probe_error = ?, last_probe_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error_text[:300], stream_id),
            )

    def mark_stream_variant_stability_result(
        self,
        stream_id: int,
        *,
        status: str,
        error_text: str = "",
        speed: str = "",
        frames: int = 0,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_stream_variants
                SET
                    last_stability_status = ?,
                    last_stability_error = ?,
                    last_stability_speed = ?,
                    last_stability_frames = ?,
                    last_stability_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error_text[:300], speed[:30], int(frames), stream_id),
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

    def mark_channels_invalid(self, channel_ids: list[int], error_text: str) -> None:
        if not channel_ids:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                UPDATE channel_validation_states
                SET
                    status = 'invalid',
                    last_checked_at = CURRENT_TIMESTAMP,
                    last_error = ?,
                    checked_version = (
                        SELECT draft_version
                        FROM channels
                        WHERE channels.id = channel_validation_states.channel_id
                    ),
                    draft_differs_from_live = 1
                WHERE channel_id = ?
                """,
                [(error_text[:300], channel_id) for channel_id in channel_ids],
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
        epg_channel_name: str = "",
    ) -> dict[str, object]:
        if not channel_xmltv_id.strip():
            raise ValueError("EPG channel ID is required")
        with self._connect() as conn:
            duplicate = conn.execute(
                """
                SELECT id
                FROM channel_epg_mappings
                WHERE channel_id = ?
                  AND epg_source_id = ?
                  AND channel_xmltv_id = ?
                LIMIT 1
                """,
                (channel_id, epg_source_id, channel_xmltv_id),
            ).fetchone()
            if duplicate is not None:
                raise ValueError("duplicate EPG mapping for this channel")
            cursor = conn.execute(
                """
                INSERT INTO channel_epg_mappings (
                    channel_id, epg_source_id, priority, channel_xmltv_id, enabled
                )
                VALUES (?, ?, ?, ?, 1)
                """,
                (channel_id, epg_source_id, priority, channel_xmltv_id),
            )
            mapping_id = int(cursor.lastrowid)
        del epg_channel_name
        return self.get_channel_epg_mapping(channel_id, mapping_id)

    def get_channel_epg_mapping(self, channel_id: int, mapping_id: int) -> dict[str, object]:
        mappings = {int(mapping["id"]): mapping for mapping in self.list_channel_epg_mappings(channel_id)}
        return mappings[mapping_id]

    def update_channel_epg_mapping(
        self,
        channel_id: int,
        mapping_id: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        current = self.get_channel_epg_mapping(channel_id, mapping_id)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE channel_epg_mappings
                SET epg_source_id = ?, priority = ?, channel_xmltv_id = ?, enabled = ?
                WHERE id = ? AND channel_id = ?
                """,
                (
                    int(payload.get("epg_source_id", current["epg_source_id"])),
                    int(payload.get("priority", current["priority"])),
                    str(payload.get("channel_xmltv_id", payload.get("epg_channel_id", current["channel_xmltv_id"]))),
                    int(bool(payload.get("enabled", current["enabled"]))),
                    mapping_id,
                    channel_id,
                ),
            )
        return self.get_channel_epg_mapping(channel_id, mapping_id)

    def delete_channel_epg_mapping(self, channel_id: int, mapping_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM channel_epg_mappings WHERE id = ? AND channel_id = ?",
                (mapping_id, channel_id),
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
