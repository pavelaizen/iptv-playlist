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

## Stable HTTP URL via lightweight static server

Serve only the published directory through a tiny static container while keeping URL path stable:

```bash
docker compose -f docker-compose.playlist.yml up -d playlist-static
```

Then point Emby to:

- `http://<host>:8080/playlist_emby_clean.m3u`

Keep this URL unchanged; updates happen in-place via atomic rename.


## Sanitizer runtime

For automated probing and guarded publishing, run:

```bash
docker compose up -d --build playlist-sanitizer
```

To run unit tests:

```bash
python -m pytest -q tests
```

This service now runs `python -m app.main`, which:
- probes channels asynchronously
- applies publish guard thresholds
- writes diagnostics when guard fails
- optionally refreshes Emby Live TV endpoints
