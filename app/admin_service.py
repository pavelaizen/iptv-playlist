from __future__ import annotations

import asyncio
import gzip
import threading
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from app.admin_epg import sync_epg, collect_channel_epg_icons
from app.admin_m3u import render_playlist
from app.admin_models import ChannelDraft, ChannelSnapshot, ChannelStreamVariant
from app.admin_store import AdminStore
from app.emby_client import refresh_livetv_after_publish
from app.epg_sources import download_epg_source, search_epgpw_channels
from app.probe import ProbeSettings, ProbeTarget, probe_channels
from app.publish import PublishGuardConfig, select_playlist_for_publish
from app.stream_stability import run_stream_stability_test


@dataclass(frozen=True)
class AdminServiceSettings:
    output_dir: Path
    diagnostics_dir: Path
    epg_work_dir: Path | None = None
    output_playlist_name: str = "playlist_emby_clean.m3u8"
    output_epg_name: str = "epg.xml"
    min_valid_channels_absolute: int = 1
    min_valid_ratio_of_previous: float = 0.7
    allow_private_source_urls: bool = False


@dataclass
class AdminJob:
    id: str
    kind: str
    status: str = "queued"
    result: dict[str, object] | None = None
    error: str = ""


class AdminService:
    def __init__(self, store: AdminStore, settings: AdminServiceSettings) -> None:
        self.store = store
        self.settings = settings
        self._job_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._jobs_lock = threading.Lock()
        self._jobs: dict[str, AdminJob] = {}

    def validate_channel(self, channel_id: int) -> dict[str, object]:
        drafts = {draft.id: draft for draft in self.store.list_channels()}
        draft = drafts.get(channel_id)
        if draft is None:
            return {"status": "not_found", "channel_id": channel_id}
        variants = [variant for variant in self.store.list_stream_variants(channel_id) if variant.enabled]
        if self._probe_urls_is_overridden():
            channel_valid = self._probe_urls([draft]).get(channel_id, False)
            probe_results = {variant.id: channel_valid for variant in variants}
        else:
            probe_results = self._probe_variants(variants)
        any_valid = False
        for variant in variants:
            valid = probe_results.get(variant.id, False)
            self.store.mark_stream_variant_probe_result(
                variant.id,
                status="valid" if valid else "invalid",
                error_text="" if valid else "ffprobe failed",
            )
            any_valid = any_valid or valid
        if not any_valid:
            self.store.mark_channel_invalid(channel_id, draft.draft_version, "ffprobe failed")
            return {"status": "invalid", "channel_id": channel_id}

        self.store.replace_live_snapshot(channel_id, draft)
        return {"status": "valid", "channel_id": channel_id, "publish": {"status": "not_run"}}

    def validate_all(self, trigger_type: str) -> dict[str, object]:
        if not self._job_lock.acquire(blocking=False):
            return {"status": "already_running"}

        try:
            drafts = [draft for draft in self.store.list_channels() if draft.enabled]
            enabled_variants = [
                variant
                for draft in drafts
                for variant in self.store.list_stream_variants(draft.id)
                if variant.enabled
            ]
            if self._probe_urls_is_overridden():
                channel_results = self._probe_urls(drafts)
                variant_results = {
                    variant.id: channel_results.get(variant.channel_id, False)
                    for variant in enabled_variants
                }
            else:
                variant_results = self._probe_variants(enabled_variants)
            valid_count = 0
            invalid_count = 0
            for draft in drafts:
                variants = [variant for variant in enabled_variants if variant.channel_id == draft.id]
                channel_valid = False
                for variant in variants:
                    valid = variant_results.get(variant.id, False)
                    self.store.mark_stream_variant_probe_result(
                        variant.id,
                        status="valid" if valid else "invalid",
                        error_text="" if valid else "ffprobe failed",
                    )
                    channel_valid = channel_valid or valid
                if channel_valid:
                    self.store.replace_live_snapshot(draft.id, draft)
                    valid_count += 1
                else:
                    self.store.mark_channel_invalid(draft.id, draft.draft_version, "ffprobe failed")
                    invalid_count += 1

            self.store.record_validation_run(
                trigger_type=trigger_type,
                status="ok",
                valid_count=valid_count,
                invalid_count=invalid_count,
                publish_changed=False,
                epg_matched_channels=0,
                epg_programmes=0,
                error_summary="",
            )
            return {
                "status": "ok",
                "trigger_type": trigger_type,
                "valid_count": valid_count,
                "invalid_count": invalid_count,
                "publish": {"status": "not_run"},
            }
        finally:
            self._job_lock.release()

    def rebuild_playlist(self, trigger_type: str) -> dict[str, object]:
        del trigger_type
        with self._publish_lock:
            snapshots = self._capture_publish_snapshots()
            candidate_content = render_playlist(snapshots)
            publish_result = self._publish_candidate_playlist(candidate_content)
            if publish_result["publish_candidate"] and publish_result["content_changed"]:
                self._refresh_emby()
            return {"status": "ok", "playlist": publish_result}

    def rebuild_epg(self, trigger_type: str) -> dict[str, object]:
        del trigger_type
        with self._publish_lock:
            epg_result = self._sync_epg()
            return {"status": "ok", "epg": epg_result}

    def rebuild_all_public_outputs(self, trigger_type: str) -> dict[str, object]:
        del trigger_type
        with self._publish_lock:
            snapshots = self._capture_publish_snapshots()
            candidate_content = render_playlist(snapshots)
            publish_result = self._publish_candidate_playlist(candidate_content)
            if publish_result["publish_candidate"] and publish_result["content_changed"]:
                self._refresh_emby()
        epg_result = self._sync_epg()
        return {"status": "ok", "playlist": publish_result, "epg": epg_result}

    def delete_epg_source_and_rebuild(self, source_id: int) -> dict[str, object]:
        with self._publish_lock:
            affected_channel_ids = self.store.list_channel_ids_for_epg_source(source_id)
            self.store.delete_epg_source(source_id)
            invalidated_channel_ids = [
                channel_id
                for channel_id in affected_channel_ids
                if not self.store.channel_has_valid_enabled_mapping(channel_id)
            ]
            self.store.mark_channels_invalid(
                invalidated_channel_ids,
                "EPG source deleted; no remaining valid enabled EPG mapping",
            )
            snapshots = self._capture_publish_snapshots()
            publish_result = self._publish_candidate_playlist(render_playlist(snapshots))
            if publish_result["publish_candidate"] and publish_result["content_changed"]:
                self._refresh_emby()
        epg_result = self._sync_epg()
        return {
            "status": "ok",
            "deleted_source_id": source_id,
            "affected_channel_ids": affected_channel_ids,
            "invalidated_channel_ids": invalidated_channel_ids,
            "playlist": publish_result,
            "epg": epg_result,
        }

    def _publish_candidate_playlist(self, candidate_content: str) -> dict[str, object]:
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
        return {
            "publish_candidate": decision.publish_candidate,
            "content_changed": decision.content_changed,
            "reason": decision.reason,
        }

    def _capture_publish_snapshots(self) -> list[ChannelSnapshot]:
        drafts = [draft for draft in self.store.list_channels() if draft.enabled]
        epg_icons = self._load_epg_icons_for_drafts(drafts)
        epg_tvg_ids = self._load_epg_tvg_ids_for_drafts(drafts)
        snapshots: list[ChannelSnapshot] = []
        for draft in drafts:
            for variant in self.store.list_stream_variants(draft.id):
                if not variant.enabled:
                    continue
                display_name = self._variant_display_name(draft, variant)
                tvg_logo = draft.tvg_logo or epg_icons.get(draft.id, "")
                tvg_id = epg_tvg_ids.get(draft.id, "") or draft.tvg_id
                snapshots.append(
                    ChannelSnapshot(
                        name=display_name,
                        group_name=draft.group_name,
                        stream_url=variant.url,
                        tvg_id=tvg_id,
                        tvg_name=draft.tvg_name or display_name,
                        tvg_logo=tvg_logo,
                        tvg_rec=draft.tvg_rec,
                        validated_version=draft.draft_version,
                    )
                )
        return snapshots

    def _variant_display_name(self, draft: ChannelDraft, variant: ChannelStreamVariant) -> str:
        label = variant.label.strip()
        if not label:
            return draft.name
        if draft.name.casefold().endswith(f" {label}".casefold()):
            return draft.name
        return f"{draft.name} {label}"

    def _epg_work_dir(self) -> Path:
        return self.settings.epg_work_dir or (self.settings.output_dir.parent / "state" / "epg")

    def _load_epg_icons_for_drafts(self, drafts: list[ChannelDraft]) -> dict[int, str]:
        channels_data: list[dict[str, object]] = []
        for draft in drafts:
            channels_data.append(
                {
                    "channel_id": draft.id,
                    "mappings": [
                        {
                            "source_key": f"source-{int(mapping['epg_source_id'])}",
                            "channel_id": str(mapping["channel_xmltv_id"]),
                        }
                        for mapping in self.store.list_channel_epg_mappings(draft.id)
                        if bool(mapping.get("enabled", True))
                    ],
                }
            )
        return collect_channel_epg_icons(channels_data, self._epg_work_dir())

    def _load_epg_tvg_ids_for_drafts(self, drafts: list[ChannelDraft]) -> dict[int, str]:
        tvg_ids: dict[int, str] = {}
        for draft in drafts:
            for mapping in self.store.list_channel_epg_mappings(draft.id):
                if bool(mapping.get("enabled", True)):
                    xmltv = str(mapping.get("channel_xmltv_id", "")).strip()
                    if xmltv:
                        tvg_ids.setdefault(draft.id, xmltv)
        return tvg_ids

    def get_epg_icon_for_channel(self, channel_id: int) -> str:
        channels_data = [
            {
                "channel_id": channel_id,
                "mappings": [
                    {
                        "source_key": f"source-{int(mapping['epg_source_id'])}",
                        "channel_id": str(mapping["channel_xmltv_id"]),
                    }
                    for mapping in self.store.list_channel_epg_mappings(channel_id)
                    if bool(mapping.get("enabled", True))
                ],
            }
        ]
        return collect_channel_epg_icons(channels_data, self._epg_work_dir()).get(channel_id, "")

    def _extract_epg_source_icon(self, source_id: int, channel_xmltv_id: str) -> str:
        source_path = self._epg_work_dir() / f"source-{source_id}.xmltv"
        if not source_path.is_file():
            return ""
        opener = gzip.open if _looks_gzip(source_path) else open
        with opener(source_path, "rb") as fh:
            for _, element in ET.iterparse(fh, events=("end",)):
                if element.tag == "channel":
                    if element.attrib.get("id", "") == channel_xmltv_id:
                        for child in element:
                            if child.tag == "icon" and child.attrib.get("src"):
                                icon = child.attrib["src"].strip()
                                element.clear()
                                return icon
                        element.clear()
                        return ""
                    element.clear()
                elif element.tag == "programme":
                    element.clear()
        return ""

    def get_all_epg_icons(self) -> dict[int, str]:
        drafts = self.store.list_channels()
        return self._load_epg_icons_for_drafts(drafts)

    def _probe_urls(self, drafts: list[ChannelDraft]) -> dict[int, bool]:
        targets = [
            ProbeTarget(url=draft.stream_url, name=draft.name, fingerprint=str(draft.id))
            for draft in drafts
        ]
        results, _ = asyncio.run(probe_channels(targets, ProbeSettings.from_env()))
        return {
            int(result.channel_fingerprint): result.valid
            for result in results
            if result.channel_fingerprint.isdigit()
        }

    def _probe_variants(self, variants: list[ChannelStreamVariant]) -> dict[int, bool]:
        targets = [
            ProbeTarget(url=variant.url, name=variant.label, fingerprint=str(variant.id))
            for variant in variants
        ]
        if not targets:
            return {}
        results, _ = asyncio.run(probe_channels(targets, ProbeSettings.from_env()))
        return {
            int(result.channel_fingerprint): result.valid
            for result in results
            if result.channel_fingerprint.isdigit()
        }

    def _probe_urls_is_overridden(self) -> bool:
        return getattr(self._probe_urls, "__func__", None) is not AdminService._probe_urls

    def _sync_epg(self) -> dict[str, object]:
        channels = []
        for draft in self.store.list_channels():
            if not draft.enabled:
                continue
            enabled_variants = [variant for variant in self.store.list_stream_variants(draft.id) if variant.enabled]
            if not enabled_variants:
                continue
            display_name = self._variant_display_name(draft, enabled_variants[0])
            epg_icons = self._load_epg_icons_for_drafts([draft])
            tvg_logo = draft.tvg_logo or epg_icons.get(draft.id, "")
            channels.append(
                {
                    "name": display_name,
                    "channel_id": draft.id,
                    "tvg_logo": tvg_logo,
                    "mappings": [
                        {
                            "source_key": f"source-{int(mapping['epg_source_id'])}",
                            "channel_id": str(mapping["channel_xmltv_id"]),
                        }
                        for mapping in self.store.list_channel_epg_mappings(draft.id)
                        if bool(mapping.get("enabled", True))
                    ],
                }
            )

        epg_sources = self.store.list_enabled_epg_sources_payload()
        output_path = self.settings.output_dir / self.settings.output_epg_name
        work_dir = self.settings.epg_work_dir or (self.settings.output_dir.parent / "state" / "epg")

        result = sync_epg(
            published_channels=channels,
            epg_sources=epg_sources,
            output_path=output_path,
            work_dir=work_dir,
        )
        return {
            "changed": result.changed,
            "matched_channels": result.matched_channels,
            "programmes": result.programmes,
            "failed_sources": result.failed_sources,
            "channel_icons": result.channel_icons,
        }

    def preview_channel_epg_programmes(
        self,
        channel_id: int,
        *,
        limit: int = 5,
    ) -> dict[str, object]:
        self.store.get_channel(channel_id)
        mappings = [
            mapping
            for mapping in self.store.list_channel_epg_mappings(channel_id)
            if bool(mapping.get("enabled", True)) and str(mapping.get("channel_xmltv_id", "")).strip()
        ]
        if not mappings:
            return {"items": [], "empty_message": "No EPG mappings attached."}

        enabled_sources = {
            source.id: source
            for source in self.store.list_epg_sources()
            if source.enabled
        }
        wanted_by_source: dict[int, set[str]] = {}
        for mapping in mappings:
            source_id = int(mapping["epg_source_id"])
            if source_id not in enabled_sources:
                continue
            wanted_by_source.setdefault(source_id, set()).add(str(mapping["channel_xmltv_id"]))

        if not wanted_by_source:
            return {"items": [], "empty_message": "No enabled EPG source is attached."}

        work_dir = self.settings.epg_work_dir or (self.settings.output_dir.parent / "state" / "epg")
        now = datetime.now(timezone.utc)
        items: list[dict[str, object]] = []
        saw_cached_source = False
        for source_id, channel_ids in wanted_by_source.items():
            source = enabled_sources[source_id]
            source_path = _cached_epg_preview_source_path(source_id, source.source_url, work_dir)
            if source_path is None:
                continue
            saw_cached_source = True
            items.extend(
                _read_xmltv_programme_preview(
                    source_path=source_path,
                    source_id=source_id,
                    source_name=source.display_name,
                    wanted_channel_ids=channel_ids,
                    now=now,
                )
            )

        items.sort(key=lambda item: float(item["_sort"]))
        preview_items = [
            {key: value for key, value in item.items() if key != "_sort"}
            for item in items[: max(limit, 0)]
        ]
        if preview_items:
            return {"items": preview_items, "empty_message": ""}
        if not saw_cached_source:
            return {"items": [], "empty_message": "No cached EPG guide is available for these mappings yet."}
        return {"items": [], "empty_message": "No upcoming programmes found for attached mappings."}

    def _refresh_emby(self) -> None:
        refresh_livetv_after_publish()

    def start_validate_all_job(self, trigger_type: str) -> dict[str, object]:
        return self._start_job("validate-all", lambda: self.validate_all(trigger_type))

    def start_validate_channel_job(self, channel_id: int) -> dict[str, object]:
        return self._start_job("validate-channel", lambda: self.validate_channel(channel_id))

    def start_validate_stream_job(self, stream_id: int) -> dict[str, object]:
        return self._start_job("validate-stream", lambda: self.validate_stream(stream_id))

    def start_stream_stability_job(self, stream_id: int) -> dict[str, object]:
        return self._start_job(
            "validate-stream-extended",
            lambda: self.validate_stream_stability(stream_id),
        )

    def start_rebuild_playlist_job(self, trigger_type: str) -> dict[str, object]:
        return self._start_job("rebuild-playlist", lambda: self.rebuild_playlist(trigger_type))

    def start_rebuild_epg_job(self, trigger_type: str) -> dict[str, object]:
        return self._start_job("rebuild-epg", lambda: self.rebuild_epg(trigger_type))

    def start_reload_epg_source_job(self, source_id: int) -> dict[str, object]:
        return self._start_job("reload-epg-source", lambda: self.reload_epg_source(source_id))

    def get_job(self, job_id: str) -> dict[str, object]:
        with self._jobs_lock:
            job = self._jobs.get(job_id)
            if job is None:
                return {"status": "not_found", "job_id": job_id}
            return {
                "job_id": job.id,
                "kind": job.kind,
                "status": job.status,
                "result": job.result,
                "error": job.error,
            }

    def _start_job(self, kind: str, func) -> dict[str, object]:
        job = AdminJob(id=str(uuid.uuid4()), kind=kind)
        with self._jobs_lock:
            self._jobs[job.id] = job

        def run() -> None:
            with self._jobs_lock:
                job.status = "running"
            try:
                result = func()
            except Exception as exc:  # noqa: BLE001 - persist concise job failure
                with self._jobs_lock:
                    job.status = "error"
                    job.error = str(exc)[:300]
            else:
                with self._jobs_lock:
                    job.status = "ok"
                    job.result = result

        threading.Thread(target=run, daemon=True, name=f"playlist-admin-{kind}").start()
        return {"job_id": job.id, "status": job.status}

    def validate_stream(self, stream_id: int) -> dict[str, object]:
        try:
            variant = self.store.get_stream_variant(stream_id)
        except KeyError:
            return {"status": "not_found", "stream_id": stream_id}
        result = self._probe_variants([variant]).get(stream_id, False)
        self.store.mark_stream_variant_probe_result(
            stream_id,
            status="valid" if result else "invalid",
            error_text="" if result else "ffprobe failed",
        )
        return {"status": "valid" if result else "invalid", "stream_id": stream_id}

    def validate_stream_stability(self, stream_id: int) -> dict[str, object]:
        try:
            variant = self.store.get_stream_variant(stream_id)
        except KeyError:
            return {"status": "not_found", "stream_id": stream_id}

        result = run_stream_stability_test(variant.url)
        self.store.mark_stream_variant_stability_result(
            stream_id,
            status=result.status,
            error_text=result.issues,
            speed=result.speed,
            frames=result.frames,
        )
        return {
            "status": result.status,
            "stream_id": stream_id,
            "frames": result.frames,
            "speed": result.speed,
            "issues": result.issues,
            "returncode": result.returncode,
        }

    def reload_epg_source(self, source_id: int) -> dict[str, object]:
        source = self.store.get_epg_source(source_id)
        try:
            self._validate_public_source_url(source.source_url)
            source_path = self._download_source(source_id, source.source_url)
            channel_count = self.store.replace_epg_channel_cache_from_iterable(
                source_id,
                _iter_xmltv_channel_cache(source_path),
            )
        except Exception as exc:  # noqa: BLE001 - failed reload keeps prior cache
            self.store.mark_epg_source_error(source_id, str(exc))
            return {"status": "error", "source_id": source_id, "error": str(exc)[:300]}
        return {"status": "ok", "source_id": source_id, "channel_count": channel_count}

    def auto_add_epgpw_mapping(
        self,
        channel_id: int,
        epgpw_channel_id: str,
        display_name: str,
    ) -> dict[str, object]:
        source_url = f"https://epg.pw/api/epg.xml?channel_id={epgpw_channel_id}"
        source_name = f"epg.pw: {display_name}" if display_name else f"epg.pw channel {epgpw_channel_id}"
        source = self.store.ensure_epg_source(source_url, source_name)
        existing_mappings = self.store.list_channel_epg_mappings(channel_id)
        for m in existing_mappings:
            if (
                int(m["epg_source_id"]) == source.id
                and str(m["channel_xmltv_id"]) == epgpw_channel_id
            ):
                return {
                    "status": "duplicate",
                    "source_id": source.id,
                    "mapping_id": int(m["id"]),
                }
        next_priority = (
            max((int(m["priority"]) for m in existing_mappings), default=-1) + 1
        )
        mapping = self.store.add_channel_epg_mapping(
            channel_id,
            source.id,
            next_priority,
            epgpw_channel_id,
        )

        icon_url = ""
        reload_result = self.reload_epg_source(source.id)
        if reload_result.get("status") == "ok":
            icon_url = self._extract_epg_source_icon(source.id, epgpw_channel_id)
            if icon_url:
                self.store.set_channel_logo_url(channel_id, icon_url)

        rebuild_job = self._start_job(
            "epgpw-map-rebuild",
            lambda: self.rebuild_all_public_outputs("epgpw-map"),
        )
        return {
            "status": "ok",
            "source_id": source.id,
            "mapping": mapping,
            "icon_url": icon_url,
            "rebuild_job": rebuild_job,
        }

    def _download_source(self, source_id: int, source_url: str) -> Path:
        work_dir = self.settings.epg_work_dir or (self.settings.output_dir.parent / "state" / "epg")
        work_dir.mkdir(parents=True, exist_ok=True)
        destination = work_dir / f"source-{source_id}.xmltv"
        download_epg_source(source_url, destination)
        return destination

    def _validate_public_source_url(self, source_url: str) -> None:
        parts = urlsplit(source_url)
        if parts.scheme not in {"http", "https"}:
            raise ValueError("EPG source URL must use http or https")
        host = (parts.hostname or "").casefold()
        if not host:
            raise ValueError("EPG source URL is missing a host")
        if host in {"localhost", "0.0.0.0"} or host.startswith("127."):
            raise ValueError("EPG source URL host is not allowed")
        if not self.settings.allow_private_source_urls and _is_private_host_literal(host):
            raise ValueError("private EPG source URLs are disabled")


