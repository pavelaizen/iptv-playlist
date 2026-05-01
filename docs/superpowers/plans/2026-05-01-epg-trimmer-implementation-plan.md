# EPG Trimmer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a daily EPG worker that trims the upstream XMLTV guide to the channels in the published clean playlist and serves the result from the existing static container.

**Architecture:** Add pure XMLTV trimming logic in `app/epg.py`, then add `app/epg_worker.py` for scheduling, download, guarded publish, and Emby refresh. Wire the worker as a separate `epg-trimmer` service in `docker-compose.yml` while keeping static serving in `docker-compose.playlist.yml`.

**Tech Stack:** Python 3.12 standard library, `gzip`, `xml.etree.ElementTree`, `urllib.request`, Docker Compose, pytest.

---

## File Structure

- Create `app/epg.py`: playlist channel-name extraction, channel-name normalization, streaming XMLTV match/trim logic, and trim summary dataclasses.
- Create `app/epg_worker.py`: environment parsing, daily scheduling helpers, download-to-temp, guarded atomic EPG publishing, state writes, and Emby refresh calls.
- Create `tests/test_epg.py`: unit tests for matching, trimming, zero-match guard data, and safe playlist parsing.
- Create `tests/test_epg_worker.py`: unit tests for scheduler helpers, guarded publish behavior, unchanged output, changed output, and refresh calls.
- Modify `docker-compose.yml`: add `epg-trimmer` service using the existing sanitizer image build and shared `published`/`output` mounts.
- Modify `tests/test_compose_config.py`: assert the EPG service, output path, state path, and shared static path.
- Modify `README.md`: document the EPG worker, output URL, environment variables, and verification commands.
- Modify `AGENTS.md`: record the new module, service, environment variables, and verification guidance for future sessions.

## Task 1: Pure EPG Trimming Library

**Files:**
- Create: `app/epg.py`
- Create: `tests/test_epg.py`

- [ ] **Step 1: Write failing tests for playlist parsing and normalization**

Add `tests/test_epg.py` with tests equivalent to:

```python
from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

from app import epg


def write_gzip(path: Path, text: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)


def read_gzip(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def test_extract_playlist_channel_names_reads_extinf_without_urls(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:-1 tvg-id=\"one\",  Channel One  \n"
        "http://provider.invalid/secret-token\n"
        "#EXTGRP:News\n"
        "#EXTINF:0,Channel Two\n"
        "http://provider.invalid/another-token\n",
        encoding="utf-8",
    )

    names = epg.extract_playlist_channel_names(playlist)

    assert names == ["Channel One", "Channel Two"]
    assert "provider.invalid" not in repr(names)
    assert "secret-token" not in repr(names)


def test_normalize_channel_name_handles_case_punctuation_and_whitespace():
    assert epg.normalize_channel_name("  Кино-UHD!!  ") == epg.normalize_channel_name("кино uhd")
    assert epg.normalize_channel_name("Channel   One") == "channel one"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_epg.py
```

Expected: fails because `app.epg` does not exist yet.

- [ ] **Step 3: Implement playlist parsing, normalization, and summary types**

Create `app/epg.py` with:

```python
from __future__ import annotations

import gzip
import os
import re
import unicodedata
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from xml.sax.saxutils import quoteattr


EXTINF_RE = re.compile(r"^#EXTINF(?P<attrs>[^,]*),(?P<name>.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class EpgTrimSummary:
    playlist_channel_count: int
    source_channel_count: int
    matched_channel_count: int
    programme_count: int
    unmatched_playlist_names: tuple[str, ...]


def normalize_channel_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\s_\-]+", " ", normalized)
    normalized = re.sub(r"[^\w\s]+", "", normalized)
    return re.sub(r"\s+", " ", normalized).strip()


def extract_playlist_channel_names(path: Path) -> list[str]:
    names: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        match = EXTINF_RE.match(line.strip())
        if not match:
            continue
        name = match.group("name").strip()
        if name:
            names.append(name)
    return names
```

- [ ] **Step 4: Run parser tests**

Run:

```bash
python -m pytest -q tests/test_epg.py::test_extract_playlist_channel_names_reads_extinf_without_urls tests/test_epg.py::test_normalize_channel_name_handles_case_punctuation_and_whitespace
```

Expected: parser tests pass.

- [ ] **Step 5: Write failing tests for XMLTV trimming**

Extend `tests/test_epg.py` with:

```python
def test_trim_xmltv_keeps_only_matching_channels_and_programmes(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n"
        "#EXTINF:-1,Channel One\n"
        "http://provider.invalid/one\n"
        "#EXTINF:-1,Channel Two\n"
        "http://provider.invalid/two\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.xml.gz"
    output = tmp_path / "epg.xml.gz"
    write_gzip(
        source,
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<tv source-info-name='unit-test'>\n"
        "  <channel id='one'><display-name> channel one </display-name></channel>\n"
        "  <channel id='two'><display-name>CHANNEL-TWO</display-name></channel>\n"
        "  <channel id='three'><display-name>Other</display-name></channel>\n"
        "  <programme channel='one' start='20260501040000 +0000' stop='20260501050000 +0000'><title>One</title></programme>\n"
        "  <programme channel='two' start='20260501050000 +0000' stop='20260501060000 +0000'><title>Two</title></programme>\n"
        "  <programme channel='three' start='20260501060000 +0000' stop='20260501070000 +0000'><title>Three</title></programme>\n"
        "</tv>\n",
    )

    summary = epg.trim_xmltv_to_playlist_channels(
        source_xmltv_gz_path=source,
        playlist_path=playlist,
        output_xmltv_gz_path=output,
    )

    text = read_gzip(output)
    root = ET.fromstring(text)
    assert root.tag == "tv"
    assert root.attrib["source-info-name"] == "unit-test"
    assert [channel.attrib["id"] for channel in root.findall("channel")] == ["one", "two"]
    assert [programme.attrib["channel"] for programme in root.findall("programme")] == ["one", "two"]
    assert summary == epg.EpgTrimSummary(
        playlist_channel_count=2,
        source_channel_count=3,
        matched_channel_count=2,
        programme_count=2,
        unmatched_playlist_names=(),
    )


def test_trim_xmltv_reports_unmatched_playlist_names(tmp_path: Path):
    playlist = tmp_path / "playlist.m3u"
    playlist.write_text(
        "#EXTM3U\n#EXTINF:-1,Missing Channel\nhttp://provider.invalid/missing\n",
        encoding="utf-8",
    )
    source = tmp_path / "source.xml.gz"
    output = tmp_path / "epg.xml.gz"
    write_gzip(
        source,
        "<tv><channel id='one'><display-name>Channel One</display-name></channel></tv>",
    )

    summary = epg.trim_xmltv_to_playlist_channels(source, playlist, output)

    assert output.exists()
    assert summary.matched_channel_count == 0
    assert summary.programme_count == 0
    assert summary.unmatched_playlist_names == ("Missing Channel",)
```

- [ ] **Step 6: Run trim tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_epg.py
```

Expected: fails because `trim_xmltv_to_playlist_channels` is not implemented.

- [ ] **Step 7: Implement streaming XMLTV trimming**

Extend `app/epg.py` with these functions:

```python
def trim_xmltv_to_playlist_channels(
    source_xmltv_gz_path: Path,
    playlist_path: Path,
    output_xmltv_gz_path: Path,
) -> EpgTrimSummary:
    playlist_names = extract_playlist_channel_names(playlist_path)
    normalized_playlist_names = {
        normalize_channel_name(name): name
        for name in playlist_names
        if normalize_channel_name(name)
    }
    root_attrs, matched_ids, matched_names, source_channel_count = _find_matching_channel_ids(
        source_xmltv_gz_path,
        normalized_playlist_names.keys(),
    )

    output_xmltv_gz_path.parent.mkdir(parents=True, exist_ok=True)
    programme_count = _write_trimmed_xmltv(
        source_xmltv_gz_path=source_xmltv_gz_path,
        output_xmltv_gz_path=output_xmltv_gz_path,
        root_attrs=root_attrs,
        matched_channel_ids=matched_ids,
    )

    unmatched = tuple(
        original_name
        for normalized, original_name in normalized_playlist_names.items()
        if normalized not in matched_names
    )
    return EpgTrimSummary(
        playlist_channel_count=len(playlist_names),
        source_channel_count=source_channel_count,
        matched_channel_count=len(matched_ids),
        programme_count=programme_count,
        unmatched_playlist_names=unmatched,
    )


