from pathlib import Path

import asyncio
from datetime import datetime, timedelta, timezone

from app import main as app_main


def test_run_once_smoke(monkeypatch, tmp_path: Path):
    raw = tmp_path / "raw.m3u"
    raw.write_text("#EXTM3U\n#EXTINF:-1,Chan1\nhttp://ok\n#EXTINF:-1,Chan2\nhttp://bad\n", encoding="utf-8")

    out_dir = tmp_path / "out"
    state_file = out_dir / ".state"

    monkeypatch.setattr(app_main, "RAW_PLAYLIST_PATH", raw)
    monkeypatch.setattr(app_main, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(app_main, "OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u8")
    monkeypatch.setattr(app_main, "PREVIOUS_CLEAN_PLAYLIST_NAME", "playlist_clean_prev.m3u")
    monkeypatch.setattr(app_main, "STATE_FILE", state_file)
    monkeypatch.setattr(app_main, "DIAGNOSTICS_DIR", out_dir / "diag")

    class _Result:
        def __init__(self, channel: str, valid: bool):
            self.channel = channel
            self.valid = valid

    class _Stats:
        def as_dict(self):
            return {"total_channels": 2, "valid": 1, "invalid": 1, "timeout": 0, "retry_success": 0}

    async def fake_probe_channels(channels, settings):  # noqa: ARG001
        return [_Result("http://ok", True), _Result("http://bad", False)], _Stats()

    monkeypatch.setattr(app_main, "probe_channels", fake_probe_channels)
    monkeypatch.setattr(app_main, "refresh_livetv_after_publish", lambda logger: None)

    ok = asyncio.run(app_main.run_once())
    assert ok is True

    output = (out_dir / "playlist_clean.m3u8").read_text(encoding="utf-8")
    assert "http://ok" in output
    assert "http://bad" not in output
    assert state_file.exists()


def test_build_candidate_playlist_does_not_duplicate_m3u_header(tmp_path: Path):
    raw = tmp_path / "raw.m3u"
    raw.write_text(
        "#EXTM3U\n#EXTINF:-1,Chan1\nhttp://ok\n",
        encoding="utf-8",
    )

    entries = app_main.parse_m3u(raw)
    output = app_main.build_candidate_playlist(entries, {"http://ok"})

    assert output.count("#EXTM3U") == 1


def test_build_candidate_playlist_adds_israeli_tvg_id_overrides(tmp_path: Path):
    raw = tmp_path / "raw.m3u"
    raw.write_text(
        "#EXTM3U\n"
        '#EXTINF:0 tvg-rec="3",Channel 9 FHD IL\n'
        "http://ok1\n"
        '#EXTINF:0 tvg-rec="3",Kan 11 HD IL\n'
        "http://ok2\n"
        '#EXTINF:0 tvg-rec="3",Keshet 12 FHD IL\n'
        "http://ok3\n"
        '#EXTINF:0 tvg-rec="3",Reshet 13 HD IL\n'
        "http://ok4\n"
        '#EXTINF:0 tvg-rec="0",Channel 14 FHD IL\n'
        "http://ok5\n",
        encoding="utf-8",
    )

    entries = app_main.parse_m3u(raw)
    output = app_main.build_candidate_playlist(
        entries,
        {"http://ok1", "http://ok2", "http://ok3", "http://ok4", "http://ok5"},
    )

    assert 'Channel 9 FHD IL' in output
    assert 'tvg-id="9kanal-israel"' in output
    assert 'tvg-id="channel-11-il"' in output
    assert 'tvg-id="channel-12-il"' in output
    assert 'tvg-id="channel-13-il"' in output
    assert 'tvg-id="ערוץ14.il"' in output


def test_build_candidate_playlist_replaces_existing_tvg_id_for_override_channels(tmp_path: Path):
    raw = tmp_path / "raw.m3u"
    raw.write_text(
        "#EXTM3U\n"
        '#EXTINF:0 tvg-id="wrong-id" tvg-rec="3",Keshet 12 HD IL\n'
        "http://ok\n",
        encoding="utf-8",
    )

    entries = app_main.parse_m3u(raw)
    output = app_main.build_candidate_playlist(entries, {"http://ok"})

    assert 'tvg-id="wrong-id"' not in output
    assert 'tvg-id="channel-12-il"' in output


def test_publish_candidate_does_not_update_state_when_guard_rejects(monkeypatch, tmp_path: Path):
    out_dir = tmp_path / "out"
    state_file = out_dir / ".state"

    monkeypatch.setattr(app_main, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(app_main, "OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u8")
    monkeypatch.setattr(app_main, "PREVIOUS_CLEAN_PLAYLIST_NAME", "playlist_clean.m3u8")
    monkeypatch.setattr(app_main, "STATE_FILE", state_file)
    monkeypatch.setattr(app_main, "DIAGNOSTICS_DIR", out_dir / "diag")
    monkeypatch.setattr(app_main, "MIN_VALID_CHANNELS_ABSOLUTE", 1)
    monkeypatch.setattr(app_main, "MIN_VALID_RATIO_OF_PREVIOUS", 0.7)

    published = app_main._publish_candidate("#EXTM3U\n")

    assert published is False
    assert not state_file.exists()


def test_publish_candidate_skips_emby_refresh_when_content_unchanged(monkeypatch, tmp_path: Path):
    out_dir = tmp_path / "out"
    state_file = out_dir / ".state"
    content = "#EXTM3U\n#EXTINF:-1,Chan1\nhttp://ok\n"
    out_dir.mkdir()
    (out_dir / "playlist_clean.m3u8").write_text(content, encoding="utf-8")

    refresh_calls: list[object] = []

    monkeypatch.setattr(app_main, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(app_main, "OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u8")
    monkeypatch.setattr(app_main, "PREVIOUS_CLEAN_PLAYLIST_NAME", "playlist_clean.m3u8")
    monkeypatch.setattr(app_main, "STATE_FILE", state_file)
    monkeypatch.setattr(app_main, "DIAGNOSTICS_DIR", out_dir / "diag")
    monkeypatch.setattr(app_main, "MIN_VALID_CHANNELS_ABSOLUTE", 1)
    monkeypatch.setattr(app_main, "MIN_VALID_RATIO_OF_PREVIOUS", 0.7)
    monkeypatch.setattr(app_main, "refresh_livetv_after_publish", lambda logger: refresh_calls.append(logger))

    published = app_main._publish_candidate(content)

    assert published is True
    assert refresh_calls == []
    assert state_file.exists()


def test_build_probe_targets_creates_safe_metadata(tmp_path: Path):
    raw = tmp_path / "raw.m3u"
    raw.write_text(
        "#EXTM3U\n#EXTINF:-1 tvg-id=\"c1\",Channel One\nhttp://example.invalid/stream?token=secret\n",
        encoding="utf-8",
    )

    entries = app_main.parse_m3u(raw)
    targets = app_main.build_probe_targets(entries)

    assert len(targets) == 1
    assert targets[0].name == "Channel One"
    assert len(targets[0].fingerprint) == 10
    assert "http://" not in app_main.format_probe_target(targets[0])
    assert "secret" not in app_main.format_probe_target(targets[0])


def test_parse_full_check_time_accepts_hour_minute():
    assert app_main.parse_full_check_time("03:00") == (3, 0)


def test_seconds_until_next_full_check_time_rolls_to_tomorrow(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)
    now = datetime(2026, 4, 30, 3, 1, tzinfo=timezone.utc)

    assert app_main.seconds_until_next_full_check_time(now, (3, 0)) == 23 * 3600 + 59 * 60


def test_seconds_until_next_full_check_time_uses_tz_dst_rules(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Jerusalem")
    now = datetime(2026, 3, 26, 4, 0, tzinfo=timezone(timedelta(hours=2)))

    assert app_main.seconds_until_next_full_check_time(now, (3, 0)) == 22 * 3600


def test_should_run_immediately_on_start_requires_state_and_clean_playlist(monkeypatch, tmp_path: Path):
    out_dir = tmp_path / "out"
    state_file = out_dir / ".state"
    monkeypatch.setattr(app_main, "OUTPUT_DIR", out_dir)
    monkeypatch.setattr(app_main, "OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u8")
    monkeypatch.setattr(app_main, "STATE_FILE", state_file)

    assert app_main.should_run_immediately_on_start() is True

    out_dir.mkdir()
    state_file.write_text("2026-04-30T00:00:00+00:00\n", encoding="utf-8")
    (out_dir / "playlist_clean.m3u8").write_text("#EXTM3U\n", encoding="utf-8")

    assert app_main.should_run_immediately_on_start() is False
