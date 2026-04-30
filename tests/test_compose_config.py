from pathlib import Path


def test_sanitizer_writes_same_playlist_that_static_server_serves():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    static_compose = Path("docker-compose.playlist.yml").read_text(encoding="utf-8")

    assert "LOG_LEVEL: ${LOG_LEVEL:-INFO}" in compose
    assert "TZ: ${TZ:-Asia/Jerusalem}" in compose
    assert "FULL_CHECK_TIME: ${FULL_CHECK_TIME:-03:00}" in compose
    assert "./published:/data/output:rw" in compose
    assert "OUTPUT_PLAYLIST_NAME: ${OUTPUT_PLAYLIST_NAME:-playlist_emby_clean.m3u}" in compose
    assert "STATE_FILE: ${STATE_FILE:-/data/state/.playlist_sanitizer_state}" in compose
    assert "DIAGNOSTICS_DIR: ${DIAGNOSTICS_DIR:-/data/state/diagnostics}" in compose
    assert "./output:/data/state:rw" in compose
    assert "./published:/usr/share/nginx/html:ro" in static_compose


def test_compose_does_not_expose_ignored_probe_environment_variables():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")

    assert "PROBE_READ_INTERVAL_US" not in compose
    assert "PROBE_USER_AGENT" not in compose
    assert "PROBE_FFMPEG_LOGLEVEL" not in compose
    assert "PROBE_EXTRA_ARGS" not in compose
