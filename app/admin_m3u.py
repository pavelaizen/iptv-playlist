from __future__ import annotations

import re
from pathlib import Path

from app.main import parse_m3u

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def import_playlist_entries(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for metadata_lines, url in parse_m3u(path):
        extinf_line = next(
            (line.strip() for line in metadata_lines if line.strip().upper().startswith("#EXTINF")),
            "",
        )
        group_line = next(
            (line.strip() for line in metadata_lines if line.strip().upper().startswith("#EXTGRP:")),
            "#EXTGRP:",
        )
        _, _, channel_name = extinf_line.partition(",")
        attrs = dict(ATTR_RE.findall(extinf_line))
        rows.append(
            {
                "name": channel_name.strip() or "unnamed-channel",
                "group_name": group_line.split(":", 1)[1].strip(),
                "stream_url": url.strip(),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "tvg_logo": attrs.get("tvg-logo", ""),
                "tvg_rec": attrs.get("tvg-rec", ""),
            }
        )
    return rows
