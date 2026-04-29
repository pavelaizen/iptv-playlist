"""Playlist publishing helpers with channel-count guardrails."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import logging

LOGGER = logging.getLogger(__name__)

MIN_VALID_CHANNELS_ABSOLUTE = 1
MIN_VALID_RATIO_OF_PREVIOUS = 0.7


@dataclass(frozen=True)
class PublishGuardConfig:
    """Guard thresholds used to validate a new clean playlist."""

    min_valid_channels_absolute: int = MIN_VALID_CHANNELS_ABSOLUTE
    min_valid_ratio_of_previous: float = MIN_VALID_RATIO_OF_PREVIOUS
    diagnostics_dir: Path | None = None


@dataclass(frozen=True)
class GuardDecision:
    """Result from evaluating whether a candidate clean playlist can be published."""

    publish_candidate: bool
    selected_path: Path
    candidate_valid_channels: int
    previous_valid_channels: int
    required_minimum: int
    reason: str
    diagnostic_path: Path | None = None


def count_valid_channels(lines: Iterable[str]) -> int:
    """Count channels in M3U content using EXTINF records."""

    return sum(1 for line in lines if line.startswith("#EXTINF"))


def _calculate_required_minimum(
    previous_valid_channels: int,
    config: PublishGuardConfig,
) -> int:
    ratio_floor = int(previous_valid_channels * config.min_valid_ratio_of_previous)
    return max(config.min_valid_channels_absolute, ratio_floor)


def _write_diagnostic_file(
    *,
    failed_output_path: Path,
    candidate_content: str,
    diagnostics_dir: Path,
) -> Path:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    diag_path = diagnostics_dir / f"{failed_output_path.stem}.guard-failed.{timestamp}{failed_output_path.suffix}"
    diag_path.write_text(candidate_content, encoding="utf-8")
    return diag_path


def select_playlist_for_publish(
    *,
    candidate_output_path: Path,
    previous_clean_path: Path,
    candidate_content: str,
    config: PublishGuardConfig,
) -> GuardDecision:
    """Apply guardrails and choose whether to publish candidate or keep previous clean file."""

    candidate_valid_channels = count_valid_channels(candidate_content.splitlines())
    previous_content = previous_clean_path.read_text(encoding="utf-8") if previous_clean_path.exists() else ""
    previous_valid_channels = count_valid_channels(previous_content.splitlines())

    required_minimum = _calculate_required_minimum(previous_valid_channels, config)

    if candidate_valid_channels >= required_minimum:
        candidate_output_path.write_text(candidate_content, encoding="utf-8")
        return GuardDecision(
            publish_candidate=True,
            selected_path=candidate_output_path,
            candidate_valid_channels=candidate_valid_channels,
            previous_valid_channels=previous_valid_channels,
            required_minimum=required_minimum,
            reason="candidate_passed_guard",
        )

    diagnostic_path: Path | None = None
    if config.diagnostics_dir:
        diagnostic_path = _write_diagnostic_file(
            failed_output_path=candidate_output_path,
            candidate_content=candidate_content,
            diagnostics_dir=config.diagnostics_dir,
        )

    if previous_clean_path.exists():
        candidate_output_path.write_text(previous_content, encoding="utf-8")

    LOGGER.warning(
        "playlist_publish_guard_failed",
        extra={
            "event": "playlist_publish_guard_failed",
            "candidate_valid_channels": candidate_valid_channels,
            "previous_valid_channels": previous_valid_channels,
            "required_minimum": required_minimum,
            "min_valid_channels_absolute": config.min_valid_channels_absolute,
            "min_valid_ratio_of_previous": config.min_valid_ratio_of_previous,
            "diagnostic_path": str(diagnostic_path) if diagnostic_path else None,
        },
    )

    return GuardDecision(
        publish_candidate=False,
        selected_path=previous_clean_path if previous_clean_path.exists() else candidate_output_path,
        candidate_valid_channels=candidate_valid_channels,
        previous_valid_channels=previous_valid_channels,
        required_minimum=required_minimum,
        reason="candidate_below_threshold",
        diagnostic_path=diagnostic_path,
    )
