from __future__ import annotations

import pytest

from app import admin_runtime


def test_parse_run_time_accepts_hour_and_minute() -> None:
    assert admin_runtime.parse_run_time("04:00") == (4, 0)


def test_parse_run_time_falls_back_for_invalid_values() -> None:
    assert admin_runtime.parse_run_time("bad") == (4, 0)
    assert admin_runtime.parse_run_time("25:99") == (4, 0)


def test_seconds_until_next_run_time_wraps_to_next_day() -> None:
    from datetime import datetime, timezone

    now = datetime(2026, 5, 1, 4, 1, tzinfo=timezone.utc)

    assert admin_runtime.seconds_until_next_run_time(now, (4, 0)) == 23 * 3600 + 59 * 60


def test_scheduler_validates_then_rebuilds_public_outputs(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class Service:
        def validate_all(self, trigger_type: str) -> dict[str, object]:
            calls.append(("validate", trigger_type))
            return {"status": "ok"}

        def rebuild_all_public_outputs(self, trigger_type: str) -> dict[str, object]:
            calls.append(("rebuild", trigger_type))
            raise StopIteration

    monkeypatch.setattr(admin_runtime, "seconds_until_next_run_time", lambda now, run_time: 0)

    sleep_calls = 0

    def stop_after_second_sleep(seconds: float) -> None:
        del seconds
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise StopIteration

    monkeypatch.setattr(admin_runtime.time, "sleep", stop_after_second_sleep)

    with pytest.raises(StopIteration):
        admin_runtime._scheduler_loop(Service(), (4, 0))

    assert calls == [("validate", "scheduled"), ("rebuild", "scheduled")]
