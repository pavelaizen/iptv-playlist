# Web-Managed Playlist Control Plane Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the raw-playlist-driven runtime with a SQLite-backed admin service that owns channel definitions, validation, publishing, EPG regeneration, and a same-port web UI/API while keeping `/playlist_emby_clean.m3u8` and `/epg.xml` stable for Emby.

**Architecture:** Add a new `playlist-admin` runtime built on Python 3.12 standard library modules (`sqlite3`, `http.server`, `threading`, `json`, `urllib`) and reuse the existing probe, publish-guard, XMLTV trim, and Emby refresh code as libraries. Keep `playlist-static` as the public edge on `:8766`, serve generated artifacts from `./published`, and reverse-proxy `/ui` and `/api` to the admin service.

**Tech Stack:** Python 3.12 standard library, `sqlite3`, `http.server`, `threading`, `urllib.request`, `xml.etree.ElementTree`, Docker Compose, nginx, pytest.

---

## File Structure

- Create `app/admin_store.py`: SQLite schema bootstrap, row mappers, one-time playlist migration, and CRUD storage methods for channels, snapshots, EPG sources, mappings, and runs.
- Create `app/admin_models.py`: dataclasses and typed literals used by the admin store, service, and web layers.
- Create `app/admin_m3u.py`: playlist import helpers in Task 1, then structured channel rendering in Task 2.
- Create `app/admin_epg.py`: mapping-aware EPG orchestration that downloads configured sources and calls `app.epg` trimming helpers.
- Create `app/admin_service.py`: validation state transitions, probe orchestration, job locking, guarded playlist publish, `epg.xml` regeneration, and run-history writes.
- Create `app/admin_web.py`: `ThreadingHTTPServer` request handler, JSON API routes, and server-rendered HTML UI for channels, EPG sources, runs, and settings.
- Create `app/admin_runtime.py`: service entrypoint, boot-time migration, daily `04:00` scheduler thread, and HTTP server startup.
- Create `nginx/playlist-static.conf`: static serving plus reverse proxy rules for `/ui` and `/api/`.
- Modify `app/epg.py`: add generic source-strategy trimming helpers that accept explicit per-channel source/id mappings and global fallback sources.
- Modify `docker-compose.yml`: replace loop-based `playlist-sanitizer` and `epg-trimmer` with `playlist-admin`.
- Modify `docker-compose.playlist.yml`: mount the custom nginx config and proxy to `playlist-admin`.
- Modify `tests/test_compose_config.py`: assert the new admin service and nginx routing shape.
- Create `tests/test_admin_store.py`: schema, migration, duplicate handling, and EPG source persistence tests.
- Create `tests/test_admin_m3u.py`: structured channel rendering and import parsing tests.
- Create `tests/test_admin_service.py`: validation transitions, publish pipeline, job lock, and partial EPG-source failure tests.
- Create `tests/test_admin_web.py`: API and HTML UI response tests.
- Modify `README.md`: document the admin service, first-run migration, same-port routes, and verification commands.
- Modify `AGENTS.md`: update repo runtime map, storage ownership, operational flow, and verification guidance.

## Task 1: Admin Data Model, SQLite Store, and One-Time Migration

**Files:**
- Create: `app/admin_models.py`
- Create: `app/admin_store.py`
- Create: `app/admin_m3u.py`
- Create: `tests/test_admin_store.py`

- [ ] **Step 1: Write the failing store and migration tests**

Create `tests/test_admin_store.py` with:

```python
from __future__ import annotations

from pathlib import Path

from app.admin_store import AdminStore, bootstrap_from_playlist


def write_playlist(path: Path) -> None:
    path.write_text(
        "#EXTM3U\n"
        "#EXTINF:0 tvg-id=\"chan-1\" tvg-name=\"Channel 1\" tvg-logo=\"logo1\" tvg-rec=\"3\",Channel One\n"
        "#EXTGRP:News\n"
        "http://provider.invalid/one\n"
        "#EXTINF:0,Channel One\n"
        "#EXTGRP:News\n"
        "http://provider.invalid/one-backup\n"
        "#EXTINF:0,Channel Two\n"
        "#EXTGRP:Sports\n"
        "http://provider.invalid/two\n",
        encoding="utf-8",
    )


def test_bootstrap_from_playlist_imports_rows_and_preserves_duplicates(tmp_path: Path):
    db_path = tmp_path / "playlist.db"
    playlist_path = tmp_path / "original_playlist.m3u8"
    write_playlist(playlist_path)

    store = AdminStore(db_path)
    store.initialize()
    imported = bootstrap_from_playlist(store, playlist_path, fallback_playlist_path=None)

    channels = store.list_channels()
    assert imported is True
    assert [channel.display_order for channel in channels] == [0, 1, 2]
    assert [channel.name for channel in channels] == ["Channel One", "Channel One", "Channel Two"]
    assert [channel.stream_url for channel in channels] == [
        "http://provider.invalid/one",
        "http://provider.invalid/one-backup",
        "http://provider.invalid/two",
    ]
    assert [channel.status for channel in channels] == ["new", "new", "new"]
    assert all(channel.live_snapshot is not None for channel in channels)


def test_bootstrap_from_playlist_is_one_shot_when_channels_exist(tmp_path: Path):
    db_path = tmp_path / "playlist.db"
    playlist_path = tmp_path / "original_playlist.m3u8"
    write_playlist(playlist_path)

    store = AdminStore(db_path)
    store.initialize()
    assert bootstrap_from_playlist(store, playlist_path, fallback_playlist_path=None) is True
    assert bootstrap_from_playlist(store, playlist_path, fallback_playlist_path=None) is False
    assert len(store.list_channels()) == 3


def test_default_epg_sources_are_seeded_once(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()

    store.seed_default_epg_sources(
        [
            ("Main", "http://epg.one/epg2.xml.gz"),
            ("IL fallback", "https://iptv-epg.org/files/epg-il.xml.gz"),
        ]
    )
    store.seed_default_epg_sources([("Ignored", "http://example.invalid/other.xml")])

    sources = store.list_epg_sources()
    assert [(source.display_name, source.source_url, source.priority) for source in sources] == [
        ("Main", "http://epg.one/epg2.xml.gz", 0),
        ("IL fallback", "https://iptv-epg.org/files/epg-il.xml.gz", 1),
    ]
```

