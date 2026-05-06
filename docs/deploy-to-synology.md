# Deploying to Synology NAS

## Architecture

Two containers, both using `network_mode: host`:

- **`playlist-admin`** — Python app on port `8780`. Manages channels, validation, EPG sync, and publishing. Built from `Dockerfile.playlist-sanitizer`.
- **`playlist-static`** — Nginx on port `8766`. Serves static files from `published/` and reverse-proxies `/ui/` and `/api/` to the admin container at `127.0.0.1:8780`.

Both containers share the host network directly. This avoids Synology's Docker bridge firewall which blocks inter-container traffic even on the same Docker network.

```
Internet/LAN → :8766 (nginx) → /ui/*, /api/* → 127.0.0.1:8780 (admin)
                                → /playlist_emby_clean.m3u8, /epg.xml → static files
```

## Prerequisites

- Synology NAS with Docker (Container Manager) installed
- SSH access with `sudo` privileges
- Docker Compose v2 (`docker compose` not `docker-compose`)
- Docker binary at `/usr/local/bin/docker` on Synology
- Shell alias may be needed: `alias docker=/usr/local/bin/docker`

## One-Time Setup

### 1. Prepare the NAS directory

```bash
ssh titan18@192.168.1.113
sudo mkdir -p /volume1/docker/iptv-playlist/published
sudo mkdir -p /volume1/docker/iptv-playlist/output
sudo chown -R titan18:users /volume1/docker/iptv-playlist
```

### 2. Copy the subscription playlist

```bash
# From dev machine, using base64 since SCP subystem may be broken on Synology:
base64 original_playlist.m3u8 | ssh titan18@192.168.1.113 'base64 -d > /volume1/docker/iptv-playlist/original_playlist.m3u8'
```

SCP is often broken on Synology (`subsystem request failed`). Use base64 pipe or `rsync` instead.

## Deploying Code Changes

### Quick deploy (code changes only — no Docker rebuild needed)

The `app/` directory is bind-mounted read-only (`./app:/app/app:ro`). A container restart picks up Python changes immediately.

```bash
# From repo root on dev machine:
tar czf - -C /home/titan18/projects/iptv-playlist \
  app/ \
  docker-compose.yml \
  docker-compose.playlist.yml \
  nginx/playlist-static.conf \
| ssh titan18@192.168.1.113 'cd /volume1/docker/iptv-playlist && tar xzf -'

# Restart containers:
ssh titan18@192.168.1.113 \
  'echo PASSWORD | sudo -S /usr/local/bin/docker restart playlist-admin playlist-static'
```

**Password prompt:** Synology `sudo` requires the user's password piped via `-S`. Replace `PASSWORD` with the actual password. Use `sshpass -p 'PASSWORD'` to automate.

### Full deploy (Dockerfile changes — requires rebuild)

**Important:** Synology DNS is often broken inside Docker builds for `apt-get`. If rebuilding fails with `Temporary failure resolving 'deb.debian.org'`, use the quick restart approach instead — the image is already built with ffmpeg.

```bash
ssh titan18@192.168.1.113 \
  'cd /volume1/docker/iptv-playlist && \
   echo PASSWORD | sudo -S /usr/local/bin/docker compose up -d --force-recreate playlist-admin && \
   echo PASSWORD | sudo -S /usr/local/bin/docker compose -f docker-compose.playlist.yml up -d --force-recreate playlist-static'
```

Use `--force-recreate` (not just `restart`) when `docker-compose.yml` volumes or environment variables change. Plain `restart` does not pick up compose file changes.

## Networking Details

### Why `network_mode: host`

Synology's Docker bridge network has a firewall that **blocks container-to-container traffic** (including container-to-host on LAN IP). Symptoms:

- `ping` from one container to another: 100% packet loss
- `nc -z <container_ip> <port>`: connection refused
- nginx proxy to `playlist-admin:8780`: 504 Gateway Timeout

Using `network_mode: host` on both containers eliminates this problem. Both services bind directly to host ports:

- `playlist-admin` binds to `0.0.0.0:8780`
- `playlist-static` (nginx) binds to `8766`

### DNS inside containers

When using `network_mode: host`, containers use the host's DNS resolvers. The `dns:` and `extra_hosts:` directives are **incompatible** with `network_mode: host` — Docker will refuse to start the container.

If you need custom DNS (e.g., for provider hostnames that need `/etc/hosts` overrides), you must:

1. Add entries to the Synology's `/etc/hosts` file, or
2. Use the Synology DNS Server package to create local DNS records, or
3. Switch back to bridge networking (but then you must fix the firewall issue).

Currently, `extra_hosts` for `eqak8jqn.megogo.xyz` is **removed** from `docker-compose.yml`. If provider DNS resolution fails, add the mapping to Synology's `/etc/hosts`:

```bash
ssh titan18@192.168.1.113 \
  'echo "103.163.132.53 eqak8jqn.megogo.xyz" | sudo tee -a /etc/hosts'
```

### EPG source downloads

EPG source downloads (from `epg.one`, `iptvx.one`, `iptv-epg.org`, `epg.pw`) frequently fail from Synology due to DNS or connectivity issues. The app handles this gracefully — it falls back to cached source files from previous successful downloads located at:

- `output/state/epg/source-{id}.xmltv` (per-source cached files; may contain plain XML or gzip data)
- `output/state/epg/source-{id}.xml.gz` (legacy per-source cached files)
- `output/state/epg/source.xml.gz` (legacy default)
- `output/state/epg/source_israel_primary.xml.gz` (legacy)
- `output/state/epg/source_israel_fallback.xml.gz` (legacy)

