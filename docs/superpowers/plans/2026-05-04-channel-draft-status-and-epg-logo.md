# Channel Draft Status And EPG Logo Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make metadata-only channel edits stop showing as unvalidated drafts, rename the visible `new` status to `draft`, and use XMLTV channel icons as fallback `tvg-logo` values.

**Architecture:** Keep the existing SQLite schema and validation model, but make `AdminStore.update_channel` distinguish validation-relevant edits from metadata-only edits. Extend the EPG sync pipeline to extract selected XMLTV channel icon URLs and feed them back into playlist rendering and editor display as effective fallback logos.

**Tech Stack:** Python 3.12, sqlite3, stdlib SAX XML parsing, pytest, Synology Docker deployment

---

### Task 1: Lock status-label behavior with tests

**Files:**
- Modify: `tests/test_admin_web.py`
- Modify: `tests/test_admin_service.py`

- [ ] **Step 1: Write a failing web test for the visible `draft` label**

```python
def test_channel_editor_page_renders_draft_label_for_new_status(tmp_path: Path) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = store.add_channel({"name": "Draft Channel", "stream_url": "http://example.com/live.m3u8"})
    service = AdminService(
        store,
        AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics"),
    )

    def app(method: str, path: str, payload):
        from app.admin_web import _dispatch_request
        return _dispatch_request(store, service, method=method, path=path, body=payload)

    status, headers, body = app("GET", f"/ui/channels/{channel_id}", None)

    assert status == 200
    assert headers["Content-Type"] == "text/html; charset=utf-8"
    assert "Status: <span" in body
    assert ">draft<" in body
    assert ">new<" not in body
```

- [ ] **Step 2: Run the targeted test and confirm it fails**

Run: `python -m pytest -q tests/test_admin_web.py::test_channel_editor_page_renders_draft_label_for_new_status`
Expected: FAIL because the page still renders `new`.

- [ ] **Step 3: Add store/service tests for metadata-only versus validation-relevant edits**

```python
def test_update_channel_logo_only_preserves_valid_state(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    service = AdminService(
        store=store,
        settings=AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics"),
    )
    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: True})
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 1, "programmes": 2, "failed_sources": [], "channel_icons": {}})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)
    assert service.validate_channel(channel_id)["status"] == "valid"

    store.update_channel(
        channel_id,
        {
            "name": "Channel One",
            "group_name": "News",
            "stream_url": "http://provider.invalid/one",
            "tvg_id": "chan-1",
            "tvg_name": "Channel One",
            "tvg_logo": "http://example.com/logo.png",
            "tvg_rec": "3",
            "enabled": True,
        },
    )
    channel = store.list_channels()[0]

    assert channel.status == "valid"
    assert channel.draft_differs_from_live is False
    assert channel.tvg_logo == "http://example.com/logo.png"


def test_update_channel_stream_url_marks_draft_different(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    service = AdminService(
        store=store,
        settings=AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics"),
    )
    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: True})
    monkeypatch.setattr(service, "_sync_epg", lambda: {"changed": False, "matched_channels": 1, "programmes": 2, "failed_sources": [], "channel_icons": {}})
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)
    assert service.validate_channel(channel_id)["status"] == "valid"

    store.update_channel(
        channel_id,
        {
            "name": "Channel One",
            "group_name": "News",
            "stream_url": "http://provider.invalid/two",
            "tvg_id": "chan-1",
            "tvg_name": "Channel One",
            "tvg_logo": "",
            "tvg_rec": "3",
            "enabled": True,
        },
    )
    channel = store.list_channels()[0]

    assert channel.status == "new"
    assert channel.draft_differs_from_live is True
```

- [ ] **Step 4: Run the targeted service tests and confirm at least the metadata-only case fails**

Run: `python -m pytest -q tests/test_admin_service.py::test_update_channel_logo_only_preserves_valid_state tests/test_admin_service.py::test_update_channel_stream_url_marks_draft_different`
Expected: metadata-only test fails because `update_channel` currently always marks the channel as `new`.

### Task 2: Lock EPG icon fallback behavior with tests

**Files:**
- Modify: `tests/test_admin_service.py`
- Modify: `tests/test_admin_web.py`
- Create or modify: `tests/test_admin_epg.py`

- [ ] **Step 1: Write a failing service test for playlist fallback icons**

