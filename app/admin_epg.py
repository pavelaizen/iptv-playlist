from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.epg import trim_xmltv_with_source_strategies
from app.epg_worker import download_epg


@dataclass(frozen=True)
class EpgSyncResult:
    changed: bool
    matched_channels: int
    programmes: int
    failed_sources: list[str]


def sync_epg(
    *,
    published_channels: list[dict[str, object]],
    epg_sources: list[dict[str, object]],
    output_path: Path,
    work_dir: Path,
) -> EpgSyncResult:
    work_dir.mkdir(parents=True, exist_ok=True)

    source_paths: dict[str, Path] = {}
    failed_sources: list[str] = []

    for source in epg_sources:
        if not bool(source.get("enabled", True)):
            continue

        source_id = int(source["id"])
        source_url = str(source["source_url"])
        source_key = f"source-{source_id}"
        source_path = work_dir / f"{source_key}.xml.gz"

        try:
            download_epg(source_url, source_path)
        except Exception:  # noqa: BLE001 - degraded source behavior by design
            failed_sources.append(source_url)
            continue

        source_paths[source_key] = source_path

    default_source_order = [
        f"source-{int(source['id'])}"
        for source in epg_sources
        if bool(source.get("enabled", True)) and f"source-{int(source['id'])}" in source_paths
    ]

    summary = trim_xmltv_with_source_strategies(
        published_channels=published_channels,
        sources=source_paths,
        default_source_order=default_source_order,
        output_xmltv_path=output_path,
    )

    return EpgSyncResult(
        changed=True,
        matched_channels=summary.matched_channel_count,
        programmes=summary.programme_count,
        failed_sources=failed_sources,
    )
