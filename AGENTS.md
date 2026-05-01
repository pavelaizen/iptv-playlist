# AGENTS.md

Guidance for future Codex sessions in this repository.

## Project Purpose

This repo maintains an IPTV playlist publishing flow for Emby:

- source M3U playlists are local runtime inputs and may live at the repo root
- `app.main` probes channel URLs with `ffprobe`
- valid channels are written into a clean playlist candidate
- `app.publish` applies guardrails so a bad probe run does not wipe the usable playlist
- `app.epg_worker` trims the upstream XMLTV guide to the published clean playlist
- optional Emby Live TV refresh calls run only after a successful publish
- Docker Compose provides long-running sanitizer/EPG containers and a separate static file server

Do not paste real playlist URL lines, provider hostnames, subscription tokens, or API keys into chat, docs, commits, or logs unless the user explicitly asks. Runtime M3U files may contain live subscription URLs and should stay out of commits.

## Repository Map

- `app/main.py` - scheduler and orchestration. Parses M3U, probes URLs, builds clean content, publishes guarded output, writes state, and schedules extra recovery checks.
- `app/probe.py` - async `ffprobe` worker utilities. A channel is valid when `ffprobe` exits 0 and returns at least one stream in JSON.
- `app/publish.py` - publish guard logic. Counts `#EXTINF` records, compares candidate vs previous clean playlist, writes diagnostics on guard failure, and preserves previous content when available.
- `app/emby_client.py` - optional non-fatal Emby API refresh client. Reads Emby env vars and posts Live TV refresh/reset endpoints after successful publish.
- `app/epg.py` - XMLTV trimming library. Extracts clean-playlist channel names, matches upstream EPG display names, and writes a plain XMLTV containing only matched channels and programmes.
- `app/epg_worker.py` - daily EPG worker. Downloads upstream XMLTV, calls the trimmer, publishes `epg.xml` atomically, and refreshes Emby only after changed successful output.
- `healthcheck.py` - container healthcheck. Fails when the sanitizer state file is missing, invalid, or older than twice `RUN_INTERVAL_HOURS` with a 1 hour minimum.
- `publish_emby_playlist.sh` - simple atomic publisher for the raw Emby playlist. Normalizes CRLF and removes empty lines before moving a temp file into place.
- `Dockerfile.playlist-sanitizer` - Python 3.12 slim image with `ffmpeg` installed for `ffprobe`.
- `docker-compose.yml` - long-running sanitizer and EPG trimmer services. Writes served artifacts into `./published` and private state/diagnostics into `./output`.
- `docker-compose.playlist.yml` - nginx static server for `./published` on port `8766`.
- `tests/` - pytest coverage for smoke orchestration and publish guard behavior.
- `.github/workflows/ci.yml` - compileall plus pytest on Python 3.12.

## Data Files

- `original_playlist.m3u8` - local source-of-truth raw Emby input playlist mounted by `docker-compose.yml`; do not commit real subscription material.
- `published/playlist_emby_clean.m3u` - generated Emby-facing clean playlist served by nginx; do not commit it.
- `published/epg.xml` - generated trimmed XMLTV guide served by nginx; do not commit it.

Treat the playlist files as subscription material, not examples to quote verbatim. The source-of-truth raw playlist is `original_playlist.m3u8`.
Generated playlist and EPG files under `published/` should stay out of commits.

## Runtime Flow

`python -m app.main` runs forever:

1. Parse `RAW_PLAYLIST_PATH` into `(metadata_lines, url)` entries.
2. Probe every URL with `probe_channels`.
3. Build candidate M3U content containing only valid URLs.
4. Publish through `select_playlist_for_publish`.
5. If the candidate passes the guard and changes the clean playlist content, call `refresh_livetv_after_publish`; unchanged content skips Emby refresh.
6. After a successful guarded publish, write `STATE_FILE` with the current UTC ISO timestamp.
7. Run extra recovery checks at configured offsets. Recovery checks probe only previously offline URLs and republish only if some recover.
8. Sleep until the next configured full-check time.

`run_once()` is a backwards-compatible test helper. It performs a single full check and returns whether a state object was produced.

`python -m app.epg_worker` runs forever:

1. Download `EPG_SOURCE_URL` to private state storage.
2. Read `EPG_PLAYLIST_PATH`, normally `published/playlist_emby_clean.m3u`.
3. Match playlist channel names against XMLTV `<channel><display-name>`.
4. Write a candidate plain XMLTV containing only matched channels and programmes.
5. Reject zero-match or zero-programme candidates and preserve the previous EPG.
6. Atomically publish changed output to `EPG_OUTPUT_PATH`, normally `published/epg.xml`.
7. Refresh Emby only after changed successful output.
8. Sleep until the next configured local-container `EPG_RUN_TIME`.

## Publish Guard Semantics

`app.publish.select_playlist_for_publish` uses channel counts based on lines starting with `#EXTINF`.

Required minimum:

```text
max(MIN_VALID_CHANNELS_ABSOLUTE, int(previous_valid_channels * MIN_VALID_RATIO_OF_PREVIOUS))
```

If the candidate count meets the minimum, changed candidate content is atomically written to `candidate_output_path`; unchanged content skips the write.

If it fails:

- a diagnostic candidate file is written when `diagnostics_dir` is configured
- previous clean content is atomically copied to the candidate output path when a previous file exists
- the decision returns `publish_candidate=False`
- Emby refresh must not run
- `STATE_FILE` must not be updated

## Environment Variables

Read by `app/main.py`:

