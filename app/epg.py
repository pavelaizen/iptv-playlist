from __future__ import annotations

import gzip
import os
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from xml.sax import ContentHandler, SAXNotRecognizedException, SAXNotSupportedException, make_parser
from xml.sax.saxutils import XMLGenerator
from xml.sax.saxutils import escape, quoteattr


@dataclass(frozen=True)
class EpgTrimSummary:
    playlist_channel_count: int
    source_channel_count: int
    matched_channel_count: int
    programme_count: int
    unmatched_playlist_names: tuple[str, ...]


ISRAEL_PRIMARY_SOURCE_KEY = "israel_primary"
ISRAEL_FALLBACK_SOURCE_KEY = "israel_fallback"


@dataclass(frozen=True)
class ChannelIdOverride:
    primary_source_key: str
    primary_channel_id: str
    fallback_source_key: str | None = None
    fallback_channel_id: str | None = None


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
            if not _is_extinf_line(stripped):
                continue

            name = _extract_extinf_name(stripped)
            if name:
                names.append(name.strip())

    return names


def _is_extinf_line(value: str) -> bool:
    lowered = value.casefold()
    return lowered.startswith("#extinf:") or lowered.startswith("#extinf,")


def _extract_extinf_name(value: str) -> str | None:
    in_quotes = False

    for index, char in enumerate(value):
        if char == '"':
            in_quotes = not in_quotes
            continue

        if char == "," and not in_quotes:
            name = value[index + 1 :].strip()
            return name or None

    return None