- [ ] **Step 2: Run the store tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_admin_store.py
```

Expected: FAIL with import errors because `app.admin_store` does not exist yet.

- [ ] **Step 3: Implement admin dataclasses, playlist import helpers, and the SQLite store**

Create `app/admin_models.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ChannelStatus = Literal["new", "valid", "invalid"]


@dataclass(frozen=True, slots=True)
class ChannelDraft:
    id: int
    display_order: int
    enabled: bool
    name: str
    group_name: str
    stream_url: str
    tvg_id: str
    tvg_name: str
    tvg_logo: str
    tvg_rec: str
    draft_version: int
    status: ChannelStatus
    draft_differs_from_live: bool
    live_snapshot: "ChannelSnapshot | None"


@dataclass(frozen=True, slots=True)
class ChannelSnapshot:
    name: str
    group_name: str
    stream_url: str
    tvg_id: str
    tvg_name: str
    tvg_logo: str
    tvg_rec: str
    validated_version: int


@dataclass(frozen=True, slots=True)
class EpgSource:
    id: int
    display_name: str
    source_url: str
    enabled: bool
    priority: int
```

Create `app/admin_m3u.py` with:

```python
from __future__ import annotations

import re
from pathlib import Path

from app.main import parse_m3u


ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def import_playlist_entries(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for metadata_lines, url in parse_m3u(path):
        extinf_line = next(
            (line.strip() for line in metadata_lines if line.strip().upper().startswith("#EXTINF")),
            "",
        )
        group_line = next(
            (line.strip() for line in metadata_lines if line.strip().upper().startswith("#EXTGRP:")),
            "#EXTGRP:",
        )
        _, _, channel_name = extinf_line.partition(",")
        attrs = dict(ATTR_RE.findall(extinf_line))
        rows.append(
            {
                "name": channel_name.strip() or "unnamed-channel",
                "group_name": group_line.split(":", 1)[1].strip(),
                "stream_url": url.strip(),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "tvg_logo": attrs.get("tvg-logo", ""),
                "tvg_rec": attrs.get("tvg-rec", ""),
            }
        )
    return rows
```

Create `app/admin_store.py` with:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from app.admin_models import ChannelDraft, ChannelSnapshot, EpgSource
from app.admin_m3u import import_playlist_entries


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

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.executescript(SCHEMA_SQL)

    def list_channels(self) -> list[ChannelDraft]:
        with sqlite3.connect(self.db_path) as conn:
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
```

- [ ] **Step 4: Implement one-time playlist bootstrap and default EPG source seeding**

Extend `app/admin_store.py` with:

```python
    def seed_default_epg_sources(self, defaults: list[tuple[str, str]]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            existing = conn.execute("SELECT COUNT(*) FROM epg_sources").fetchone()[0]
            if existing:
                return
            conn.executemany(
                """
                INSERT INTO epg_sources (display_name, source_url, enabled, priority)
                VALUES (?, ?, 1, ?)
                """,
                [(display_name, source_url, index) for index, (display_name, source_url) in enumerate(defaults)],
            )

    def channel_count(self) -> int:
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]

    def import_channels(self, imported_rows: list[dict[str, str]]) -> None:
        with sqlite3.connect(self.db_path) as conn:
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

    def list_epg_sources(self) -> list[EpgSource]:
        with sqlite3.connect(self.db_path) as conn:
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

    def list_channel_epg_mappings(self, channel_id: int) -> list[dict[str, object]]:
        with sqlite3.connect(self.db_path) as conn:
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

    def list_runs(self, limit: int = 20) -> list[dict[str, object]]:
        with sqlite3.connect(self.db_path) as conn:
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
    source_path = playlist_path if playlist_path and playlist_path.exists() else fallback_playlist_path
    if source_path is None or not source_path.exists():
        return False
    imported_rows = import_playlist_entries(source_path)
    if not imported_rows:
        return False
    store.import_channels(imported_rows)
    return True
```

- [ ] **Step 5: Run the store tests to verify they pass**

Run:

```bash
python -m pytest -q tests/test_admin_store.py
```

Expected: PASS for bootstrap, duplicate-preservation, and EPG source seeding tests.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add app/admin_models.py app/admin_m3u.py app/admin_store.py tests/test_admin_store.py
git commit -m "feat: add admin sqlite store and migration bootstrap"
```

## Task 2: Structured Channel Rendering and Playlist Import Helpers

**Files:**
- Modify: `app/admin_m3u.py`
- Create: `tests/test_admin_m3u.py`

- [ ] **Step 1: Write the failing M3U import and rendering tests**

Create `tests/test_admin_m3u.py` with:

```python
from __future__ import annotations

from pathlib import Path

from app.admin_m3u import import_playlist_entries, render_channel_entry, render_playlist
from app.admin_models import ChannelSnapshot


def test_import_playlist_entries_extracts_structured_fields(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u8"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:0 tvg-id=\"chan-1\" tvg-name=\"Channel 1\" tvg-logo=\"logo1\" tvg-rec=\"3\",Channel One\n"
        "#EXTGRP:ישראלי\n"
        "http://provider.invalid/one\n",
        encoding="utf-8",
    )

    rows = import_playlist_entries(playlist)

    assert rows == [
        {
            "name": "Channel One",
            "group_name": "ישראלי",
            "stream_url": "http://provider.invalid/one",
            "tvg_id": "chan-1",
            "tvg_name": "Channel 1",
            "tvg_logo": "logo1",
            "tvg_rec": "3",
        }
    ]


def test_render_playlist_uses_structured_fields_and_live_order():
    snapshot = ChannelSnapshot(
        name="Keshet 12 FHD IL",
        group_name="ישראלי",
        stream_url="http://provider.invalid/2329/index.m3u8",
        tvg_id="channel-12-il",
        tvg_name="Keshet 12 FHD IL",
        tvg_logo="logo12",
        tvg_rec="3",
        validated_version=4,
    )

    playlist = render_playlist([snapshot])

    assert "#EXTM3U" in playlist
    assert '#EXTINF:0 tvg-id="channel-12-il" tvg-name="Keshet 12 FHD IL" tvg-logo="logo12" tvg-rec="3",Keshet 12 FHD IL' in playlist
    assert "#EXTGRP:ישראלי" in playlist
    assert "http://provider.invalid/2329/index.m3u8" in playlist
```

- [ ] **Step 2: Run the M3U tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_admin_m3u.py
```

Expected: FAIL because `render_channel_entry()` and `render_playlist()` do not exist yet.

- [ ] **Step 3: Reuse the existing import parser and prepare the rendering tests**

Keep the import parser from Task 1 and confirm `tests/test_admin_m3u.py::test_import_playlist_entries_extracts_structured_fields` now passes once rendering helpers are added.

- [ ] **Step 4: Implement deterministic rendering from structured fields**

Extend `app/admin_m3u.py` with:

```python
from app.admin_models import ChannelSnapshot

def _render_extinf(snapshot: ChannelSnapshot) -> str:
    attributes = []
    if snapshot.tvg_id:
        attributes.append(f'tvg-id="{snapshot.tvg_id}"')
    if snapshot.tvg_name:
        attributes.append(f'tvg-name="{snapshot.tvg_name}"')
    if snapshot.tvg_logo:
        attributes.append(f'tvg-logo="{snapshot.tvg_logo}"')
    if snapshot.tvg_rec:
        attributes.append(f'tvg-rec="{snapshot.tvg_rec}"')
    attr_text = f" {' '.join(attributes)}" if attributes else ""
    return f"#EXTINF:0{attr_text},{snapshot.name}"


def render_channel_entry(snapshot: ChannelSnapshot) -> list[str]:
    lines = [_render_extinf(snapshot)]
    if snapshot.group_name:
        lines.append(f"#EXTGRP:{snapshot.group_name}")
    lines.append(snapshot.stream_url)
    return lines


def render_playlist(snapshots: list[ChannelSnapshot]) -> str:
    lines = ["#EXTM3U"]
    for snapshot in snapshots:
        lines.extend(render_channel_entry(snapshot))
    return "\n".join(lines) + "\n"
```

- [ ] **Step 5: Run the M3U tests to verify they pass**

Run:

```bash
python -m pytest -q tests/test_admin_m3u.py
```

Expected: PASS for import parsing and deterministic M3U rendering.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add app/admin_m3u.py tests/test_admin_m3u.py
git commit -m "feat: add structured channel import and playlist rendering"
```

## Task 3: Validation Pipeline, Live Snapshot Updates, and Guarded Playlist Publish

**Files:**
- Create: `app/admin_service.py`
- Create: `tests/test_admin_service.py`
- Modify: `app/admin_store.py`

- [ ] **Step 1: Write the failing service tests**

Create `tests/test_admin_service.py` with:

```python
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


def test_validate_channel_success_promotes_draft_to_live_snapshot(tmp_path: Path, monkeypatch):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    settings = AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics")
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {"http://provider.invalid/one": True})
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 1, "programmes": 2})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.validate_channel(channel_id)
    channel = store.list_channels()[0]

    assert result["status"] == "valid"
    assert channel.status == "valid"
    assert channel.draft_differs_from_live is False
    assert channel.live_snapshot is not None
    assert (tmp_path / "published" / "playlist_emby_clean.m3u8").exists()