For `epg.pw` per-channel sources, the admin service stores a canonical date-free URL and adds the current date plus `EPGPW_TIMEZONE` at download time. The default timezone is `Asia/Jerusalem`.

## File Permissions

Containers run as root. Written files (playlist, EPG) get mode `600` by default. Nginx running in the static container needs read access. The app calls `os.chmod(path, 0o644)` after writing public files, but docker restarts may re-create files. If nginx returns 403:

```bash
ssh titan18@192.168.1.113 \
  'echo PASSWORD | sudo -S /usr/local/bin/docker exec playlist-admin chmod 644 /data/published/epg.xml /data/published/playlist_emby_clean.m3u8'
```

## Volume Mapping

```
Host path                              Container path           Mode
./original_playlist.m3u8              /data/input/playlist.m3u  ro
./published/                         /data/published            rw
./output/                            /data/state                rw
./app/                               /app/app                   ro
```

- `./published/` is served by nginx and written by the admin container
- `./output/` holds SQLite DB, EPG work files, and diagnostics
- `./app/` is mounted read-only for hot-reload during development

## Verification Commands

```bash
# Check playlist is served
curl -I http://192.168.1.113:8766/playlist_emby_clean.m3u8

# Check EPG is served
curl -I http://192.168.1.113:8766/epg.xml

# Check admin API
curl http://192.168.1.113:8766/api/channels | python3 -m json.tool | head -20

# Check admin UI (returns HTML)
curl -s http://192.168.1.113:8766/ui/channels | head -5

# Direct admin API (bypasses nginx)
ssh titan18@192.168.1.113 \
  'curl -s http://localhost:8780/api/channels | python3 -m json.tool | head -20'

# Check container status
ssh titan18@192.168.1.113 \
  'echo PASSWORD | sudo -S /usr/local/bin/docker ps --filter name=playlist'

# Check container logs
ssh titan18@192.168.1.113 \
  'echo PASSWORD | sudo -S /usr/local/bin/docker logs playlist-admin --tail=30'

# Run Python inside the container
ssh titan18@192.168.1.113 \
  'echo PASSWORD | sudo -S /usr/local/bin/docker exec playlist-admin python3 -c "from app.admin_store import AdminStore; print(AdminStore(Path(\"/data/state/admin/playlist.db\")).channel_count())"'
```

## Common Issues

### nginx 504 Gateway Timeout on `/api/channels/validate`

Full validation takes 3+ minutes (ffprobe probes all channels). nginx's `proxy_read_timeout` is 180s. Options:

1. Increase `proxy_read_timeout` in `nginx/playlist-static.conf`
2. Make validation a background job (not yet implemented)
3. Validate individual channels via `POST /api/channels/{id}/validate` instead

### Container can't reach provider URLs

```bash
ssh titan18@192.168.1.113 \
  'echo PASSWORD | sudo -S /usr/local/bin/docker exec playlist-admin python3 -c "import urllib.request; urllib.request.urlopen(\"http://eqak8jqn.megogo.xyz/\", timeout=10); print(\"OK\")"'
```

If this fails, add the hostname to `/etc/hosts` on the Synology host.

### Docker build fails on `apt-get`

Synology Docker DNS breaks `apt-get update`. Workaround: don't rebuild the image unless the Dockerfile changes. For Python-only changes, just restart the container — the code is bind-mounted.

If you must rebuild and DNS is broken:
1. Use a pre-built image pushed to a registry
2. Fix Synology DNS: `Settings → Network → DNS Server` or add `nameserver 8.8.8.8` to `/etc/resolv.conf`
3. Build locally and transfer the image

### Transferring files when SCP is broken

Synology's SSH often has a broken SCP subsystem. Use these alternatives:

```bash
# Pipe via base64 (works for small-ish files):
base64 file.tar.gz | ssh titan18@192.168.1.113 'base64 -d > /tmp/file.tar.gz'

# Pipe tar directly:
tar czf - -C /local/path file1 file2 | ssh titan18@192.168.1.113 'cd /remote/path && tar xzf -'

# rsync over SSH:
rsync -avz -e ssh file titan18@192.168.1.113:/remote/path/
```

## Full Deploy Script

Save as `deploy.sh` and run from the repo root:

```bash
#!/usr/bin/env bash
set -euo pipefail
REMOTE_HOST="titan18@192.168.1.113"
REMOTE_DIR="/volume1/docker/iptv-playlist"
SSH="sshpass -p '${DEPLOY_PASSWORD}' ssh -o StrictHostKeyChecking=no ${REMOTE_HOST}"
DOCKER="echo '${DEPLOY_PASSWORD}' | sudo -S /usr/local/bin/docker"

# Transfer files
tar czf - app/ docker-compose.yml docker-compose.playlist.yml nginx/ | ${SSH} "cd ${REMOTE_DIR} && tar xzf -"

# Restart containers (code-only change)
${SSH} "${DOCKER} restart playlist-admin playlist-static"

# For compose file changes, use instead:
# ${SSH} "cd ${REMOTE_DIR} && ${DOCKER} compose up -d --force-recreate playlist-admin && ${DOCKER} compose -f docker-compose.playlist.yml up -d --force-recreate playlist-static"
```

Set `DEPLOY_PASSWORD` as an environment variable before running.

## Port Reference

| Port | Service | Protocol | Notes |
|------|---------|----------|-------|
| 8766 | playlist-static (nginx) | HTTP | Public-facing; serves playlist, EPG, admin UI |
| 8780 | playlist-admin (Python) | HTTP | Internal; admin API + UI (proxied through nginx) |
