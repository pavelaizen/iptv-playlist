from __future__ import annotations

import gzip
import io
import shutil
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import epg, epg_worker


def gzip_text(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def plain_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_gzip(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)


def gzip_bytes(text: str) -> bytes:
    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb") as fh:
        fh.write(text.encode("utf-8"))
    return buffer.getvalue()


class _BytesResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):  # noqa: ANN001
        self.close()


def settings_for(tmp_path: Path) -> epg_worker.EpgWorkerSettings:
    return epg_worker.EpgWorkerSettings(
        source_url="http://example.invalid/epg.xml.gz",
        israel_primary_source_url="http://example.invalid/israel-primary.xml.gz",
        israel_fallback_source_url="http://example.invalid/israel-fallback.xml.gz",
        israel_overrides_enabled=True,
        run_time=(4, 0),
        playlist_path=tmp_path / "playlist.m3u",
        output_path=tmp_path / "published" / "epg.xml",
        state_file=tmp_path / "state" / ".epg_trimmer_state",
        work_dir=tmp_path / "state" / "epg",
        min_matched_channels=1,
        min_programmes=1,
    )


def test_parse_run_time_accepts_hour_and_minute():
    assert epg_worker.parse_run_time("04:00") == (4, 0)


def test_seconds_until_next_run_time_wraps_to_next_day():
    now = datetime(2026, 5, 1, 4, 1, tzinfo=timezone.utc)

    assert epg_worker.seconds_until_next_run_time(now, (4, 0)) == 23 * 3600 + 59 * 60


def test_should_run_immediately_when_output_missing(tmp_path: Path):
    assert epg_worker.should_run_immediately(tmp_path / "missing.xml") is True


def test_settings_from_env_defaults_to_plain_xml_output(monkeypatch):
    monkeypatch.delenv("EPG_OUTPUT_PATH", raising=False)

    settings = epg_worker.EpgWorkerSettings.from_env()

    assert settings.output_path == Path("/data/output/epg.xml")


def test_settings_from_env_rejects_negative_guard_values(monkeypatch):
    monkeypatch.setenv("EPG_MIN_MATCHED_CHANNELS", "-1")
    monkeypatch.setenv("EPG_MIN_PROGRAMMES", "-2")

    settings = epg_worker.EpgWorkerSettings.from_env()

    assert settings.min_matched_channels == 1
    assert settings.min_programmes == 1


def test_settings_from_env_parses_israeli_override_flag(monkeypatch):
    monkeypatch.setenv("EPG_ISRAEL_OVERRIDES_ENABLED", "0")

    settings = epg_worker.EpgWorkerSettings.from_env()

    assert settings.israel_overrides_enabled is False


