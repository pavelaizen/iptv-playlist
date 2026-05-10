# AGENTS.md

Guidance for future Codex sessions in this repository.

## Project Purpose

This repo maintains a DB-backed IPTV control plane for Emby:

- `playlist-admin` manages channels and EPG source configuration in SQLite.
- `playlist-nightly` runs the 04:00 one-shot EPG reload, validation, and publish flow.
- Channel validation uses `ffprobe` (`app/probe.py`).
- Playlist output is rendered from last validated channel snapshots and guarded by `app.publish`.
- EPG output is regenerated via `app.epg` and `app.admin_epg`.
- Emby refresh is best-effort and only runs after changed successful publish paths.
- `playlist-static` serves public artifacts all day and proxies admin UI/API only when `playlist-admin` is running.

Do not paste provider URLs, tokens, passwords, or API keys into commits/docs/chat unless the user explicitly asks.

## Repository Map

- `app/admin_bootstrap.py` - shared env parsing, DB bootstrap, default EPG seed, service construction.
- `app/admin_runtime.py` - optional admin HTTP server entrypoint.
- `app/admin_jobs.py` - one-shot operational jobs such as `nightly`.
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
- `docker-compose.yml` - shared admin image config, optional `playlist-admin`, one-shot `playlist-nightly`.
- `docker-compose.playlist.yml` - `playlist-static` nginx service on `:8766`.
- `nginx/playlist-static.conf` - static + reverse proxy routes (`/ui`, `/api`).
- `publish_emby_playlist.sh` - atomic raw playlist publisher.

## Runtime Flow

`python -m app.admin_runtime`:

1. Initialize SQLite schema.
2. One-time bootstrap import from `RAW_PLAYLIST_PATH`; fallback to published playlist if needed.
3. Seed default EPG source URLs when DB has none.
4. Serve admin UI/API HTTP server until stopped.

`python -m app.admin_jobs nightly`:

1. Initialize the same DB/service context as the admin UI.
2. Reload enabled EPG sources into the cache, preserving existing cache on source failure.
3. Run `AdminService.validate_all("scheduled")`.
4. If validation succeeds, publish playlist and EPG from the cached EPG data.
5. Exit with status `0` for `ok`; non-`ok` job results exit non-zero.

Validation (`AdminService.validate_all`) only updates validation state and live snapshots:

1. Acquire non-blocking job lock.
2. Probe enabled channel drafts.
3. Promote valid drafts into live snapshots; mark invalid drafts accordingly.
4. Persist run summary.

Publishing (`publish_from_cache` / `rebuild_all_public_outputs`) renders enabled live snapshots, applies the publish guard, regenerates `epg.xml`, and refreshes Emby only when the playlist changed successfully.

## Data Files

- Source input: `original_playlist.m3u8` (subscription material, do not commit real data).
- Public outputs: `published/playlist_emby_clean.m3u8`, `published/epg.xml` (generated).
- Private state: `output/` (SQLite DB, EPG work files, diagnostics).

## Environment Variables

Primary runtime (`app.admin_runtime`):

- `RAW_PLAYLIST_PATH` default `/data/input/playlist.m3u`
- `OUTPUT_DIR` default `/data/output`
- `DIAGNOSTICS_DIR` default `/data/state/diagnostics`
- `ADMIN_DB_PATH` default `/data/state/admin/playlist.db`
- `ADMIN_BIND_HOST` default `0.0.0.0`
- `ADMIN_BIND_PORT` default `8780`
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
- Compose services use `network_mode: host` (Synology Docker bridge firewall blocks inter-container traffic).
- `playlist-static` should stay up all day; `playlist-admin` is profile-gated and only needed for dashboard/API sessions.
- Schedule `playlist-nightly` externally at `04:00`; do not reintroduce an in-process scheduler thread.
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
docker compose --profile admin up -d --build playlist-admin
docker compose --profile jobs run --rm playlist-nightly
docker compose -f docker-compose.playlist.yml up -d playlist-static
docker compose --profile admin ps playlist-admin
curl -I "$NAS_PUBLIC_BASE_URL/playlist_emby_clean.m3u8"
curl -I "$NAS_PUBLIC_BASE_URL/epg.xml"
curl -I "$NAS_PUBLIC_BASE_URL/ui/channels"
./publish_emby_playlist.sh
```

## Development Notes

- Keep Python 3.12 compatibility.
- Keep runtime standard-library-first; avoid adding new dependencies unless needed.
- Preserve fail-safe behavior: bad runs must not wipe good published outputs.
- Keep Emby refresh non-fatal.
- Avoid network access in unit tests; monkeypatch downloads/probe/Emby calls.
- Keep `epg.pw` handling in `app/epg_sources.py`; do not scrape HTML search pages for runtime guide generation.