- `LOG_LEVEL` default `INFO`
- `RAW_PLAYLIST_PATH` default `/data/input/playlist.m3u`
- `OUTPUT_DIR` default `/data/output`
- `OUTPUT_PLAYLIST_NAME` default `playlist_clean.m3u`
- `PREVIOUS_CLEAN_PLAYLIST_NAME` default same as `OUTPUT_PLAYLIST_NAME`
- `STATE_FILE` default `/data/output/.playlist_sanitizer_state`
- `FULL_CHECK_TIME` default `03:00`, local container time for the daily full playlist scan
- `DIAGNOSTICS_DIR` default `OUTPUT_DIR / "diagnostics"`
- `MIN_VALID_CHANNELS_ABSOLUTE` default `1`
- `MIN_VALID_RATIO_OF_PREVIOUS` default `0.7`
- `EXTRA_RUN_DELAYS_MINUTES` default `30,60,240`

Read by `app/probe.py`:

- `PROBE_TIMEOUT_SECONDS` default `10.0`
- `PROBE_CONCURRENCY` default `20`
- `PROBE_RETRIES` default `1`
- `PROBE_RETRY_DELAY_SECONDS` default `1.0`

Read by `app/emby_client.py`:

- `EMBY_BASE_URL`
- `EMBY_API_KEY`
- `EMBY_LIVETV_TUNER_ID` optional

Read by `app/epg_worker.py`:

- `LOG_LEVEL` default `INFO`
- `EPG_SOURCE_URL` default `http://epg.one/epg2.xml.gz`
- `EPG_RUN_TIME` default `04:00`, local container time for daily EPG trimming
- `EPG_PLAYLIST_PATH` default `/data/output/playlist_emby_clean.m3u`
- `EPG_OUTPUT_PATH` default `/data/output/epg.xml`
- `EPG_STATE_FILE` default `/data/state/.epg_trimmer_state`
- `EPG_WORK_DIR` default `/data/state/epg`
- `EPG_MIN_MATCHED_CHANNELS` default `1`
- `EPG_MIN_PROGRAMMES` default `1`

Read by `healthcheck.py`:

- `STATE_FILE`
- `RUN_INTERVAL_HOURS`

Read by `publish_emby_playlist.sh`:

- `SRC_FILE` default `original_playlist.m3u8`
- `PUBLISH_DIR` default `published`
- `DEST_FILE_NAME` default `playlist_emby_clean.m3u`

Compose note: `docker-compose.yml` maps `./published` to `/data/output` so the sanitizer updates the same `playlist_emby_clean.m3u` file served by nginx. It maps `./output` to `/data/state` for healthcheck state and diagnostics, keeping diagnostics out of the public static directory.
The EPG trimmer uses the same `./published` and `./output` mounts, publishes `epg.xml` into the static directory, and keeps downloaded/candidate EPG state under `./output`.

## Common Commands

Run tests:

```bash
python -m pytest -q tests
```

Run syntax check matching CI:

```bash
python -m compileall -q app tests
```

Start sanitizer:

```bash
docker compose up -d --build playlist-sanitizer
```

Start EPG trimmer:

```bash
docker compose up -d --build epg-trimmer
```

Start static published-playlist server:

```bash
docker compose -f docker-compose.playlist.yml up -d playlist-static
```

Publish raw Emby playlist atomically:

```bash
./publish_emby_playlist.sh
```

Count channels without printing playlist URLs:

```bash
rg -c "^#EXTINF" playlist.m3u original_playlist.m3u8 playlist_smartone.m3u published/playlist_emby_clean.m3u
```

Do not run the channel count command if those local playlist files are absent; they are runtime artifacts, not required tracked files.

## Repo-local Skills

- `skills/updating-emby-playlist/SKILL.md` - use when the playlist is already published and the remaining work is to register or refresh the Emby Live TV source. This skill must not contain or reuse stored credentials; require current-session env vars or interactive auth.
- `skills/deploying-to-synology/SKILL.md` - use when syncing this repo to the Synology NAS and restarting the playlist services there. This skill must not hardcode SSH targets, passwords, API keys, or remote paths; require current-session inputs.

## Development Notes

- Runtime code uses only the Python standard library plus the external `ffprobe` binary.
- Tests require `pytest`; there is no checked-in `requirements.txt` or `pyproject.toml`.
- Keep code compatible with Python 3.12 because CI and Docker both use it.
- Prefer tests that monkeypatch network/probe/Emby calls. Do not hit real IPTV URLs or Emby APIs in unit tests.
- Keep Emby refresh non-fatal. Publishing a valid playlist should not be rolled back because Emby refresh failed.
- Preserve atomic publishing semantics when touching `publish_emby_playlist.sh` or any static-server output path.
- Do not assume a clean playlist is empty-safe. Guard failures should preserve the previous playlist when possible.
- If changing scheduler timing, verify the base interval remains anchored to cycle start and extra recovery runs do not drift the full-check cadence.
- If changing playlist parsing, preserve all metadata lines immediately preceding a URL and skip blank lines.
- If changing publish paths, update Docker Compose, healthcheck assumptions, and README together.

## Verification Before Completion

For code changes, run at least:

```bash
python -m compileall -q app tests
python -m pytest -q tests
```

For Docker/runtime changes, also build or run the relevant Compose service when practical:

```bash
docker compose up -d --build playlist-sanitizer
docker compose ps playlist-sanitizer
docker compose up -d --build epg-trimmer
docker compose ps epg-trimmer
```

For static publishing changes, verify the final path remains stable:

```bash
./publish_emby_playlist.sh
docker compose -f docker-compose.playlist.yml up -d playlist-static
```
