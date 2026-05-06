from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass


BAD_PATTERNS = (
    "connection timed out",
    "http error",
    "404 not found",
    "403 forbidden",
    "failed to open",
    "invalid data found",
    "server returned",
    "end of file",
)

DECODE_ERROR_PATTERNS = (
    "error while decoding",
    "invalid data found",
    "invalid nal",
    "missing picture",
    "no frame!",
    "invalid frame",
)

WARN_PATTERNS = (
    "non-monotonous",
    "non monotonically",
    "concealing",
    "packet mismatch",
    "timestamp discontinuity",
)


RETRY_PATTERNS = (
    "connection timed out",
    "ffmpeg timeout",
    "http error",
    "failed to open",
    "server returned",
    "end of file",
)


@dataclass(frozen=True, slots=True)
class StabilitySettings:
    duration_seconds: int = 60
    timeout_padding_seconds: int = 40
    slow_speed_threshold: float = 0.85
    retries: int = 1
    retry_delay_seconds: float = 1.0
    freeze_bad_total_seconds: float = 4.0
    freeze_bad_events: int = 2
    black_bad_total_seconds: float = 3.0

    @classmethod
    def from_env(cls) -> "StabilitySettings":
        return cls(
            duration_seconds=max(1, int(os.getenv("STABILITY_TEST_SECONDS", "60"))),
            timeout_padding_seconds=max(
                1,
                int(os.getenv("STABILITY_TEST_TIMEOUT_PADDING_SECONDS", "40")),
            ),
            retries=max(0, int(os.getenv("STABILITY_TEST_RETRIES", "1"))),
            retry_delay_seconds=max(
                0.0,
                float(os.getenv("STABILITY_RETRY_DELAY_SECONDS", "1.0")),
            ),
        )


@dataclass(frozen=True, slots=True)
class StreamStabilityResult:
    status: str
    frames: int
    speed: str
    issues: str
    returncode: int


def _parse_stream_properties(stderr: str) -> tuple[str, int, int]:
    video_codec = ""
    video_height = 0
    eac3_count = 0

    for line in stderr.splitlines():
        if "->" in line and "Stream #0:" in line:
            break

        m = re.match(r"\s*Stream #0:0[^:]*:\s*Video:\s*(\w+)", line)
        if m and not video_codec:
            video_codec = m.group(1)
            h_match = re.search(r",\s*(\d+)x(\d+)\s*\[", line)
            if h_match:
                video_height = int(h_match.group(2))

        if re.search(r"\bAudio:\s*eac3\b", line):
            eac3_count += 1

    return video_codec, video_height, eac3_count


def _parse_freezes(stderr: str) -> tuple[int, float]:
    events = len(re.findall(r"freeze_start", stderr, re.IGNORECASE))
    durations: list[float] = []
    for m in re.finditer(r"freeze_duration:\s*([0-9.]+)", stderr, re.IGNORECASE):
        try:
            durations.append(float(m.group(1)))
        except ValueError:
            pass
    return events, sum(durations)


def _parse_black(stderr: str) -> float:
    durations: list[float] = []
    for m in re.finditer(r"black_duration:\s*([0-9.]+)", stderr, re.IGNORECASE):
        try:
            durations.append(float(m.group(1)))
        except ValueError:
            pass
    return sum(durations)


