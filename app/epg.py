from __future__ import annotations

import gzip
import os
import tempfile
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

    for char in value.casefold():
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
            if not stripped.startswith("#EXTINF"):
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
    playlist_names_by_normalized = {
        normalize_channel_name(name): name
        for name in playlist_names
        if normalize_channel_name(name)
    }
    wanted_names = set(playlist_names_by_normalized)

    root_tag = "tv"
    root_attrib: dict[str, str] = {}
    source_channel_count = 0
    programme_count = 0
    matched_names: set[str] = set()
    matched_channel_ids: set[str] = set()
    kept_channels: list[str] = []
    kept_programmes: list[str] = []

    with gzip.open(source_xmltv_gz_path, "rb") as source_fh:
        for event, element in ET.iterparse(source_fh, events=("start", "end")):
            if event == "start" and element.tag == "tv":
                root_tag = element.tag
                root_attrib = dict(element.attrib)
                continue

            if event != "end":
                continue

            if element.tag == "channel":
                source_channel_count += 1
                normalized_display_names = {
                    normalize_channel_name(display_name.text or "")
                    for display_name in element.findall("display-name")
                }
                normalized_display_names.discard("")
                matching_names = wanted_names.intersection(normalized_display_names)

                if matching_names:
                    channel_id = element.attrib.get("id")
                    if channel_id is not None:
                        matched_channel_ids.add(channel_id)
                        matched_names.update(matching_names)
                        kept_channels.append(ET.tostring(element, encoding="unicode"))

                element.clear()
                continue

            if element.tag == "programme":
                if element.attrib.get("channel") in matched_channel_ids:
                    programme_count += 1
                    kept_programmes.append(ET.tostring(element, encoding="unicode"))

                element.clear()

    _write_xmltv_gzip_atomically(
        output_xmltv_gz_path,
        root_tag=root_tag,
        root_attrib=root_attrib,
        channel_elements=kept_channels,
        programme_elements=kept_programmes,
    )

    unmatched_playlist_names = tuple(
        name
        for name in playlist_names
        if normalize_channel_name(name) not in matched_names
    )

    return EpgTrimSummary(
        playlist_channel_count=len(playlist_names),
        source_channel_count=source_channel_count,
        matched_channel_count=len(matched_channel_ids),
        programme_count=programme_count,
        unmatched_playlist_names=unmatched_playlist_names,
    )


def _write_xmltv_gzip_atomically(
    output_path: Path,
    *,
    root_tag: str,
    root_attrib: dict[str, str],
    channel_elements: list[str],
    programme_elements: list[str],
) -> None:
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
            for element_xml in [*channel_elements, *programme_elements]:
                output_fh.write(element_xml)
                output_fh.write("\n")
            output_fh.write(f"</{escape(root_tag)}>\n")

        os.replace(temp_path, output_path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _format_start_tag(tag: str, attrib: dict[str, str]) -> str:
    attrs = "".join(
        f" {escape(name)}={quoteattr(value)}" for name, value in attrib.items()
    )
    return f"<{escape(tag)}{attrs}>"