def _is_private_host_literal(host: str) -> bool:
    parts = host.split(".")
    if len(parts) != 4 or not all(part.isdigit() for part in parts):
        return False
    octets = [int(part) for part in parts]
    return (
        octets[0] == 10
        or (octets[0] == 172 and 16 <= octets[1] <= 31)
        or (octets[0] == 192 and octets[1] == 168)
    )


def _parse_xmltv_channel_cache(path: Path) -> list[dict[str, str]]:
    return list(_iter_xmltv_channel_cache(path))


def _iter_xmltv_channel_cache(path: Path):
    opener = gzip.open if _looks_gzip(path) else open
    saw_channels = False
    with opener(path, "rb") as fh:
        root = None
        for event, element in ET.iterparse(fh, events=("start", "end")):
            if event == "start" and root is None:
                root = element
                continue
            if event != "end":
                continue
            if element.tag == "programme":
                element.clear()
                if root is not None and root is not element:
                    root.clear()
                continue
            if element.tag != "channel":
                continue

            channel_id = element.attrib.get("id", "")
            display_name = ""
            for child in element:
                if child.tag == "display-name" and child.text:
                    display_name = child.text.strip()
                    break
            if channel_id:
                saw_channels = True
                yield {"id": channel_id, "display_name": display_name or channel_id}
            element.clear()
            if root is not None and root is not element:
                root.clear()
    if not saw_channels:
        raise ValueError("EPG source has no channel nodes")


