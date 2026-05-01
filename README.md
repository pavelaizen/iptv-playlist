# IPTV playlist publishing

## Atomic publisher for Emby

Use the publisher script so the Emby-facing playlist is always written atomically:

```bash
./publish_emby_playlist.sh
```

By default, this writes to a temporary file first and then atomically renames it:

- temp: `published/playlist_emby_clean.m3u.tmp`
- final: `published/playlist_emby_clean.m3u`

Emby should reference only the stable final path/URL (`playlist_emby_clean.m3u`), never the `.tmp` file.

> Note: `published/playlist_emby_clean.m3u` is generated output and is intentionally not committed.
> The trimmed guide `published/epg.xml` is also generated output and should not be committed.

## Stable HTTP URL via lightweight static server

Serve only the published directory through a tiny static container while keeping URL path stable:

```bash
docker compose -f docker-compose.playlist.yml up -d playlist-static
```

Then point Emby to:

- `http://<host>:8766/playlist_emby_clean.m3u`
- `http://<host>:8766/epg.xml`

Keep this URL unchanged; updates happen in-place via atomic rename.

## Sanitizer runtime

For automated probing and guarded publishing, run:

```bash
docker compose up -d --build playlist-sanitizer
```

The sanitizer now writes the guarded clean playlist directly to `published/playlist_emby_clean.m3u`, which is the same file served by the static nginx container. Runtime state and guard-failure diagnostics are stored under `output/` so they are not exposed by nginx.

By default, full playlist checks run daily at `03:00` in the container timezone (`TZ=Asia/Jerusalem` in Compose). First deploy still runs immediately when no clean playlist/state exists. Recovery checks run after the full scan at the configured offsets, defaulting to `30,60,240` minutes.

If a scan produces the same clean playlist content that is already published, the sanitizer records the successful run but skips rewriting the file and skips the Emby refresh. This avoids unnecessary NAS and Emby load.

For active debugging, run the sanitizer with verbose probe tracing:

```bash
LOG_LEVEL=DEBUG docker compose up -d --build playlist-sanitizer
docker logs --tail 200 playlist-sanitizer
```

The runtime emits cycle, retry, recovery, and publish events to container stdout. Per-channel logs use channel names plus short fingerprints so you can trace failures and recoveries without exposing raw IPTV URLs in logs.

## EPG trimmer runtime

For automated XMLTV guide trimming, run:

```bash
docker compose up -d --build epg-trimmer
```

The worker downloads `http://epg.one/epg2.xml.gz`, matches XMLTV channel display names against `published/playlist_emby_clean.m3u`, and writes the trimmed guide to `published/epg.xml`. The existing static nginx container serves it at:

- `http://<host>:8766/epg.xml`

By default, EPG trimming runs daily at `04:00` in the container timezone, after the playlist sanitizer's default `03:00` run. A missing `epg.xml` triggers an immediate first run. A failed download, invalid XML, zero channel matches, or zero programmes preserves the previous EPG and skips Emby refresh.

The worker refreshes Emby's guide only when the trimmed EPG content changes and Emby credentials are configured through `EMBY_BASE_URL` and `EMBY_API_KEY`.

For Synology Container Manager, create the project from this Compose file and keep these host paths mounted:

- `./original_playlist.m3u8` -> `/data/input/playlist.m3u` read-only
- `./published` -> `/data/output` read-write
- `./output` -> `/data/state` read-write

The `epg-trimmer` service also uses `./published` -> `/data/output` and `./output` -> `/data/state`.

To run unit tests:

```bash
python -m pytest -q tests
```

This service now runs `python -m app.main`, which:
- probes channels asynchronously
- applies publish guard thresholds
- writes diagnostics when guard fails
- optionally refreshes Emby Live TV endpoints
