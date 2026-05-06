# Web-Managed Playlist And EPG Control Plane Design

**Goal:** Replace the file-driven playlist definition flow with a web-managed control plane that owns channel definitions, EPG mappings, validation, scheduling, and publishing, while keeping Emby-facing URLs unchanged on port `8766`.

## Scope

This change covers:

- a new web UI and JSON API served behind the existing `playlist-static` container on port `8766`
- a SQLite-backed channel registry that becomes the source of truth for the published playlist
- first-run migration from the current playlist so the UI is populated on day one
- structured channel editing instead of raw stored `#EXTINF` lines
- per-channel ordered EPG mappings with fallback to an ordered global EPG source list
- manual `Validate all channels` and per-channel validation actions
- a scheduled daily validation run at `04:00`
- automatic playlist publish and `epg.xml` regeneration after successful validation runs
- preservation of current guard behavior for playlist and EPG publishing
- unit and integration tests for migration, validation state, publishing, and EPG fallback behavior

This change does not add internet-facing authentication, multi-user roles, or a bidirectional sync back into `original_playlist.m3u8`. The existing raw playlist file is migration input only.

## Approved Product Decisions

The design below reflects these approved choices:

- keep the existing public port and artifact URLs unchanged
- make the database-backed web UI the source of truth for the published playlist
- seed the database on first run so the UI is not empty
- use SQLite as the persistent store
- support an ordered list of explicit EPG mappings per channel
- run without app auth on the LAN
- publish only validated channels
- treat `original_playlist.m3u8` as one-time import input only
- manage the global default EPG source list in the UI
- keep serving the last validated-good version of a channel until edits validate
- auto-publish and regenerate `epg.xml` when validation completes successfully
- edit channels through structured fields and generate `#EXTINF` from those fields
- keep one editable channel row plus one last validated snapshot
- preserve duplicates during migration
- support manual channel ordering in the UI

## Current Source Findings

Today the repo has three runtime roles:

- `playlist-static` serves `./published` on `:8766`
- `playlist-sanitizer` owns probing and guarded playlist publishing
- `epg-trimmer` owns daily XMLTV trimming and EPG publishing

Emby already depends on these public artifact paths:

- `/playlist_emby_clean.m3u8`
- `/epg.xml`

Those paths are stable and should not move. The safest design is to preserve the static serving contract and put the new management UI and API beside it at:

- `/ui`
- `/api/...`

The current probe, publish guard, XMLTV trimming, and Emby refresh logic are worth reusing as library code. The forever-loop container entrypoints are the part that should be replaced by a control-plane-oriented runtime.

## Architecture

Keep `playlist-static` as the public edge on port `8766`, but extend it to reverse-proxy admin traffic to a new internal `playlist-admin` service.

Public routes remain:

- `/playlist_emby_clean.m3u8`
- `/epg.xml`

New admin routes:

- `/ui`
- `/api/...`

Add one new `playlist-admin` service that owns:

- the SQLite database
- first-run migration
- channel CRUD
- EPG source CRUD
- validation jobs
- the daily `04:00` schedule
- playlist rendering and guarded publish
- XMLTV regeneration
- job status and history

The admin app reuses proven logic from:

- `app/probe.py`
- `app/publish.py`
- `app/epg.py`
- `app/emby_client.py`

The current `app.main` and `app.epg_worker` long-running loops should be retired in favor of callable job functions invoked by the admin app from HTTP actions and the scheduler.

This architecture keeps the Emby-facing surface stable while moving control and state into a proper application-owned system.

## Storage Layout

Public data remains in:

- `./published/playlist_emby_clean.m3u8`
- `./published/epg.xml`

Private admin data moves under a non-public state path, for example:

- `./output/admin/playlist.db`
- `./output/admin/...` for migration markers, job metadata, and temporary EPG downloads

No admin state should be written into `./published` except the final public artifacts.

## Data Model

Use a normalized SQLite schema.

### `channels`

Stores the current editable draft:

- `id`
- `created_at`, `updated_at`
- `display_order`
- `enabled`
- `name`
- `group_name`
- `stream_url`
- `tvg_id`
- `tvg_name`
- `tvg_logo`
- `tvg_rec`
- `source_kind` such as `migrated` or `manual`
- `draft_version`

### `channel_live_snapshots`

Stores the last validated-good version currently eligible for publishing:

- `channel_id`
- all structured channel fields needed to render the live playlist entry
- `validated_at`
- `validated_version`

### `channel_validation_states`

Stores the latest validation result for the current draft:

- `channel_id`
- `status` with values `new`, `valid`, `invalid`
- `last_checked_at`
- `last_error`
- `checked_version`
- `draft_differs_from_live`

### `channel_epg_mappings`

Stores ordered explicit mappings per channel:

- `id`
- `channel_id`
- `priority`
- `epg_source_id`
- `channel_xmltv_id`
- `enabled`

### `epg_sources`

Stores the ordered default EPG source list:

- `id`
- `display_name`
- `source_url`
- `enabled`
- `priority`
- `last_fetch_status`
- `last_fetch_error`
- `last_success_at`

### `validation_runs`

Stores job history:

- `id`
- `trigger_type` such as `manual` or `scheduled`
- `started_at`, `finished_at`
- `status`
- channel counts by result
- publish summary
- EPG summary
- error summary

## Migration

Migration is first-run only and marker-based.

Rules:

- if the database already contains channels, do not import again
- prefer importing from `original_playlist.m3u8`
- if the raw playlist is unavailable, fall back to `published/playlist_emby_clean.m3u8`
- preserve entry order exactly as imported
- preserve duplicates as separate channel rows
- initialize both the editable draft and the live snapshot from imported channel data
- mark imported channels as `new` until the first real validation run

After migration, the DB becomes authoritative. The old playlist file may remain on disk for operator reference, but the runtime ignores it.

## Channel Rendering

Channel editing uses structured fields, not raw stored `#EXTINF` text.

The application renders `#EXTINF` deterministically from:

- `tvg_id`
- `tvg_name`
- `tvg_logo`
- `tvg_rec`
- visible channel name

`#EXTGRP` is rendered from `group_name`.

This removes line-format ambiguity from the source-of-truth model while still producing a standard M3U output.

## Data Flow

### 1. First Boot

On startup, `playlist-admin` checks whether the DB has channel rows.

- empty DB: run migration, populate the UI, and wait for validation
- non-empty DB: skip migration and start normally

The first-run migration does not automatically trust imported channels. They are visible immediately in the UI, but publishing continues from the last known public artifact until validation runs.

### 2. Edit Channel

When a user edits a channel:

- update the draft row in `channels`
- increment `draft_version`
- mark the validation state as `new`
- set `draft_differs_from_live = true`
- keep the old live snapshot untouched

The published playlist continues using the live snapshot until validation of the edited draft succeeds.

### 3. Validate One Channel

When a user validates a single channel:

- probe the draft stream URL with existing probe logic
- on success, update the live snapshot from the draft, mark status `valid`, and clear the draft/live diff
- on failure, keep the live snapshot unchanged and mark status `invalid`

Single-channel validation uses the same publish pipeline as `Validate all channels` whenever the live snapshot changes. That means a successful single-channel validation rebuilds the public playlist, regenerates `epg.xml`, and triggers Emby refresh only if the public artifacts changed.

### 4. Validate All Channels

Manual `Validate all channels` and the scheduled `04:00` run use the same job:

1. load enabled channel drafts
2. probe their stream URLs
3. update validation status per channel
4. refresh live snapshots only for successful drafts
5. rebuild the candidate playlist from enabled channels with valid live snapshots
6. apply the existing publish guard before replacing the public playlist
7. regenerate `epg.xml`
8. refresh Emby only after changed successful publish
9. record the run summary

## EPG Resolution

EPG resolution is explicit-first.

For each published channel:

1. try the channel's enabled `channel_epg_mappings` in priority order
2. if no mapping yields usable programmes, fall back to the enabled global `epg_sources` in priority order
3. when evaluating a global source without an explicit usable mapping, allow normalized name-based matching against that source as the final fallback