```python
def test_validate_channel_uses_epg_icon_when_tvg_logo_missing(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    store.add_channel_epg_mapping(channel_id, epg_source_id=1, priority=0, channel_xmltv_id="chan-one")
    settings = AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics")
    service = AdminService(store=store, settings=settings)

    monkeypatch.setattr(service, "_probe_urls", lambda channels: {channel_id: True})
    monkeypatch.setattr(
        service,
        "_sync_epg",
        lambda: {
            "changed": True,
            "matched_channels": 1,
            "programmes": 2,
            "failed_sources": [],
            "channel_icons": {channel_id: "http://epg.example/icon.png"},
        },
    )
    monkeypatch.setattr(service, "_refresh_emby", lambda: None)

    assert service.validate_channel(channel_id)["status"] == "valid"
    playlist = (tmp_path / "published" / "playlist_emby_clean.m3u8").read_text(encoding="utf-8")

    assert 'tvg-logo="http://epg.example/icon.png"' in playlist
```

- [ ] **Step 2: Write a failing web test for editor fallback icon display**

```python
def test_channel_editor_page_shows_epg_icon_fallback_in_logo_field(tmp_path: Path, monkeypatch) -> None:
    store = AdminStore(tmp_path / "playlist.db")
    store.initialize()
    channel_id = seed_channel(store)
    service = AdminService(
        store,
        AdminServiceSettings(output_dir=tmp_path / "published", diagnostics_dir=tmp_path / "diagnostics"),
    )
    monkeypatch.setattr(service, "resolve_channel_editor_logo", lambda channel_id: "http://epg.example/icon.png")

    def app(method: str, path: str, payload):
        from app.admin_web import _dispatch_request
        return _dispatch_request(store, service, method=method, path=path, body=payload)

    status, _headers, body = app("GET", f"/ui/channels/{channel_id}", None)

    assert status == 200
    assert 'name="tvg_logo" value="http://epg.example/icon.png"' in body
```

- [ ] **Step 3: Write a failing EPG sync test for icon extraction**

```python
def test_sync_epg_returns_selected_channel_icons(tmp_path: Path, monkeypatch) -> None:
    work_dir = tmp_path / "epg"
    output_path = tmp_path / "published" / "epg.xml"
    source_path = work_dir / "source-1.xml.gz"
    work_dir.mkdir(parents=True)
    source_path.write_bytes(gzip.compress(b"<?xml version='1.0' encoding='UTF-8'?><tv><channel id='chan-one'><display-name>Channel One</display-name><icon src='http://epg.example/icon.png'/></channel><programme channel='chan-one' start='20260504000000 +0000' stop='20260504010000 +0000'><title>Show</title></programme></tv>"))

    monkeypatch.setattr("app.admin_epg.download_epg", lambda source_url, destination: destination.write_bytes(source_path.read_bytes()))

    result = sync_epg(
        published_channels=[{"channel_id": 1, "name": "Channel One", "mappings": [{"source_key": "source-1", "channel_id": "chan-one"}]}],
        epg_sources=[{"id": 1, "source_url": "http://epg.example/source.xml.gz", "enabled": True}],
        output_path=output_path,
        work_dir=work_dir,
    )

    assert result.channel_icons == {1: "http://epg.example/icon.png"}
```

- [ ] **Step 4: Run the targeted tests and confirm they fail**

Run: `python -m pytest -q tests/test_admin_service.py::test_validate_channel_uses_epg_icon_when_tvg_logo_missing tests/test_admin_web.py::test_channel_editor_page_shows_epg_icon_fallback_in_logo_field tests/test_admin_epg.py::test_sync_epg_returns_selected_channel_icons`
Expected: FAIL because the current code neither returns channel icons nor renders them.

### Task 3: Implement metadata-aware draft handling and visible `draft` label

**Files:**
- Modify: `app/admin_store.py`
- Modify: `app/admin_web.py`

- [ ] **Step 1: Change `_status_badge` to render `draft` for stored `new`**

```python
def _status_badge(status: str) -> str:
    visible = "draft" if status == "new" else status
    color = {
        "draft": "#6a737d",
        "valid": "#22863a",
        "invalid": "#cb2431",
    }.get(visible, "#6a737d")
    return f'<span style="display:inline-block;padding:2px 6px;border-radius:999px;background:{color};color:white;font-size:12px;">{escape(visible)}</span>'
```

- [ ] **Step 2: Update `AdminStore.update_channel` to only dirty validation state for `enabled` or `stream_url` changes**

