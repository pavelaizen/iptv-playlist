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


@dataclass(frozen=True, slots=True)
class EpgSource:
    id: int
    display_name: str
    source_url: str
    enabled: bool
    priority: int