def test_validate_channel_failure_keeps_previous_live_snapshot(tmp_path: Path, monkeypatch):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    settings = AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics")
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {"http://provider.invalid/one": True})
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 1, "programmes": 2})
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


def test_validate_all_rejects_overlapping_run(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    settings = AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics")
    service = AdminService(store=store, settings=settings)

    service._job_lock.acquire()
    try:
        result = service.validate_all(trigger_type="manual")
    finally:
        service._job_lock.release()

    assert result["status"] == "already_running"
```

- [ ] **Step 2: Run the service tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_admin_service.py
```

Expected: FAIL because `app.admin_service` does not exist and store update helpers are missing.

- [ ] **Step 3: Add update helpers and validation-state mutation methods to the store**

Extend `app/admin_store.py` with:

```python
    def update_channel(self, channel_id: int, payload: dict[str, object]) -> None:
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
        with sqlite3.connect(self.db_path) as conn:
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
```

- [ ] **Step 4: Implement the validation and publish service**

Create `app/admin_service.py` with:

```python
from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

from app.admin_m3u import render_playlist
from app.admin_store import AdminStore
from app.emby_client import refresh_livetv_after_publish
from app.probe import ProbeSettings, ProbeTarget, probe_channels
from app.publish import PublishGuardConfig, select_playlist_for_publish


@dataclass(frozen=True)
class AdminServiceSettings:
    output_dir: Path
    diagnostics_dir: Path
    output_playlist_name: str = "playlist_emby_clean.m3u8"
    min_valid_channels_absolute: int = 1
    min_valid_ratio_of_previous: float = 0.7


class AdminService:
    def __init__(self, store: AdminStore, settings: AdminServiceSettings) -> None:
        self.store = store
        self.settings = settings
        self._job_lock = threading.Lock()

    def validate_channel(self, channel_id: int) -> dict[str, object]:
        drafts = {draft.id: draft for draft in self.store.list_channels()}
        draft = drafts[channel_id]
        probe_results = self._probe_urls([draft])
        if not probe_results.get(draft.stream_url):
            self.store.mark_channel_invalid(channel_id, draft.draft_version, "ffprobe failed")
            return {"status": "invalid", "channel_id": channel_id}

        self.store.replace_live_snapshot(channel_id, draft)
        publish_result = self._publish_from_live_snapshots()
        return {"status": "valid", "channel_id": channel_id, "publish": publish_result}

    def validate_all(self, trigger_type: str) -> dict[str, object]:
        if not self._job_lock.acquire(blocking=False):
            return {"status": "already_running"}
        try:
            drafts = [draft for draft in self.store.list_channels() if draft.enabled]
            probe_results = self._probe_urls(drafts)
            valid_count = 0
            invalid_count = 0
            for draft in drafts:
                if probe_results.get(draft.stream_url):
                    self.store.replace_live_snapshot(draft.id, draft)
                    valid_count += 1
                else:
                    self.store.mark_channel_invalid(draft.id, draft.draft_version, "ffprobe failed")
                    invalid_count += 1
            publish_result = self._publish_from_live_snapshots()
            self.store.record_validation_run(
                trigger_type=trigger_type,
                status="ok",
                valid_count=valid_count,
                invalid_count=invalid_count,
                publish_changed=bool(publish_result["content_changed"]),
                epg_matched_channels=int(publish_result["epg"]["matched_channels"]),
                epg_programmes=int(publish_result["epg"]["programmes"]),
                error_summary="",
            )
            return {
                "status": "ok",
                "trigger_type": trigger_type,
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "publish": publish_result,
            }
        finally:
            self._job_lock.release()

    def _publish_from_live_snapshots(self) -> dict[str, object]:
        snapshots = [
            draft.live_snapshot
            for draft in self.store.list_channels()
            if draft.enabled and draft.live_snapshot is not None
        ]
        candidate_content = render_playlist([snapshot for snapshot in snapshots if snapshot is not None])
        candidate_output_path = self.settings.output_dir / self.settings.output_playlist_name
        decision = select_playlist_for_publish(
            candidate_output_path=candidate_output_path,
            previous_clean_path=candidate_output_path,
            candidate_content=candidate_content,
            config=PublishGuardConfig(
                min_valid_channels_absolute=self.settings.min_valid_channels_absolute,
                min_valid_ratio_of_previous=self.settings.min_valid_ratio_of_previous,
                diagnostics_dir=self.settings.diagnostics_dir,
            ),
        )
        epg_result = self._sync_epg()
        if decision.publish_candidate and decision.content_changed:
            self._refresh_emby()
        return {
            "publish_candidate": decision.publish_candidate,
            "content_changed": decision.content_changed,
            "epg": epg_result,
        }

    def _probe_urls(self, drafts) -> dict[str, bool]:
        targets = [
            ProbeTarget(url=draft.stream_url, name=draft.name, fingerprint=str(draft.id))
            for draft in drafts
        ]
        results, _ = asyncio.run(probe_channels(targets, ProbeSettings.from_env()))
        return {result.url: result.success for result in results}

    def _sync_epg(self) -> dict[str, object]:
        return {"changed": False, "matched_channels": 0, "programmes": 0}

    def _refresh_emby(self) -> None:
        refresh_livetv_after_publish()
```

Also add `record_validation_run()` to `app/admin_store.py`:

```python
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
        with sqlite3.connect(self.db_path) as conn:
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
```

- [ ] **Step 5: Run the service tests to verify they pass**

Run:

```bash
python -m pytest -q tests/test_admin_service.py
```

Expected: PASS for validation success, failure preserving live snapshot, and job-lock behavior.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add app/admin_service.py app/admin_store.py tests/test_admin_service.py
git commit -m "feat: add validation service and guarded playlist publish"
```

## Task 4: Mapping-Aware EPG Regeneration With Ordered Fallback Sources

**Files:**
- Create: `app/admin_epg.py`
- Modify: `app/epg.py`
- Modify: `app/admin_store.py`
- Modify: `tests/test_admin_service.py`
- Modify: `tests/test_epg.py`

- [ ] **Step 1: Write the failing explicit-mapping and fallback tests**

Add to `tests/test_epg.py`:

```python
def test_trim_xmltv_prefers_explicit_source_mapping_then_global_name_fallback(tmp_path: Path):
    primary = tmp_path / "primary.xml.gz"
    fallback = tmp_path / "fallback.xml.gz"
    output = tmp_path / "epg.xml"
    write_gzip(primary, "<tv><channel id='x'><display-name>Other</display-name></channel></tv>")
    write_gzip(
        fallback,
        "<tv>"
        "<channel id='chan-12'><display-name>Keshet 12 FHD IL</display-name></channel>"
        "<programme channel='chan-12' start='20260504040000 +0000' stop='20260504050000 +0000'><title>Morning</title></programme>"
        "</tv>",
    )

    summary = epg.trim_xmltv_with_source_strategies(
        published_channels=[
            {
                "name": "Keshet 12 FHD IL",
                "mappings": [
                    {"source_key": "primary", "channel_id": "missing-id"},
                ],
            }
        ],
        sources={
            "primary": primary,
            "fallback": fallback,
        },
        default_source_order=["primary", "fallback"],
        output_xmltv_path=output,
    )

    assert summary.matched_channel_count == 1
    assert summary.programme_count == 1
    assert "Keshet 12 FHD IL" not in summary.unmatched_playlist_names
```

Add to `tests/test_admin_service.py`:

```python
def test_validate_all_continues_when_one_epg_source_download_fails(tmp_path: Path, monkeypatch):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    seed_channel(store)
    settings = AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics")
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {"http://provider.invalid/one": True})
    monkeypatch.setattr(
        service,
        "_sync_epg",
        lambda: {"changed": True, "matched_channels": 1, "programmes": 2, "failed_sources": ["http://bad.invalid/epg.xml.gz"]},
    )
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    result = service.validate_all(trigger_type="manual")

    assert result["status"] == "ok"
    assert result["publish"]["epg"]["failed_sources"] == ["http://bad.invalid/epg.xml.gz"]
