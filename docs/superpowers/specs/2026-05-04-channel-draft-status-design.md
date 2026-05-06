# Channel Draft Status UX Design

## Goal

Reduce confusion in the channel editor and channel list when a user changes metadata such as `tvg_logo`, and reuse icon URLs already present in XMLTV channel metadata.

Today, any channel edit marks the channel as `new` and `draft_differs_from_live = true`, which makes a saved metadata change look like a brand new or unvalidated stream. Separately, XMLTV channel metadata may already contain `<icon src="..."/>` values, but there is currently no code path that uses those icons to populate `tvg_logo` or the effective playlist logo.

The design should make the UI clearer, avoid requiring validation for metadata-only edits, and use EPG-provided icons as the default logo source when a channel does not already have an explicit `tvg_logo`.

## Current Problem

- The UI label `new` is misleading. Users read it as "new channel" rather than "there is an unvalidated draft".
- Any save in `update_channel`, including logo-only edits, forces:
  - `status = 'new'`
  - `draft_differs_from_live = 1`
- This causes the editor to show `new (draft differs from live)` after harmless metadata changes.
- The saved draft is real, but the warning implies the stream itself needs revalidation even when only display metadata changed.
- XMLTV output can contain channel icons, but `app/admin_m3u.py` renders `tvg-logo` only from the stored channel snapshot.
- No code extracts icon URLs from mapped EPG sources and feeds them back into playlist rendering or the channel editor.

## Selected Approach

Use a minimal behavioral change with no storage migration:

1. Rename the visible UI label for `new` status to `draft`.
2. Treat metadata-only edits as non-validation-relevant changes.
3. Only mark a channel as needing validation when validation-relevant fields change.
4. When a channel has no explicit `tvg_logo`, use the best matched EPG `<icon src>` as its effective fallback logo.

This keeps the existing database model and validation flow intact while improving the user-facing semantics.

## Field Classification

### Validation-relevant fields

Changes to these fields require validation and should continue to mark the channel as a draft that differs from live:

- `enabled`
- `stream_url`

### Metadata-only fields

Changes to these fields should save immediately without marking the channel as draft-different:

- `name`
- `group_name`
- `tvg_id`
- `tvg_name`
- `tvg_logo`
- `tvg_rec`

## EPG Icon Fallback

### Desired behavior

- If a channel already has an explicit `tvg_logo`, keep using it.
- If `tvg_logo` is empty and the selected/matched XMLTV channel has an `<icon src="..."/>`, use that icon as the effective logo.
- The channel editor should display that effective logo URL in the `TVG-Logo` field when no explicit draft value exists, so the user can see what will be used.
- The rendered playlist should emit `tvg-logo="..."` from the explicit logo when present, otherwise from the resolved EPG icon.

### Resolution source

- Reuse the EPG source-selection logic that already determines which XMLTV channel ID is chosen for each published channel.
- Extract icon URLs from the chosen XMLTV `<channel>` elements during EPG processing.
- Return the resolved icon URL per published channel so the admin service can use it while rendering the playlist and while building editor/API views.

### Conflict behavior

- Prefer explicit `tvg_logo` over any EPG icon.
- If multiple icons are present for a selected XMLTV channel, use the first `src` encountered.
- If no icon exists, behavior remains unchanged.

## Desired Behavior

### When only metadata changes

- Persist the updated draft values in `channels`.
- Do not force `status = 'new'`.
- Do not set `draft_differs_from_live = 1`.
- The editor should not show the red `draft differs from live` warning.
- The visible status badge should remain whatever validation state already existed, except that if the raw stored status is `new`, the UI renders it as `draft`.
- If the stored `tvg_logo` is empty and an EPG icon is available, the editor should show the EPG icon URL in the `TVG-Logo` field.

### When `stream_url` or `enabled` changes

- Persist the updated values in `channels`.
- Mark the validation state as needing revalidation using the current mechanism.
- The visible status badge should show `draft`.
- The editor should show the red `draft differs from live` warning.

## UI Changes

### Channel list and channel editor

- Wherever `_status_badge` renders `new`, it should render `draft` instead.
- The explanatory text should align with the new wording.
- The channel editor and preview should use the effective logo value, which is:
  - explicit `tvg_logo` if present
  - otherwise resolved EPG icon if present
- Example:
  - Before: `Status: new (draft differs from live)`
  - After for validation-relevant edits: `Status: draft (draft differs from live)`
  - After for logo-only edits on a previously valid channel: `Status: valid`

## Store Changes

`AdminStore.update_channel` should compare the current persisted channel row to the submitted payload.

- If `enabled` or `stream_url` changed:
  - keep the current validation-state update behavior
- Otherwise:
  - update the channel row only
  - leave the validation-state row unchanged

No schema change is required.

## Service And EPG Changes

- `sync_epg` should return the resolved effective icon URL per published channel in addition to the current trim summary.
- `AdminService._publish_from_live_snapshots` should render playlist entries using that effective icon when the live snapshot logo is blank.
- The editor/API path should be able to surface the effective logo value for display, without requiring a manual save first.

## API Impact

- Existing JSON APIs may continue returning the stored `status` value.
- This change is primarily a UI/behavior fix and does not require an external API contract change.

If later needed, API semantics can be cleaned up separately.

## Testing

Add or update tests to cover:

1. `_status_badge("new")` renders `draft` in HTML output.
2. A metadata-only edit such as `tvg_logo` does not set `draft_differs_from_live`.
3. A metadata-only edit preserves the existing validation status.
4. A validation-relevant edit such as `stream_url` still marks the channel as draft-different.
5. The editor page no longer shows the red warning after a logo-only edit on a previously validated channel.
6. A mapped XMLTV channel icon is used as fallback `tvg-logo` in rendered playlist output when explicit `tvg_logo` is empty.
7. An explicit `tvg_logo` still wins over the XMLTV icon fallback.
8. The channel editor displays the fallback EPG icon in the `TVG-Logo` field when no explicit logo exists.

## Deployment And Verification

After implementation:

1. Run local tests.
2. Run `python -m compileall -q app tests`.
3. Deploy to Synology.
4. Verify end-to-end through the live UI:
   - edit only `tvg_logo` for a valid channel
   - save
   - confirm the editor no longer shows `new (draft differs from live)`
   - confirm the saved logo persists in `/api/channels`
   - clear `tvg_logo` for a channel that has an XMLTV icon available
   - validate or otherwise trigger publish as needed
   - confirm playlist output still contains `tvg-logo="..."` from the XMLTV icon fallback
   - confirm the editor shows the fallback icon in the `TVG-Logo` field
   - edit `stream_url` for a test case if feasible and confirm it does show `draft differs from live`

## Out Of Scope

- Auto-promoting metadata changes into the live snapshot
- Renaming stored DB statuses
- Background validation workflow changes
- EPG matching fixes