def test_run_once_uses_single_source_trim_when_israeli_overrides_disabled(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    settings.playlist_path.write_text(
        "#EXTM3U\n#EXTINF:-1,Channel One\nhttp://provider.invalid/one\n",
        encoding="utf-8",
    )
    settings = epg_worker.EpgWorkerSettings(
        source_url=settings.source_url,
        israel_primary_source_url=settings.israel_primary_source_url,
        israel_fallback_source_url=settings.israel_fallback_source_url,
        israel_overrides_enabled=False,
        run_time=settings.run_time,
        playlist_path=settings.playlist_path,
        output_path=settings.output_path,
        state_file=settings.state_file,
        work_dir=settings.work_dir,
        min_matched_channels=settings.min_matched_channels,
        min_programmes=settings.min_programmes,
    )

    calls = {"download": [], "single": 0, "override": 0}

    def fake_download(source_url: str, destination: Path):
        calls["download"].append((source_url, destination.name))
        write_gzip(destination, "<tv></tv>")

    def fake_single_trim(*, source_xmltv_gz_path, playlist_path, output_xmltv_path):
        calls["single"] += 1
        output_xmltv_path.write_text(
            "<tv><channel id='one'/><programme channel='one'/></tv>",
            encoding="utf-8",
        )
        return epg.EpgTrimSummary(
            playlist_channel_count=1,
            source_channel_count=1,
            matched_channel_count=1,
            programme_count=1,
            unmatched_playlist_names=(),
        )

    def fake_override_trim(**kwargs):
        calls["override"] += 1
        raise AssertionError("override trim should not be called")

    monkeypatch.setattr(epg_worker, "download_epg", fake_download)
    monkeypatch.setattr(epg_worker, "trim_xmltv_to_playlist_channels", fake_single_trim)
    monkeypatch.setattr(
        epg_worker,
        "trim_xmltv_to_playlist_channels_with_israeli_overrides",
        fake_override_trim,
    )
    monkeypatch.setattr(
        epg_worker,
        "refresh_livetv_after_publish",
        lambda log: None,  # noqa: ARG005
    )

    assert epg_worker.run_once(settings) is True
    assert calls["single"] == 1
    assert calls["override"] == 0
    assert calls["download"] == [("http://example.invalid/epg.xml.gz", "source.xml.gz")]


def test_run_once_uses_dual_source_trim_when_israeli_overrides_enabled(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    settings.playlist_path.write_text(
        "#EXTM3U\n#EXTINF:-1,Kan 11 HD IL\nhttp://provider.invalid/kan11\n",
        encoding="utf-8",
    )

    calls = {"download": [], "single": 0, "override": 0}

    def fake_download(source_url: str, destination: Path):
        calls["download"].append((source_url, destination.name))
        write_gzip(destination, "<tv></tv>")

    def fake_single_trim(*, source_xmltv_gz_path, playlist_path, output_xmltv_path):  # noqa: ARG001
        calls["single"] += 1
        raise AssertionError("single-source trim should not be called")

    def fake_override_trim(**kwargs):
        calls["override"] += 1
        kwargs["output_xmltv_path"].write_text(
            "<tv><channel id='kan11'/><programme channel='kan11'/></tv>",
            encoding="utf-8",
        )
        return epg.EpgTrimSummary(
            playlist_channel_count=1,
            source_channel_count=3,
            matched_channel_count=1,
            programme_count=1,
            unmatched_playlist_names=(),
        )

    monkeypatch.setattr(epg_worker, "download_epg", fake_download)
    monkeypatch.setattr(epg_worker, "trim_xmltv_to_playlist_channels", fake_single_trim)
    monkeypatch.setattr(
        epg_worker,
        "trim_xmltv_to_playlist_channels_with_israeli_overrides",
        fake_override_trim,
    )
    monkeypatch.setattr(
        epg_worker,
        "refresh_livetv_after_publish",
        lambda log: None,  # noqa: ARG005
    )

    assert epg_worker.run_once(settings) is True
    assert calls["single"] == 0
    assert calls["override"] == 1
    assert calls["download"] == [
        ("http://example.invalid/epg.xml.gz", "source.xml.gz"),
        ("http://example.invalid/israel-primary.xml.gz", "source_israel_primary.xml.gz"),
        ("http://example.invalid/israel-fallback.xml.gz", "source_israel_fallback.xml.gz"),
    ]


def test_main_schedules_with_local_aware_time(tmp_path: Path, monkeypatch):
    class StopScheduler(Exception):
        pass

    class LocalClock:
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return datetime(2026, 5, 1, 1, 0, tzinfo=tz)
            return cls()

        def astimezone(self):
            return datetime(2026, 5, 1, 4, 0, tzinfo=timezone(timedelta(hours=3)))

    captured = {}

    monkeypatch.setattr(
        epg_worker.EpgWorkerSettings,
        "from_env",
        classmethod(lambda cls: settings_for(tmp_path)),
    )
    monkeypatch.setattr(epg_worker, "should_run_immediately", lambda output_path: False)

    def fake_seconds_until_next_run_time(now: datetime, run_time: tuple[int, int]):
        captured["now"] = now
        raise StopScheduler

    monkeypatch.setattr(
        epg_worker,
        "seconds_until_next_run_time",
        fake_seconds_until_next_run_time,
    )
    monkeypatch.setattr(epg_worker, "datetime", LocalClock)

    with pytest.raises(StopScheduler):
        epg_worker.main()

    assert captured["now"].tzinfo is not None
    assert captured["now"].tzinfo is not timezone.utc


def test_download_epg_rejects_truncated_gzip_without_replacing_destination(
    tmp_path: Path,
    monkeypatch,
):
    destination = tmp_path / "epg.xml.gz"
    original_payload = "<tv><channel id='old'/></tv>"
    write_gzip(destination, original_payload)
    truncated_payload = gzip_bytes("<tv>" + ("x" * 4096) + "</tv>")[:-8]

    def fake_urlopen(source_url: str, timeout: int):  # noqa: ARG001
        assert timeout == 180
        return _BytesResponse(truncated_payload)

    monkeypatch.setattr(epg_worker.request, "urlopen", fake_urlopen)

    with pytest.raises((EOFError, gzip.BadGzipFile, OSError, zlib.error)):
        epg_worker.download_epg("http://example.invalid/epg.xml.gz", destination)

    assert gzip_text(destination) == original_payload
    assert list(tmp_path.glob(".epg.xml.gz.*.tmp")) == []


def test_download_epg_writes_valid_gzip_payload(tmp_path: Path, monkeypatch):
    destination = tmp_path / "epg.xml.gz"
    payload = "<tv><channel id='new'/></tv>"

    def fake_urlopen(source_url: str, timeout: int):  # noqa: ARG001
        assert timeout == 180
        return _BytesResponse(gzip_bytes(payload))

    monkeypatch.setattr(epg_worker.request, "urlopen", fake_urlopen)

    epg_worker.download_epg("http://example.invalid/epg.xml.gz", destination)

    assert gzip_text(destination) == payload
    assert list(tmp_path.glob(".epg.xml.gz.*.tmp")) == []


def test_publish_candidate_rejects_zero_matches_preserves_previous_and_skips_side_effects(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    previous_payload = "<tv><channel id='previous'/></tv>"
    candidate = tmp_path / "candidate.xml"
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    settings.output_path.write_text(previous_payload, encoding="utf-8")
    candidate.write_text("<tv></tv>", encoding="utf-8")
    refresh_calls = []
    monkeypatch.setattr(
        epg_worker,
        "refresh_livetv_after_publish",
        lambda log: refresh_calls.append(log),
    )

    accepted = epg_worker.publish_candidate(
        candidate,
        settings,
        epg.EpgTrimSummary(
            playlist_channel_count=1,
            source_channel_count=1,
            matched_channel_count=0,
            programme_count=1,
            unmatched_playlist_names=("Missing",),
        ),
    )

    assert accepted is False
    assert plain_text(settings.output_path) == previous_payload
    assert not settings.state_file.exists()
    assert refresh_calls == []


def test_publish_candidate_skips_refresh_when_payload_unchanged_and_writes_state(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    candidate = tmp_path / "candidate.xml"
    payload = "<tv><channel id='one'/><programme channel='one'/></tv>"
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    settings.output_path.write_text(payload, encoding="utf-8")
    shutil.copyfile(settings.output_path, candidate)
    refresh_calls = []
    monkeypatch.setattr(
        epg_worker,
        "refresh_livetv_after_publish",
        lambda log: refresh_calls.append(log),
    )

    accepted = epg_worker.publish_candidate(
        candidate,
        settings,
        epg.EpgTrimSummary(
            playlist_channel_count=1,
            source_channel_count=1,
            matched_channel_count=1,
            programme_count=1,
            unmatched_playlist_names=(),
        ),
    )

    assert accepted is True
    assert plain_text(settings.output_path) == payload
    assert settings.state_file.read_text(encoding="utf-8").strip()
    assert refresh_calls == []


def test_publish_candidate_replaces_changed_output_and_refreshes_once(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    candidate = tmp_path / "candidate.xml"
    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    settings.output_path.write_text("<tv><channel id='old'/></tv>", encoding="utf-8")
    candidate.write_text(
        "<tv><channel id='new'/><programme channel='new'/></tv>",
        encoding="utf-8",
    )
    refresh_calls = []
    monkeypatch.setattr(
        epg_worker,
        "refresh_livetv_after_publish",
        lambda log: refresh_calls.append(log) or None,
    )

    accepted = epg_worker.publish_candidate(
        candidate,
        settings,
        epg.EpgTrimSummary(
            playlist_channel_count=1,
            source_channel_count=1,
            matched_channel_count=1,
            programme_count=1,
            unmatched_playlist_names=(),
        ),
    )

    assert accepted is True
    assert plain_text(settings.output_path) == (
        "<tv><channel id='new'/><programme channel='new'/></tv>"
    )
    assert settings.state_file.read_text(encoding="utf-8").strip()
    assert refresh_calls == [epg_worker.LOG]
