from __future__ import annotations

import gzip
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape, quoteattr


@dataclass(frozen=True)
class EpgTrimSummary:
    playlist_channel_count: int
    source_channel_count: int
    matched_channel_count: int
    programme_count: int
    unmatched_playlist_names: tuple[str, ...]


def normalize_channel_name(value: str) -> str:
    words: list[str] = []
    current: list[str] = []

    normalized = unicodedata.normalize("NFKC", value).casefold()

    for char in normalized:
        if char.isalnum():
            current.append(char)
        elif current:
            words.append("".join(current))
            current.clear()

    if current:
        words.append("".join(current))

    return " ".join(words)


def extract_playlist_channel_names(path: Path) -> list[str]:
    names: list[str] = []

    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped.casefold().startswith("#extinf"):
                continue

            _, separator, name = stripped.partition(",")
            if separator and name.strip():
                names.append(name.strip())

    return names


def trim_xmltv_to_playlist_channels(
    source_xmltv_gz_path: Path,
    playlist_path: Path,
    output_xmltv_gz_path: Path,
) -> EpgTrimSummary:
    playlist_names = extract_playlist_channel_names(playlist_path)
    wanted_names = {
        normalized_name
        for name in playlist_names
        if (normalized_name := normalize_channel_name(name))
    }

    first_pass = _collect_xmltv_matches(source_xmltv_gz_path, wanted_names)

    programme_count = _write_trimmed_xmltv_atomically(
        source_xmltv_gz_path,
        output_xmltv_gz_path,
        root_tag=first_pass.root_tag,
        root_attrib=first_pass.root_attrib,
        matched_channel_ids=first_pass.matched_channel_ids,
    )

    unmatched_playlist_names = tuple(
        name
        for name in playlist_names
        if normalize_channel_name(name) not in first_pass.matched_names
    )

    return EpgTrimSummary(
        playlist_channel_count=len(playlist_names),
        source_channel_count=first_pass.source_channel_count,
        matched_channel_count=len(first_pass.matched_channel_ids),
        programme_count=programme_count,
        unmatched_playlist_names=unmatched_playlist_names,
    )


@dataclass(frozen=True)
class _FirstPassResult:
    root_tag: str
    root_attrib: dict[str, str]
    source_channel_count: int
    matched_channel_ids: frozenset[str]
    matched_names: frozenset[str]


def _collect_xmltv_matches(
    source_path: Path,
    wanted_names: set[str],
) -> _FirstPassResult:
    root_tag = "tv"
    root_attrib: dict[str, str] = {}
    source_channel_count = 0
    matched_channel_ids: set[str] = set()
    matched_names: set[str] = set()
    root_element: ET.Element | None = None

    with gzip.open(source_path, "rb") as source_fh:
        for event, element in ET.iterparse(source_fh, events=("start", "end")):
            if event == "start" and element.tag == "tv":
                root_element = element
                root_tag = element.tag
                root_attrib = dict(element.attrib)
                continue

            if event != "end" or element.tag != "channel":
                continue

            source_channel_count += 1
            normalized_display_names = {
                normalize_channel_name(display_name.text or "")
                for display_name in element.findall("display-name")
            }
            normalized_display_names.discard("")
            matching_names = wanted_names.intersection(normalized_display_names)

            if matching_names and (channel_id := element.attrib.get("id")) is not None:
                matched_channel_ids.add(channel_id)
                matched_names.update(matching_names)

            element.clear()
            if root_element is not None:
                root_element.clear()

    return _FirstPassResult(
        root_tag=root_tag,
        root_attrib=root_attrib,
        source_channel_count=source_channel_count,
        matched_channel_ids=frozenset(matched_channel_ids),
        matched_names=frozenset(matched_names),
    )


def _write_trimmed_xmltv_atomically(
    source_path: Path,
    output_path: Path,
    *,
    root_tag: str,
    root_attrib: dict[str, str],
    matched_channel_ids: frozenset[str],
) -> int:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.",
        suffix=".tmp",
        dir=output_path.parent,
    )
    os.close(fd)
    temp_path = Path(temp_name)

    try:
        with gzip.open(temp_path, "wt", encoding="utf-8") as output_fh:
            output_fh.write("<?xml version='1.0' encoding='UTF-8'?>\n")
            output_fh.write(_format_start_tag(root_tag, root_attrib))
            output_fh.write("\n")
            programme_count = _stream_matching_xmltv_elements(
                source_path,
                output_fh,
                root_tag=root_tag,
                matched_channel_ids=matched_channel_ids,
            )

        os.replace(temp_path, output_path)
        return programme_count
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _stream_matching_xmltv_elements(
    source_path: Path,
    output_fh,
    *,
    root_tag: str,
    matched_channel_ids: frozenset[str],
) -> int:
    programme_count = 0
    root_element: ET.Element | None = None

    with gzip.open(source_path, "rb") as source_fh:
        for event, element in ET.iterparse(source_fh, events=("start", "end")):
            if event == "start" and element.tag == root_tag:
                root_element = element
                continue

            if event != "end":
                continue

            if element.tag == "channel":
                if element.attrib.get("id") in matched_channel_ids:
                    _write_element(output_fh, element)
                element.clear()
                if root_element is not None:
                    root_element.clear()
                continue

            if element.tag == "programme":
                if element.attrib.get("channel") in matched_channel_ids:
                    programme_count += 1
                    _write_element(output_fh, element)
                element.clear()
                if root_element is not None:
                    root_element.clear()

    output_fh.write(f"</{escape(root_tag)}>\n")
    return programme_count


def _write_element(output_fh, element: ET.Element) -> None:
    output_fh.write(ET.tostring(element, encoding="unicode"))
    output_fh.write("\n")


def _format_start_tag(tag: str, attrib: dict[str, str]) -> str:
    attrs = "".join(
        f" {escape(name)}={quoteattr(value)}" for name, value in attrib.items()
    )
    return f"<{escape(tag)}{attrs}>"
