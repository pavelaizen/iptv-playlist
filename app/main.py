#!/usr/bin/env python3
"""Playlist sanitizer and publisher runtime."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.emby_client import refresh_livetv_after_publish
from app.probe import ProbeSettings, probe_channels
from app.publish import PublishGuardConfig, select_playlist_for_publish

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("playlist-runtime")

RAW_PLAYLIST_PATH = Path(os.getenv("RAW_PLAYLIST_PATH", "/data/input/playlist.m3u"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/data/output"))
OUTPUT_PLAYLIST_NAME = os.getenv("OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u")
PREVIOUS_CLEAN_PLAYLIST_NAME = os.getenv("PREVIOUS_CLEAN_PLAYLIST_NAME", OUTPUT_PLAYLIST_NAME)
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/output/.playlist_sanitizer_state"))
RUN_INTERVAL_HOURS = float(os.getenv("RUN_INTERVAL_HOURS", "24"))
DIAGNOSTICS_DIR = Path(os.getenv("DIAGNOSTICS_DIR", str(OUTPUT_DIR / "diagnostics")))
MIN_VALID_CHANNELS_ABSOLUTE = int(os.getenv("MIN_VALID_CHANNELS_ABSOLUTE", "1"))
MIN_VALID_RATIO_OF_PREVIOUS = float(os.getenv("MIN_VALID_RATIO_OF_PREVIOUS", "0.7"))
EXTRA_RUN_DELAYS_MINUTES_RAW = os.getenv("EXTRA_RUN_DELAYS_MINUTES", "30,60,240")


@dataclass(slots=True)
class CycleState:
    entries: list[tuple[list[str], str]]
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


def parse_m3u(path: Path) -> list[tuple[list[str], str]]:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    entries: list[tuple[list[str], str]] = []
    pending_meta: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped:
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
        "publish complete guard=%s candidate_valid=%d previous_valid=%d required_minimum=%d selected=%s",
        guard_decision.reason,
        guard_decision.candidate_valid_channels,
        guard_decision.previous_valid_channels,
        guard_decision.required_minimum,
        guard_decision.selected_path,
    )

    if guard_decision.publish_candidate:
        warning = refresh_livetv_after_publish(LOG)
        if warning:
            LOG.warning(warning)

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(now_iso() + "\n", encoding="utf-8")
    return guard_decision.publish_candidate


async def run_full_check() -> CycleState | None:
    if not RAW_PLAYLIST_PATH.exists():
        LOG.error("input playlist missing: %s", RAW_PLAYLIST_PATH)
        return None

    entries = parse_m3u(RAW_PLAYLIST_PATH)
    urls = [url.strip() for _, url in entries]
    probe_results, stats = await probe_channels(urls, ProbeSettings.from_env())

    valid_urls = {result.channel for result in probe_results if result.valid}
    offline_urls = {result.channel for result in probe_results if not result.valid}

    LOG.info("full check complete stats=%s offline=%d", stats.as_dict(), len(offline_urls))
    candidate_content = build_candidate_playlist(entries, valid_urls)
    _publish_candidate(candidate_content)

    return CycleState(
        entries=entries,
        known_valid_urls=valid_urls,
        pending_offline_urls=offline_urls,
    )


async def run_recovery_check(state: CycleState) -> None:
    if not state.pending_offline_urls:
        LOG.info("recovery check skipped: no offline channels pending")
        return

    probe_results, stats = await probe_channels(sorted(state.pending_offline_urls), ProbeSettings.from_env())
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

    elapsed_since_cycle_start = 0.0
    for target_offset in extra_run_offsets_seconds:
        sleep_seconds = max(0.0, target_offset - elapsed_since_cycle_start)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
            elapsed_since_cycle_start += sleep_seconds

        LOG.info(
            "starting extra recovery run offset_minutes=%.2f pending_offline=%d",
            target_offset / 60.0,
            len(state.pending_offline_urls),
        )
        _safe_run_recovery_check(state)


def main() -> None:
    interval_seconds = max(60.0, RUN_INTERVAL_HOURS * 3600.0)
    extra_run_offsets_seconds = parse_extra_run_offsets_seconds(EXTRA_RUN_DELAYS_MINUTES_RAW)

    LOG.info(
        "scheduler configured base_interval_hours=%.2f extra_run_offsets_minutes=%s",
        RUN_INTERVAL_HOURS,
        [round(offset / 60.0, 3) for offset in extra_run_offsets_seconds],
    )

    while True:
        cycle_started_at = time.monotonic()
        _run_cycle_with_extra_delays(extra_run_offsets_seconds)
        elapsed = time.monotonic() - cycle_started_at
        sleep_seconds = max(0.0, interval_seconds - elapsed)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