```python
def update_channel(self, channel_id: int, payload: dict[str, object]) -> None:
    current = self.get_channel(channel_id)
    requires_revalidation = (
        current.stream_url != str(payload["stream_url"])
        or current.enabled != bool(payload["enabled"])
    )
    with self._connect() as conn:
        conn.execute(...)
        if requires_revalidation:
            conn.execute(
                """
                UPDATE channel_validation_states
                SET status = 'new', draft_differs_from_live = 1
                WHERE channel_id = ?
                """,
                (channel_id,),
            )
```

- [ ] **Step 3: Run the targeted tests and make sure they pass**

Run: `python -m pytest -q tests/test_admin_web.py::test_channel_editor_page_renders_draft_label_for_new_status tests/test_admin_service.py::test_update_channel_logo_only_preserves_valid_state tests/test_admin_service.py::test_update_channel_stream_url_marks_draft_different`
Expected: PASS.

### Task 4: Implement XMLTV icon extraction and playlist/editor fallback

**Files:**
- Modify: `app/admin_epg.py`
- Modify: `app/admin_service.py`
- Modify: `app/admin_m3u.py`
- Modify: `app/admin_web.py`
- Modify: `app/epg.py`

- [ ] **Step 1: Extend EPG sync result to include icon mappings per channel**

```python
@dataclass(frozen=True)
class EpgSyncResult:
    changed: bool
    matched_channels: int
    programmes: int
    failed_sources: list[str]
    channel_icons: dict[int, str]
```

- [ ] **Step 2: Add XMLTV icon extraction alongside source-selection logic**

```python
def extract_xmltv_channel_icons(*, published_channels: list[dict[str, object]], sources: dict[str, Path], default_source_order: list[str]) -> dict[int, str]:
    ...
    return {channel_id: icon_url}
```

- [ ] **Step 3: Use resolved icons in playlist rendering when snapshot `tvg_logo` is empty**

```python
candidate_content = render_playlist(
    [
        _with_effective_logo(snapshot, channel_icons.get(draft.id, ""))
        for draft, snapshot in validated_channels
    ]
)
```

- [ ] **Step 4: Surface the effective logo in the editor when explicit `tvg_logo` is blank**

```python
effective_logo = channel.tvg_logo or service.resolve_channel_editor_logo(channel.id)
```

- [ ] **Step 5: Run targeted icon tests and make sure they pass**

Run: `python -m pytest -q tests/test_admin_service.py::test_validate_channel_uses_epg_icon_when_tvg_logo_missing tests/test_admin_web.py::test_channel_editor_page_shows_epg_icon_fallback_in_logo_field tests/test_admin_epg.py::test_sync_epg_returns_selected_channel_icons`
Expected: PASS.

### Task 5: Run full verification

**Files:**
- Modify: `tests/test_admin_epg.py`
- Modify: `tests/test_admin_service.py`
- Modify: `tests/test_admin_web.py`

- [ ] **Step 1: Run the focused test modules**

Run: `python -m pytest -q tests/test_admin_epg.py tests/test_admin_service.py tests/test_admin_web.py`
Expected: PASS.

- [ ] **Step 2: Run the full test suite**

Run: `python -m pytest -q tests`
Expected: PASS.

- [ ] **Step 3: Run bytecode compilation**

Run: `python -m compileall -q app tests`
Expected: no output.

### Task 6: Deploy and verify end-to-end on Synology

**Files:**
- Sync workspace to: `/volume1/docker/iptv-playlist`

- [ ] **Step 1: Deploy the updated code to Synology**

Run: existing tar-over-SSH deployment flow used for this repo.
Expected: remote code updated in `/volume1/docker/iptv-playlist`.

- [ ] **Step 2: Rebuild/restart the Synology services**

Run: `docker compose up -d --build playlist-admin` and `docker compose -f docker-compose.playlist.yml up -d playlist-static` on the Synology host.
Expected: containers healthy.

- [ ] **Step 3: Verify metadata-only edit UX live**

Check in browser and API:
- edit only `TVG-Logo` on a valid channel
- save
- confirm the page no longer shows `new (draft differs from live)`
- confirm `/api/channels` returns the saved `tvg_logo`

- [ ] **Step 4: Verify XMLTV icon fallback live**

Check in browser and playlist output:
- pick a channel with mapped XMLTV icon and blank explicit logo
- open editor and confirm `TVG-Logo` shows the fallback icon URL
- validate or trigger publish if needed
- confirm `published/playlist_emby_clean.m3u8` contains `tvg-logo="..."`

- [ ] **Step 5: Verify validation-relevant edits still dirty the draft live state**

Check in browser:
- change `stream_url` on a test channel
- save
- confirm the page shows `Status: draft (draft differs from live)`