def _find_matching_channel_ids(
    source_xmltv_gz_path: Path,
    normalized_playlist_names: Iterable[str],
) -> tuple[dict[str, str], set[str], set[str], int]:
    wanted_names = set(normalized_playlist_names)
    root_attrs: dict[str, str] = {}
    matched_ids: set[str] = set()
    matched_names: set[str] = set()
    source_channel_count = 0

    with gzip.open(source_xmltv_gz_path, "rb") as fh:
        for event, elem in ET.iterparse(fh, events=("start", "end")):
            if event == "start" and elem.tag == "tv" and not root_attrs:
                root_attrs = dict(elem.attrib)
                continue
            if event != "end":
                continue
            if elem.tag != "channel":
                elem.clear()
                continue
            source_channel_count += 1
            channel_id = elem.attrib.get("id", "")
            display_names = [
                normalize_channel_name(child.text or "")
                for child in elem
                if child.tag == "display-name"
            ]
            hits = set(display_names) & wanted_names
            if channel_id and hits:
                matched_ids.add(channel_id)
                matched_names.update(hits)
            elem.clear()

    return root_attrs, matched_ids, matched_names, source_channel_count


def _write_trimmed_xmltv(
    source_xmltv_gz_path: Path,
    output_xmltv_gz_path: Path,
    root_attrs: dict[str, str],
    matched_channel_ids: set[str],
) -> int:
    programme_count = 0
    temp_path = output_xmltv_gz_path.with_suffix(output_xmltv_gz_path.suffix + ".tmp")
    with gzip.open(source_xmltv_gz_path, "rb") as source, gzip.open(temp_path, "wt", encoding="utf-8") as out:
        out.write("<?xml version='1.0' encoding='UTF-8'?>\n")
        out.write(_format_tv_start(root_attrs))
        for event, elem in ET.iterparse(source, events=("end",)):
            if elem.tag == "channel":
                if elem.attrib.get("id") in matched_channel_ids:
                    out.write(ET.tostring(elem, encoding="unicode"))
                    out.write("\n")
                elem.clear()
                continue
            if elem.tag == "programme":
                if elem.attrib.get("channel") in matched_channel_ids:
                    out.write(ET.tostring(elem, encoding="unicode"))
                    out.write("\n")
                    programme_count += 1
                elem.clear()
        out.write("</tv>\n")
    os.replace(temp_path, output_xmltv_gz_path)
    return programme_count


def _format_tv_start(attrs: dict[str, str]) -> str:
    if not attrs:
        return "<tv>\n"
    rendered_attrs = " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items())
    return f"<tv {rendered_attrs}>\n"
```

Keep the implementation pure: no network, no Emby calls, no scheduling, and no logging raw playlist URLs.

- [ ] **Step 8: Run EPG library tests**

Run:

```bash
python -m pytest -q tests/test_epg.py
```

Expected: all `tests/test_epg.py` tests pass.

- [ ] **Step 9: Commit Task 1**

Run:

```bash
git add app/epg.py tests/test_epg.py
GIT_AUTHOR_NAME='Codex' GIT_AUTHOR_EMAIL='codex@local' GIT_COMMITTER_NAME='Codex' GIT_COMMITTER_EMAIL='codex@local' git commit -m "feat: add epg trimming library"
```

## Task 2: EPG Worker Runtime

**Files:**
- Create: `app/epg_worker.py`
- Create: `tests/test_epg_worker.py`

- [ ] **Step 1: Write failing tests for scheduler helpers**

Create `tests/test_epg_worker.py` with:

```python
from __future__ import annotations

import gzip
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app import epg
from app import epg_worker


