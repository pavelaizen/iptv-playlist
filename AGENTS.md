# AGENTS.md

Guidance for future Codex sessions in this repository.

## Project Purpose

This repo maintains an IPTV playlist publishing flow for Emby:

- source M3U playlists live at the repo root
- `app.main` probes channel URLs with `ffprobe`
- valid channels are written into a clean playlist candidate
- `app.publish` applies guardrails so a bad probe run does not wipe the usable playlist
- optional Emby Live TV refresh calls run only after a successful publish
- Docker Compose provides a long-running sanitizer container and a separate static file server

Do not paste real playlist URL lines, provider hostnames, subscription tokens, or API keys into chat, docs, commits, or logs unless the user explicitly asks. The checked-in M3U files may contain live subscription URLs.

## Repository Map

- `app/main.py` - scheduler and orchestration. Parses M3U, probes URLs, builds clean content, publishes guarded output, writes state, and schedules extra recovery checks.
- `app/probe.py` - async `ffprobe` worker utilities. A channel is valid when `ffprobe` exits 0 and returns at least one stream in JSON.
- `app/publish.py` - publish guard logic. Counts `#EXTINF` records, compares candidate vs previous clean playlist, writes diagnostics on guard failure, and preserves previous content when available.
- `app/emby_client.py` - optional non-fatal Emby API refresh client. Reads Emby env vars and posts Live TV refresh/reset endpoints after successful publish.
- `healthcheck.py` - container healthcheck. Fails when the sanitizer state file is missing, invalid, or older than twice `RUN_INTERVAL_HOURS` with a 1 hour minimum.
- `publish_emby_playlist.sh` - simple atomic publisher for the raw Emby playlist. Normalizes CRLF and removes empty lines before moving a temp file into place.
- `Dockerfile.playlist-sanitizer` - Python 3.12 slim image with `ffmpeg` installed for `ffprobe`.
- `docker-compose.yml` - long-running sanitizer service.
- `docker-compose.playlist.yml` - nginx static server for `./published` on port `8080`.
- `tests/` - pytest coverage for smoke orchestration and publish guard behavior.
- `.github/workflows/ci.yml` - compileall plus pytest on Python 3.12.

## Data Files

- `playlist.m3u` - root playlist, currently CRLF encoded and using `#EXTGRP` metadata.
- `playlist_smartone.m3u` - alternate playlist, currently CRLF encoded and using `group-title` metadata.
- `playlist_emby_raw.m3u` - raw Emby input playlist mounted by `docker-compose.yml`.
- `published/playlist_emby_clean.m3u` - Emby-facing clean playlist served by nginx.

All four tracked playlists currently contain 1011 `#EXTINF` channel records. Treat these as data/subscription material, not examples to quote verbatim.

Important inconsistency to preserve unless the user asks to change it: `README.md` says `published/playlist_emby_clean.m3u` is generated output and intentionally not committed, but the file is currently tracked in git.

## Runtime Flow

`python -m app.main` runs forever:

1. Parse `RAW_PLAYLIST_PATH` into `(metadata_lines, url)` entries.
2. Probe every URL with `probe_channels`.
3. Build candidate M3U content containing only valid URLs.
4. Publish through `select_playlist_for_publish`.
5. If the candidate passes the guard, call `refresh_livetv_after_publish`.
6. Write `STATE_FILE` with the current UTC ISO timestamp.
7. Run extra recovery checks at configured offsets. Recovery checks probe only previously offline URLs and republish only if some recover.
8. Sleep until the next base interval anchored to cycle start.

`run_once()` is a backwards-compatible test helper. It performs a single full check and returns whether a state object was produced.

## Publish Guard Semantics

`app.publish.select_playlist_for_publish` uses channel counts based on lines starting with `#EXTINF`.

Required minimum:

```text
max(MIN_VALID_CHANNELS_ABSOLUTE, int(previous_valid_channels * MIN_VALID_RATIO_OF_PREVIOUS))
```

If the candidate count meets the minimum, the candidate content is written to `candidate_output_path`.

If it fails:

- a diagnostic candidate file is written when `diagnostics_dir` is configured
- previous clean content is copied to the candidate output path when a previous file exists
- the decision returns `publish_candidate=False`
- Emby refresh must not run

## Environment Variables

Read by `app/main.py`:

- `LOG_LEVEL` default `INFO`
- `RAW_PLAYLIST_PATH` default `/data/input/playlist.m3u`
- `OUTPUT_DIR` default `/data/output`
- `OUTPUT_PLAYLIST_NAME` default `playlist_clean.m3u`
- `PREVIOUS_CLEAN_PLAYLIST_NAME` default same as `OUTPUT_PLAYLIST_NAME`
- `STATE_FILE` default `/data/output/.playlist_sanitizer_state`
- `RUN_INTERVAL_HOURS` default `24`, minimum scheduler sleep interval is 60 seconds
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

Read by `healthcheck.py`:

- `STATE_FILE`
- `RUN_INTERVAL_HOURS`

Read by `publish_emby_playlist.sh`:

- `SRC_FILE` default `playlist_emby_raw.m3u`
- `PUBLISH_DIR` default `published`
- `DEST_FILE_NAME` default `playlist_emby_clean.m3u`

Note: `docker-compose.yml` currently exposes `PROBE_READ_INTERVAL_US`, `PROBE_USER_AGENT`, `PROBE_FFMPEG_LOGLEVEL`, and `PROBE_EXTRA_ARGS`, but the Python code does not currently read them.

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
rg -c "^#EXTINF" playlist.m3u playlist_emby_raw.m3u playlist_smartone.m3u published/playlist_emby_clean.m3u
```

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
```

For static publishing changes, verify the final path remains stable:

```bash
./publish_emby_playlist.sh
docker compose -f docker-compose.playlist.yml up -d playlist-static
```