```

- [ ] **Step 2: Run the EPG and service tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_epg.py::test_trim_xmltv_prefers_explicit_source_mapping_then_global_name_fallback tests/test_admin_service.py::test_validate_all_continues_when_one_epg_source_download_fails
```

Expected: FAIL because the generic mapping-aware trim function does not exist yet.

- [ ] **Step 3: Extend `app.epg` with generic source-strategy trimming**

Add to `app/epg.py`:

```python
def trim_xmltv_with_source_strategies(
    *,
    published_channels: list[dict[str, object]],
    sources: dict[str, Path],
    default_source_order: list[str],
    output_xmltv_path: Path,
) -> EpgTrimSummary:
    candidates: list[tuple[str, Path, str | None]] = []
    for channel in published_channels:
        name = str(channel["name"])
        mappings = channel.get("mappings", [])
        for mapping in mappings:
            source_key = str(mapping["source_key"])
            source_path = sources.get(source_key)
            if source_path is not None:
                candidates.append((name, source_path, str(mapping["channel_id"])))
        for source_key in default_source_order:
            source_path = sources.get(source_key)
            if source_path is not None:
                candidates.append((name, source_path, None))
    return _trim_first_matching_candidates(candidates, output_xmltv_path)
```

Keep `_trim_first_matching_candidates()` in the same module and implement it by:

