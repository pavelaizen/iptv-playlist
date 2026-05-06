from __future__ import annotations

import re
from pathlib import Path

from app.admin_models import ChannelSnapshot
from app.main import parse_m3u

ATTR_RE = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')


def import_playlist_entries(path: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for metadata_lines, url in parse_m3u(path):
        extinf_line = next(
            (line.strip() for line in metadata_lines if line.strip().upper().startswith("#EXTINF")),
            "",
        )
        _, _, channel_name = extinf_line.partition(",")
        attrs = dict(ATTR_RE.findall(extinf_line))
        extgrp_group = next(
            (
                line.strip().split(":", 1)[1].strip()
                for line in metadata_lines
                if line.strip().upper().startswith("#EXTGRP:")
            ),
            "",
        )
        rows.append(
            {
                "name": channel_name.strip() or "unnamed-channel",
                "group_name": attrs.get("group-title", "") or extgrp_group,
                "stream_url": url.strip(),
                "tvg_id": attrs.get("tvg-id", ""),
                "tvg_name": attrs.get("tvg-name", ""),
                "tvg_logo": attrs.get("tvg-logo", ""),
                "tvg_rec": attrs.get("tvg-rec", ""),
            }
        )
    return rows


def _render_extinf(snapshot: ChannelSnapshot) -> str:
    attributes: list[str] = []
    if snapshot.tvg_id:
        attributes.append(f'tvg-id="{snapshot.tvg_id}"')
    if snapshot.tvg_rec:
        attributes.append(f'tvg-rec="{snapshot.tvg_rec}"')
    if snapshot.tvg_logo:
        attributes.append(f'tvg-logo="{snapshot.tvg_logo}"')
    attr_text = f" {' '.join(attributes)}" if attributes else ""
    return f"#EXTINF:0{attr_text},{snapshot.name}"


def render_channel_entry(snapshot: ChannelSnapshot) -> list[str]:
    lines = [_render_extinf(snapshot)]
    if snapshot.group_name:
        lines.append(f"#EXTGRP:{snapshot.group_name}")
    lines.append(snapshot.stream_url)
    return lines


def render_playlist(snapshots: list[ChannelSnapshot]) -> str:
    lines = ["#EXTM3U"]
    for snapshot in snapshots:
        lines.extend(render_channel_entry(snapshot))
    return "\n".join(lines) + "\n"
