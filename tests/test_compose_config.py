from pathlib import Path


def test_playlist_admin_runs_http_service_and_owns_private_state():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    static_compose = Path("docker-compose.playlist.yml").read_text(encoding="utf-8")
    nginx_conf = Path("nginx/playlist-static.conf").read_text(encoding="utf-8")
    publish_script = Path("publish_emby_playlist.sh").read_text(encoding="utf-8")

    assert "playlist-admin:" in compose
    assert "container_name: playlist-admin" in compose
    assert "LOG_LEVEL: ${LOG_LEVEL:-INFO}" in compose
    assert "TZ: ${TZ:-Asia/Jerusalem}" in compose
    assert "./original_playlist.m3u8:/data/input/playlist.m3u:ro" in compose
    assert "./published:/data/output:rw" in compose
    assert "./output:/data/state:rw" in compose
    assert "ADMIN_DB_PATH: ${ADMIN_DB_PATH:-/data/state/admin/playlist.db}" in compose
    assert "ADMIN_BIND_PORT: ${ADMIN_BIND_PORT:-8780}" in compose
    assert "EPG_RUN_TIME: ${EPG_RUN_TIME:-04:00}" in compose
    assert "EPG_SOURCE_URL: ${EPG_SOURCE_URL:-http://epg.one/epg2.xml.gz}" in compose
    assert "EPG_ISRAEL_PRIMARY_URL: ${EPG_ISRAEL_PRIMARY_URL:-https://iptvx.one/EPG}" in compose
    assert "EPG_ISRAEL_FALLBACK_URL: ${EPG_ISRAEL_FALLBACK_URL:-https://iptv-epg.org/files/epg-il.xml.gz}" in compose
    assert 'command: ["python", "-m", "app.admin_runtime"]' in compose

    assert "./published:/usr/share/nginx/html:ro" in static_compose
    assert "./nginx/playlist-static.conf:/etc/nginx/conf.d/default.conf:ro" in static_compose
    assert 'SRC_FILE="${SRC_FILE:-original_playlist.m3u8}"' in publish_script

    assert "location /ui/" in nginx_conf
    assert "location /api/" in nginx_conf
    assert "proxy_pass http://playlist-admin:8780;" in nginx_conf


def test_compose_does_not_expose_ignored_probe_environment_variables():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "PROBE_READ_INTERVAL_US" not in compose
    assert "PROBE_USER_AGENT" not in compose
    assert "PROBE_FFMPEG_LOGLEVEL" not in compose
    assert "PROBE_EXTRA_ARGS" not in compose
