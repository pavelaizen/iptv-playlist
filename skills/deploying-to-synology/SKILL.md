---
name: deploying-to-synology
description: Use when working in this IPTV playlist repo and the task is to update the Synology deployment, sync local repo changes to the NAS, or rebuild the playlist services there
---

# Deploying To Synology

Use this skill for the Synology-hosted Docker deployment of this repo.

Do not hardcode SSH targets, remote repo paths, passwords, or API keys into the skill, repo, or memory. Ask the user for current credentials or let SSH prompt in the current session.

## Required Inputs

- SSH target such as `<user@host>`
- Remote repo path such as `/volume1/docker/iptv-playlist`
- Whether the remote host has working `git`, `docker`, and `docker compose` on `PATH`

## Workflow

1. Verify the local repo before deploy:

```bash
python -m compileall -q app tests
python -m pytest -q tests
./publish_emby_playlist.sh
```

2. Confirm the remote repo path and tool paths. Do not assume the SSH shell has `docker` on `PATH`:

```bash
ssh <user@host> 'hostname; pwd'
ssh <user@host> 'command -v git || true; command -v docker || true; ls -l /var/packages/ContainerManager/target/usr/bin/docker 2>/dev/null || true'
```

3. Sync only the required tracked files. Do not overwrite remote secrets or operational data.

If remote `git` works and the repo is connected correctly, a remote pull may be enough. If not, copy the changed files explicitly.

Typical copy-based deploy inputs in this repo:

- repo files such as `app/`, `docker-compose.yml`, `docker-compose.playlist.yml`, `publish_emby_playlist.sh`, `README.md`, `AGENTS.md`
- the source playlist file `original_playlist.m3u8`

4. Regenerate the published playlist on the NAS:

```bash
ssh <user@host> 'cd /absolute/remote/repo && ./publish_emby_playlist.sh'
```

5. Start or refresh the static playlist server:

```bash
ssh <user@host> 'cd /absolute/remote/repo && printf "%s\n" "<sudo-password-if-needed>" | sudo -S -p "" /var/packages/ContainerManager/target/usr/bin/docker compose -f docker-compose.playlist.yml up -d playlist-static'
```

6. Refresh the sanitizer container without forcing a remote rebuild unless the image actually changed:

```bash
ssh <user@host> 'cd /absolute/remote/repo && printf "%s\n" "<sudo-password-if-needed>" | sudo -S -p "" /var/packages/ContainerManager/target/usr/bin/docker compose up -d --no-build --force-recreate playlist-sanitizer'
```

7. If the Synology host does not have the needed image locally and its on-box build hangs on package mirrors, build locally, transfer the tar, and load it remotely:

```bash
docker build -f Dockerfile.playlist-sanitizer -t iptv-playlist-playlist-sanitizer:latest .
docker save iptv-playlist-playlist-sanitizer:latest -o /tmp/iptv-playlist-playlist-sanitizer.tar
scp -O /tmp/iptv-playlist-playlist-sanitizer.tar <user@host>:/tmp/
ssh <user@host> 'printf "%s\n" "<sudo-password-if-needed>" | sudo -S -p "" /var/packages/ContainerManager/target/usr/bin/docker load -i /tmp/iptv-playlist-playlist-sanitizer.tar'
ssh <user@host> 'cd /absolute/remote/repo && printf "%s\n" "<sudo-password-if-needed>" | sudo -S -p "" /var/packages/ContainerManager/target/usr/bin/docker compose up -d --no-build --force-recreate playlist-sanitizer'
```

## Verification

Verify the static server, generated playlist, and container status on the NAS:

```bash
ssh <user@host> 'curl -I --max-time 5 http://127.0.0.1:8766/playlist_emby_clean.m3u8'
ssh <user@host> 'cd /absolute/remote/repo && rg -c "^#EXTINF" original_playlist.m3u8 published/playlist_emby_clean.m3u8'
ssh <user@host> 'printf "%s\n" "<sudo-password-if-needed>" | sudo -S -p "" /var/packages/ContainerManager/target/usr/bin/docker compose ps playlist-sanitizer'
ssh <user@host> 'printf "%s\n" "<sudo-password-if-needed>" | sudo -S -p "" /var/packages/ContainerManager/target/usr/bin/docker logs --tail 80 playlist-sanitizer'
```

Healthy startup in this repo should show the scheduler configuration and either an initial full run or a delayed next full-check schedule.

## Failure Patterns

- `scp` may need legacy mode on Synology SSH: use `scp -O`.
- `rsync` over SSH can fail against Synology’s SSH subsystem. Prefer `scp -O` or tar-over-SSH when that happens.
- `docker` may exist only under `/var/packages/ContainerManager/target/usr/bin/docker`.
- `docker compose up` without `--no-build` can trigger a slow remote image build that stalls on package mirror fetches.
- Do not overwrite remote env files, Emby secrets, or generated operational state unless the user explicitly asks.
