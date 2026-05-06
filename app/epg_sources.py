from __future__ import annotations

import base64
import gzip
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib import request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_EPGPW_TIMEZONE = "Asia/Jerusalem"


def canonicalize_epg_source_url(source_url: str) -> str:
    source_url = source_url.strip()
    parts = urlsplit(source_url)
    if not _is_epgpw_host(parts.hostname or ""):
        return source_url

    channel_id = _extract_epgpw_channel_id(parts.path, parts.query)
    if not channel_id:
        return source_url

    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    lang = str(params.get("lang") or "en").strip() or "en"
    timezone_name = _decode_timezone_param(
        str(
            params.get("timezone")
            or os.getenv("EPGPW_TIMEZONE")
            or os.getenv("TZ")
            or DEFAULT_EPGPW_TIMEZONE
        )
    )
    query = urlencode(
        {
            "channel_id": channel_id,
            "lang": lang,
            "timezone": _encode_timezone_param(timezone_name),
        }
    )
    return urlunsplit(("https", "epg.pw", "/api/epg.xml", query, ""))


def effective_epg_source_url(
    source_url: str,
    *,
    now: datetime | None = None,
) -> str:
    canonical = canonicalize_epg_source_url(source_url)
    parts = urlsplit(canonical)
    if not _is_epgpw_host(parts.hostname or ""):
        return source_url.strip()
    if parts.path != "/api/epg.xml":
        return canonical

    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    channel_id = str(params.get("channel_id") or "").strip()
    if not channel_id:
        return canonical

    timezone_name = _decode_timezone_param(str(params.get("timezone") or ""))
    query = urlencode(
        {
            "channel_id": channel_id,
            "lang": str(params.get("lang") or "en"),
            "timezone": _encode_timezone_param(timezone_name or DEFAULT_EPGPW_TIMEZONE),
            "date": _today_for_timezone(timezone_name, now),
        }
    )
    return urlunsplit(("https", "epg.pw", "/api/epg.xml", query, ""))


def download_epg_source(source_url: str, destination: Path) -> str:
    effective_url = effective_epg_source_url(source_url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        with request.urlopen(effective_url, timeout=180) as response:
            with temp_path.open("wb") as output_fh:
                shutil.copyfileobj(response, output_fh)
        _validate_xmltv_source(temp_path)
        os.replace(temp_path, destination)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return effective_url


def search_epgpw_channels(
    query: str,
    *,
    lang: str = "en",
    timeout: int = 15,
) -> list[dict[str, str]]:
    query = query.strip()
    if not query:
        return []

    encoded_query = base64.b64encode(query.encode("utf-8")).decode("ascii")
    timezone_name = _decode_timezone_param(
        os.getenv("EPGPW_TIMEZONE") or os.getenv("TZ") or DEFAULT_EPGPW_TIMEZONE
    )
    encoded_tz = _encode_timezone_param(timezone_name)
    params = urlencode({"lang": lang, "timezone": encoded_tz})
    search_url = f"https://epg.pw/search/channel/{encoded_query}.html?{params}"

    try:
        with request.urlopen(search_url, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
    except OSError:
        return []

    results: list[dict[str, str]] = []
    for m in re.finditer(r"/last/(\d+)\.html.*?>([^<]+)", html, re.DOTALL):
        channel_id = m.group(1)
        raw_name = m.group(2).strip()
        display_name = raw_name
        country = ""
        name_match = re.match(r"(.+)\[(.+)\]$", raw_name)
        if name_match:
            display_name = name_match.group(1).strip()
            country = name_match.group(2).strip()
        results.append(
            {
                "channel_id": channel_id,
                "display_name": display_name,
                "country": country,
            }
        )
    return results


def _is_epgpw_host(host: str) -> bool:
    return host.casefold() in {"epg.pw", "www.epg.pw"}


def _extract_epgpw_channel_id(path: str, query: str) -> str:
    params = dict(parse_qsl(query, keep_blank_values=True))
    channel_id = str(params.get("channel_id") or "").strip()
    if channel_id.isdigit():
        return channel_id

    parts = [part for part in path.strip("/").split("/") if part]
    if len(parts) >= 2 and parts[0] in {"last", "channels"}:
        candidate = parts[1].removesuffix(".html")
        if candidate.isdigit():
            return candidate
    return ""


def _encode_timezone_param(timezone_name: str) -> str:
    return base64.b64encode(timezone_name.encode("utf-8")).decode("ascii")


def _decode_timezone_param(value: str) -> str:
    value = value.strip()
    if not value:
        return DEFAULT_EPGPW_TIMEZONE
    if _is_known_timezone(value):
        return value
    try:
        padded = value + ("=" * (-len(value) % 4))
        decoded = base64.b64decode(padded, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return value
    return decoded or DEFAULT_EPGPW_TIMEZONE


def _is_known_timezone(value: str) -> bool:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return False
    return True


def _today_for_timezone(timezone_name: str, now: datetime | None) -> str:
    try:
        zone = ZoneInfo(timezone_name or DEFAULT_EPGPW_TIMEZONE)
    except ZoneInfoNotFoundError:
        zone = timezone.utc
    if now is None:
        local_now = datetime.now(zone)
    elif now.tzinfo is None:
        local_now = now.replace(tzinfo=zone)
    else:
        local_now = now.astimezone(zone)
    return local_now.strftime("%Y%m%d")


def _validate_xmltv_source(path: Path) -> None:
    opener = gzip.open if _looks_gzip(path) else open
    root = None
    saw_channel = False
    with opener(path, "rb") as fh:
        for event, element in ET.iterparse(fh, events=("start", "end")):
            if event == "start" and root is None:
                root = element
                if element.tag != "tv":
                    raise ValueError("EPG source root must be tv")
                continue
            if event != "end":
                continue
            if element.tag == "channel":
                saw_channel = True
            element.clear()
            if root is not None and root is not element:
                root.clear()
    if root is None:
        raise ValueError("EPG source is empty")
    if not saw_channel:
        raise ValueError("EPG source has no channel nodes")


def _looks_gzip(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"
