from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import gzip
import json
import os
import tempfile
import xml.etree.ElementTree as ET

from app.epg import trim_xmltv_with_source_strategies
from app.epg_sources import download_epg_source


@dataclass(frozen=True)
class EpgSyncResult:
    changed: bool
    matched_channels: int
    programmes: int
    failed_sources: list[str]
    channel_icons: dict[int, str]


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
        source_path = work_dir / f"{source_key}.xmltv"

        try:
            download_epg_source(source_url, source_path)
        except Exception:  # noqa: BLE001 - degraded source behavior by design
            failed_sources.append(source_url)
            cached_path = _cached_source_path(source_id, source_url, work_dir)
            if cached_path is None:
                continue
            source_path = cached_path

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

    _inject_channel_icons(output_path, published_channels)

    icons = collect_channel_epg_icons(published_channels, work_dir)
    if icons:
        _save_epg_icon_cache(icons, work_dir)
    return EpgSyncResult(
        changed=True,
        matched_channels=summary.matched_channel_count,
        programmes=summary.programme_count,
        failed_sources=failed_sources,
        channel_icons=icons,
    )


def _inject_channel_icons(output_path: Path, published_channels: list[dict[str, object]]) -> None:
    icon_by_xmltv_id: dict[str, str] = {}
    for channel in published_channels:
        tvg_logo = str(channel.get("tvg_logo", "")).strip()
        if not tvg_logo:
            continue
        for mapping in channel.get("mappings", []):
            if not isinstance(mapping, dict):
                continue
            xmltv_id = str(mapping.get("channel_id", "")).strip()
            if xmltv_id:
                icon_by_xmltv_id[xmltv_id] = tvg_logo

    if not icon_by_xmltv_id:
        return

    if not output_path.exists():
        return

    tree = ET.parse(str(output_path))
    root = tree.getroot()
    modified = False
    for channel_el in root.findall("channel"):
        cid = channel_el.get("id", "")
        if cid not in icon_by_xmltv_id:
            continue
        new_icon = icon_by_xmltv_id[cid]
        existing = channel_el.findall("icon")
        if existing and (existing[0].get("src") or "") == new_icon:
            continue
        for icon_el in existing:
            channel_el.remove(icon_el)
        icon_el = ET.SubElement(channel_el, "icon")
        icon_el.set("src", new_icon)
        modified = True

    if modified:
        tree.write(str(output_path), encoding="utf-8", xml_declaration=True)
        os.chmod(str(output_path), 0o644)


def fetch_source(*, source_url: str, source_id: int, work_dir: Path) -> dict[str, object]:
    work_dir.mkdir(parents=True, exist_ok=True)
    source_path = work_dir / f"source-{source_id}.xmltv"
    try:
        download_epg_source(source_url, source_path)
    except Exception:  # noqa: BLE001 - preview should use last loaded source when possible
        cached_path = _cached_source_path(source_id, source_url, work_dir)
        if cached_path is None:
            raise
        source_path = cached_path

    channels = _list_source_channels(source_path)
    return {
        "status": "ok",
        "channel_count": len(channels),
        "channels": channels,
    }


