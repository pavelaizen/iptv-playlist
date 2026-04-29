#!/usr/bin/env python3
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

state_file = Path(os.getenv("STATE_FILE", "/data/output/.playlist_sanitizer_state"))
run_interval_hours = float(os.getenv("RUN_INTERVAL_HOURS", "24"))
max_age = timedelta(hours=max(run_interval_hours * 2, 1))

if not state_file.exists():
    print(f"state file missing: {state_file}")
    sys.exit(1)

try:
    raw = state_file.read_text(encoding="utf-8").strip()
    ts = datetime.fromisoformat(raw)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
    if age > max_age:
        print(f"stale last-successful-run timestamp: {raw}")
        sys.exit(1)
    print(f"healthy last-successful-run={raw}")
    sys.exit(0)
except Exception as e:
    print(f"invalid state file: {e}")
    sys.exit(1)
