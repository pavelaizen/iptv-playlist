from __future__ import annotations

import gzip
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app import epg, epg_worker


def gzip_text(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def write_gzip(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)


def settings_for(tmp_path: Path) -> epg_worker.EpgWorkerSettings:
    return epg_worker.EpgWorkerSettings(
        source_url="http://example.invalid/epg.xml.gz",
        run_time=(4, 0),
        playlist_path=tmp_path / "playlist.m3u",
        output_path=tmp_path / "published" / "epg.xml.gz",
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
    assert epg_worker.should_run_immediately(tmp_path / "missing.xml.gz") is True


def test_publish_candidate_rejects_zero_matches_preserves_previous_and_skips_side_effects(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    previous_payload = "<tv><channel id='previous'/></tv>"
    candidate = tmp_path / "candidate.xml.gz"
    write_gzip(settings.output_path, previous_payload)
    write_gzip(candidate, "<tv></tv>")
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
    assert gzip_text(settings.output_path) == previous_payload
    assert not settings.state_file.exists()
    assert refresh_calls == []


def test_publish_candidate_skips_refresh_when_payload_unchanged_and_writes_state(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    candidate = tmp_path / "candidate.xml.gz"
    payload = "<tv><channel id='one'/><programme channel='one'/></tv>"
    write_gzip(settings.output_path, payload)
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
    assert gzip_text(settings.output_path) == payload
    assert settings.state_file.read_text(encoding="utf-8").strip()
    assert refresh_calls == []


def test_publish_candidate_replaces_changed_output_and_refreshes_once(
    tmp_path: Path,
    monkeypatch,
):
    settings = settings_for(tmp_path)
    candidate = tmp_path / "candidate.xml.gz"
    write_gzip(settings.output_path, "<tv><channel id='old'/></tv>")
    write_gzip(candidate, "<tv><channel id='new'/><programme channel='new'/></tv>")
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
    assert gzip_text(settings.output_path) == (
        "<tv><channel id='new'/><programme channel='new'/></tv>"
    )
    assert settings.state_file.read_text(encoding="utf-8").strip()
    assert refresh_calls == [epg_worker.LOG]
