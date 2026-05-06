# AGENTS.md

Guidance for future Codex sessions in this repository.

## Project Purpose

This repo maintains a DB-backed IPTV control plane for Emby:

- `playlist-admin` manages channels and EPG source configuration in SQLite.
- Channel validation uses `ffprobe` (`app/probe.py`).
- Playlist output is rendered from last validated channel snapshots and guarded by `app.publish`.
- EPG output is regenerated via `app.epg` and `app.admin_epg`.
- Emby refresh is best-effort and only runs after changed successful publish paths.
- `playlist-static` serves public artifacts and proxies admin UI/API on the same port.

Do not paste provider URLs, tokens, passwords, or API keys into commits/docs/chat unless the user explicitly asks.

## Repository Map

- `app/admin_runtime.py` - main runtime entrypoint, boot migration, scheduler, HTTP server startup.
- `app/admin_store.py` - SQLite schema, migration import, channel/EPG/run persistence APIs.
- `app/admin_service.py` - validation orchestration, guard publish, EPG sync integration, job lock.
- `app/admin_web.py` - admin HTTP routes (`/api/*`, `/ui/*`) and HTML rendering.
- `app/admin_epg.py` - EPG source download orchestration and mapping-aware trim invocation.
- `app/epg_sources.py` - EPG source URL canonicalization, dynamic `epg.pw` expansion, shared XML/XML.GZ downloader.
- `app/admin_m3u.py` - M3U import parsing and deterministic channel rendering.
- `app/admin_models.py` - shared dataclasses and typed literals.
- `app/epg.py` - XMLTV trimming primitives, Israeli overrides, generic source-strategy trim.
- `app/probe.py` - async ffprobe worker utilities.
- `app/stream_stability.py` - longer `ffmpeg` decode checks for stream stability.
- `app/publish.py` - publish guard behavior for playlist content.
- `app/emby_client.py` - optional Emby refresh client.
- `docker-compose.yml` - `playlist-admin` service.
- `docker-compose.playlist.yml` - `playlist-static` nginx service on `:8766`.
- `nginx/playlist-static.conf` - static + reverse proxy routes (`/ui`, `/api`).
- `publish_emby_playlist.sh` - atomic raw playlist publisher.

## Runtime Flow

`python -m app.admin_runtime`:

1. Initialize SQLite schema.
2. One-time bootstrap import from `RAW_PLAYLIST_PATH`; fallback to published playlist if needed.
3. Seed default EPG source URLs when DB has none.
4. Start scheduler thread for daily `EPG_RUN_TIME` validation.
5. Serve admin UI/API HTTP server.

Validation (`AdminService.validate_all`):

1. Acquire non-blocking job lock.
2. Probe enabled channel drafts.
3. Promote valid drafts into live snapshots; mark invalid drafts accordingly.
4. Render candidate playlist from enabled live snapshots.
5. Apply publish guard.
6. Regenerate `epg.xml` with explicit mapping-first and fallback source strategy.
7. Refresh Emby only when publish path changed and succeeded.
8. Persist run summary.

## Data Files

- Source input: `original_playlist.m3u8` (subscription material, do not commit real data).
- Public outputs: `published/playlist_emby_clean.m3u8`, `published/epg.xml` (generated).
- Private state: `output/` (SQLite DB, scheduler/EPG work files, diagnostics).

## Environment Variables

Primary runtime (`app.admin_runtime`):

- `RAW_PLAYLIST_PATH` default `/data/input/playlist.m3u`
- `OUTPUT_DIR` default `/data/output`
- `DIAGNOSTICS_DIR` default `/data/state/diagnostics`
- `ADMIN_DB_PATH` default `/data/state/admin/playlist.db`
- `ADMIN_BIND_HOST` default `0.0.0.0`
- `ADMIN_BIND_PORT` default `8780`
- `EPG_RUN_TIME` default `04:00`
- `EPG_WORK_DIR` default `/data/state/epg`
- `EPGPW_TIMEZONE` default `Asia/Jerusalem`

Probe/runtime:

- `LOG_LEVEL` default `INFO`
- `PROBE_TIMEOUT_SECONDS` default `15`
- `PROBE_CONCURRENCY` default `4`
- `PROBE_RETRIES` default `1`
- `PROBE_RETRY_DELAY_SECONDS` default `1`
- `STABILITY_TEST_SECONDS` default `60`
- `STABILITY_TEST_TIMEOUT_PADDING_SECONDS` default `40`

Extended stream stability checks:

- The normal Validate action stays a fast `ffprobe` availability check.
- Extended tests use `ffmpeg -f null -` to decode video/audio for
  `STABILITY_TEST_SECONDS`.
- Extended results are stored on stream variants separately from channel
  validity and do not block publishing by themselves.

Default EPG source seeds:

- `EPG_SOURCE_URL` default `http://epg.one/epg2.xml.gz`
- `EPG_ISRAEL_PRIMARY_URL` default `https://iptvx.one/EPG`
- `EPG_ISRAEL_FALLBACK_URL` default `https://iptv-epg.org/files/epg-il.xml.gz`

`epg.pw` per-channel URLs are accepted as `/last/<id>.html` or `/api/epg.xml`
links. They are stored without a stale `date=` parameter; downloads add the
current date and Base64-encoded `EPGPW_TIMEZONE`.

Emby:

- `EMBY_BASE_URL`
- `EMBY_API_KEY`
- `EMBY_LIVETV_TUNER_ID` optional

## Deployment

See `docs/deploy-to-synology.md` for full Synology deployment guide including networking, DNS, and troubleshooting.

Key points:
- Both containers use `network_mode: host` (Synology Docker bridge firewall blocks inter-container traffic).
- Nginx on `:8766` proxies `/ui/` and `/api/` to `127.0.0.1:8780`.
- `extra_hosts` and `dns` directives are incompatible with `network_mode: host`. Use Synology `/etc/hosts` instead.
- `scp` subsystem is often broken on Synology SSH. Use `tar czf - | ssh ... 'tar xzf -'` or `base64` pipe.
- Docker image rebuild often fails due to Synology DNS. Restart containers for code-only changes (Python code is bind-mounted).
- Use `--force-recreate` (not `restart`) when `docker-compose.yml` volumes/env change.
- Docker binary on Synology is at `/usr/local/bin/docker`. `sudo` requires `-S` flag for password from stdin.

## Common Commands

```bash
python -m pytest -q tests
python -m compileall -q app tests
docker compose up -d --build playlist-admin
docker compose -f docker-compose.playlist.yml up -d playlist-static
docker compose ps playlist-admin
curl -I http://192.168.1.113:8766/playlist_emby_clean.m3u8
curl -I http://192.168.1.113:8766/epg.xml
curl -I http://192.168.1.113:8766/ui/channels
./publish_emby_playlist.sh
```

## Development Notes

- Keep Python 3.12 compatibility.
- Keep runtime standard-library-first; avoid adding new dependencies unless needed.
- Preserve fail-safe behavior: bad runs must not wipe good published outputs.
- Keep Emby refresh non-fatal.
- Avoid network access in unit tests; monkeypatch downloads/probe/Emby calls.
- Keep `epg.pw` handling in `app/epg_sources.py`; do not scrape HTML search pages for runtime guide generation.
