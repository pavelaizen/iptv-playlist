"""Emby Live TV refresh client.

This module is intentionally non-fatal: publish workflows should keep successfully
published playlist files even if Emby refresh calls fail. Call
`refresh_livetv_after_publish()` only after publish succeeds.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional
from urllib import error, parse, request

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmbyConfig:
    base_url: str
    api_key: str
    tuner_id: Optional[str] = None

    @classmethod
    def from_env(cls) -> Optional["EmbyConfig"]:
        """Load Emby config from env vars.

        Returns None when required vars are missing so callers can treat Emby
        integration as optional.
        """
        base_url = os.getenv("EMBY_BASE_URL", "").strip().rstrip("/")
        api_key = os.getenv("EMBY_API_KEY", "").strip()
        tuner_id = os.getenv("EMBY_LIVETV_TUNER_ID", "").strip() or None

        if not base_url or not api_key:
            return None

        return cls(base_url=base_url, api_key=api_key, tuner_id=tuner_id)


def refresh_livetv_after_publish(logger: Optional[logging.Logger] = None) -> Optional[str]:
    """Trigger Emby Live TV refresh endpoints after successful publish.

    Returns:
        None on success or no-op, otherwise a non-fatal warning string. The
        caller should keep the published file and surface warning in logs/UI.

    Idempotency behavior:
      * If API calls fail, this function returns a warning instead of raising.
      * Caller can run this function again on the next schedule.
    """
    log = logger or _LOG
    config = EmbyConfig.from_env()
    if config is None:
        log.info(
            "Skipping Emby Live TV refresh because EMBY_BASE_URL or EMBY_API_KEY is unset"
        )
        return None

    failures: list[str] = []
    ok, detail = _trigger_refresh_guide(config, log)
    if not ok:
        failures.append(f"refresh-guide: {detail}")

    if config.tuner_id:
        endpoint = f"/LiveTv/Tuners/{parse.quote(config.tuner_id, safe='')}/Reset"
        ok, detail = _post_emby(config, endpoint, log)
        if not ok:
            failures.append(f"{endpoint}: {detail}")

    if failures:
        warning = (
            "Published playlist successfully, but Emby Live TV refresh had non-fatal "
            f"errors; will retry on next schedule: {'; '.join(failures)}"
        )
        log.warning(warning)
        return warning

    return None


def _trigger_refresh_guide(
    config: EmbyConfig,
    log: logging.Logger,
) -> tuple[bool, str]:
    """Run the Live TV guide refresh on the current Emby flavor.

    Older/newer Emby builds expose this as a scheduled task rather than a
    dedicated `/LiveTv/RefreshGuide` endpoint, so prefer task discovery first.
    """
    task_id, task_label = _find_refresh_guide_task(config, log)
    if task_id is not None:
        endpoint = f"/ScheduledTasks/Running/{parse.quote(task_id, safe='')}"
        ok, detail = _post_emby(config, endpoint, log)
        if ok:
            return True, f"scheduled-task {task_label or task_id}"
        return False, f"scheduled-task {task_label or task_id}: {detail}"

    return _post_emby(config, "/LiveTv/RefreshGuide", log)


def _find_refresh_guide_task(
    config: EmbyConfig,
    log: logging.Logger,
) -> tuple[str | None, str | None]:
    ok, payload = _get_emby_json(config, "/ScheduledTasks", log)
    if not ok:
        return None, None

    if not isinstance(payload, list):
        return None, None

    for task in payload:
        if not isinstance(task, dict):
            continue
        key = str(task.get("Key", "")).strip()
        name = str(task.get("Name", "")).strip()
        if key == "RefreshGuide" or name.lower() == "refresh guide":
            task_id = str(task.get("Id", "")).strip() or None
            if task_id is None:
                continue
            return task_id, name or key

    return None, None


def _post_emby(config: EmbyConfig, endpoint: str, log: logging.Logger) -> tuple[bool, str]:
    url = f"{config.base_url}{endpoint}"
    req = request.Request(
        url=url,
        method="POST",
        headers={
            "X-Emby-Token": config.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        data=json.dumps({}).encode("utf-8"),
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read(300).decode("utf-8", errors="replace")
            status = getattr(resp, "status", None) or resp.getcode()
            log.info("Emby refresh POST %s -> HTTP %s, body=%r", endpoint, status, body)
            if 200 <= status < 300:
                return True, f"HTTP {status}"
            return False, f"HTTP {status}, body={body!r}"
    except error.HTTPError as exc:
        body = exc.read(300).decode("utf-8", errors="replace") if exc.fp else ""
        log.warning(
            "Emby refresh POST %s -> HTTP %s, body=%r", endpoint, exc.code, body
        )
        return False, f"HTTP {exc.code}, body={body!r}"
    except Exception as exc:  # noqa: BLE001 - non-fatal by design.
        log.warning("Emby refresh POST %s failed: %s", endpoint, exc)
        return False, str(exc)


def _get_emby_json(
    config: EmbyConfig,
    endpoint: str,
    log: logging.Logger,
) -> tuple[bool, Any]:
    url = f"{config.base_url}{endpoint}"
    req = request.Request(
        url=url,
        method="GET",
        headers={
            "X-Emby-Token": config.api_key,
            "Accept": "application/json",
        },
    )

    try:
        with request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", None) or resp.getcode()
            log.info("Emby refresh GET %s -> HTTP %s", endpoint, status)
            if not (200 <= status < 300):
                return False, None
            return True, json.loads(body)
    except Exception as exc:  # noqa: BLE001 - non-fatal by design.
        log.warning("Emby refresh GET %s failed: %s", endpoint, exc)
        return False, None