def trim_xmltv_to_playlist_channels(
    source_xmltv_gz_path: Path,
    playlist_path: Path,
    output_xmltv_path: Path,
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
        output_xmltv_path,
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


def trim_xmltv_to_playlist_channels_with_israeli_overrides(
    *,
    default_source_xmltv_gz_path: Path,
    israel_primary_source_xmltv_gz_path: Path,
    israel_fallback_source_xmltv_gz_path: Path,
    playlist_path: Path,
    output_xmltv_path: Path,
) -> EpgTrimSummary:
    playlist_names = extract_playlist_channel_names(playlist_path)
    normalized_playlist_names = [
        normalized_name
        for name in playlist_names
        if (normalized_name := normalize_channel_name(name))
    ]

    non_override_wanted_names = {
        name
        for name in normalized_playlist_names
        if name not in _ISRAELI_CHANNEL_OVERRIDES_BY_NAME
    }
    default_first_pass = _collect_xmltv_matches(
        default_source_xmltv_gz_path,
        non_override_wanted_names,
    )

    source_settings: dict[str, _SourceSelection] = {
        "default": _SourceSelection(
            path=default_source_xmltv_gz_path,
            root_tag=default_first_pass.root_tag,
            root_attrib=default_first_pass.root_attrib,
            selected_channel_ids=set(default_first_pass.matched_channel_ids),
            source_channel_count=default_first_pass.source_channel_count,
        ),
        ISRAEL_PRIMARY_SOURCE_KEY: _SourceSelection(
            path=israel_primary_source_xmltv_gz_path,
            root_tag="tv",
            root_attrib={},
            selected_channel_ids=set(),
            source_channel_count=0,
        ),
        ISRAEL_FALLBACK_SOURCE_KEY: _SourceSelection(
            path=israel_fallback_source_xmltv_gz_path,
            root_tag="tv",
            root_attrib={},
            selected_channel_ids=set(),
            source_channel_count=0,
        ),
    }

    matched_names = set(default_first_pass.matched_names)
    id_stats_by_source = _collect_israeli_channel_id_stats(
        israel_primary_source_xmltv_gz_path,
        israel_fallback_source_xmltv_gz_path,
    )
    for source_key, id_stats in id_stats_by_source.items():
        source_settings[source_key].root_tag = id_stats.root_tag
        source_settings[source_key].root_attrib = id_stats.root_attrib
        source_settings[source_key].source_channel_count = id_stats.source_channel_count

    for normalized_name in normalized_playlist_names:
        override = _ISRAELI_CHANNEL_OVERRIDES_BY_NAME.get(normalized_name)
        if override is None:
            continue

        selected_channel_id = _select_override_channel_id(override, id_stats_by_source)
        if selected_channel_id is None:
            continue

        matched_names.add(normalized_name)
        source_settings[selected_channel_id[0]].selected_channel_ids.add(
            selected_channel_id[1]
        )

    programme_count = _write_combined_trimmed_xmltv_atomically(
        output_path=output_xmltv_path,
        root_tag=default_first_pass.root_tag,
        root_attrib=default_first_pass.root_attrib,
        sources_in_order=(
            source_settings["default"],
            source_settings[ISRAEL_PRIMARY_SOURCE_KEY],
            source_settings[ISRAEL_FALLBACK_SOURCE_KEY],
        ),
    )

    unmatched_playlist_names = tuple(
        name
        for name in playlist_names
        if normalize_channel_name(name) not in matched_names
    )

    matched_channel_count = sum(
        len(selection.selected_channel_ids)
        for selection in source_settings.values()
    )
    source_channel_count = sum(
        selection.source_channel_count for selection in source_settings.values()
    )

    return EpgTrimSummary(
        playlist_channel_count=len(playlist_names),
        source_channel_count=source_channel_count,
        matched_channel_count=matched_channel_count,
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


@dataclass
class _SourceSelection:
    path: Path
    root_tag: str
    root_attrib: dict[str, str]
    selected_channel_ids: set[str]
    source_channel_count: int


@dataclass(frozen=True)
class _ChannelIdStats:
    root_tag: str
    root_attrib: dict[str, str]
    source_channel_count: int
    present_channel_ids: frozenset[str]
    programme_counts_by_channel_id: dict[str, int]


def _build_israeli_channel_overrides() -> dict[str, ChannelIdOverride]:
    return {
        normalize_channel_name("Channel 9 FHD IL"): ChannelIdOverride(
            primary_source_key=ISRAEL_PRIMARY_SOURCE_KEY,
            primary_channel_id="9kanal-israel",
        ),
        normalize_channel_name("Kan 11 HD IL"): ChannelIdOverride(
            primary_source_key=ISRAEL_PRIMARY_SOURCE_KEY,
            primary_channel_id="channel-11-il",
            fallback_source_key=ISRAEL_FALLBACK_SOURCE_KEY,
            fallback_channel_id="כאן11.il",
        ),
        normalize_channel_name("Keshet 12 HD IL"): ChannelIdOverride(
            primary_source_key=ISRAEL_PRIMARY_SOURCE_KEY,
            primary_channel_id="channel-12-il",
            fallback_source_key=ISRAEL_FALLBACK_SOURCE_KEY,
            fallback_channel_id="קשת.il",
        ),
        normalize_channel_name("Keshet 12 FHD IL"): ChannelIdOverride(
            primary_source_key=ISRAEL_PRIMARY_SOURCE_KEY,
            primary_channel_id="channel-12-il",
            fallback_source_key=ISRAEL_FALLBACK_SOURCE_KEY,
            fallback_channel_id="קשת.il",
        ),
        normalize_channel_name("Reshet 13 HD IL"): ChannelIdOverride(
            primary_source_key=ISRAEL_PRIMARY_SOURCE_KEY,
            primary_channel_id="channel-13-il",
            fallback_source_key=ISRAEL_FALLBACK_SOURCE_KEY,
            fallback_channel_id="רשת.il",
        ),
        normalize_channel_name("Channel 14 FHD IL"): ChannelIdOverride(
            primary_source_key=ISRAEL_FALLBACK_SOURCE_KEY,
            primary_channel_id="ערוץ14.il",
        ),
    }


_ISRAELI_CHANNEL_OVERRIDES_BY_NAME = _build_israeli_channel_overrides()


def _collect_israeli_channel_id_stats(
    israel_primary_source_xmltv_gz_path: Path,
    israel_fallback_source_xmltv_gz_path: Path,
) -> dict[str, _ChannelIdStats]:
    primary_channel_ids = {
        override.primary_channel_id
        for override in _ISRAELI_CHANNEL_OVERRIDES_BY_NAME.values()
        if override.primary_source_key == ISRAEL_PRIMARY_SOURCE_KEY
    }
    fallback_channel_ids = {
        override.primary_channel_id
        for override in _ISRAELI_CHANNEL_OVERRIDES_BY_NAME.values()
        if override.primary_source_key == ISRAEL_FALLBACK_SOURCE_KEY
    }
    fallback_channel_ids.update(
        override.fallback_channel_id
        for override in _ISRAELI_CHANNEL_OVERRIDES_BY_NAME.values()
        if override.fallback_source_key == ISRAEL_FALLBACK_SOURCE_KEY
        and override.fallback_channel_id is not None
    )

    return {
        ISRAEL_PRIMARY_SOURCE_KEY: _collect_channel_id_stats(
            israel_primary_source_xmltv_gz_path,
            primary_channel_ids,
        ),
        ISRAEL_FALLBACK_SOURCE_KEY: _collect_channel_id_stats(
            israel_fallback_source_xmltv_gz_path,
            fallback_channel_ids,
        ),
    }


def _select_override_channel_id(
    override: ChannelIdOverride,
    id_stats_by_source: dict[str, _ChannelIdStats],
) -> tuple[str, str] | None:
    if _is_usable_channel_id(
        source_key=override.primary_source_key,
        channel_id=override.primary_channel_id,
        id_stats_by_source=id_stats_by_source,
    ):
        return override.primary_source_key, override.primary_channel_id

    if (
        override.fallback_source_key is not None
        and override.fallback_channel_id is not None
        and _is_usable_channel_id(
            source_key=override.fallback_source_key,
            channel_id=override.fallback_channel_id,
            id_stats_by_source=id_stats_by_source,
        )
    ):
        return override.fallback_source_key, override.fallback_channel_id

    return None


def _is_usable_channel_id(
    *,
    source_key: str,
    channel_id: str,
    id_stats_by_source: dict[str, _ChannelIdStats],
) -> bool:
    stats = id_stats_by_source[source_key]
    if channel_id not in stats.present_channel_ids:
        return False
    return stats.programme_counts_by_channel_id.get(channel_id, 0) > 0


def _collect_channel_id_stats(
    source_path: Path,
    wanted_channel_ids: set[str],
) -> _ChannelIdStats:
    handler = _ChannelIdStatsHandler(wanted_channel_ids)
    _parse_gzip_xml(source_path, handler)
    return handler.result()


def _write_combined_trimmed_xmltv_atomically(
    *,
    output_path: Path,
    root_tag: str,
    root_attrib: dict[str, str],
    sources_in_order: tuple[_SourceSelection, ...],
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
        with temp_path.open("w", encoding="utf-8") as output_fh:
            output_fh.write("<?xml version='1.0' encoding='UTF-8'?>\n")
            output_fh.write(_format_start_tag(root_tag, root_attrib))
            output_fh.write("\n")

            programme_count = 0
            for source in sources_in_order:
                if not source.selected_channel_ids:
                    continue
                programme_count += _stream_matching_xmltv_elements_from_source(
                    source.path,
                    output_fh,
                    root_tag=source.root_tag,
                    matched_channel_ids=frozenset(source.selected_channel_ids),
                )

            output_fh.write(f"</{escape(root_tag)}>\n")

        os.replace(temp_path, output_path)
        return programme_count
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _collect_xmltv_matches(
    source_path: Path,
    wanted_names: set[str],
) -> _FirstPassResult:
    handler = _ChannelMatchHandler(wanted_names)
    _parse_gzip_xml(source_path, handler)
    return handler.result()


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
        with temp_path.open("w", encoding="utf-8") as output_fh:
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
    programme_count = _stream_matching_xmltv_elements_from_source(
        source_path,
        output_fh,
        root_tag=root_tag,
        matched_channel_ids=matched_channel_ids,
    )
    output_fh.write(f"</{escape(root_tag)}>\n")
    return programme_count


def _stream_matching_xmltv_elements_from_source(
    source_path: Path,
    output_fh,
    *,
    root_tag: str,
    matched_channel_ids: frozenset[str],
) -> int:
    handler = _MatchingXmltvWriter(
        output_fh=output_fh,
        root_tag=root_tag,
        matched_channel_ids=matched_channel_ids,
    )
    _parse_gzip_xml(source_path, handler)
    return handler.programme_count


def _format_start_tag(tag: str, attrib: dict[str, str]) -> str:
    attrs = "".join(
        f" {escape(name)}={quoteattr(value)}" for name, value in attrib.items()
    )
    return f"<{escape(tag)}{attrs}>"


class _ChannelMatchHandler(ContentHandler):
    def __init__(self, wanted_names: set[str]) -> None:
        super().__init__()
        self._wanted_names = wanted_names
        self._root_tag = "tv"
        self._root_attrib: dict[str, str] = {}
        self._depth = 0
        self._source_channel_count = 0
        self._matched_channel_ids: set[str] = set()
        self._matched_names: set[str] = set()
        self._current_channel_id: str | None = None
        self._display_name_depth = 0
        self._display_name_parts: list[str] = []
        self._current_display_names: set[str] = set()

    def startElement(self, name, attrs):  # noqa: N802
        self._depth += 1
        if self._depth == 1:
            self._root_tag = name
            self._root_attrib = dict(attrs.items())
            return

        if name == "channel":
            self._source_channel_count += 1
            self._current_channel_id = attrs.get("id")
            self._current_display_names = set()
            return

        if self._current_channel_id is not None and name == "display-name":
            self._display_name_depth = self._depth
            self._display_name_parts = []

    def characters(self, content):  # noqa: N802
        if self._display_name_depth:
            self._display_name_parts.append(content)

    def endElement(self, name):  # noqa: N802
        if self._display_name_depth == self._depth and name == "display-name":
            normalized_name = normalize_channel_name("".join(self._display_name_parts))
            if normalized_name:
                self._current_display_names.add(normalized_name)
            self._display_name_depth = 0
            self._display_name_parts = []
        elif name == "channel" and self._current_channel_id is not None:
            matching_names = self._wanted_names.intersection(self._current_display_names)
            if matching_names:
                self._matched_channel_ids.add(self._current_channel_id)
                self._matched_names.update(matching_names)
            self._current_channel_id = None
            self._current_display_names = set()

        self._depth -= 1

    def result(self) -> _FirstPassResult:
        return _FirstPassResult(
            root_tag=self._root_tag,
            root_attrib=self._root_attrib,
            source_channel_count=self._source_channel_count,
            matched_channel_ids=frozenset(self._matched_channel_ids),
            matched_names=frozenset(self._matched_names),
        )


class _ChannelIdStatsHandler(ContentHandler):
    def __init__(self, wanted_channel_ids: set[str]) -> None:
        super().__init__()
        self._wanted_channel_ids = wanted_channel_ids
        self._root_tag = "tv"
        self._root_attrib: dict[str, str] = {}
        self._depth = 0
        self._source_channel_count = 0
        self._present_channel_ids: set[str] = set()
        self._programme_counts_by_channel_id: dict[str, int] = {}

    def startElement(self, name, attrs):  # noqa: N802
        self._depth += 1
        if self._depth == 1:
            self._root_tag = name
            self._root_attrib = dict(attrs.items())
            return

        if name == "channel":
            self._source_channel_count += 1
            channel_id = attrs.get("id")
            if channel_id in self._wanted_channel_ids:
                self._present_channel_ids.add(channel_id)
            return

        if name == "programme":
            channel_id = attrs.get("channel")
            if channel_id in self._wanted_channel_ids:
                self._programme_counts_by_channel_id[channel_id] = (
                    self._programme_counts_by_channel_id.get(channel_id, 0) + 1
                )

    def endElement(self, name):  # noqa: N802, ARG002
        self._depth -= 1

    def result(self) -> _ChannelIdStats:
        return _ChannelIdStats(
            root_tag=self._root_tag,
            root_attrib=self._root_attrib,
            source_channel_count=self._source_channel_count,
            present_channel_ids=frozenset(self._present_channel_ids),
            programme_counts_by_channel_id=dict(self._programme_counts_by_channel_id),
        )


class _MatchingXmltvWriter(ContentHandler):
    def __init__(
        self,
        *,
        output_fh,
        root_tag: str,
        matched_channel_ids: frozenset[str],
    ) -> None:
        super().__init__()
        self._generator = XMLGenerator(output_fh, encoding="utf-8", short_empty_elements=True)
        self._root_tag = root_tag
        self._matched_channel_ids = matched_channel_ids
        self._depth = 0
        self._write_depth = 0
        self.programme_count = 0

    def startElement(self, name, attrs):  # noqa: N802
        self._depth += 1
        if self._depth == 1 and name == self._root_tag:
            return

        if self._write_depth:
            self._write_depth += 1
            self._generator.startElement(name, attrs)
            return

        should_write = (
            name == "channel"
            and attrs.get("id") in self._matched_channel_ids
        ) or (
            name == "programme"
            and attrs.get("channel") in self._matched_channel_ids
        )
        if should_write:
            if name == "programme":
                self.programme_count += 1
            self._write_depth = 1
            self._generator.startElement(name, attrs)

    def characters(self, content):  # noqa: N802
        if self._write_depth:
            self._generator.characters(content)

    def ignorableWhitespace(self, whitespace):  # noqa: N802
        if self._write_depth:
            self._generator.ignorableWhitespace(whitespace)

    def endElement(self, name):  # noqa: N802
        if self._write_depth:
            self._generator.endElement(name)
            self._write_depth -= 1
            if self._write_depth == 0:
                self._generator.characters("\n")
        self._depth -= 1


def _parse_gzip_xml(path: Path, handler: ContentHandler) -> None:
    parser = make_parser()
    parser.setContentHandler(handler)
    for feature in (
        "http://xml.org/sax/features/external-general-entities",
        "http://xml.org/sax/features/external-parameter-entities",
    ):
        try:
            parser.setFeature(feature, False)
        except (SAXNotRecognizedException, SAXNotSupportedException):
            pass

    with gzip.open(path, "rb") as source_fh:
        parser.parse(source_fh)
