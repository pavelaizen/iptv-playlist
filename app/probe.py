"""Async ffprobe worker utilities for channel validation.

A channel is considered valid when ffprobe exits with status 0 and returns at
least one stream object in its JSON response.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CONCURRENCY = 20
DEFAULT_RETRIES = 1
DEFAULT_RETRY_DELAY_SECONDS = 1.0


@dataclass(slots=True)
class ProbeSettings:
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    concurrency: int = DEFAULT_CONCURRENCY
    retries: int = DEFAULT_RETRIES
    retry_delay_seconds: float = DEFAULT_RETRY_DELAY_SECONDS

    @classmethod
    def from_env(cls) -> "ProbeSettings":
        """Create settings from environment variables."""
        return cls(
            timeout_seconds=max(
                0.1,
                float(os.getenv("PROBE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)),
            ),
            concurrency=max(
                1,
                int(os.getenv("PROBE_CONCURRENCY", DEFAULT_CONCURRENCY)),
            ),
            retries=max(0, int(os.getenv("PROBE_RETRIES", DEFAULT_RETRIES))),
            retry_delay_seconds=max(
                0.0,
                float(
                    os.getenv(
                        "PROBE_RETRY_DELAY_SECONDS", DEFAULT_RETRY_DELAY_SECONDS
                    )
                ),
            ),
        )


@dataclass(slots=True)
class ProbeResult:
    channel: str
    valid: bool
    timeout: bool = False
    retries_used: int = 0
    error: str | None = None


@dataclass(slots=True)
class ProbeStats:
    total_channels: int = 0
    valid: int = 0
    invalid: int = 0
    timeout: int = 0
    retry_success: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "total_channels": self.total_channels,
            "valid": self.valid,
            "invalid": self.invalid,
            "timeout": self.timeout,
            "retry_success": self.retry_success,
        }


def _is_valid_probe_payload(payload: dict[str, Any]) -> bool:
    streams = payload.get("streams")
    return isinstance(streams, list) and len(streams) > 0


async def _run_ffprobe(channel: str, timeout_seconds: float) -> tuple[bool, bool, str | None]:
    cmd = (
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        channel,
    )
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        return False, True, f"timeout after {timeout_seconds}s"
    except OSError as exc:
        return False, False, str(exc)

    if proc.returncode != 0:
        return False, False, stderr.decode("utf-8", errors="replace").strip() or "ffprobe failed"

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return False, False, f"invalid json: {exc}"

    if _is_valid_probe_payload(payload):
        return True, False, None

    return False, False, "no streams returned"


async def probe_channel(channel: str, settings: ProbeSettings) -> ProbeResult:
    """Probe a single channel with retry support."""
    attempts = settings.retries + 1
    last_error: str | None = None
    saw_timeout = False

    for attempt in range(attempts):
        valid, is_timeout, error = await _run_ffprobe(channel, settings.timeout_seconds)
        if valid:
            return ProbeResult(
                channel=channel,
                valid=True,
                timeout=False,
                retries_used=attempt,
                error=None,
            )

        saw_timeout = saw_timeout or is_timeout
        last_error = error

        if attempt < attempts - 1 and settings.retry_delay_seconds > 0:
            await asyncio.sleep(settings.retry_delay_seconds)

    return ProbeResult(
        channel=channel,
        valid=False,
        timeout=saw_timeout,
        retries_used=settings.retries,
        error=last_error,
    )


async def probe_channels(
    channels: list[str],
    settings: ProbeSettings | None = None,
) -> tuple[list[ProbeResult], ProbeStats]:
    """Probe channels with bounded concurrency and return results + stats."""
    cfg = settings or ProbeSettings.from_env()
    sem = asyncio.Semaphore(cfg.concurrency)

    async def _bounded_probe(channel: str) -> ProbeResult:
        async with sem:
            return await probe_channel(channel, cfg)

    results = await asyncio.gather(*(_bounded_probe(ch) for ch in channels))

    stats = ProbeStats(total_channels=len(channels))
    for result in results:
        if result.valid:
            stats.valid += 1
            if result.retries_used > 0:
                stats.retry_success += 1
        else:
            stats.invalid += 1
            if result.timeout:
                stats.timeout += 1

    return results, stats
