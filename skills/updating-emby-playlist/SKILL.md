---
name: updating-emby-playlist
description: Use when working in this IPTV playlist repo and the task is to make Emby see the current published playlist, refresh the Live TV source, or repair a missing M3U tuner source
---

# Updating Emby Playlist

Use this skill when the playlist file is already generated or served and the remaining task is on the Emby side.

Do not store credentials in the repo, skill, or shell history. Ask the user for the current Emby base URL and API key, or let them provide env vars in the current session.

## Required Inputs

- `EMBY_BASE_URL` such as `http://<emby-host>:8096/emby`
- `EMBY_API_KEY`
- Playlist URL that Emby itself can reach

When Emby and the static playlist server run on the same host, prefer `http://127.0.0.1:8766/playlist_emby_clean.m3u` over the LAN IP. Verify the static server first instead of assuming it is up.

## Workflow

1. Confirm the published playlist exists and the static server is reachable:

```bash
./publish_emby_playlist.sh
docker compose -f docker-compose.playlist.yml up -d playlist-static
curl -I http://127.0.0.1:8766/playlist_emby_clean.m3u
```

2. Read Emby Live TV state before changing anything:

```bash
python - <<'PY'
import os, urllib.request
base = os.environ["EMBY_BASE_URL"].rstrip("/")
key = os.environ["EMBY_API_KEY"]
for endpoint in ["/LiveTv/Info", "/LiveTv/TunerHosts"]:
    req = urllib.request.Request(
        base + endpoint,
        headers={"X-Emby-Token": key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        print(endpoint, response.read().decode("utf-8", "replace"))
PY
```

3. If the M3U tuner host is missing, add it with the playlist URL Emby can reach:

```bash
PLAYLIST_URL='http://127.0.0.1:8766/playlist_emby_clean.m3u'
python - <<'PY'
import json, os, urllib.request
base = os.environ["EMBY_BASE_URL"].rstrip("/")
key = os.environ["EMBY_API_KEY"]
playlist = os.environ["PLAYLIST_URL"]
payload = {
    "Type": "m3u",
    "Url": playlist,
    "Source": playlist,
    "FriendlyName": "iptv-playlist",
    "ImportFavoritesOnly": False,
    "PreferEpgChannelImages": True,
    "PreferEpgChannelNumbers": False,
    "AllowHWTranscoding": True,
    "AllowMappingByNumber": False,
    "ImportGuideData": True,
    "TunerCount": 0,
    "DataVersion": 0,
}
request = urllib.request.Request(
    base + "/LiveTv/TunerHosts",
    method="POST",
    headers={
        "X-Emby-Token": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    },
    data=json.dumps(payload).encode("utf-8"),
)
with urllib.request.urlopen(request, timeout=20) as response:
    print(response.read().decode("utf-8", "replace"))
PY
```

4. Refresh guide data after the tuner exists:

```bash
python - <<'PY'
import logging
from app.emby_client import refresh_livetv_after_publish
logging.basicConfig(level=logging.INFO, format="%(message)s")
print(refresh_livetv_after_publish(logging.getLogger("emby-refresh")))
PY
```

5. Verify Emby now sees the source:

```bash
python - <<'PY'
import os, urllib.request
base = os.environ["EMBY_BASE_URL"].rstrip("/")
key = os.environ["EMBY_API_KEY"]
for endpoint in ["/LiveTv/Info", "/LiveTv/TunerHosts", "/LiveTv/GuideInfo"]:
    req = urllib.request.Request(
        base + endpoint,
        headers={"X-Emby-Token": key, "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as response:
        print(endpoint, response.read().decode("utf-8", "replace"))
PY
```

## Failure Patterns

- `Connection refused` when adding the tuner usually means `playlist-static` is down or Emby cannot reach the chosen URL.
- `IsEnabled: false` with empty `TunerHosts` means Emby has no active Live TV source yet.
- If the playlist did not change, `refresh_livetv_after_publish()` may still succeed but Emby content will remain the same. Confirm the generated playlist content first.
- Do not assume the LAN IP is correct for Emby itself. If Emby and nginx share a host, test `127.0.0.1` first.
