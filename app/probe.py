"""Async ffprobe worker utilities for channel validation.

A channel is considered valid when ffprobe exits with status 0 and returns at
least one stream object in its JSON response.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any


DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_CONCURRENCY = 20
DEFAULT_RETRIES = 1
DEFAULT_RETRY_DELAY_SECONDS = 1.0


LOGGER = logging.getLogger(__name__)




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


@dataclass(frozen=True, slots=True)
class ProbeTarget:
    url: str
    name: str = "unnamed-channel"
    fingerprint: str = "unknown"


@dataclass(slots=True)
class ProbeResult:
    channel: str
    valid: bool
    timeout: bool = False
    retries_used: int = 0
    error: str | None = None
    channel_name: str = "unnamed-channel"
    channel_fingerprint: str = "unknown"


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


def _normalize_probe_target(channel: str | ProbeTarget) -> ProbeTarget:
    if isinstance(channel, ProbeTarget):
        return channel
    return ProbeTarget(url=channel)


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
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        return False, True, f"timeout after {timeout_seconds}s"
    except OSError as exc:
        return False, False, str(exc)

    stderr_text = stderr.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        return False, False, stderr_text or "ffprobe failed"

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        return False, False, f"invalid json: {exc}"

    if _is_valid_probe_payload(payload):
        return True, False, None

    return False, False, "no streams returned"


async def probe_channel(channel: str | ProbeTarget, settings: ProbeSettings) -> ProbeResult:
    """Probe a single channel with retry support."""
    target = _normalize_probe_target(channel)
    attempts = settings.retries + 1
    last_error: str | None = None
    saw_timeout = False

    for attempt in range(attempts):
        attempt_number = attempt + 1
        LOGGER.debug(
            "probe_attempt_start name=%r fingerprint=%s attempt=%d/%d",
            target.name,
            target.fingerprint,
            attempt_number,
            attempts,
        )
        valid, is_timeout, error = await _run_ffprobe(target.url, settings.timeout_seconds)
        if valid:
            LOGGER.debug(
                "probe_attempt_success name=%r fingerprint=%s attempt=%d/%d retries_used=%d",
                target.name,
                target.fingerprint,
                attempt_number,
                attempts,
                attempt,
            )
            return ProbeResult(
                channel=target.url,
                valid=True,
                timeout=False,
                retries_used=attempt,
                error=None,
                channel_name=target.name,
                channel_fingerprint=target.fingerprint,
            )

        saw_timeout = saw_timeout or is_timeout
        last_error = error

        if attempt < attempts - 1:
            LOGGER.debug(
                "probe_retry_scheduled name=%r fingerprint=%s attempt=%d/%d timeout=%s delay_seconds=%.3f error=%r",
                target.name,
                target.fingerprint,
                attempt_number,
                attempts,
                is_timeout,
                settings.retry_delay_seconds,
                error,
            )
            if settings.retry_delay_seconds > 0:
                await asyncio.sleep(settings.retry_delay_seconds)

    LOGGER.warning(
        "probe_attempt_failed name=%r fingerprint=%s attempts=%d timeout=%s error=%r",
        target.name,
        target.fingerprint,
        attempts,
        saw_timeout,
        last_error,
    )

    return ProbeResult(
        channel=target.url,
        valid=False,
        timeout=saw_timeout,
        retries_used=settings.retries,
        error=last_error,
        channel_name=target.name,
        channel_fingerprint=target.fingerprint,
    )


async def probe_channels(
    channels: list[str | ProbeTarget],
    settings: ProbeSettings | None = None,
) -> tuple[list[ProbeResult], ProbeStats]:
    """Probe channels with bounded concurrency and return results + stats."""
    cfg = settings or ProbeSettings.from_env()
    normalized_channels = [_normalize_probe_target(channel) for channel in channels]
    sem = asyncio.Semaphore(cfg.concurrency)

    LOGGER.info(
        "probe_batch_start channels=%d timeout_seconds=%.3f concurrency=%d retries=%d retry_delay_seconds=%.3f",
        len(normalized_channels),
        cfg.timeout_seconds,
        cfg.concurrency,
        cfg.retries,
        cfg.retry_delay_seconds,
    )

    async def _bounded_probe(channel: ProbeTarget) -> ProbeResult:
        async with sem:
            return await probe_channel(channel, cfg)

    results = await asyncio.gather(*(_bounded_probe(ch) for ch in normalized_channels))

    stats = ProbeStats(total_channels=len(normalized_channels))
    for result in results:
        if result.valid:
            stats.valid += 1
            if result.retries_used > 0:
                stats.retry_success += 1
        else:
            stats.invalid += 1
            if result.timeout:
                stats.timeout += 1

    LOGGER.info(
        "probe_batch_complete total_channels=%d valid=%d invalid=%d timeout=%d retry_success=%d",
        stats.total_channels,
        stats.valid,
        stats.invalid,
        stats.timeout,
        stats.retry_success,
    )

    return results, stats