def _looks_gzip(path: Path) -> bool:
    with path.open("rb") as fh:
        return fh.read(2) == b"\x1f\x8b"


def _cached_epg_preview_source_path(source_id: int, source_url: str, work_dir: Path) -> Path | None:
    candidates = [
        work_dir / f"source-{source_id}.xmltv",
        work_dir / f"source-{source_id}.xml.gz",
    ]
    legacy_name = _legacy_epg_preview_source_filename(source_url)
    if legacy_name:
        candidates.append(work_dir / legacy_name)
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _legacy_epg_preview_source_filename(source_url: str) -> str:
    if "epg.one" in source_url:
        return "source.xml.gz"
    if "iptvx.one" in source_url:
        return "source_israel_primary.xml.gz"
    if "iptv-epg.org" in source_url:
        return "source_israel_fallback.xml.gz"
    return ""


def _read_xmltv_programme_preview(
    *,
    source_path: Path,
    source_id: int,
    source_name: str,
    wanted_channel_ids: set[str],
    now: datetime,
) -> list[dict[str, object]]:
    opener = gzip.open if _looks_gzip(source_path) else open
    items: list[dict[str, object]] = []
    with opener(source_path, "rb") as fh:
        root = None
        for event, element in ET.iterparse(fh, events=("start", "end")):
            if event == "start" and root is None:
                root = element
                continue
            if event != "end":
                continue
            if element.tag == "channel":
                element.clear()
                if root is not None and root is not element:
                    root.clear()
                continue
            if element.tag != "programme":
                continue
            channel_id = element.attrib.get("channel", "")
            if channel_id in wanted_channel_ids:
                item = _programme_preview_item(
                    element=element,
                    source_id=source_id,
                    source_name=source_name,
                    channel_id=channel_id,
                    now=now,
                )
                if item is not None:
                    items.append(item)
            element.clear()
            if root is not None and root is not element:
                root.clear()
    return items


