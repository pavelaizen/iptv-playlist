#!/usr/bin/env python3
"""Playlist sanitizer and publisher runtime.

Flow:
1) Parse raw M3U entries.
2) Probe channels asynchronously.
3) Build clean candidate playlist.
4) Apply publish guardrails against previous clean playlist.
5) Optionally trigger Emby Live TV refresh after successful publish.
6) Write state timestamp for health checks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
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


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


async def run_once() -> bool:
    if not RAW_PLAYLIST_PATH.exists():
        LOG.error("input playlist missing: %s", RAW_PLAYLIST_PATH)
        return False

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    candidate_output_path = OUTPUT_DIR / OUTPUT_PLAYLIST_NAME
    previous_clean_path = OUTPUT_DIR / PREVIOUS_CLEAN_PLAYLIST_NAME

    entries = parse_m3u(RAW_PLAYLIST_PATH)
    urls = [url.strip() for _, url in entries]

    probe_results, stats = await probe_channels(urls, ProbeSettings.from_env())
    valid_urls = {result.channel for result in probe_results if result.valid}

    candidate_content = build_candidate_playlist(entries, valid_urls)
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
        "run complete stats=%s guard=%s candidate_valid=%d previous_valid=%d required_minimum=%d selected=%s",
        stats.as_dict(),
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
    return True


def _safe_run_once() -> None:
    try:
        asyncio.run(run_once())
    except Exception:  # noqa: BLE001 - keep service alive, log and retry next cycle.
        LOG.exception("run_once failed")


def main() -> None:
    _safe_run_once()
    interval_seconds = max(60.0, RUN_INTERVAL_HOURS * 3600.0)

    while True:
        time.sleep(interval_seconds)
        _safe_run_once()


if __name__ == "__main__":
    main()
