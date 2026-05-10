# IPTV playlist publishing

## Public URLs

Start static serving:

```bash
docker compose -f docker-compose.playlist.yml up -d playlist-static
```

Public endpoints stay stable:

- `http://<host>:8766/playlist_emby_clean.m3u8`
- `http://<host>:8766/epg.xml`
- `http://<host>:8766/ui/channels`

## Admin runtime

The repository uses a DB-backed control plane. Keep the public static container
running all day; start the admin dashboard only when editing or inspecting state:

```bash
docker compose --profile admin up -d --build playlist-admin
```

`playlist-admin` owns:

- one-time migration from `original_playlist.m3u8` into SQLite
- manual channel validation and guarded playlist publishing
- extended per-stream `ffmpeg` stability tests for video/audio decode checks
- `/api/*` and `/ui/*` admin routes (proxied by `playlist-static`)

Nightly automation is a one-shot command, intended for Synology Task Scheduler
or cron at `04:00`:

```bash
docker compose --profile jobs run --rm playlist-nightly
```

`playlist-nightly` reloads enabled EPG sources into the cache, validates streams,
publishes the clean playlist, rebuilds `epg.xml` from cached EPG data, then exits.

EPG sources can be static XML/XML.GZ feeds or `epg.pw` per-channel URLs. For
`epg.pw`, paste either a `/last/<channel>.html` page URL or an `/api/epg.xml`
URL; the admin runtime stores a date-free canonical URL and adds the current
date plus Base64-encoded timezone on each download. `EPGPW_TIMEZONE` defaults to
`Asia/Jerusalem`.

Generated outputs remain in `published/`:

- `published/playlist_emby_clean.m3u8`
- `published/epg.xml`

Private runtime data remains in `output/`.

## Atomic raw-playlist publisher

For manually publishing raw source playlist updates:

```bash
./publish_emby_playlist.sh
```

## Verification

Run tests:

```bash
python -m pytest -q tests
python -m compileall -q app tests
```

Container checks:

```bash
docker compose --profile admin up -d --build playlist-admin
docker compose --profile jobs run --rm playlist-nightly
docker compose -f docker-compose.playlist.yml up -d playlist-static
docker compose --profile admin ps playlist-admin
curl -I http://127.0.0.1:8766/playlist_emby_clean.m3u8
curl -I http://127.0.0.1:8766/epg.xml
curl -I http://127.0.0.1:8766/ui/channels
```