def gzip_text(path: Path) -> str:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def write_gzip(path: Path, text: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(text)


def test_parse_run_time_accepts_hour_minute():
    assert epg_worker.parse_run_time("04:00") == (4, 0)


def test_seconds_until_next_run_time_rolls_to_tomorrow():
    now = datetime(2026, 5, 1, 4, 1, tzinfo=timezone.utc)
    assert epg_worker.seconds_until_next_run_time(now, (4, 0)) == 23 * 3600 + 59 * 60


def test_should_run_immediately_when_output_missing(tmp_path: Path):
    assert epg_worker.should_run_immediately(tmp_path / "missing.xml.gz") is True
```

- [ ] **Step 2: Run scheduler tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_epg_worker.py
```

Expected: fails because `app.epg_worker` does not exist.

- [ ] **Step 3: Implement worker settings and scheduler helpers**

Create `app/epg_worker.py` with:

```python
#!/usr/bin/env python3
"""Daily XMLTV EPG trimmer runtime."""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
from pathlib import Path
from urllib import request

from app.emby_client import refresh_livetv_after_publish
from app.epg import EpgTrimSummary, trim_xmltv_to_playlist_channels

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOG = logging.getLogger("epg-worker")


@dataclass(frozen=True)
class EpgWorkerSettings:
    source_url: str
    run_time: tuple[int, int]
    playlist_path: Path
    output_path: Path
    state_file: Path
    work_dir: Path
    min_matched_channels: int
    min_programmes: int

    @classmethod
    def from_env(cls) -> "EpgWorkerSettings":
        return cls(
            source_url=os.getenv("EPG_SOURCE_URL", "http://epg.one/epg2.xml.gz"),
            run_time=parse_run_time(os.getenv("EPG_RUN_TIME", "04:00")),
            playlist_path=Path(os.getenv("EPG_PLAYLIST_PATH", "/data/output/playlist_emby_clean.m3u")),
            output_path=Path(os.getenv("EPG_OUTPUT_PATH", "/data/output/epg.xml.gz")),
            state_file=Path(os.getenv("EPG_STATE_FILE", "/data/state/.epg_trimmer_state")),
            work_dir=Path(os.getenv("EPG_WORK_DIR", "/data/state/epg")),
            min_matched_channels=int(os.getenv("EPG_MIN_MATCHED_CHANNELS", "1")),
            min_programmes=int(os.getenv("EPG_MIN_PROGRAMMES", "1")),
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_run_time(raw_value: str) -> tuple[int, int]:
    try:
        hour_text, minute_text = raw_value.strip().split(":", 1)
        hour = int(hour_text)
        minute = int(minute_text)
    except ValueError:
        LOG.warning("invalid EPG_RUN_TIME=%r, using 04:00", raw_value)
        return 4, 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        LOG.warning("invalid EPG_RUN_TIME=%r, using 04:00", raw_value)
        return 4, 0
    return hour, minute


def seconds_until_next_run_time(now: datetime, run_time: tuple[int, int]) -> float:
    hour, minute = run_time
    target = datetime.combine(now.date(), datetime_time(hour=hour, minute=minute), tzinfo=now.tzinfo)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def should_run_immediately(output_path: Path) -> bool:
    return not output_path.exists()
```

- [ ] **Step 4: Run scheduler tests**

Run:

```bash
python -m pytest -q tests/test_epg_worker.py::test_parse_run_time_accepts_hour_minute tests/test_epg_worker.py::test_seconds_until_next_run_time_rolls_to_tomorrow tests/test_epg_worker.py::test_should_run_immediately_when_output_missing
```

Expected: scheduler tests pass.

- [ ] **Step 5: Write failing tests for guarded publish and Emby refresh behavior**

Extend `tests/test_epg_worker.py` with:

```python
def settings_for(tmp_path: Path) -> epg_worker.EpgWorkerSettings:
    return epg_worker.EpgWorkerSettings(
        source_url="http://example.invalid/epg.xml.gz",
        run_time=(4, 0),
        playlist_path=tmp_path / "playlist.m3u",
        output_path=tmp_path / "published" / "epg.xml.gz",
        state_file=tmp_path / "state" / ".epg_trimmer_state",
        work_dir=tmp_path / "state" / "epg",
        min_matched_channels=1,
        min_programmes=1,
    )


def test_publish_candidate_rejects_zero_matches_without_refresh(monkeypatch, tmp_path: Path):
    settings = settings_for(tmp_path)
    settings.output_path.parent.mkdir()
    write_gzip(settings.output_path, "<tv><channel id='old' /></tv>")
    candidate = tmp_path / "candidate.xml.gz"
    write_gzip(candidate, "<tv></tv>")
    refresh_calls: list[object] = []
    monkeypatch.setattr(epg_worker, "refresh_livetv_after_publish", lambda logger: refresh_calls.append(logger))

    summary = epg.EpgTrimSummary(
        playlist_channel_count=1,
        source_channel_count=1,
        matched_channel_count=0,
        programme_count=0,
        unmatched_playlist_names=("Missing",),
    )

    published = epg_worker.publish_candidate(candidate, settings, summary)

    assert published is False
    assert gzip_text(settings.output_path) == "<tv><channel id='old' /></tv>"
    assert not settings.state_file.exists()
    assert refresh_calls == []


def test_publish_candidate_skips_refresh_when_content_unchanged(monkeypatch, tmp_path: Path):
    settings = settings_for(tmp_path)
    settings.output_path.parent.mkdir()
    candidate = tmp_path / "candidate.xml.gz"
    write_gzip(candidate, "<tv><programme channel='one' /></tv>")
    shutil.copyfile(candidate, settings.output_path)
    refresh_calls: list[object] = []
    monkeypatch.setattr(epg_worker, "refresh_livetv_after_publish", lambda logger: refresh_calls.append(logger))

    summary = epg.EpgTrimSummary(1, 1, 1, 1, ())

    published = epg_worker.publish_candidate(candidate, settings, summary)

    assert published is True
    assert settings.state_file.exists()
    assert refresh_calls == []


def test_publish_candidate_replaces_changed_output_and_refreshes(monkeypatch, tmp_path: Path):
    settings = settings_for(tmp_path)
    settings.output_path.parent.mkdir()
    write_gzip(settings.output_path, "<tv><programme channel='old' /></tv>")
    candidate = tmp_path / "candidate.xml.gz"
    write_gzip(candidate, "<tv><programme channel='one' /></tv>")
    refresh_calls: list[object] = []
    monkeypatch.setattr(epg_worker, "refresh_livetv_after_publish", lambda logger: refresh_calls.append(logger))

    summary = epg.EpgTrimSummary(1, 1, 1, 1, ())

    published = epg_worker.publish_candidate(candidate, settings, summary)

    assert published is True
    assert gzip_text(settings.output_path) == "<tv><programme channel='one' /></tv>"
    assert settings.state_file.exists()
    assert len(refresh_calls) == 1
```

- [ ] **Step 6: Run guarded publish tests to verify they fail**

Run:

```bash
python -m pytest -q tests/test_epg_worker.py
```

Expected: fails because `publish_candidate` is not implemented.

- [ ] **Step 7: Implement download, guarded publish, run-once, and main loop**

Extend `app/epg_worker.py` with:

```python
def download_epg(source_url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_suffix(destination.suffix + ".tmp")
    with request.urlopen(source_url, timeout=180) as response, temp_path.open("wb") as fh:
        shutil.copyfileobj(response, fh)
    with gzip.open(temp_path, "rb") as fh:
        fh.read(1)
    os.replace(temp_path, destination)


def _same_gzip_payload(left: Path, right: Path) -> bool:
    if not left.exists() or not right.exists():
        return False
    with gzip.open(left, "rb") as left_fh, gzip.open(right, "rb") as right_fh:
        while True:
            left_chunk = left_fh.read(1024 * 1024)
            right_chunk = right_fh.read(1024 * 1024)
            if left_chunk != right_chunk:
                return False
            if not left_chunk:
                return True


def publish_candidate(candidate_path: Path, settings: EpgWorkerSettings, summary: EpgTrimSummary) -> bool:
    if summary.matched_channel_count < settings.min_matched_channels:
        LOG.error(
            "EPG publish rejected: matched_channels=%d minimum=%d unmatched=%s",
            summary.matched_channel_count,
            settings.min_matched_channels,
            list(summary.unmatched_playlist_names),
        )
        return False
    if summary.programme_count < settings.min_programmes:
        LOG.error(
            "EPG publish rejected: programmes=%d minimum=%d",
            summary.programme_count,
            settings.min_programmes,
        )
        return False

    settings.output_path.parent.mkdir(parents=True, exist_ok=True)
    changed = not _same_gzip_payload(candidate_path, settings.output_path)
    if changed:
        temp_output = settings.output_path.with_suffix(settings.output_path.suffix + ".tmp")
        shutil.copyfile(candidate_path, temp_output)
        os.replace(temp_output, settings.output_path)
        warning = refresh_livetv_after_publish(LOG)
        if warning:
            LOG.warning(warning)
    else:
        LOG.info("Emby refresh skipped: trimmed EPG content unchanged")

    settings.state_file.parent.mkdir(parents=True, exist_ok=True)
    settings.state_file.write_text(now_iso() + "\n", encoding="utf-8")
    LOG.info(
        "EPG publish complete changed=%s playlist_channels=%d source_channels=%d matched_channels=%d programmes=%d",
        changed,
        summary.playlist_channel_count,
        summary.source_channel_count,
        summary.matched_channel_count,
        summary.programme_count,
    )
    return True


def run_once(settings: EpgWorkerSettings | None = None) -> bool:
    active_settings = settings or EpgWorkerSettings.from_env()
    if not active_settings.playlist_path.exists():
        LOG.error("EPG playlist source missing: %s", active_settings.playlist_path)
        return False

    active_settings.work_dir.mkdir(parents=True, exist_ok=True)
    source_path = active_settings.work_dir / "source.xml.gz"
    candidate_path = active_settings.work_dir / "candidate.xml.gz"

    download_epg(active_settings.source_url, source_path)
    summary = trim_xmltv_to_playlist_channels(
        source_xmltv_gz_path=source_path,
        playlist_path=active_settings.playlist_path,
        output_xmltv_gz_path=candidate_path,
    )
    return publish_candidate(candidate_path, active_settings, summary)


def _safe_run_once(settings: EpgWorkerSettings) -> None:
    try:
        run_once(settings)
    except Exception:  # noqa: BLE001
        LOG.exception("EPG run failed")


def main() -> None:
    settings = EpgWorkerSettings.from_env()
    LOG.info("EPG scheduler configured run_time=%02d:%02d output=%s", settings.run_time[0], settings.run_time[1], settings.output_path)
    if not should_run_immediately(settings.output_path):
        sleep_seconds = seconds_until_next_run_time(datetime.now().astimezone(), settings.run_time)
        LOG.info("initial EPG run scheduled in %.0f seconds", sleep_seconds)
        time.sleep(sleep_seconds)

    while True:
        _safe_run_once(settings)
        sleep_seconds = seconds_until_next_run_time(datetime.now().astimezone(), settings.run_time)
        LOG.info("next EPG run scheduled in %.0f seconds", sleep_seconds)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
```

Use imports already listed in Step 3. If `shutil` is missing from the generated test module, add it.

- [ ] **Step 8: Run worker tests**

Run:

```bash
python -m pytest -q tests/test_epg_worker.py
```

Expected: all worker tests pass.

- [ ] **Step 9: Commit Task 2**

Run:

```bash
git add app/epg_worker.py tests/test_epg_worker.py
GIT_AUTHOR_NAME='Codex' GIT_AUTHOR_EMAIL='codex@local' GIT_COMMITTER_NAME='Codex' GIT_COMMITTER_EMAIL='codex@local' git commit -m "feat: add epg trimmer worker"
```

## Task 3: Compose And Documentation Wiring

**Files:**
- Modify: `docker-compose.yml`
- Modify: `tests/test_compose_config.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write failing Compose assertions**

Extend `tests/test_compose_config.py`:

```python
def test_epg_trimmer_writes_epg_served_by_static_container():
    compose = Path("docker-compose.yml").read_text(encoding="utf-8")
    static_compose = Path("docker-compose.playlist.yml").read_text(encoding="utf-8")

    assert "epg-trimmer:" in compose
    assert "container_name: epg-trimmer" in compose
    assert "EPG_RUN_TIME: ${EPG_RUN_TIME:-04:00}" in compose
    assert "EPG_SOURCE_URL: ${EPG_SOURCE_URL:-http://epg.one/epg2.xml.gz}" in compose
    assert "EPG_PLAYLIST_PATH: ${EPG_PLAYLIST_PATH:-/data/output/playlist_emby_clean.m3u}" in compose
    assert "EPG_OUTPUT_PATH: ${EPG_OUTPUT_PATH:-/data/output/epg.xml.gz}" in compose
    assert "EPG_STATE_FILE: ${EPG_STATE_FILE:-/data/state/.epg_trimmer_state}" in compose
    assert "EPG_WORK_DIR: ${EPG_WORK_DIR:-/data/state/epg}" in compose
    assert 'command: ["python", "-m", "app.epg_worker"]' in compose
    assert "./published:/data/output:rw" in compose
    assert "./output:/data/state:rw" in compose
    assert "./published:/usr/share/nginx/html:ro" in static_compose
```

- [ ] **Step 2: Run Compose test to verify it fails**

Run:

```bash
python -m pytest -q tests/test_compose_config.py::test_epg_trimmer_writes_epg_served_by_static_container
```

Expected: fails because `epg-trimmer` is not in Compose.

- [ ] **Step 3: Add `epg-trimmer` service**

Modify `docker-compose.yml` by adding a second service after `playlist-sanitizer`:

```yaml
  epg-trimmer:
    build:
      context: .
      dockerfile: Dockerfile.playlist-sanitizer
    container_name: epg-trimmer
    working_dir: /app
    volumes:
      - ./published:/data/output:rw
      - ./output:/data/state:rw
      - ./app:/app/app:ro
    environment:
      TZ: ${TZ:-Asia/Jerusalem}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
      EPG_SOURCE_URL: ${EPG_SOURCE_URL:-http://epg.one/epg2.xml.gz}
      EPG_RUN_TIME: ${EPG_RUN_TIME:-04:00}
      EPG_PLAYLIST_PATH: ${EPG_PLAYLIST_PATH:-/data/output/playlist_emby_clean.m3u}
      EPG_OUTPUT_PATH: ${EPG_OUTPUT_PATH:-/data/output/epg.xml.gz}
      EPG_STATE_FILE: ${EPG_STATE_FILE:-/data/state/.epg_trimmer_state}
      EPG_WORK_DIR: ${EPG_WORK_DIR:-/data/state/epg}
      EPG_MIN_MATCHED_CHANNELS: ${EPG_MIN_MATCHED_CHANNELS:-1}
      EPG_MIN_PROGRAMMES: ${EPG_MIN_PROGRAMMES:-1}

      # Emby settings
      EMBY_BASE_URL: ${EMBY_BASE_URL:-}
      EMBY_API_KEY: ${EMBY_API_KEY:-}
      EMBY_LIVETV_TUNER_ID: ${EMBY_LIVETV_TUNER_ID:-}

    command: ["python", "-m", "app.epg_worker"]
    restart: unless-stopped
```

- [ ] **Step 4: Update README**

Add an EPG section to `README.md`:

````markdown
## EPG trimmer runtime

For automated XMLTV guide trimming, run:

```bash
docker compose up -d --build epg-trimmer
```

The worker downloads `http://epg.one/epg2.xml.gz`, matches XMLTV channel display names against `published/playlist_emby_clean.m3u`, and writes the trimmed guide to `published/epg.xml.gz`. The existing static nginx container serves it at:

- `http://<host>:8766/epg.xml.gz`

By default, EPG trimming runs daily at `04:00` in the container timezone, after the playlist sanitizer's default `03:00` run. A missing `epg.xml.gz` triggers an immediate first run. A failed download, invalid XML, zero channel matches, or zero programmes preserves the previous EPG and skips Emby refresh.

The worker refreshes Emby's guide only when the trimmed EPG content changes and Emby credentials are configured through `EMBY_BASE_URL` and `EMBY_API_KEY`.
````

Also add `./published` and `./output` to the Synology mount list for the EPG worker if the README already lists service mounts.

- [ ] **Step 5: Update AGENTS.md**

Add these facts to `AGENTS.md`:

```markdown
- `app/epg.py` - XMLTV trimming library. Extracts clean-playlist channel names, matches upstream EPG display names, and writes a gzip XMLTV containing only matched channels and programmes.
- `app/epg_worker.py` - daily EPG worker. Downloads upstream XMLTV, calls the trimmer, publishes `epg.xml.gz` atomically, and refreshes Emby only after changed successful output.
```

Add EPG environment variables and the command:

```bash
docker compose up -d --build epg-trimmer
```

Document that generated playlist and EPG files under `published/` should not be committed.

- [ ] **Step 6: Run Compose/doc-related tests**

Run:

```bash
python -m pytest -q tests/test_compose_config.py
```

Expected: all compose config tests pass.

- [ ] **Step 7: Commit Task 3**

Run:

```bash
git add docker-compose.yml tests/test_compose_config.py README.md AGENTS.md
GIT_AUTHOR_NAME='Codex' GIT_AUTHOR_EMAIL='codex@local' GIT_COMMITTER_NAME='Codex' GIT_COMMITTER_EMAIL='codex@local' git commit -m "docs: wire epg trimmer service"
```

## Task 4: Full Verification And Runtime Smoke

**Files:**
- Modify only if verification reveals a defect.

- [ ] **Step 1: Run syntax check**

Run:

```bash
python -m compileall -q app tests
```

Expected: command exits 0.

- [ ] **Step 2: Run full unit tests**

Run:

```bash
python -m pytest -q tests
```

Expected: all tests pass.

- [ ] **Step 3: Run a local synthetic EPG worker smoke without network**

Use a temporary directory and monkeypatch-free command-line smoke:

```bash
tmpdir="$(mktemp -d)"
mkdir -p "$tmpdir/published" "$tmpdir/state"
printf '#EXTM3U\n#EXTINF:-1,Channel One\nhttp://example.invalid/one\n' > "$tmpdir/published/playlist_emby_clean.m3u"
python - <<'PY' "$tmpdir/state/source.xml.gz"
import gzip
import sys
from pathlib import Path
path = Path(sys.argv[1])
with gzip.open(path, "wt", encoding="utf-8") as fh:
    fh.write("<tv><channel id='one'><display-name>Channel One</display-name></channel><programme channel='one' start='20260501040000 +0000' stop='20260501050000 +0000'><title>One</title></programme></tv>")
PY
python - <<'PY' "$tmpdir"
from pathlib import Path
from app import epg_worker
base = Path(__import__("sys").argv[1])
settings = epg_worker.EpgWorkerSettings(
    source_url="unused",
    run_time=(4, 0),
    playlist_path=base / "published" / "playlist_emby_clean.m3u",
    output_path=base / "published" / "epg.xml.gz",
    state_file=base / "state" / ".epg_trimmer_state",
    work_dir=base / "state" / "epg",
    min_matched_channels=1,
    min_programmes=1,
)
source = base / "state" / "source.xml.gz"
candidate = base / "state" / "candidate.xml.gz"
summary = epg_worker.trim_xmltv_to_playlist_channels(source, settings.playlist_path, candidate)
assert epg_worker.publish_candidate(candidate, settings, summary) is True
assert settings.output_path.exists()
assert settings.state_file.exists()
PY
```

Expected: command exits 0 and produces `epg.xml.gz` plus `.epg_trimmer_state` in the temp directory.

- [ ] **Step 4: Inspect final diff**

Run:

```bash
git status --short --branch
git diff --stat HEAD
```

Expected: only intended implementation changes are present. No generated playlist or EPG output files are staged or modified.

- [ ] **Step 5: Commit verification fixes when required**

If Step 1, 2, or 3 required fixes, commit them:

```bash
git add app tests docker-compose.yml README.md AGENTS.md
GIT_AUTHOR_NAME='Codex' GIT_AUTHOR_EMAIL='codex@local' GIT_COMMITTER_NAME='Codex' GIT_COMMITTER_EMAIL='codex@local' git commit -m "fix: stabilize epg trimmer verification"
```

If no fixes were needed, do not create an empty commit.
