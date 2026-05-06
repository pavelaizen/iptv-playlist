from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ChannelStatus = Literal["new", "valid", "invalid"]


@dataclass(frozen=True, slots=True)
class ChannelSnapshot:
    name: str
    group_name: str
    stream_url: str
    tvg_id: str
    tvg_name: str
    tvg_logo: str
    tvg_rec: str
    validated_version: int


@dataclass(frozen=True, slots=True)
class ChannelDraft:
    id: int
    display_order: int
    enabled: bool
    name: str
    group_name: str
    stream_url: str
    tvg_id: str
    tvg_name: str
    tvg_logo: str
    tvg_rec: str
    draft_version: int
    status: ChannelStatus
    draft_differs_from_live: bool
    live_snapshot: ChannelSnapshot | None
    epg_mapping_count: int = 0


@dataclass(frozen=True, slots=True)
class ChannelStreamVariant:
    id: int
    channel_id: int
    label: str
    url: str
    display_order: int
    enabled: bool
    last_probe_status: ChannelStatus
    last_probe_error: str
    last_probe_at: str | None
    last_stability_status: str = "new"
    last_stability_error: str = ""
    last_stability_speed: str = ""
    last_stability_frames: int = 0
    last_stability_at: str | None = None


@dataclass(frozen=True, slots=True)
class EpgSource:
    id: int
    display_name: str
    source_url: str
    enabled: bool
    priority: int
    normalized_url: str = ""
    status: str = ""
    channel_count: int = 0
    last_loaded_at: str | None = None
    last_error: str = ""