- trying explicit `channel_id` matches first
- then normalized display-name matches against the same source
- merging matched `<channel>` and `<programme>` nodes into one plain `epg.xml`
- returning an `EpgTrimSummary`

- [ ] **Step 4: Implement admin EPG orchestration and store-backed mapping reads**

Create `app/admin_epg.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.epg import trim_xmltv_with_source_strategies
from app.epg_worker import download_epg


@dataclass(frozen=True)
class EpgSyncResult:
    changed: bool
    matched_channels: int
    programmes: int
    failed_sources: list[str]


def sync_epg(
    *,
    published_channels: list[dict[str, object]],
    epg_sources: list[dict[str, object]],
    output_path: Path,
    work_dir: Path,
) -> EpgSyncResult:
    sources: dict[str, Path] = {}
    failed_sources: list[str] = []
    for source in epg_sources:
        if not source["enabled"]:
            continue
        source_key = f"source-{source['id']}"
        destination = work_dir / f"{source_key}.xml.gz"
        try:
            download_epg(str(source["source_url"]), destination)
        except Exception:
            failed_sources.append(str(source["source_url"]))
            continue
        sources[source_key] = destination

    summary = trim_xmltv_with_source_strategies(
        published_channels=published_channels,
        sources=sources,
        default_source_order=[f"source-{source['id']}" for source in epg_sources if source["enabled"]],
        output_xmltv_path=output_path,
    )
    return EpgSyncResult(
        changed=True,
        matched_channels=summary.matched_channel_count,
        programmes=summary.programme_count,
        failed_sources=failed_sources,
    )
```

Extend `app/admin_store.py` with methods to list enabled EPG sources and create per-channel mapping rows:

```python
    def list_enabled_epg_sources_payload(self) -> list[dict[str, object]]:
        return [
            {"id": source.id, "display_name": source.display_name, "source_url": source.source_url, "enabled": source.enabled}
            for source in self.list_epg_sources()
            if source.enabled
        ]

    def add_channel_epg_mapping(self, channel_id: int, epg_source_id: int, priority: int, channel_xmltv_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO channel_epg_mappings (channel_id, epg_source_id, priority, channel_xmltv_id, enabled)
                VALUES (?, ?, ?, ?, 1)
                """,
                (channel_id, epg_source_id, priority, channel_xmltv_id),
            )
```

- [ ] **Step 5: Wire the service to the real EPG sync path and run tests**

Replace the stub `_sync_epg()` in `app/admin_service.py` with a call to `sync_epg()` and run:

```bash
python -m pytest -q tests/test_epg.py tests/test_admin_service.py
```

Expected: PASS for explicit mapping preference, global fallback, and degraded behavior when one source download fails.

- [ ] **Step 6: Commit Task 4**

Run:

```bash
git add app/admin_epg.py app/admin_service.py app/admin_store.py app/epg.py tests/test_admin_service.py tests/test_epg.py
git commit -m "feat: add mapping-aware epg regeneration"
```

## Task 5: HTTP API and Server-Rendered Admin UI

**Files:**
- Create: `app/admin_web.py`
- Create: `tests/test_admin_web.py`

- [ ] **Step 1: Write the failing API and UI tests**

Create `tests/test_admin_web.py` with:

```python
from __future__ import annotations

import json
from pathlib import Path

from app.admin_service import AdminService, AdminServiceSettings
from app.admin_store import AdminStore
from app.admin_web import AdminRequestHandler, build_test_server


def test_get_channels_api_returns_json(tmp_path: Path):
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
    service = AdminService(store, AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics"))
    app = build_test_server(store, service)

    status, headers, body = app("GET", "/api/channels", None)

    assert status == 200
    assert headers["Content-Type"] == "application/json"
    payload = json.loads(body)
    assert payload["channels"][0]["name"] == "Channel One"


def test_channels_ui_renders_validate_button(tmp_path: Path):
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    service = AdminService(store, AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics"))
    app = build_test_server(store, service)

    status, headers, body = app("GET", "/ui/channels", None)

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "Validate all channels" in body
    assert "EPG Sources" in body
```

- [ ] **Step 2: Run the web tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_admin_web.py
```

Expected: FAIL because `app.admin_web` does not exist yet.

- [ ] **Step 3: Implement the JSON API and HTML rendering helpers**

Create `app/admin_web.py` with:

```python
from __future__ import annotations

import json
from html import escape
from http import HTTPStatus


def build_test_server(store, service):
    def call(method: str, path: str, payload: dict[str, object] | None):
        if method == "GET" and path == "/api/channels":
            body = json.dumps(
                {
                    "channels": [
                        {
                            "id": channel.id,
                            "name": channel.name,
                            "group_name": channel.group_name,
                            "status": channel.status,
                            "draft_differs_from_live": channel.draft_differs_from_live,
                        }
                        for channel in store.list_channels()
                    ]
                }
            )
            return 200, {"Content-Type": "application/json"}, body
        if method == "GET" and path == "/ui/channels":
            body = render_channels_page(store.list_channels())
            return 200, {"Content-Type": "text/html; charset=utf-8"}, body
        return 404, {"Content-Type": "text/plain; charset=utf-8"}, "not found"

    return call


def render_channels_page(channels) -> str:
    rows = "\n".join(
        f"<tr><td>{channel.display_order}</td><td>{escape(channel.status)}</td><td>{escape(channel.name)}</td><td>{escape(channel.group_name)}</td></tr>"
        for channel in channels
    )
    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <title>Playlist Admin</title>
  </head>
  <body>
    <nav><a href="/ui/channels">Channels</a> <a href="/ui/epg-sources">EPG Sources</a> <a href="/ui/runs">Runs</a></nav>
    <form method="post" action="/api/channels/validate"><button type="submit">Validate all channels</button></form>
    <table>
      <thead><tr><th>Order</th><th>Status</th><th>Name</th><th>Group</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
  </body>
