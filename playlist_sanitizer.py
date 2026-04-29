#!/usr/bin/env python3
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

RAW_PLAYLIST_PATH = Path(os.getenv("RAW_PLAYLIST_PATH", "/data/input/playlist.m3u"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/data/output"))
OUTPUT_PLAYLIST_NAME = os.getenv("OUTPUT_PLAYLIST_NAME", "playlist_clean.m3u")
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/output/.playlist_sanitizer_state"))
RUN_INTERVAL_HOURS = float(os.getenv("RUN_INTERVAL_HOURS", "24"))

# Probe settings
PROBE_TIMEOUT_SECONDS = float(os.getenv("PROBE_TIMEOUT_SECONDS", "15"))
PROBE_READ_INTERVAL_US = int(os.getenv("PROBE_READ_INTERVAL_US", "5000000"))
PROBE_USER_AGENT = os.getenv("PROBE_USER_AGENT", "playlist-sanitizer/1.0")
PROBE_FFMPEG_LOGLEVEL = os.getenv("PROBE_FFMPEG_LOGLEVEL", "error")
PROBE_EXTRA_ARGS = os.getenv("PROBE_EXTRA_ARGS", "")

# Emby settings (exposed for compatibility/integration)
EMBY_BASE_URL = os.getenv("EMBY_BASE_URL", "")
EMBY_API_KEY = os.getenv("EMBY_API_KEY", "")
EMBY_LIBRARY_NAME = os.getenv("EMBY_LIBRARY_NAME", "")
EMBY_USER_ID = os.getenv("EMBY_USER_ID", "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_m3u(path: Path):
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    entries = []
    pending_meta = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            pending_meta.append(line)
            continue
        # URL line
        entries.append((pending_meta[:], line))
        pending_meta = []
    return entries


def probe_url(url: str) -> bool:
    cmd = [
        "ffprobe",
        "-v", PROBE_FFMPEG_LOGLEVEL,
        "-rw_timeout", str(int(PROBE_TIMEOUT_SECONDS * 1_000_000)),
        "-analyzeduration", str(PROBE_READ_INTERVAL_US),
        "-user_agent", PROBE_USER_AGENT,
        "-show_streams",
        "-select_streams", "v:0",
        "-of", "compact=p=0:nk=1",
        url,
    ]
    if PROBE_EXTRA_ARGS.strip():
        cmd[1:1] = re.split(r"\s+", PROBE_EXTRA_ARGS.strip())

    try:
        completed = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS + 3,
            check=False,
        )
        return completed.returncode == 0 and bool(completed.stdout.strip())
    except Exception:
        return False


def run_once() -> bool:
    if not RAW_PLAYLIST_PATH.exists():
        print(f"Input playlist does not exist: {RAW_PLAYLIST_PATH}")
        return False

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = OUTPUT_DIR / OUTPUT_PLAYLIST_NAME

    entries = parse_m3u(RAW_PLAYLIST_PATH)
    kept = []
    for meta, url in entries:
        if probe_url(url.strip()):
            kept.append((meta, url))

    with output_path.open("w", encoding="utf-8") as f:
        f.write("#EXTM3U\n")
        for meta, url in kept:
            for m in meta:
                f.write(f"{m}\n")
            f.write(f"{url}\n")

    timestamp = now_iso()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(timestamp + "\n", encoding="utf-8")
    print(f"run completed: kept={len(kept)}/{len(entries)} at {timestamp}")
    return True


def main():
    # immediate run on startup
    run_once()

    interval_seconds = max(60.0, RUN_INTERVAL_HOURS * 3600.0)
    while True:
        time.sleep(interval_seconds)
        run_once()


if __name__ == "__main__":
    main()