This preserves today’s flexibility for generic channels while giving operators full control for channels that need exact XMLTV IDs.

Global EPG sources remain auto-updated by downloading the remote XML or XML.GZ at validation time using the latest content at each configured URL.

## UI

The UI should be operational and status-forward.

### Channels Screen

Shows:

- status
- order
- enabled state
- name
- group
- stream URL
- last checked time
- last published version update time
- whether the draft differs from live
- quick actions

Actions:

- add channel
- edit channel
- validate channel
- delete or disable channel
- reorder channels
- validate all channels

### Channel Editor

Structured fields:

- name
- group
- stream URL
- `tvg_id`
- `tvg_name`
- `tvg_logo`
- `tvg_rec`

Also shows:

- generated preview of the M3U entry
- ordered EPG mapping rows
- current validation result
- live-vs-draft difference indicators

### EPG Sources Screen

Shows:

- ordered default source list
- enabled state
- source URL
- last fetch status
- last success time

Actions:

- add source
- edit source
- enable or disable source
- reorder sources

### Runs Screen

Shows recent manual and scheduled runs with:

- start and finish time
- trigger type
- valid and invalid counts
- playlist publish result
- EPG result
- operator-visible error messages

## API

The admin API should stay narrow and job-oriented.

- `GET /api/channels`
- `POST /api/channels`
- `PATCH /api/channels/:id`
- `DELETE /api/channels/:id`
- `POST /api/channels/reorder`
- `POST /api/channels/validate`
- `POST /api/channels/:id/validate`
- `GET /api/epg-sources`
- `POST /api/epg-sources`
- `PATCH /api/epg-sources/:id`
- `DELETE /api/epg-sources/:id`
- `POST /api/epg-sources/reorder`
- `GET /api/runs`
- `GET /api/system/status`

The UI can be server-rendered, progressively enhanced HTML, or a small frontend bundle. The important contract is the route shape and the status model, not a heavy frontend architecture.

## Operational Rules

- publish only enabled channels whose live snapshot is valid
- one validation or publish job at a time
- a second manual validation request while one is running returns an `already running` response
- deleting a channel removes both its draft and live snapshot, so the next successful publish drops it
- if a rebuilt playlist fails the publish guard, preserve the previous published playlist
- if a regenerated EPG yields zero usable channels or zero programmes, preserve the previous `epg.xml`
- if one EPG source download fails, continue with remaining configured sources
- Emby refresh remains non-fatal

## Testing

Add tests for:

- migration from M3U into SQLite rows
- duplicate preservation during migration
- structured field rendering into M3U metadata lines
- draft/live snapshot separation
- validate-success path updating the live snapshot
- validate-failure path preserving the live snapshot
- playlist rebuild from valid snapshots only
- ordered per-channel EPG mapping resolution
- fallback from per-channel mappings to global EPG sources
- partial EPG source download failure
- job locking
- API CRUD for channels and EPG sources
- reorder endpoints
- validate-all to publish to EPG end-to-end flow

No unit or integration tests should hit live IPTV sources or a live Emby server.

## Risks And Mitigations

- Introducing a DB-backed control plane changes the operational model:
  mitigate with one-time migration, stable public artifact paths, and reuse of existing publish guards.
- Users may edit drafts into a broken state:
  mitigate by continuing to publish the last validated snapshot.
- EPG sources can be flaky:
  mitigate by continuing with remaining sources instead of aborting the run.
- Migration may accidentally duplicate or miss rows:
  mitigate with deterministic import order, duplicate-preserving tests, and an explicit one-shot marker.
- UI complexity could sprawl:
  mitigate by keeping the first version focused on channels, EPG sources, runs, and validation only.

## Verification Before Implementation

Before starting implementation, the plan should preserve these repo contracts:

- `playlist-static` remains the public entry on `:8766`
- `/playlist_emby_clean.m3u8` and `/epg.xml` remain stable
- guard behavior for bad playlist or EPG runs remains fail-safe
- Emby refresh remains best-effort
- generated public artifacts stay out of commits