</html>"""


def render_epg_sources_page(sources) -> str:
    items = "".join(
        f"<li>{escape(source.display_name)} - {escape(source.source_url)}</li>"
        for source in sources
    )
    return f"<html><body><h1>EPG Sources</h1><ul>{items}</ul></body></html>"


def render_runs_page(runs) -> str:
    items = "".join(
        f"<li>{escape(str(run['trigger_type']))}: valid={run['valid_count']} invalid={run['invalid_count']}</li>"
        for run in runs
    )
    return f"<html><body><h1>Runs</h1><ul>{items}</ul></body></html>"


def render_channel_editor_page(channel, mappings) -> str:
    items = "".join(
        f"<li>source={mapping['epg_source_id']} channel_id={escape(str(mapping['channel_xmltv_id']))}</li>"
        for mapping in mappings
    )
    return (
        "<html><body>"
        f"<h1>{escape(channel.name)}</h1>"
        f"<p>{escape(channel.stream_url)}</p>"
        f"<ul>{items}</ul>"
        "</body></html>"
    )
```

- [ ] **Step 4: Add POST endpoints for validation and PATCH-like form submits**

Extend `app/admin_web.py` with real request handling in `AdminRequestHandler`:

```python
class AdminRequestHandler(BaseHTTPRequestHandler):
    store = None
    service = None

    def do_GET(self) -> None:
        if self.path == "/api/channels":
            return self._write_json(
                {
                    "channels": [
                        {
                            "id": channel.id,
                            "name": channel.name,
                            "group_name": channel.group_name,
                            "status": channel.status,
                            "draft_differs_from_live": channel.draft_differs_from_live,
                        }
                        for channel in self.store.list_channels()
                    ]
                }
            )
        if self.path == "/ui/channels":
            return self._write_html(render_channels_page(self.store.list_channels()))
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if self.path == "/api/channels/validate":
            result = self.service.validate_all(trigger_type="manual")
            return self._write_json(result, status=HTTPStatus.ACCEPTED)
        self.send_error(HTTPStatus.NOT_FOUND)

    def _write_json(self, payload: dict[str, object], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_html(self, body: str, status: int = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)
```

Also extend the handler with concrete route branches for `/ui/epg-sources`, `/ui/runs`, and `/ui/channels/<id>`:

```python
        if self.path == "/ui/epg-sources":
            return self._write_html(render_epg_sources_page(self.store.list_epg_sources()))
        if self.path == "/ui/runs":
            return self._write_html(render_runs_page(self.store.list_runs(limit=20)))
        if self.path.startswith("/ui/channels/"):
            channel_id = int(self.path.rsplit("/", 1)[1])
            channel = self.store.get_channel(channel_id)
            return self._write_html(render_channel_editor_page(channel, self.store.list_channel_epg_mappings(channel_id)))
```

Finish `app/admin_web.py` with a concrete server entrypoint:

```python
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def serve(*, bind_host: str, bind_port: int, store, service) -> None:
    AdminRequestHandler.store = store
    AdminRequestHandler.service = service
    httpd = ThreadingHTTPServer((bind_host, bind_port), AdminRequestHandler)
    httpd.serve_forever()
```

- [ ] **Step 5: Run the web tests to verify they pass**

Run:

```bash
python -m pytest -q tests/test_admin_web.py
```

Expected: PASS for basic JSON and HTML routes. Add follow-up coverage in the same file for `POST /api/channels/validate`, `POST /api/channels/<id>/validate`, and `GET /api/system/status` before committing.
Also add route coverage before committing for:

- `POST /api/channels`
- `PATCH /api/channels/<id>`
- `DELETE /api/channels/<id>`
- `GET /api/epg-sources`
- `POST /api/epg-sources`
- `PATCH /api/epg-sources/<id>`
- `DELETE /api/epg-sources/<id>`
- `GET /api/runs`

- [ ] **Step 6: Commit Task 5**

Run:

```bash
git add app/admin_web.py tests/test_admin_web.py
git commit -m "feat: add admin api and web ui"
```

## Task 6: Runtime Entrypoint, Daily Scheduler, Compose Wiring, and Docs

**Files:**
- Create: `app/admin_runtime.py`
- Create: `nginx/playlist-static.conf`
- Modify: `docker-compose.yml`
- Modify: `docker-compose.playlist.yml`
- Modify: `tests/test_compose_config.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write the failing compose and runtime tests**

Extend `tests/test_compose_config.py` with:

```python
def test_playlist_admin_runs_http_service_and_owns_private_state():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    static_compose = Path("docker-compose.playlist.yml").read_text(encoding="utf-8")
    nginx_conf = Path("nginx/playlist-static.conf").read_text(encoding="utf-8")

    assert "playlist-admin:" in compose
    assert 'command: ["python", "-m", "app.admin_runtime"]' in compose
    assert "./output:/data/state:rw" in compose
    assert "./published:/data/output:rw" in compose
    assert "./nginx/playlist-static.conf:/etc/nginx/conf.d/default.conf:ro" in static_compose
    assert "location /ui/" in nginx_conf
    assert "location /api/" in nginx_conf
    assert "proxy_pass http://playlist-admin:8780;" in nginx_conf
```

- [ ] **Step 2: Run the compose test to verify it fails**

Run:

```bash
python -m pytest -q tests/test_compose_config.py::test_playlist_admin_runs_http_service_and_owns_private_state
```

Expected: FAIL because the nginx config and new compose service do not exist yet.

- [ ] **Step 3: Implement the runtime entrypoint and scheduler**

Create `app/admin_runtime.py` with:

```python
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
from app.main import parse_full_check_time, seconds_until_next_full_check_time


@dataclass(frozen=True)
class RuntimeSettings:
    db_path: Path
    raw_playlist_path: Path
    fallback_playlist_path: Path
    output_dir: Path
    diagnostics_dir: Path
    run_time: tuple[int, int]
    bind_host: str
    bind_port: int

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        return cls(
            db_path=Path(os.getenv("ADMIN_DB_PATH", "/data/state/admin/playlist.db")),
            raw_playlist_path=Path(os.getenv("RAW_PLAYLIST_PATH", "/data/input/playlist.m3u")),
            fallback_playlist_path=Path(os.getenv("OUTPUT_DIR", "/data/output")) / "playlist_emby_clean.m3u8",
            output_dir=Path(os.getenv("OUTPUT_DIR", "/data/output")),
            diagnostics_dir=Path(os.getenv("DIAGNOSTICS_DIR", "/data/state/diagnostics")),
            run_time=parse_full_check_time(os.getenv("EPG_RUN_TIME", "04:00")),
            bind_host=os.getenv("ADMIN_BIND_HOST", "0.0.0.0"),
            bind_port=int(os.getenv("ADMIN_BIND_PORT", "8780")),
        )


def main() -> None:
    settings = RuntimeSettings.from_env()
    store = AdminStore(settings.db_path)
    store.initialize()
    bootstrap_from_playlist(store, settings.raw_playlist_path, settings.fallback_playlist_path)
    store.seed_default_epg_sources(
        [
            ("Default", os.getenv("EPG_SOURCE_URL", "http://epg.one/epg2.xml.gz")),
            ("Israel primary", os.getenv("EPG_ISRAEL_PRIMARY_URL", "https://iptvx.one/EPG")),
            ("Israel fallback", os.getenv("EPG_ISRAEL_FALLBACK_URL", "https://iptv-epg.org/files/epg-il.xml.gz")),
        ]
    )
    service = AdminService(store, AdminServiceSettings(settings.output_dir, settings.diagnostics_dir))
    threading.Thread(target=_scheduler_loop, args=(service, settings.run_time), daemon=True).start()
    serve(bind_host=settings.bind_host, bind_port=settings.bind_port, store=store, service=service)


def _scheduler_loop(service: AdminService, run_time: tuple[int, int]) -> None:
    while True:
        time.sleep(seconds_until_next_full_check_time(datetime.now().astimezone(), run_time))
        service.validate_all(trigger_type="scheduled")
```

- [ ] **Step 4: Wire nginx, compose, and docs**

Create `nginx/playlist-static.conf` with:

```nginx
server {
    listen 80;
    server_name _;

    location = /playlist_emby_clean.m3u8 {
        root /usr/share/nginx/html;
    }

    location = /epg.xml {
        root /usr/share/nginx/html;
        types { text/xml xml; }
    }

    location /ui/ {
        proxy_pass http://playlist-admin:8780;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }

    location /api/ {
        proxy_pass http://playlist-admin:8780;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

Modify `docker-compose.yml` to add:

```yaml
  playlist-admin:
    build:
      context: .
      dockerfile: Dockerfile.playlist-sanitizer
    container_name: playlist-admin
    working_dir: /app
    volumes:
      - ./original_playlist.m3u8:/data/input/playlist.m3u:ro
      - ./published:/data/output:rw
      - ./output:/data/state:rw
      - ./app:/app/app:ro
    environment:
      TZ: ${TZ:-Asia/Jerusalem}
      OUTPUT_DIR: ${OUTPUT_DIR:-/data/output}
      DIAGNOSTICS_DIR: ${DIAGNOSTICS_DIR:-/data/state/diagnostics}
      EPG_RUN_TIME: ${EPG_RUN_TIME:-04:00}
      ADMIN_DB_PATH: ${ADMIN_DB_PATH:-/data/state/admin/playlist.db}
      ADMIN_BIND_HOST: ${ADMIN_BIND_HOST:-0.0.0.0}
      ADMIN_BIND_PORT: ${ADMIN_BIND_PORT:-8780}
      EPG_SOURCE_URL: ${EPG_SOURCE_URL:-http://epg.one/epg2.xml.gz}
      EPG_ISRAEL_PRIMARY_URL: ${EPG_ISRAEL_PRIMARY_URL:-https://iptvx.one/EPG}
      EPG_ISRAEL_FALLBACK_URL: ${EPG_ISRAEL_FALLBACK_URL:-https://iptv-epg.org/files/epg-il.xml.gz}
      EMBY_BASE_URL: ${EMBY_BASE_URL:-}
      EMBY_API_KEY: ${EMBY_API_KEY:-}
      EMBY_LIVETV_TUNER_ID: ${EMBY_LIVETV_TUNER_ID:-}
    command: ["python", "-m", "app.admin_runtime"]
    restart: unless-stopped
```

Modify `docker-compose.playlist.yml` to mount:

```yaml
    volumes:
      - ./published:/usr/share/nginx/html:ro
      - ./nginx/playlist-static.conf:/etc/nginx/conf.d/default.conf:ro
```

Update `README.md` and `AGENTS.md` to describe:

- DB-backed source of truth
- first-run migration behavior
- `/ui`, `/api`, `/playlist_emby_clean.m3u8`, and `/epg.xml`
- `playlist-admin` replacing loop-based runtime services
- new verification flow

- [ ] **Step 5: Run the full verification set**

Run:

```bash
python -m compileall -q app tests
python -m pytest -q tests
docker compose up -d --build playlist-admin
docker compose -f docker-compose.playlist.yml up -d playlist-static
docker compose ps playlist-admin
curl -I http://127.0.0.1:8766/playlist_emby_clean.m3u8
curl -I http://127.0.0.1:8766/epg.xml
curl -I http://127.0.0.1:8766/ui/channels
```

Expected:

- compile step succeeds
- all pytest tests pass
- `playlist-admin` is `Up`
- playlist and EPG endpoints return `200`
- `/ui/channels` returns `200`

- [ ] **Step 6: Commit Task 6**

Run:

```bash
git add app/admin_runtime.py nginx/playlist-static.conf docker-compose.yml docker-compose.playlist.yml tests/test_compose_config.py README.md AGENTS.md
git commit -m "feat: add web-managed playlist admin runtime"
```

## Spec Coverage Check

- same-port public contract: covered in Task 6 nginx + compose wiring
- SQLite source of truth and first-run migration: covered in Task 1
- structured fields and generated M3U: covered in Task 2
- keep last validated-good live version during failed edits: covered in Task 3
- publish only validated channels: covered in Task 3
- ordered per-channel EPG mappings and global fallback sources: covered in Task 4
- manual and scheduled validation flows: covered in Tasks 3 and 6
- UI/API for channels, EPG sources, and runs: covered in Task 5
- guard-preserving artifact publish behavior: covered in Tasks 3 and 4
- docs and operational verification: covered in Task 6

## Placeholder Scan

- No `TODO` or `TBD` markers are allowed during implementation.
- If any helper name in the implementation differs from this plan, update all later tasks before continuing.
- Keep generated artifacts out of commits even if local verification writes them.

## Type Consistency Check

- `ChannelDraft`, `ChannelSnapshot`, and `EpgSource` are the shared cross-layer types.
- `AdminStore` remains the single persistence boundary.
- `AdminService` owns validation, publish, and job locking.
- `sync_epg()` is the only EPG orchestration entrypoint from the admin service.
- `app.epg` remains the pure XMLTV library and should not gain HTTP or DB logic.
