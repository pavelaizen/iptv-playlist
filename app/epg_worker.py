from __future__ import annotations

import logging
import os
import shutil
import tempfile
import time as time_module
import gzip
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from urllib import request

from app.emby_client import refresh_livetv_after_publish
from app.epg import EpgTrimSummary, trim_xmltv_to_playlist_channels


LOG = logging.getLogger("epg-worker")


@dataclass(frozen=True)
class EpgWorkerSettings:
    source_url: str
    run_time: tuple[int, int]
    playlist_path: Path
    output_path: Path
    state_file: Path
    work_dir: Path
    min_matched_channels: int
    min_programmes: int

    @classmethod
    def from_env(cls) -> "EpgWorkerSettings":
        return cls(
            source_url=os.getenv("EPG_SOURCE_URL", "http://epg.one/epg2.xml.gz"),
            run_time=parse_run_time(os.getenv("EPG_RUN_TIME", "04:00")),
            playlist_path=Path(
                os.getenv("EPG_PLAYLIST_PATH", "/data/output/playlist_emby_clean.m3u")
            ),
            output_path=Path(os.getenv("EPG_OUTPUT_PATH", "/data/output/epg.xml")),
            state_file=Path(
                os.getenv("EPG_STATE_FILE", "/data/state/.epg_trimmer_state")
            ),
            work_dir=Path(os.getenv("EPG_WORK_DIR", "/data/state/epg")),
            min_matched_channels=_env_int(
                "EPG_MIN_MATCHED_CHANNELS",
                1,
                minimum=1,
            ),
            min_programmes=_env_int("EPG_MIN_PROGRAMMES", 1, minimum=1),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_run_time(raw_value: str) -> tuple[int, int]:
    try:
        hour_raw, minute_raw = raw_value.strip().split(":", maxsplit=1)
        hour = int(hour_raw)
        minute = int(minute_raw)
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    except (AttributeError, TypeError, ValueError):
        pass

    LOG.warning("Invalid EPG_RUN_TIME=%r; falling back to 04:00", raw_value)
    return 4, 0


def seconds_until_next_run_time(now: datetime, run_time: tuple[int, int]) -> float:
    hour, minute = run_time
    target = datetime.combine(now.date(), time(hour, minute), tzinfo=now.tzinfo)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def should_run_immediately(output_path: Path) -> bool:
    return not output_path.exists()


def download_epg(source_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        with request.urlopen(source_url, timeout=180) as response:
            with temp_path.open("wb") as output_fh:
                shutil.copyfileobj(response, output_fh)

        _validate_gzip_stream(temp_path)

        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _same_file_payload(left: Path, right: Path) -> bool:
    if not left.exists() or not right.exists():
        return False

    with left.open("rb") as left_fh, right.open("rb") as right_fh:
        while True:
            left_chunk = left_fh.read(1024 * 1024)
            right_chunk = right_fh.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def publish_candidate(
    candidate_path: Path,
    settings: EpgWorkerSettings,
    summary: EpgTrimSummary,
) -> bool:
    if summary.matched_channel_count < settings.min_matched_channels:
        LOG.warning(
            "Rejecting EPG candidate with %s matched channels; minimum is %s",
            summary.matched_channel_count,
            settings.min_matched_channels,
        )
        return False

    if summary.programme_count < settings.min_programmes:
        LOG.warning(
            "Rejecting EPG candidate with %s programmes; minimum is %s",
            summary.programme_count,
            settings.min_programmes,
        )
        return False

    content_changed = not _same_file_payload(candidate_path, settings.output_path)
    if content_changed:
        _replace_file(candidate_path, settings.output_path)
        warning = refresh_livetv_after_publish(LOG)
        if warning:
            LOG.warning("%s", warning)
    else:
        LOG.info("EPG output payload is unchanged; skipping Emby refresh")

    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.state_file.write_text(f"{now_iso()}\n", encoding="utf-8")
    return True


def run_once(settings: EpgWorkerSettings | None = None) -> bool:
    settings = settings or EpgWorkerSettings.from_env()
    if not settings.playlist_path.exists():
        LOG.error("EPG playlist path is missing: %s", settings.playlist_path)
        return False

    settings.work_dir.mkdir(parents=True, exist_ok=True)
    source_path = settings.work_dir / "source.xml.gz"
    candidate_path = settings.work_dir / "candidate.xml"

    download_epg(settings.source_url, source_path)
    summary = trim_xmltv_to_playlist_channels(
        source_xmltv_gz_path=source_path,
        playlist_path=settings.playlist_path,
        output_xmltv_path=candidate_path,
    )
    return publish_candidate(candidate_path, settings, summary)


def _safe_run_once(settings: EpgWorkerSettings) -> bool:
    try:
        return run_once(settings)
    except Exception:
        LOG.exception("EPG worker run failed")
        return False


def main() -> None:
    settings = EpgWorkerSettings.from_env()

    if should_run_immediately(settings.output_path):
        _safe_run_once(settings)

    while True:
        sleep_seconds = seconds_until_next_run_time(
            datetime.now().astimezone(),
            settings.run_time,
        )
        LOG.info("Sleeping %.0f seconds until next EPG run", sleep_seconds)
        time_module.sleep(sleep_seconds)
        _safe_run_once(settings)


def _replace_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        shutil.copyfile(source, temp_path)
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _gzip_open_read(path: Path):
    return closing(gzip.open(path, "rb"))


def _validate_gzip_stream(path: Path) -> None:
    with _gzip_open_read(path) as gzip_fh:
        while gzip_fh.read(1024 * 1024):
            pass


def _env_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError:
        LOG.warning("Invalid %s=%r; falling back to %s", name, raw_value, default)
        return default

    if minimum is not None and value < minimum:
        LOG.warning(
            "Invalid %s=%r; minimum is %s, falling back to %s",
            name,
            raw_value,
            minimum,
            default,
        )
        return default

    return value


logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


if __name__ == "__main__":
    main()