def _programme_preview_item(
    *,
    element: ET.Element,
    source_id: int,
    source_name: str,
    channel_id: str,
    now: datetime,
) -> dict[str, object] | None:
    start = _parse_xmltv_datetime(element.attrib.get("start", ""))
    stop = _parse_xmltv_datetime(element.attrib.get("stop", ""))
    if start is None:
        return None
    if stop is not None and stop < now:
        return None

    return {
        "source_id": source_id,
        "source_name": source_name,
        "channel_id": channel_id,
        "_sort": start.astimezone(timezone.utc).timestamp(),
        "start": start.isoformat(),
        "stop": stop.isoformat() if stop is not None else "",
        "title": _first_child_text(element, "title") or "(untitled)",
        "description": _first_child_text(element, "desc"),
    }


def _first_child_text(element: ET.Element, tag: str) -> str:
    for child in element:
        if child.tag == tag and child.text:
            return child.text.strip()
    return ""


def _parse_xmltv_datetime(value: str) -> datetime | None:
    parts = value.strip().split()
    if not parts:
        return None
    timestamp = parts[0]
    if len(timestamp) < 14 or not timestamp[:14].isdigit():
        return None
    parsed = datetime.strptime(timestamp[:14], "%Y%m%d%H%M%S")
    if len(parts) < 2:
        return parsed.replace(tzinfo=timezone.utc)

    offset = parts[1]
    if len(offset) != 5 or offset[0] not in {"+", "-"} or not offset[1:].isdigit():
        return parsed.replace(tzinfo=timezone.utc)
    hours = int(offset[1:3])
    minutes = int(offset[3:5])
    delta = timedelta(hours=hours, minutes=minutes)
    if offset[0] == "-":
        delta = -delta
    return parsed.replace(tzinfo=timezone(delta))
