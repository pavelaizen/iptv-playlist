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
