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

## Stable HTTP URL via lightweight static server

Serve only the published directory through a tiny static container while keeping URL path stable:

```bash
docker compose up -d playlist-static
```

Then point Emby to:

- `http://<host>:8080/playlist_emby_clean.m3u`

Keep this URL unchanged; updates happen in-place via atomic rename.
