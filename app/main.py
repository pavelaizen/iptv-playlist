#!/usr/bin/env python3
"""Playlist sanitizer and publisher runtime."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.emby_client import refresh_livetv_after_publish
from app.probe import ProbeSettings, ProbeTarget, probe_channels
from app.publish import PublishGuardConfig, select_playlist_for_publish

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("playlist-runtime")

RAW_PLAYLIST_PATH = Path(os.getenv("RAW_PLAYLIST_PATH", "/data/input/playlist.m3u"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/data/output"))
OUTPUT_PLAYLIST_NAME = os.getenv("OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u8")
PREVIOUS_CLEAN_PLAYLIST_NAME = os.getenv("PREVIOUS_CLEAN_PLAYLIST_NAME", OUTPUT_PLAYLIST_NAME)
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/output/.playlist_sanitizer_state"))
DIAGNOSTICS_DIR = Path(os.getenv("DIAGNOSTICS_DIR", str(OUTPUT_DIR / "diagnostics")))
MIN_VALID_CHANNELS_ABSOLUTE = int(os.getenv("MIN_VALID_CHANNELS_ABSOLUTE", "1"))
MIN_VALID_RATIO_OF_PREVIOUS = float(os.getenv("MIN_VALID_RATIO_OF_PREVIOUS", "0.7"))
EXTRA_RUN_DELAYS_MINUTES_RAW = os.getenv("EXTRA_RUN_DELAYS_MINUTES", "30,60,240")
FULL_CHECK_TIME = os.getenv("FULL_CHECK_TIME", "03:00")


@dataclass(slots=True)
class CycleState:
    entries: list[tuple[list[str], str]]
    targets_by_url: dict[str, ProbeTarget]
    known_valid_urls: set[str]
    pending_offline_urls: set[str]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_extra_run_offsets_seconds(raw_value: str) -> list[float]:
    if not raw_value.strip():
        return []

    offsets: set[float] = set()
    for chunk in raw_value.split(","):
        value = chunk.strip()
        if not value:
            continue
        try:
            minutes = float(value)
        except ValueError:
            LOG.warning("ignored invalid EXTRA_RUN_DELAYS_MINUTES token: %r", value)
            continue
        if minutes <= 0:
            LOG.warning("ignored non-positive EXTRA_RUN_DELAYS_MINUTES value: %s", value)
            continue
        offsets.add(minutes * 60.0)

    return sorted(offsets)


def parse_full_check_time(raw_value: str) -> tuple[int, int]:
    value = raw_value.strip()
    try:
        hour_text, minute_text = value.split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        LOG.warning("invalid FULL_CHECK_TIME=%r, using 03:00", raw_value)
        return 3, 0

    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        LOG.warning("invalid FULL_CHECK_TIME=%r, using 03:00", raw_value)
        return 3, 0

    return hour, minute


def seconds_until_next_full_check_time(now: datetime, full_check_time: tuple[int, int]) -> float:
    hour, minute = full_check_time
    local_now = now.astimezone(_scheduler_zoneinfo(now.tzinfo))
    target = datetime.combine(
        local_now.date(),
        datetime_time(hour=hour, minute=minute),
        tzinfo=local_now.tzinfo,
    )
    if target <= local_now:
        target += timedelta(days=1)
    return target.timestamp() - local_now.timestamp()


def _scheduler_zoneinfo(fallback_tzinfo):
    timezone_name = os.getenv("TZ")
    if not timezone_name:
        return fallback_tzinfo

    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        LOG.warning("invalid TZ=%r, using caller timezone", timezone_name)
        return fallback_tzinfo


def should_run_immediately_on_start() -> bool:
    clean_playlist_path = OUTPUT_DIR / OUTPUT_PLAYLIST_NAME
    return not (STATE_FILE.exists() and clean_playlist_path.exists())


def parse_m3u(path: Path) -> list[tuple[list[str], str]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    entries: list[tuple[list[str], str]] = []
    pending_meta: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.upper().startswith("#EXTM3U"):
            continue
        if stripped.startswith("#"):
            pending_meta.append(line)
            continue
        entries.append((pending_meta[:], line))
        pending_meta = []

    return entries


def build_candidate_playlist(entries: list[tuple[list[str], str]], valid_urls: set[str]) -> str:
    out_lines = ["#EXTM3U"]
    for meta, url in entries:
        stripped = url.strip()
        if stripped not in valid_urls:
            continue
        out_lines.extend(meta)
        out_lines.append(url)
    return "\n".join(out_lines) + "\n"


def extract_channel_name(metadata_lines: list[str]) -> str:
    for line in reversed(metadata_lines):
        stripped = line.strip()
        if not stripped.upper().startswith("#EXTINF"):
            continue
        _, _, candidate = stripped.rpartition(",")
        candidate = candidate.strip()
        if candidate:
            return candidate
    return "unnamed-channel"


def build_probe_targets(entries: list[tuple[list[str], str]]) -> list[ProbeTarget]:
    targets: list[ProbeTarget] = []
    for metadata_lines, url in entries:
        normalized_url = url.strip()
        targets.append(
            ProbeTarget(
                url=normalized_url,
                name=extract_channel_name(metadata_lines),
                fingerprint=hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:10],
            )
        )
    return targets


def format_probe_target(target: ProbeTarget) -> str:
    return f"name={target.name!r} fingerprint={target.fingerprint}"


def _publish_candidate(candidate_content: str) -> bool:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    candidate_output_path = OUTPUT_DIR / OUTPUT_PLAYLIST_NAME
    previous_clean_path = OUTPUT_DIR / PREVIOUS_CLEAN_PLAYLIST_NAME

    guard_decision = select_playlist_for_publish(
        candidate_output_path=candidate_output_path,
        previous_clean_path=previous_clean_path,
        candidate_content=candidate_content,
        config=PublishGuardConfig(
            min_valid_channels_absolute=MIN_VALID_CHANNELS_ABSOLUTE,
            min_valid_ratio_of_previous=MIN_VALID_RATIO_OF_PREVIOUS,
            diagnostics_dir=DIAGNOSTICS_DIR,
        ),
    )

    LOG.info(
        "publish complete guard=%s changed=%s candidate_valid=%d previous_valid=%d required_minimum=%d selected=%s",
        guard_decision.reason,
        guard_decision.content_changed,
        guard_decision.candidate_valid_channels,
        guard_decision.previous_valid_channels,
        guard_decision.required_minimum,
        guard_decision.selected_path,
    )

    if guard_decision.publish_candidate and guard_decision.content_changed:
        warning = refresh_livetv_after_publish(LOG)
        if warning:
            LOG.warning(warning)
    elif guard_decision.publish_candidate:
        LOG.info("Emby refresh skipped: clean playlist content unchanged")

    if guard_decision.publish_candidate:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(now_iso() + "\n", encoding="utf-8")
    return guard_decision.publish_candidate


async def run_full_check() -> CycleState | None:
    if not RAW_PLAYLIST_PATH.exists():
        LOG.error("input playlist missing: %s", RAW_PLAYLIST_PATH)
        return None

    entries = parse_m3u(RAW_PLAYLIST_PATH)
    probe_targets = build_probe_targets(entries)
    LOG.info("full check starting entries=%d input=%s", len(probe_targets), RAW_PLAYLIST_PATH)
    probe_results, stats = await probe_channels(probe_targets, ProbeSettings.from_env())

    valid_urls = {result.channel for result in probe_results if result.valid}
    offline_urls = {result.channel for result in probe_results if not result.valid}

    LOG.info("full check complete stats=%s offline=%d", stats.as_dict(), len(offline_urls))
    candidate_content = build_candidate_playlist(entries, valid_urls)
    _publish_candidate(candidate_content)

    return CycleState(
        entries=entries,
        targets_by_url={target.url: target for target in probe_targets},
        known_valid_urls=valid_urls,
        pending_offline_urls=offline_urls,
    )

async def run_once() -> bool:
    """Backward-compatible single full check entrypoint used by tests."""
    state = await run_full_check()
    return state is not None


async def run_recovery_check(state: CycleState) -> None:
    if not state.pending_offline_urls:
        LOG.info("recovery check skipped: no offline channels pending")
        return

    pending_targets = [
        state.targets_by_url.get(url, ProbeTarget(url=url))
        for url in sorted(state.pending_offline_urls)
    ]
    LOG.info("recovery check starting pending=%d", len(pending_targets))
    for target in pending_targets:
        LOG.debug("recovery_retry_target %s", format_probe_target(target))

    probe_results, stats = await probe_channels(pending_targets, ProbeSettings.from_env())
    recovered = {result.channel for result in probe_results if result.valid}
    still_offline = {result.channel for result in probe_results if not result.valid}

    state.known_valid_urls.update(recovered)
    state.pending_offline_urls = still_offline

    LOG.info(
        "recovery check complete tested=%d recovered=%d still_offline=%d stats=%s",
        len(probe_results),
        len(recovered),
        len(still_offline),
        stats.as_dict(),
    )

    if recovered:
        for url in sorted(recovered):
            target = state.targets_by_url.get(url, ProbeTarget(url=url))
            LOG.info("recovery_channel_restored %s", format_probe_target(target))
        candidate_content = build_candidate_playlist(state.entries, state.known_valid_urls)
        _publish_candidate(candidate_content)


def _safe_run_full_check() -> CycleState | None:
    try:
        return asyncio.run(run_full_check())
    except Exception:  # noqa: BLE001
        LOG.exception("run_full_check failed")
        return None


def _safe_run_recovery_check(state: CycleState) -> None:
    try:
        asyncio.run(run_recovery_check(state))
    except Exception:  # noqa: BLE001
        LOG.exception("run_recovery_check failed")


def _run_cycle_with_extra_delays(extra_run_offsets_seconds: list[float]) -> None:
    state = _safe_run_full_check()
    if state is None:
        return

    cycle_started_at = time.monotonic()
    for target_offset in extra_run_offsets_seconds:
        elapsed_since_cycle_start = time.monotonic() - cycle_started_at
        sleep_seconds = max(0.0, target_offset - elapsed_since_cycle_start)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

        LOG.info(
            "starting extra recovery run offset_minutes=%.2f pending_offline=%d",
            target_offset / 60.0,
            len(state.pending_offline_urls),
        )
        _safe_run_recovery_check(state)


def main() -> None:
    extra_run_offsets_seconds = parse_extra_run_offsets_seconds(EXTRA_RUN_DELAYS_MINUTES_RAW)
    full_check_time = parse_full_check_time(FULL_CHECK_TIME)

    LOG.info(
        "scheduler configured full_check_time=%02d:%02d extra_run_offsets_minutes=%s",
        full_check_time[0],
        full_check_time[1],
        [round(offset / 60.0, 3) for offset in extra_run_offsets_seconds],
    )

    if not should_run_immediately_on_start():
        sleep_seconds = seconds_until_next_full_check_time(datetime.now().astimezone(), full_check_time)
        LOG.info("initial full check scheduled in %.0f seconds", sleep_seconds)
        time.sleep(sleep_seconds)

    while True:
        _run_cycle_with_extra_delays(extra_run_offsets_seconds)
        sleep_seconds = seconds_until_next_full_check_time(datetime.now().astimezone(), full_check_time)
        LOG.info("next full check scheduled in %.0f seconds", sleep_seconds)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