def _run_single_ffmpeg_test(
    stream_url: str,
    cfg: StabilitySettings,
) -> StreamStabilityResult:
    vf = "fps=10,scale=640:-2,freezedetect=n=0.003:d=3,blackdetect=d=2:pic_th=0.98"
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel", "info",
        "-rw_timeout",
        "15000000",
        "-analyzeduration",
        "10000000",
        "-probesize",
        "10000000",
        "-re",
        "-t",
        str(cfg.duration_seconds),
        "-i",
        stream_url,
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-vf", vf,
        "-f",
        "null",
        "-",
    ]
    timeout_seconds = cfg.duration_seconds + cfg.timeout_padding_seconds

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return StreamStabilityResult(
            status="BAD",
            frames=0,
            speed="",
            issues="ffmpeg timeout",
            returncode=-1,
        )
    except OSError as exc:
        return StreamStabilityResult(
            status="BAD",
            frames=0,
            speed="",
            issues=str(exc),
            returncode=-1,
        )

    stderr = completed.stderr or ""
    log = stderr.casefold()
    speed = _last_regex_match(r"speed=\s*([0-9.]+)x", stderr)
    frames_text = _last_regex_match(r"frame=\s*([0-9]+)", stderr)
    frames = int(frames_text) if frames_text.isdigit() else 0
    issues: list[str] = []

    for pattern in BAD_PATTERNS:
        if pattern in log:
            issues.append(pattern)
    for pattern in WARN_PATTERNS:
        if pattern in log:
            issues.append(pattern)
    for pattern in DECODE_ERROR_PATTERNS:
        if pattern in log:
            issues.append(pattern)

    status = "GOOD"
    if completed.returncode != 0 or any(pattern in log for pattern in BAD_PATTERNS):
        status = "BAD"
    elif any(pattern in log for pattern in DECODE_ERROR_PATTERNS):
        status = "BAD"
    elif any(pattern in log for pattern in WARN_PATTERNS):
        status = "WARN"

    if speed:
        try:
            if float(speed) < cfg.slow_speed_threshold:
                if status == "GOOD":
                    status = "WARN"
                elif status == "WARN":
                    status = "BAD"
                issues.append(f"slow decode speed={speed}x")
        except ValueError:
            pass

    video_codec, video_height, eac3_count = _parse_stream_properties(stderr)
    if video_codec == "hevc" and video_height >= 2160:
        if status == "GOOD":
            status = "WARN"
        issues.append("4K HEVC stream, heavy client decode load")
    if eac3_count > 1:
        issues.append(f"multiple EAC3 audio tracks ({eac3_count})")

    freeze_events, freeze_total = _parse_freezes(stderr)
    if freeze_events >= cfg.freeze_bad_events or freeze_total >= cfg.freeze_bad_total_seconds:
        status = "BAD"
        issues.append(f"video freezes: {freeze_events} events, {freeze_total:.1f}s total")
    elif freeze_events > 0:
        if status == "GOOD":
            status = "WARN"
        issues.append(f"minor video freeze: {freeze_events} events, {freeze_total:.1f}s total")

    black_total = _parse_black(stderr)
    if black_total >= cfg.black_bad_total_seconds:
        status = "BAD"
        issues.append(f"black screen: {black_total:.1f}s total")
    elif black_total > 0:
        if status == "GOOD":
            status = "WARN"
        issues.append(f"minor black screen: {black_total:.1f}s total")

    return StreamStabilityResult(
        status=status,
        frames=frames,
        speed=speed,
        issues="; ".join(sorted(set(issues))),
        returncode=completed.returncode,
    )


def _has_retryable_issue(log: str) -> bool:
    return log and any(pattern in log for pattern in RETRY_PATTERNS)


def run_stream_stability_test(
    stream_url: str,
    settings: StabilitySettings | None = None,
) -> StreamStabilityResult:
    cfg = settings or StabilitySettings.from_env()
    attempts = cfg.retries + 1
    last_result: StreamStabilityResult | None = None

    for attempt in range(attempts):
        result = _run_single_ffmpeg_test(stream_url, cfg)
        last_result = result

        if result.status != "BAD":
            return result

        if attempt < attempts - 1 and _has_retryable_issue(result.issues):
            if cfg.retry_delay_seconds > 0:
                time.sleep(cfg.retry_delay_seconds)
            continue

        return result

    return last_result or StreamStabilityResult(
        status="BAD",
        frames=0,
        speed="",
        issues="no results",
        returncode=-1,
    )


def _last_regex_match(pattern: str, value: str) -> str:
    matches = re.findall(pattern, value)
    return str(matches[-1]) if matches else ""
