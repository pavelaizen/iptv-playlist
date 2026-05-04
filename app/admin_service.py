from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from pathlib import Path

from app.admin_m3u import render_playlist
from app.admin_models import ChannelDraft
from app.admin_store import AdminStore
from app.emby_client import refresh_livetv_after_publish
from app.probe import ProbeSettings, ProbeTarget, probe_channels
from app.publish import PublishGuardConfig, select_playlist_for_publish


@dataclass(frozen=True)
class AdminServiceSettings:
    output_dir: Path
    diagnostics_dir: Path
    output_playlist_name: str = "playlist_emby_clean.m3u8"
    min_valid_channels_absolute: int = 1
    min_valid_ratio_of_previous: float = 0.7


class AdminService:
    def __init__(self, store: AdminStore, settings: AdminServiceSettings) -> None:
        self.store = store
        self.settings = settings
        self._job_lock = threading.Lock()

    def validate_channel(self, channel_id: int) -> dict[str, object]:
        drafts = {draft.id: draft for draft in self.store.list_channels()}
        draft = drafts[channel_id]
        probe_results = self._probe_urls([draft])
        if not probe_results.get(draft.stream_url):
            self.store.mark_channel_invalid(channel_id, draft.draft_version, "ffprobe failed")
            return {"status": "invalid", "channel_id": channel_id}

        self.store.replace_live_snapshot(channel_id, draft)
        publish_result = self._publish_from_live_snapshots()
        return {"status": "valid", "channel_id": channel_id, "publish": publish_result}

    def validate_all(self, trigger_type: str) -> dict[str, object]:
        if not self._job_lock.acquire(blocking=False):
            return {"status": "already_running"}

        try:
            drafts = [draft for draft in self.store.list_channels() if draft.enabled]
            probe_results = self._probe_urls(drafts)
            valid_count = 0
            invalid_count = 0
            for draft in drafts:
                if probe_results.get(draft.stream_url):
                    self.store.replace_live_snapshot(draft.id, draft)
                    valid_count += 1
                else:
                    self.store.mark_channel_invalid(draft.id, draft.draft_version, "ffprobe failed")
                    invalid_count += 1

            publish_result = self._publish_from_live_snapshots()
            self.store.record_validation_run(
                trigger_type=trigger_type,
                status="ok",
                valid_count=valid_count,
                invalid_count=invalid_count,
                publish_changed=bool(publish_result["content_changed"]),
                epg_matched_channels=int(publish_result["epg"]["matched_channels"]),
                epg_programmes=int(publish_result["epg"]["programmes"]),
                error_summary="",
            )
            return {
                "status": "ok",
                "trigger_type": trigger_type,
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "publish": publish_result,
            }
        finally:
            self._job_lock.release()

    def _publish_from_live_snapshots(self) -> dict[str, object]:
        snapshots = [
            draft.live_snapshot
            for draft in self.store.list_channels()
            if draft.enabled and draft.live_snapshot is not None
        ]
        candidate_content = render_playlist([snapshot for snapshot in snapshots if snapshot is not None])
        candidate_output_path = self.settings.output_dir / self.settings.output_playlist_name
        decision = select_playlist_for_publish(
            candidate_output_path=candidate_output_path,
            previous_clean_path=candidate_output_path,
            candidate_content=candidate_content,
            config=PublishGuardConfig(
                min_valid_channels_absolute=self.settings.min_valid_channels_absolute,
                min_valid_ratio_of_previous=self.settings.min_valid_ratio_of_previous,
                diagnostics_dir=self.settings.diagnostics_dir,
            ),
        )
        epg_result = self._sync_epg()
        if decision.publish_candidate and decision.content_changed:
            self._refresh_emby()
        return {
            "publish_candidate": decision.publish_candidate,
            "content_changed": decision.content_changed,
            "epg": epg_result,
        }

    def _probe_urls(self, drafts: list[ChannelDraft]) -> dict[str, bool]:
        targets = [
            ProbeTarget(url=draft.stream_url, name=draft.name, fingerprint=str(draft.id))
            for draft in drafts
        ]
        results, _ = asyncio.run(probe_channels(targets, ProbeSettings.from_env()))
        return {result.channel: result.valid for result in results}

    def _sync_epg(self) -> dict[str, object]:
        return {"changed": False, "matched_channels": 0, "programmes": 0}

    def _refresh_emby(self) -> None:
        refresh_livetv_after_publish()