def _cached_source_path(source_id: int, source_url: str, work_dir: Path) -> Path | None:
    candidates = [
        work_dir / f"source-{source_id}.xmltv",
        work_dir / f"source-{source_id}.xml.gz",
    ]
    legacy_name = _legacy_source_filename(source_url)
    if legacy_name:
        candidates.append(work_dir / legacy_name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _legacy_source_filename(source_url: str) -> str:
    if "epg.one" in source_url:
        return "source.xml.gz"
    if "iptvx.one" in source_url:
        return "source_israel_primary.xml.gz"
    if "iptv-epg.org" in source_url:
        return "source_israel_fallback.xml.gz"
    return ""


def _list_source_channels(source_path: Path) -> list[dict[str, object]]:
    opener = gzip.open if _looks_gzip(source_path) else open
    channels: list[dict[str, object]] = []
    with opener(source_path, "rb") as fh:
        for _, element in ET.iterparse(fh, events=("end",)):
            if element.tag != "channel":
                continue
            display_names = [
                child.text.strip()
                for child in element
                if child.tag == "display-name" and child.text and child.text.strip()
            ]
            icon_url = next(
                (
                    child.attrib.get("src", "")
                    for child in element
                    if child.tag == "icon" and child.attrib.get("src")
                ),
                "",
            )
            channels.append(
                {
                    "id": element.attrib.get("id", ""),
                    "display_names": display_names,
                    "icon_url": icon_url,
                }
            )
            element.clear()
    return channels


def collect_channel_epg_icons(
    channels: list[dict[str, object]],
    epg_work_dir: Path,
) -> dict[int, str]:
    wanted: dict[tuple[str, str], int] = {}
    for channel in channels:
        raw_channel_id = channel.get("channel_id")
        if raw_channel_id is None:
            continue
        channel_db_id = int(raw_channel_id)
        for mapping in channel.get("mappings", []):
            if not isinstance(mapping, dict):
                continue
            source_key = str(mapping.get("source_key", ""))
            xmltv_id = str(mapping.get("channel_id", ""))
            if source_key and xmltv_id:
                wanted[(source_key, xmltv_id)] = channel_db_id

    icons: dict[int, str] = {}
    if not wanted:
        return icons

    cached = _load_epg_icon_cache(epg_work_dir)
    if cached is not None:
        for channel_db_id in set(wanted.values()):
            icon = cached.get(str(channel_db_id)) or cached.get(channel_db_id)
            if icon:
                icons[channel_db_id] = str(icon)
        return icons

    return _build_epg_icons_from_sources(wanted, epg_work_dir)


def _build_epg_icons_from_sources(
    wanted: dict[tuple[str, str], int],
    epg_work_dir: Path,
) -> dict[int, str]:
    icons: dict[int, str] = {}
    wanted_by_source: dict[str, set[str]] = {}
    for source_key, xmltv_id in wanted:
        wanted_by_source.setdefault(source_key, set()).add(xmltv_id)

    for source_key, xmltv_ids in wanted_by_source.items():
        source_path = _resolve_source_path(source_key, epg_work_dir)
        if source_path is None:
            continue
        for channel in _list_source_channels(source_path):
            xmltv_id = str(channel["id"])
            if xmltv_id not in xmltv_ids:
                continue
            icon_url = str(channel.get("icon_url", ""))
            if not icon_url:
                continue
            output_channel_id = wanted[(source_key, xmltv_id)]
            icons.setdefault(output_channel_id, icon_url)
    return icons


_ICON_CACHE_FILENAME = "channel_icons.json"


def _load_epg_icon_cache(work_dir: Path) -> dict[str, str] | None:
    cache_path = work_dir / _ICON_CACHE_FILENAME
    if not cache_path.exists():
        return None
    try:
        with cache_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            return {str(k): str(v) for k, v in raw.items()}
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


def _save_epg_icon_cache(icons: dict[int, str], work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_path = work_dir / _ICON_CACHE_FILENAME
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{_ICON_CACHE_FILENAME}.",
        suffix=".tmp",
        dir=work_dir,
    )
    os.close(fd)
    try:
        with Path(tmp_name).open("w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in icons.items()}, fh)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, cache_path)
    except Exception:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _resolve_source_path(source_key: str, work_dir: Path) -> Path | None:
    candidate = work_dir / f"{source_key}.xml.gz"
    if candidate.exists():
        return candidate
    candidate = work_dir / f"{source_key}.xmltv"
    if candidate.exists():
        return candidate
    source_id_str = source_key.removeprefix("source-")
    if source_id_str.isdigit():
        for entry in work_dir.glob(f"source-{int(source_id_str)}.*"):
            if entry.suffix in (".xml.gz", ".xmltv"):
                return entry
    legacy = _legacy_source_filename_by_key(source_key)
    if legacy:
        legacy_path = work_dir / legacy
        if legacy_path.exists():
            return legacy_path
    return None


def _legacy_source_filename_by_key(source_key: str) -> str:
    if source_key == "source-1":
        return "source.xml.gz"
    if source_key == "source-2":
        return "source_israel_primary.xml.gz"
    if source_key == "source-3":
        return "source_israel_fallback.xml.gz"
    return ""


def _looks_gzip(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def _collect_selected_channel_icons(
    published_channels: list[dict[str, object]],
    source_paths: dict[str, Path],
) -> dict[int, str]:
    wanted: dict[tuple[str, str], int] = {}
    for channel in published_channels:
        raw_channel_id = channel.get("channel_id")
        if raw_channel_id is None:
            continue
        for mapping in channel.get("mappings", []):
            if not isinstance(mapping, dict):
                continue
            source_key = str(mapping.get("source_key", ""))
            xmltv_id = str(mapping.get("channel_id", ""))
            if source_key and xmltv_id:
                wanted[(source_key, xmltv_id)] = int(raw_channel_id)

    icons: dict[int, str] = {}
    if not wanted:
        return icons

    wanted_by_source: dict[str, set[str]] = {}
    for source_key, xmltv_id in wanted:
        wanted_by_source.setdefault(source_key, set()).add(xmltv_id)

    for source_key, xmltv_ids in wanted_by_source.items():
        source_path = source_paths.get(source_key)
        if source_path is None:
            continue
        for channel in _list_source_channels(source_path):
            xmltv_id = str(channel["id"])
            if xmltv_id not in xmltv_ids:
                continue
            icon_url = str(channel.get("icon_url", ""))
            if not icon_url:
                continue
            output_channel_id = wanted[(source_key, xmltv_id)]
            icons.setdefault(output_channel_id, icon_url)
    return icons
