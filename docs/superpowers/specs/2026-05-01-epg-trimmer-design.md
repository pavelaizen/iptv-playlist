# EPG Trimmer Worker Design

**Goal:** Add a separate daily worker that downloads the upstream XMLTV guide, keeps only programmes for channels in the published clean playlist, writes the trimmed guide under `published/`, and triggers Emby guide refresh after successful changes.

## Scope

This change covers:

- a new Docker Compose service for daily EPG trimming
- downloading `http://epg.one/epg2.xml.gz`
- parsing the local clean playlist from `published/playlist_emby_clean.m3u`
- exact normalized channel-name matching against upstream XMLTV channel display names
- atomic publish of the trimmed EPG into `published/` so the existing nginx static container serves it
- optional Emby guide refresh after the trimmed EPG changes
- unit tests for channel matching, XMLTV trimming, guard behavior, scheduling helpers, and Emby refresh integration

This change does not add fuzzy matching or a channel alias file. The current source playlist was adjusted so all remaining playlist channel names match the upstream EPG by exact normalized name.

## Current Source Findings

The upstream guide is XMLTV-shaped:

- root element: `tv`
- channel catalogue: `<channel id="...">` with one or more `<display-name>` children
- programme rows: `<programme start="..." stop="..." channel="...">`

The inspected upstream file had 3,128 channel entries and about 707,000 programme entries. After removing the unmatched `Кино UHD` channel from `original_playlist.m3u8`, the local source playlist has 15 channels, zero missing exact-name EPG matches, and maps to 14 unique upstream EPG channel ids.

## Architecture

Add a new worker entrypoint instead of folding EPG work into `playlist-sanitizer`.

The playlist sanitizer remains responsible for probing streams and publishing `playlist_emby_clean.m3u`. The EPG worker becomes a separate long-running process that depends on the clean playlist output and writes an EPG file into the same `published/` directory.

This keeps stream-probe failures, EPG download failures, and Emby guide-refresh failures isolated. It also lets the worker run at `04:00`, after the existing `03:00` playlist sanitizer cycle.

## Components

### `app/epg.py`

Owns pure EPG and playlist logic:

- extract candidate channel names from `#EXTINF` lines
- normalize names for matching with Unicode NFKC, case folding, punctuation cleanup, and whitespace normalization
- stream-parse upstream XMLTV with `xml.etree.ElementTree.iterparse`
- collect matching upstream channel ids from `<channel>` display names
- write a trimmed XMLTV document with matching `<channel>` elements and matching `<programme>` elements
- return a summary with source channel count, playlist channel count, matched id count, programme count, and unmatched playlist names

The module must not log stream URLs. It only works with channel display names and XMLTV channel ids.

### `app/epg_worker.py`

Owns runtime behavior:

- read environment variables
- run once immediately only when the output EPG is missing
- otherwise sleep until the next configured daily run time
- download the upstream gzip file to a private temp path under the state directory
- call `app.epg` to produce a candidate trimmed guide
- reject publish when no playlist channels match or the output would contain zero programmes
- atomically replace the served output only when candidate content differs from the existing file
- call `refresh_livetv_after_publish` only after a successful changed publish
- keep Emby refresh non-fatal
- write a state timestamp after successful publish or successful unchanged validation

Failures preserve the previously served EPG.

### Docker Compose

Add service `epg-trimmer` to `docker-compose.yml` using the existing Python image build.

The service mounts:

- `./published:/data/output:rw`
- `./output:/data/state:rw`
- `./app:/app/app:ro`

The existing `playlist-static` service continues to serve `./published`. Emby can use:

```text
http://<host>:8766/epg.xml.gz
```

## Environment Variables

Read by `app/epg_worker.py`:

- `LOG_LEVEL` default `INFO`
- `EPG_SOURCE_URL` default `http://epg.one/epg2.xml.gz`
- `EPG_RUN_TIME` default `04:00`
- `EPG_PLAYLIST_PATH` default `/data/output/playlist_emby_clean.m3u`
- `EPG_OUTPUT_PATH` default `/data/output/epg.xml.gz`
- `EPG_STATE_FILE` default `/data/state/.epg_trimmer_state`
- `EPG_WORK_DIR` default `/data/state/epg`
- `EPG_MIN_MATCHED_CHANNELS` default `1`
- `EPG_MIN_PROGRAMMES` default `1`

The worker reuses existing Emby variables from `app.emby_client`:

- `EMBY_BASE_URL`
- `EMBY_API_KEY`
- `EMBY_LIVETV_TUNER_ID` optional

## Data Flow

1. Sleep until `EPG_RUN_TIME`, unless the output EPG is missing.
2. Download `EPG_SOURCE_URL` to `EPG_WORK_DIR`.
3. Read `EPG_PLAYLIST_PATH`.
4. Extract normalized playlist channel names from `#EXTINF`.
5. Stream through XMLTV once to find matching channel ids and keep matching `<channel>` elements.
6. Stream through XMLTV again to keep matching `<programme>` elements.
7. Write a candidate gzip XMLTV file under `EPG_WORK_DIR`.
8. If candidate differs from `EPG_OUTPUT_PATH`, atomically replace the served file.
9. If the served file changed, trigger Emby guide refresh.
10. Write `EPG_STATE_FILE`.
11. Sleep until the next configured run time.

Two XML passes are acceptable because the upstream file is moderate in size and streaming keeps memory usage bounded.

## Emby Refresh

Use the existing `app.emby_client.refresh_livetv_after_publish` function.

That function discovers the current Emby scheduled task with `GET /ScheduledTasks`, starts it with `POST /ScheduledTasks/Running/{Id}`, and falls back to `/LiveTv/RefreshGuide` when needed. The EPG worker should call it only after a changed trimmed guide is published.

## Error Handling

- Missing clean playlist: log an error, preserve the previous EPG, do not refresh Emby, and retry on the next schedule.
- Download failure: log an error, preserve the previous EPG, do not refresh Emby, and retry on the next schedule.
- Invalid gzip or XML: log an error, preserve the previous EPG, do not refresh Emby, and retry on the next schedule.
- Zero matched channels: write diagnostics to logs, preserve the previous EPG, do not refresh Emby, and do not update state.
- Zero programmes: write diagnostics to logs, preserve the previous EPG, do not refresh Emby, and do not update state.
- Emby refresh failure: keep the published EPG and log the existing non-fatal warning.

## Testing

Add tests with small synthetic XMLTV files and playlists:

- playlist parser extracts channel names without printing URLs
- exact normalized name matching handles case, punctuation, and extra whitespace
- unmatched channels are reported by name
- trimming preserves root XMLTV metadata, matched `<channel>` elements, and matched `<programme>` elements
- trimming excludes programmes for unmatched channel ids
- zero-match guard preserves previous output
- unchanged output skips Emby refresh
- changed output calls Emby refresh
- scheduler helper rolls past times to the next day
- Compose service writes `epg.xml.gz` under `published/` and uses the existing static server path

For implementation verification, run:

```bash
python -m compileall -q app tests
python -m pytest -q tests
```

For Docker/runtime verification when practical, run:

```bash
docker compose up -d --build epg-trimmer
docker compose ps epg-trimmer
```

## Risks and Mitigations

- Upstream display names drift:
  mitigate by reporting unmatched playlist names in logs and failing safe only if matches fall below the configured guard.
- The clean playlist is temporarily missing during sanitizer startup:
  mitigate by preserving the previous EPG and retrying on the next daily cycle or container restart.
- Large XML memory use:
  mitigate with streaming `iterparse` and gzip temp files instead of loading the full guide into memory.
- Emby refresh starts before the static file is atomically visible:
  mitigate by calling Emby only after the atomic replace completes.
- Generated EPG output accidentally committed:
  mitigate by documenting `published/epg.xml.gz` as generated output and keeping it out of commits.
