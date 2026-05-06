from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.epg_sources import search_epgpw_channels


def _json_response(payload: object, status: int = 200):
    return status, {"Content-Type": "application/json"}, json.dumps(payload, ensure_ascii=False)


def _text_response(body: str, status: int = 200, content_type: str = "text/plain; charset=utf-8"):
    return status, {"Content-Type": content_type}, body


def _html_response(body: str, status: int = 200):
    return _text_response(body, status=status, content_type="text/html; charset=utf-8")


def _parse_payload(body: object | None) -> dict[str, object]:
    if body is None:
        return {}
    if isinstance(body, dict):
        return body
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    if not isinstance(body, str) or not body:
        return {}
    stripped = body.strip()
    if not stripped:
        return {}
    if stripped.startswith("{"):
        parsed = json.loads(stripped)
        if not isinstance(parsed, dict):
            return {}
        return parsed
    return {key: values[-1] if values else "" for key, values in parse_qs(stripped).items()}


def _channel_payload(channel, variants=None, mappings=None, epg_icon="") -> dict[str, object]:
    return {
        "id": channel.id,
        "display_order": channel.display_order,
        "enabled": channel.enabled,
        "name": channel.name,
        "group_title": channel.group_name,
        "group_name": channel.group_name,
        "logo": channel.tvg_logo,
        "epg_icon": epg_icon,
        "tvg_id": channel.tvg_id,
        "tvg_name": channel.tvg_name,
        "validation_status": channel.status,
        "draft_differs_from_live": channel.draft_differs_from_live,
        "has_epg": getattr(channel, "epg_mapping_count", 0) > 0,
        "streams": [_stream_payload(variant) for variant in variants or []],
        "mappings": [_mapping_payload(mapping) for mapping in mappings or []],
    }


def _stream_payload(variant) -> dict[str, object]:
    return {
        "id": variant.id,
        "channel_id": variant.channel_id,
        "label": variant.label,
        "url": variant.url,
        "display_order": variant.display_order,
        "enabled": variant.enabled,
        "last_probe_status": variant.last_probe_status,
        "last_probe_error": variant.last_probe_error,
        "last_probe_at": variant.last_probe_at,
        "last_stability_status": variant.last_stability_status,
        "last_stability_error": variant.last_stability_error,
        "last_stability_speed": variant.last_stability_speed,
        "last_stability_frames": variant.last_stability_frames,
        "last_stability_at": variant.last_stability_at,
    }


def _mapping_payload(mapping: dict[str, object]) -> dict[str, object]:
    return {
        "id": mapping["id"],
        "channel_id": mapping["channel_id"],
        "epg_source_id": mapping["epg_source_id"],
        "epg_channel_id": mapping["channel_xmltv_id"],
        "epg_channel_name": mapping.get("epg_channel_name", ""),
        "enabled": bool(mapping.get("enabled", True)),
    }


def _source_payload(source) -> dict[str, object]:
    return {
        "id": source.id,
        "name": source.display_name,
        "display_name": source.display_name,
        "url": source.source_url,
        "source_url": source.source_url,
        "masked_url": _mask_url(source.source_url),
        "normalized_url": source.normalized_url,
        "enabled": source.enabled,
        "status": source.status,
        "channel_count": source.channel_count,
        "last_loaded_at": source.last_loaded_at,
        "last_error": source.last_error,
        "priority": source.priority,
    }


def _mask_url(source_url: str) -> str:
    parsed = urlparse(source_url)
    if not parsed.query:
        return source_url
    return source_url[: source_url.find("?")] + "?..."


def _epg_preview_rows(programmes: list[dict[str, object]]) -> str:
    return "\n".join(
        (
            f"<tr><td>{escape(_format_programme_time(str(programme.get('start', ''))))}</td>"
            f"<td><strong>{escape(str(programme.get('title', '')))}</strong>"
            f"<div class=\"muted\">{escape(str(programme.get('description', '')))}</div></td>"
            f"<td>{escape(str(programme.get('source_name', '')))}"
            f"<div><code>{escape(str(programme.get('channel_id', '')))}</code></div></td></tr>"
        )
        for programme in programmes
    )


def _format_programme_time(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%a %d/%m %H:%M")
    except ValueError:
        return value


def render_channels_page(channels, epg_icons=None) -> str:
    rows = "\n".join(
        (
            f"<tr data-channel-id=\"{channel.id}\" draggable=\"true\">"
            f"<td class=\"td-icon\"><img class=\"ch-icon\" src=\"{escape(channel.tvg_logo or (epg_icons or {}).get(channel.id, ''))}\" onerror=\"this.style.display='none'\" alt=\"\"></td>"
            "<td class=\"td-drag\"><span class=\"drag-handle\" title=\"Drag to reorder\">::</span></td>"
            f"<td><span class=\"chip chip-{escape(channel.status)}\">{escape(channel.status)}</span></td>"
            f"<td><span class=\"chip chip-epg-{'yes' if getattr(channel, 'epg_mapping_count', 0) > 0 else 'no'}\">{'EPG' if getattr(channel, 'epg_mapping_count', 0) > 0 else 'no EPG'}</span></td>"
            f"<td><a href=\"/ui/channels/{channel.id}\">{escape(channel.name)}</a></td>"
            f"<td>{escape(channel.group_name)}</td>"
            f"<td>{'on' if channel.enabled else 'off'}</td>"
            f"<td><button data-action=\"validate-channel\" data-id=\"{channel.id}\">Validate</button></td>"
            "</tr>"
        )
        for channel in channels
    )
    return _page(
        "Channels",
        """
        <div class="toolbar">
          <a class="button-link" href="/ui/channels/new">Add channel</a>
          <input id="channel-filter" type="search" placeholder="Search channels">
          <button data-action="validate-all">Validate all channels</button>
          <button data-action="reload-epg-sources">Reload EPG</button>
          <button data-action="rebuild-epg">Rebuild EPG</button>
          <button id="save-order" data-action="save-channel-order" hidden>Save order</button>
        </div>
        <table id="channels-table">
          <thead><tr><th></th><th>Move</th><th>Status</th><th>EPG</th><th>Name</th><th>Group</th><th>Enabled</th><th>Action</th></tr></thead>
          <tbody>
        """
        + rows
        + """
          </tbody>
        </table>
        <section id="job-panel" aria-live="polite"></section>
        """,
    )


def render_new_channel_page() -> str:
    return _page(
        "Add Channel",
        """
        <form id="channel-create-form" class="channel-create-form">
          <h2>Add channel</h2>
          <div class="inline-grid">
            <input name="name" placeholder="Name" required>
            <input name="stream_url" placeholder="Stream URL" required>
            <input name="group_name" placeholder="Group">
            <input name="tvg_logo" placeholder="Logo">
            <input name="tvg_id" placeholder="TVG ID">
            <label><input type="checkbox" name="enabled" checked> Enabled</label>
            <button type="submit">Add channel</button>
          </div>
        </form>
        <section id="job-panel" aria-live="polite"></section>
        """,
    )


def _stream_rows(variants) -> str:
    rows = []
    for variant in variants:
        stability_status = variant.last_stability_status or "new"
        stability_class = stability_status.casefold()
        speed = f"{variant.last_stability_speed}x" if variant.last_stability_speed else ""
        rows.append(
            f"<tr data-stream-id=\"{variant.id}\"><td>{variant.display_order}</td><td>{escape(variant.label)}</td>"
            f"<td><code>{escape(_mask_url(variant.url))}</code></td>"
            f"<td><span class=\"chip chip-{escape(variant.last_probe_status)}\">{escape(variant.last_probe_status)}</span></td>"
            f"<td><span class=\"chip chip-{escape(stability_class)}\">{escape(stability_status)}</span></td>"
            f"<td>{escape(speed)}</td><td class=\"muted\">{escape(variant.last_stability_error)}</td>"
            f"<td>{'on' if variant.enabled else 'off'}</td>"
            f"<td><button data-action=\"validate-stream\" data-id=\"{variant.id}\">Validate</button> "
            f"<button data-action=\"test-stream-stability\" data-id=\"{variant.id}\">Extended test</button></td></tr>"
        )
    return "\n".join(rows)


def render_channel_editor_page(channel, variants, mappings, epg_sources, epg_preview, epg_icon="") -> str:
    variant_rows = _stream_rows(variants)
    mapping_rows = "\n".join(
        (
            f"<tr data-mapping-id=\"{int(mapping['id'])}\"><td>{int(mapping['epg_source_id'])}</td>"
            f"<td><code>{escape(str(mapping['channel_xmltv_id']))}</code></td>"
            f"<td>{'on' if mapping.get('enabled', True) else 'off'}</td>"
            f"<td><button class=\"danger\" data-action=\"delete-mapping\" data-channel-id=\"{channel.id}\" data-id=\"{int(mapping['id'])}\">Delete</button></td></tr>"
        )
        for mapping in mappings
    )
    source_options = "\n".join(
        f"<option value=\"{source.id}\">{escape(source.display_name)}</option>"
        for source in epg_sources
        if source.enabled
    )
    preview_items = list(epg_preview.get("items", []))
    preview_empty_message = str(epg_preview.get("empty_message", ""))
    preview_rows = _epg_preview_rows(preview_items)
    preview_table_hidden = " hidden" if not preview_items else ""
    preview_empty_hidden = " hidden" if preview_items else ""
    return _page(
        channel.name,
        f"""
        <div class="layout-two">
          <form id="channel-form" data-channel-id="{channel.id}">
            <h2>Channel</h2>
            <label>Name<input name="name" value="{escape(channel.name)}"></label>
            <label>Group<input name="group_name" value="{escape(channel.group_name)}"></label>
            <label>Logo<input name="tvg_logo" value="{escape(channel.tvg_logo)}"></label>
            {f'<label>EPG Icon <img class="ch-icon" src="{escape(epg_icon)}" alt="" style="vertical-align:middle;max-width:80px"> <code style="font-size:11px">{escape(epg_icon)}</code></label>' if epg_icon else ''}
            <label>TVG ID<input name="tvg_id" value="{escape(channel.tvg_id)}"></label>
            <label><input type="checkbox" name="enabled" {"checked" if channel.enabled else ""}> Enabled</label>
            <button type="submit">Save channel</button>
            <button type="button" class="danger" data-action="delete-channel" data-id="{channel.id}">Delete channel</button>
          </form>
          <section>
            <h2>Streams</h2>
            <table><thead><tr><th>Order</th><th>Label</th><th>URL</th><th>Status</th><th>Stability</th><th>Speed</th><th>Issues</th><th>Enabled</th><th>Action</th></tr></thead><tbody id="stream-variants-body">{variant_rows}</tbody></table>
            <form id="stream-form" data-channel-id="{channel.id}" class="inline-grid">
              <input name="label" placeholder="Label">
              <input name="url" placeholder="https://...">
              <button type="submit">Add stream</button>
            </form>
          </section>
        </div>
        <section>
          <h2>EPG mappings</h2>
          <table id="epg-mappings-table"><thead><tr><th>Source</th><th>Channel ID</th><th>Enabled</th><th>Action</th></tr></thead><tbody id="epg-mappings-body">{mapping_rows}</tbody></table>
          <div class="layout-two">
            <form id="mapping-form" data-channel-id="{channel.id}" class="inline-grid">
              <h3>Existing sources</h3>
              <select id="epg-source-picker" name="epg_source_id">{source_options}</select>
              <input id="epg-search" type="search" placeholder="Search cached EPG">
              <select id="epg-channel-picker" name="epg_channel_id"></select>
              <button type="submit">Add mapping</button>
            </form>
            <form id="epgpw-form" class="inline-grid">
              <h3>epg.pw search</h3>
              <input id="epgpw-search" type="search" placeholder="Type channel name...">
              <select id="epgpw-picker" name="channel_id" size="6"></select>
              <button id="epgpw-add" type="submit" disabled>Add from epg.pw</button>
            </form>
          </div>
        </section>
        <section id="epg-preview-card">
          <h2>TV Guide <span id="epg-preview-spinner" style="font-weight:normal;color:var(--muted);font-size:13px">loading...</span></h2>
          <p id="epg-preview-empty" class="muted"{preview_empty_hidden}>{escape(preview_empty_message)}</p>
          <table id="epg-preview-table"{preview_table_hidden}><thead><tr><th>When</th><th>Programme</th><th>Mapping</th></tr></thead><tbody id="epg-preview-body">{preview_rows}</tbody></table>
        </section>
        <section id="job-panel" aria-live="polite"></section>
        """,
        f"""<script>
        (async () => {{
          const channelId = {channel.id};
          try {{
            const preview = await api('/api/channels/' + channelId + '/epg-preview');
            updateEpgPreview(preview);
          }} catch (e) {{}}
          const spinner = document.getElementById('epg-preview-spinner');
          if (spinner) spinner.remove();
          await watchInitialStabilityJob(channelId);
        }})();
        </script>""",
    )


def render_epg_sources_page(sources) -> str:
    rows = render_epg_source_rows(sources)
    return _page(
        "EPG Sources",
        f"""
        <form id="epg-source-form" class="inline-grid">
          <input name="display_name" placeholder="Name">
          <input name="source_url" placeholder="https://example.com/epg.xml">
          <button type="submit">Add source</button>
        </form>
        <table id="epg-sources-table"><thead><tr><th>ID</th><th>Name</th><th>URL</th><th>Status</th><th>Channels</th><th>Last error</th><th>Action</th></tr></thead><tbody id="epg-sources-body">{rows}</tbody></table>
        <section id="job-panel" aria-live="polite"></section>
        """,
    )


def render_epg_source_rows(sources) -> str:
    return "\n".join(
        (
            f"<tr data-source-id=\"{source.id}\"><td>{source.id}</td><td>{escape(source.display_name)}</td>"
            f"<td><code>{escape(_mask_url(source.source_url))}</code></td>"
            f"<td><span class=\"chip chip-{escape(source.status or 'new')}\">{escape(source.status or 'new')}</span></td>"
            f"<td>{source.channel_count}</td><td>{escape(source.last_error or '')}</td>"
            f"<td><button data-action=\"reload-epg-source\" data-id=\"{source.id}\">Reload</button> "
            f"<button class=\"danger\" data-action=\"delete-epg-source\" data-id=\"{source.id}\">Delete</button></td></tr>"
        )
        for source in sources
    )


def render_runs_page(runs) -> str:
    rows = "\n".join(
        (
            f"<tr><td>{run['id']}</td><td>{escape(str(run['trigger_type']))}</td>"
            f"<td>{escape(str(run['status']))}</td><td>{int(run['valid_count'])}</td>"
            f"<td>{int(run['invalid_count'])}</td><td>{escape(str(run['finished_at']))}</td></tr>"
        )
        for run in runs
    )
    return _page(
        "Runs",
        f"<table><thead><tr><th>ID</th><th>Trigger</th><th>Status</th><th>Valid</th><th>Invalid</th><th>Finished</th></tr></thead><tbody>{rows}</tbody></table>",
    )


def _page(title: str, body: str, extra_script: str = "") -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Playlist Admin - {escape(title)}</title>
<style>
:root {{ color-scheme: light; --bg:#f8fafc; --panel:#ffffff; --line:#cbd5e1; --text:#0f172a; --muted:#475569; --primary:#1e40af; --accent:#b45309; --bad:#b91c1c; --ok:#047857; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:Fira Sans, Inter, system-ui, sans-serif; background:var(--bg); color:var(--text); }}
header {{ display:flex; align-items:center; justify-content:space-between; gap:16px; padding:16px 24px; border-bottom:1px solid var(--line); background:#fff; position:sticky; top:0; z-index:1; }}
nav a {{ color:var(--primary); text-decoration:none; font-weight:600; margin-right:16px; }}
main {{ padding:20px 24px; }}
h1 {{ font-size:22px; margin:0; }}
h2 {{ font-size:16px; margin:0 0 12px; }}
.toolbar,.inline-grid {{ display:flex; flex-wrap:wrap; align-items:center; gap:10px; margin-bottom:14px; }}
.layout-two {{ display:grid; grid-template-columns:minmax(280px,360px) 1fr; gap:18px; align-items:start; }}
form, section {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px; margin-bottom:16px; }}
label {{ display:block; color:var(--muted); font-size:13px; font-weight:600; margin-bottom:10px; }}
input,select,button {{ min-height:38px; border:1px solid var(--line); border-radius:6px; padding:8px 10px; font:inherit; }}
input,select {{ background:#fff; color:var(--text); }}
button {{ background:var(--primary); color:#fff; cursor:pointer; font-weight:700; }}
.button-link {{ display:inline-flex; align-items:center; min-height:38px; border-radius:6px; padding:8px 10px; background:var(--primary); color:#fff; text-decoration:none; font-weight:700; }}
button.danger {{ background:var(--bad); }}
button:hover,.button-link:hover {{ filter:brightness(1.06); }}
button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; }}
table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); }}
th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:middle; font-size:14px; }}
th {{ background:#eef2ff; color:#1e3a8a; font-size:12px; text-transform:uppercase; }}
tr:hover td {{ background:#f1f5f9; }}
code {{ font-family:Fira Code, ui-monospace, monospace; font-size:12px; overflow-wrap:anywhere; }}
.chip {{ display:inline-block; min-width:64px; text-align:center; border-radius:999px; padding:3px 8px; background:#e2e8f0; font-size:12px; font-weight:700; }}
.chip-valid,.chip-loaded,.chip-ok {{ background:#d1fae5; color:var(--ok); }}
.chip-invalid,.chip-error {{ background:#fee2e2; color:var(--bad); }}
.chip-good {{ background:#d1fae5; color:var(--ok); }}
.chip-warn {{ background:#fef3c7; color:#92400e; }}
.chip-bad {{ background:#fee2e2; color:var(--bad); }}
.chip-epg-yes {{ background:#dbeafe; color:#1e40af; }}
.chip-epg-no {{ background:#f3f4f6; color:#6b7280; }}
.td-icon {{ width:48px; padding:2px; text-align:center; }}
.td-drag {{ width:54px; text-align:center; cursor:grab; }}
.drag-handle {{ display:inline-flex; align-items:center; justify-content:center; width:32px; height:32px; border:1px solid var(--line); border-radius:6px; background:#f8fafc; color:var(--muted); font-weight:800; cursor:grab; user-select:none; }}
tr.dragging td {{ opacity:.55; background:#e0f2fe; }}
.ch-icon {{ width:32px; height:32px; object-fit:contain; border-radius:4px; background:#f3f4f6; }}
.muted {{ color:var(--muted); font-size:13px; }}
.channel-create-form .inline-grid {{ margin-bottom:0; }}
#job-panel:empty {{ display:none; }}
@media (max-width: 760px) {{ header,.layout-two {{ display:block; }} main {{ padding:12px; }} table {{ display:block; overflow-x:auto; }} }}
@media (prefers-reduced-motion: reduce) {{ * {{ transition:none !important; scroll-behavior:auto !important; }} }}
</style>
</head>
<body>
<header><h1>{escape(title)}</h1><nav><a href="/ui/channels">Channels</a><a href="/ui/epg-sources">EPG Sources</a><a href="/ui/runs">Runs</a></nav></header>
<main>{body}</main>
<script>
async function api(path, options) {{
  const response = await fetch(path, options || {{}});
  const text = await response.text();
  let payload = text ? JSON.parse(text) : {{}};
  if (!response.ok) throw new Error(payload.error || text || response.statusText);
  return payload;
}}
function escapeHtml(value) {{
  return String(value ?? '').replace(/[&<>"']/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[ch]));
}}
function formJson(form) {{
  const data = Object.fromEntries(new FormData(form).entries());
  for (const box of form.querySelectorAll('input[type="checkbox"]')) data[box.name] = box.checked;
  return data;
}}
function setButtonWorking(button, working) {{
  if (!button) return;
  if (working) {{
    button.dataset.originalText = button.textContent;
    button.disabled = true;
    button.textContent = 'Working...';
  }} else {{
    button.disabled = false;
    button.textContent = button.dataset.originalText || button.textContent;
    delete button.dataset.originalText;
  }}
}}
function showActionError(error) {{
  const panel = document.getElementById('job-panel');
  if (panel) panel.textContent = 'Action failed: ' + (error?.message || error);
}}
function sourceRowsHtml(sources) {{
  return sources.map(source => {{
    const status = source.status || 'new';
    return `<tr data-source-id="${{source.id}}"><td>${{source.id}}</td><td>${{escapeHtml(source.display_name || source.name || '')}}</td>` +
      `<td><code>${{escapeHtml(source.masked_url || source.source_url || source.url || '')}}</code></td>` +
      `<td><span class="chip chip-${{escapeHtml(status)}}">${{escapeHtml(status)}}</span></td>` +
      `<td>${{source.channel_count || 0}}</td><td>${{escapeHtml(source.last_error || '')}}</td>` +
      `<td><button data-action="reload-epg-source" data-id="${{source.id}}">Reload</button> ` +
      `<button class="danger" data-action="delete-epg-source" data-id="${{source.id}}">Delete</button></td></tr>`;
  }}).join('');
}}
function mappingRowsHtml(channelId, mappings) {{
  return mappings.map(mapping =>
    `<tr data-mapping-id="${{mapping.id}}"><td>${{mapping.epg_source_id}}</td>` +
    `<td><code>${{escapeHtml(mapping.epg_channel_id || '')}}</code></td>` +
    `<td>${{mapping.enabled ? 'on' : 'off'}}</td>` +
    `<td><button class="danger" data-action="delete-mapping" data-channel-id="${{channelId}}" data-id="${{mapping.id}}">Delete</button></td></tr>`
  ).join('');
}}
function streamRowsHtml(streams) {{
  return streams.map(stream => {{
    const stability = stream.last_stability_status || 'new';
    const stabilityClass = stability.toLowerCase();
    const speed = stream.last_stability_speed ? stream.last_stability_speed + 'x' : '';
    return `<tr data-stream-id="${{stream.id}}"><td>${{stream.display_order}}</td>` +
      `<td>${{escapeHtml(stream.label || '')}}</td>` +
      `<td><code>${{escapeHtml(stream.url || '')}}</code></td>` +
      `<td><span class="chip chip-${{escapeHtml(stream.last_probe_status || 'new')}}">${{escapeHtml(stream.last_probe_status || 'new')}}</span></td>` +
      `<td><span class="chip chip-${{escapeHtml(stabilityClass)}}">${{escapeHtml(stability)}}</span></td>` +
      `<td>${{escapeHtml(speed)}}</td><td class="muted">${{escapeHtml(stream.last_stability_error || '')}}</td>` +
      `<td>${{stream.enabled ? 'on' : 'off'}}</td>` +
      `<td><button data-action="validate-stream" data-id="${{stream.id}}">Validate</button> ` +
      `<button data-action="test-stream-stability" data-id="${{stream.id}}">Extended test</button></td></tr>`;
  }}).join('');
}}
function formatProgrammeTime(value) {{
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.valueOf())) return value;
  return date.toLocaleString([], {{weekday:'short', hour:'2-digit', minute:'2-digit', day:'2-digit', month:'2-digit'}});
}}
function programmeRowsHtml(programmes) {{
  return programmes.map(programme =>
    `<tr><td>${{escapeHtml(formatProgrammeTime(programme.start))}}</td>` +
    `<td><strong>${{escapeHtml(programme.title || '')}}</strong><div class="muted">${{escapeHtml(programme.description || '')}}</div></td>` +
    `<td>${{escapeHtml(programme.source_name || '')}}<div><code>${{escapeHtml(programme.channel_id || '')}}</code></div></td></tr>`
  ).join('');
}}
function updateEpgPreview(preview) {{
  const body = document.getElementById('epg-preview-body');
  const table = document.getElementById('epg-preview-table');
  const empty = document.getElementById('epg-preview-empty');
  if (!body || !table || !empty) return;
  const programmes = preview?.items || [];
  body.innerHTML = programmeRowsHtml(programmes);
  table.hidden = programmes.length === 0;
  empty.hidden = programmes.length > 0;
  empty.textContent = preview?.empty_message || '';
}}
async function refreshEpgSources() {{
  const body = document.getElementById('epg-sources-body');
  if (!body) return;
  const payload = await api('/api/epg-sources');
  body.innerHTML = sourceRowsHtml(payload.epg_sources || []);
}}
async function refreshChannelEditor(channelId) {{
  const mappingBody = document.getElementById('epg-mappings-body');
  const streamBody = document.getElementById('stream-variants-body');
  if (!mappingBody && !streamBody) return;
  const payload = await api('/api/channels/' + channelId);
  if (mappingBody) mappingBody.innerHTML = mappingRowsHtml(channelId, payload.channel?.mappings || []);
  if (streamBody) streamBody.innerHTML = streamRowsHtml(payload.channel?.streams || []);
}}
async function watchInitialStabilityJob(channelId) {{
  const params = new URLSearchParams(window.location.search);
  const jobId = params.get('stability_job');
  if (!jobId) return;
  await watchJob({{job_id: jobId, status: 'queued', kind: 'validate-stream-extended'}}, {{refresh: () => refreshChannelEditor(channelId)}});
  params.delete('stability_job');
  const nextQuery = params.toString();
  const nextUrl = window.location.pathname + (nextQuery ? '?' + nextQuery : '');
  window.history.replaceState(null, '', nextUrl);
}}
async function watchJob(job, options) {{
  const panel = document.getElementById('job-panel');
  if (!panel || !job.job_id) return;
  const button = options?.button;
  setButtonWorking(button, true);
  panel.textContent = job.kind ? job.kind + ' queued' : 'Job queued';
  let state = job;
  try {{
    for (;;) {{
      state = await api('/api/jobs/' + job.job_id);
      panel.textContent = state.kind + ': ' + state.status + (state.error ? ' - ' + state.error : '');
      if (state.status === 'ok' || state.status === 'error' || state.status === 'not_found') break;
      await new Promise(resolve => setTimeout(resolve, 1200));
    }}
    if (options?.refresh) await options.refresh();
  }} catch (error) {{
    showActionError(error);
  }} finally {{
    setButtonWorking(button, false);
  }}
  return state;
}}
async function runJobSilently(job, refresh) {{
  if (!job || !job.job_id) return;
  const panel = document.getElementById('job-panel');
  if (panel) panel.textContent = (job.kind || 'Job') + ' queued';
  try {{
    for (;;) {{
      const state = await api('/api/jobs/' + job.job_id);
      if (panel) panel.textContent = state.kind + ': ' + state.status + (state.error ? ' - ' + state.error : '');
      if (state.status === 'ok' || state.status === 'error' || state.status === 'not_found') break;
      await new Promise(resolve => setTimeout(resolve, 1200));
    }}
    if (refresh) await refresh();
  }} catch (error) {{
    showActionError(error);
  }}
}}
async function refreshChannelsPage() {{
  const tbody = document.querySelector('#channels-table tbody');
  if (!tbody) return;
  const payload = await api('/api/channels');
  tbody.innerHTML = (payload.channels || []).map(channel => {{
    const icon = channel.logo || channel.epg_icon || '';
    const iconHtml = icon ? `<img class="ch-icon" src="${{escapeHtml(icon)}}" onerror="this.style.display='none'" alt="">` : '';
    return `<tr data-channel-id="${{channel.id}}" draggable="true">` +
    `<td class="td-icon">${{iconHtml}}</td>` +
    `<td class="td-drag"><span class="drag-handle" title="Drag to reorder">::</span></td>` +
    `<td><span class="chip chip-${{escapeHtml(channel.validation_status)}}">${{escapeHtml(channel.validation_status)}}</span></td>` +
    `<td><span class="chip chip-epg-${{channel.has_epg ? 'yes' : 'no'}}">${{channel.has_epg ? 'EPG' : 'no EPG'}}</span></td>` +
    `<td><a href="/ui/channels/${{channel.id}}">${{escapeHtml(channel.name)}}</a></td>` +
    `<td>${{escapeHtml(channel.group_name)}}</td>` +
    `<td>${{channel.enabled ? 'on' : 'off'}}</td>` +
    `<td><button data-action="validate-channel" data-id="${{channel.id}}">Validate</button></td>` +
    `</tr>`;
  }}).join('');
  enableChannelDragOrdering();
}}
let draggedChannelRow = null;
function markChannelOrderDirty() {{
  const saveButton = document.getElementById('save-order');
  if (saveButton) saveButton.hidden = false;
}}
function enableChannelDragOrdering() {{
  const tbody = document.querySelector('#channels-table tbody');
  if (!tbody) return;
  for (const row of tbody.querySelectorAll('tr')) {{
    if (row.dataset.dragReady === 'true') continue;
    row.dataset.dragReady = 'true';
    row.draggable = true;
    row.addEventListener('dragstart', event => {{
      draggedChannelRow = row;
      row.classList.add('dragging');
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', row.dataset.channelId || '');
    }});
    row.addEventListener('dragover', event => {{
      event.preventDefault();
      if (!draggedChannelRow || draggedChannelRow === row) return;
      const rect = row.getBoundingClientRect();
      const placeAfter = event.clientY > rect.top + rect.height / 2;
      row.parentNode.insertBefore(draggedChannelRow, placeAfter ? row.nextSibling : row);
    }});
    row.addEventListener('drop', event => {{
      event.preventDefault();
      markChannelOrderDirty();
    }});
    row.addEventListener('dragend', () => {{
      row.classList.remove('dragging');
      draggedChannelRow = null;
      markChannelOrderDirty();
    }});
  }}
}}
enableChannelDragOrdering();
document.addEventListener('click', async (event) => {{
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const action = button.dataset.action;
  const id = button.dataset.id;
  try {{
    if (action === 'validate-all') watchJob(await api('/api/jobs/validate-all', {{method:'POST'}}), {{button, refresh: refreshChannelsPage}});
    if (action === 'reload-epg-sources') watchJob(await api('/api/jobs/reload-epg-sources', {{method:'POST'}}), {{button, refresh: refreshEpgSources}});
    if (action === 'rebuild-epg') watchJob(await api('/api/jobs/rebuild-epg', {{method:'POST'}}), {{button, refresh: refreshChannelsPage}});
    if (action === 'validate-channel') watchJob(await api('/api/jobs/validate-channel/' + id, {{method:'POST'}}), {{button, refresh: refreshChannelsPage}});
    if (action === 'validate-stream') watchJob(await api('/api/jobs/validate-stream/' + id, {{method:'POST'}}), {{button, refresh: () => refreshChannelEditor(document.getElementById('channel-form')?.dataset.channelId)}});
    if (action === 'test-stream-stability') watchJob(await api('/api/jobs/validate-stream-extended/' + id, {{method:'POST'}}), {{button, refresh: () => refreshChannelEditor(document.getElementById('channel-form')?.dataset.channelId)}});
    if (action === 'reload-epg-source') watchJob(await api('/api/jobs/reload-epg-source/' + id, {{method:'POST'}}), {{button, refresh: refreshEpgSources}});
    if (action === 'delete-channel' && confirm('Delete this channel?')) {{
      setButtonWorking(button, true);
      const response = await api('/api/channels/' + id, {{method:'DELETE'}});
      setButtonWorking(button, false);
      runJobSilently(response.job);
      window.location.href = '/ui/channels';
    }}
    if (action === 'delete-epg-source' && confirm('Delete this EPG source and revalidate affected mappings?')) {{
      setButtonWorking(button, true);
      const response = await api('/api/epg-sources/' + id, {{method:'DELETE'}});
      await refreshEpgSources();
      setButtonWorking(button, false);
      runJobSilently(response.job, refreshEpgSources);
    }}
    if (action === 'delete-mapping' && confirm('Delete this EPG mapping?')) {{
      const channelId = button.dataset.channelId;
      setButtonWorking(button, true);
      const response = await api('/api/channels/' + channelId + '/mappings/' + id, {{method:'DELETE'}});
      await refreshChannelEditor(channelId);
      setButtonWorking(button, false);
      runJobSilently(response.job, () => refreshChannelEditor(channelId));
    }}
    if (action === 'save-channel-order') {{
      setButtonWorking(button, true);
      const ids = [...document.querySelectorAll('#channels-table tbody tr')].map(row => Number(row.dataset.channelId)).filter(id => Number.isFinite(id));
      const response = await api('/api/channels/reorder', {{method:'POST', body:JSON.stringify({{channel_ids:ids}})}});
      document.getElementById('save-order').hidden = true;
      setButtonWorking(button, false);
      runJobSilently(response, refreshChannelsPage);
    }}
  }} catch (error) {{
    showActionError(error);
    setButtonWorking(button, false);
  }}
}});
document.addEventListener('input', (event) => {{
  if (event.target.id === 'channel-filter') {{
    const q = event.target.value.toLowerCase();
    for (const row of document.querySelectorAll('#channels-table tbody tr')) row.hidden = !row.textContent.toLowerCase().includes(q);
  }}
}});
document.getElementById('channel-create-form')?.addEventListener('submit', async (event) => {{
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  setButtonWorking(button, true);
  try {{
    const payload = await api('/api/channels', {{method:'POST', body:JSON.stringify(formJson(form))}});
    setButtonWorking(button, false);
    const jobId = payload.stability_job?.job_id;
    window.location.href = '/ui/channels/' + payload.channel.id + (jobId ? '?stability_job=' + encodeURIComponent(jobId) : '');
  }} catch (error) {{
    showActionError(error);
    setButtonWorking(button, false);
  }}
}});
document.getElementById('channel-form')?.addEventListener('submit', async (event) => {{
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  setButtonWorking(button, true);
  try {{
    const response = await api('/api/channels/' + form.dataset.channelId, {{method:'PATCH', body:JSON.stringify(formJson(form))}});
    setButtonWorking(button, false);
    runJobSilently(response.job);
  }} catch (error) {{
    showActionError(error);
    setButtonWorking(button, false);
  }}
}});
document.getElementById('stream-form')?.addEventListener('submit', async (event) => {{
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  setButtonWorking(button, true);
  try {{
    const response = await api('/api/channels/' + form.dataset.channelId + '/streams', {{method:'POST', body:JSON.stringify(formJson(form))}});
    setButtonWorking(button, false);
    runJobSilently(response.job);
  }} catch (error) {{
    showActionError(error);
    setButtonWorking(button, false);
  }}
}});
document.getElementById('mapping-form')?.addEventListener('submit', async (event) => {{
  event.preventDefault();
  const form = event.target;
  const channelId = form.dataset.channelId;
  const button = form.querySelector('button[type="submit"]');
  setButtonWorking(button, true);
  try {{
    const payload = await api('/api/channels/' + channelId + '/mappings', {{method:'POST', body:JSON.stringify(formJson(form))}});
    await refreshChannelEditor(channelId);
    setButtonWorking(button, false);
    if (payload.icon_url) {{
      const logoInput = document.querySelector('input[name="tvg_logo"]');
      if (logoInput && !logoInput.value.trim()) logoInput.value = payload.icon_url;
    }}
    runJobSilently(payload.job, () => refreshChannelEditor(channelId));
  }} catch (error) {{
    showActionError(error);
    setButtonWorking(button, false);
  }}
}});
document.getElementById('epg-source-form')?.addEventListener('submit', async (event) => {{
  event.preventDefault();
  const form = event.target;
  const button = form.querySelector('button[type="submit"]');
  setButtonWorking(button, true);
  try {{
    const payload = await api('/api/epg-sources', {{method:'POST', body:JSON.stringify(formJson(form))}});
    await refreshEpgSources();
    setButtonWorking(button, false);
    form.reset();
    runJobSilently(payload.job, refreshEpgSources);
  }} catch (error) {{
    showActionError(error);
    setButtonWorking(button, false);
  }}
}});
document.getElementById('epg-search')?.addEventListener('input', async (event) => {{
  const source = document.getElementById('epg-source-picker').value;
  const picker = document.getElementById('epg-channel-picker');
  const payload = await api('/api/epg-sources/' + source + '/channels?q=' + encodeURIComponent(event.target.value));
  picker.innerHTML = payload.channels.map(ch => `<option value="${{ch.epg_channel_id}}">${{ch.display_name}} (${{ch.epg_channel_id}})</option>`).join('');
}});
let epgpwTimer = null;
document.getElementById('epgpw-search')?.addEventListener('input', async (event) => {{
  const q = event.target.value.trim();
  const picker = document.getElementById('epgpw-picker');
  const btn = document.getElementById('epgpw-add');
  if (epgpwTimer) clearTimeout(epgpwTimer);
  if (q.length < 2) {{ picker.innerHTML = ''; btn.disabled = true; return; }}
  btn.disabled = true;
  picker.innerHTML = '<option>Searching...</option>';
  epgpwTimer = setTimeout(async () => {{
    try {{
      const payload = await api('/api/epgpw/search?q=' + encodeURIComponent(q));
      const results = payload.results || [];
      if (results.length === 0) {{
        picker.innerHTML = '<option>No results</option>';
      }} else {{
        picker.innerHTML = results.map(r =>
          `<option value="${{r.channel_id}}">${{r.display_name}}${{r.country ? ' [' + r.country + ']' : ''}} (${{r.channel_id}})</option>`
        ).join('');
        btn.disabled = false;
      }}
    }} catch (e) {{
      picker.innerHTML = '<option>Search failed</option>';
    }}
  }}, 400);
}});
document.getElementById('epgpw-form')?.addEventListener('submit', async (event) => {{
  event.preventDefault();
  const channelForm = document.getElementById('channel-form');
  const channelId = channelForm ? channelForm.dataset.channelId : null;
  if (!channelId) return;
  const picker = document.getElementById('epgpw-picker');
  const epgpwChannelId = picker.value;
  const selectedOption = picker.selectedOptions[0];
  const displayName = selectedOption ? selectedOption.textContent.replace(/\\s*\\(\\d+\\)$/,'').replace(/\\s*\\[.*\\]$/,'').trim() : '';
  const button = document.getElementById('epgpw-add');
  setButtonWorking(button, true);
  try {{
    const result = await api('/api/channels/' + channelId + '/epgpw-map', {{
      method:'POST',
      body:JSON.stringify({{epgpw_channel_id: epgpwChannelId, display_name: displayName}})
    }});
    button.textContent = 'Added!';
    setTimeout(() => {{ button.textContent = 'Add from epg.pw'; setButtonWorking(button, false); }}, 2000);
    if (result.icon_url) {{
      const logoInput = document.querySelector('input[name="tvg_logo"]');
      if (logoInput && !logoInput.value.trim()) logoInput.value = result.icon_url;
    }}
    await refreshChannelEditor(channelId);
  }} catch (error) {{
    showActionError(error);
    setButtonWorking(button, false);
  }}
}});
</script>
{extra_script}
</body>
</html>"""


def _dispatch_request(store, service, method: str, raw_path: str, body: object | None = None):
    parsed = urlparse(raw_path)
    path = parsed.path
    query = parse_qs(parsed.query)
    payload = _parse_payload(body)

    try:
        return _dispatch_request_checked(store, service, method, path, query, payload)
    except KeyError:
        return _json_response({"error": "not found"}, status=404)
    except ValueError as exc:
        return _json_response({"error": str(exc)}, status=400)
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).casefold():
            if path.startswith("/ui/"):
                return _html_response(
                    _page(
                        "Database busy",
                        "<section><h2>Still working</h2><p>The database is busy processing a previous action. This page will be available shortly.</p></section>",
                    ),
                    status=503,
                )
            return _json_response({"error": "database busy; retry shortly"}, status=503)
        raise


def _dispatch_request_checked(store, service, method: str, path: str, query: dict[str, list[str]], payload: dict[str, object]):
    if method == "GET" and path == "/api/channels":
        all_icons = service.get_all_epg_icons()
        channels = [_channel_payload(channel, epg_icon=all_icons.get(channel.id, "")) for channel in store.list_channels()]
        return _json_response({"channels": channels})

    if method == "POST" and path == "/api/channels":
        channel = store.add_channel(payload)
        variants = store.list_stream_variants(channel.id)
        job = service.start_rebuild_playlist_job("channel-create")
        stability_job = service.start_stream_stability_job(variants[0].id) if variants else None
        return _json_response(
            {
                "channel": _channel_payload(channel, variants),
                "job": job,
                "stability_job": stability_job,
            },
            status=201,
        )

    if method == "POST" and path == "/api/channels/reorder":
        channel_ids = [int(value) for value in payload.get("channel_ids", [])]
        store.reorder_channels(channel_ids)
        return _json_response(service.start_rebuild_playlist_job("channel-reorder"), status=202)

    channel_prefix = "/api/channels/"
    if path.startswith(channel_prefix):
        parts = path.removeprefix(channel_prefix).strip("/").split("/")
        if parts == [""]:
            # /api/channels/ with trailing slash → treat as list
            all_icons = service.get_all_epg_icons()
            return _json_response({"channels": [_channel_payload(channel, epg_icon=all_icons.get(channel.id, "")) for channel in store.list_channels()]})
        channel_id = int(parts[0])
        if len(parts) == 1:
            if method == "GET":
                channel = store.get_channel(channel_id)
                variants = store.list_stream_variants(channel_id)
                mappings = store.list_channel_epg_mappings(channel_id)
                return _json_response(
                    {
                        "channel": _channel_payload(channel, variants, mappings, epg_icon=service.get_epg_icon_for_channel(channel_id)),
                    }
                )
            if method == "PATCH":
                current = store.get_channel(channel_id)
                store.update_channel(
                    channel_id,
                    {
                        "enabled": payload.get("enabled", current.enabled),
                        "name": payload.get("name", current.name),
                        "group_name": payload.get("group_name", payload.get("group_title", current.group_name)),
                        "stream_url": payload.get("stream_url", current.stream_url),
                        "tvg_id": payload.get("tvg_id", current.tvg_id),
                        "tvg_name": payload.get("tvg_name", current.tvg_name),
                        "tvg_logo": payload.get("tvg_logo", payload.get("logo", current.tvg_logo)),
                        "tvg_rec": payload.get("tvg_rec", current.tvg_rec),
                    },
                )
                return _json_response(service.start_rebuild_playlist_job("channel-update"), status=202)
            if method == "DELETE":
                store.delete_channel(channel_id)
                return _json_response(service._start_job("channel-delete-rebuild", lambda: service.rebuild_all_public_outputs("channel-delete")), status=202)
        if len(parts) == 2 and parts[1] == "epg-preview" and method == "GET":
            return _json_response(service.preview_channel_epg_programmes(channel_id))
        if len(parts) == 2 and parts[1] == "streams" and method == "POST":
            variant = store.add_stream_variant(channel_id, payload)
            job = service.start_rebuild_playlist_job("stream-create")
            return _json_response({"stream": _stream_payload(variant), "job": job}, status=201)
        if len(parts) == 3 and parts[1] == "streams":
            stream_id = int(parts[2])
            if method == "PATCH":
                variant = store.update_stream_variant(stream_id, payload)
                return _json_response({"stream": _stream_payload(variant), "job": service.start_rebuild_playlist_job("stream-update")}, status=202)
            if method == "DELETE":
                store.delete_stream_variant(stream_id)
                return _json_response(service.start_rebuild_playlist_job("stream-delete"), status=202)
        if len(parts) == 2 and parts[1] == "mappings" and method == "POST":
            epg_source_id = int(payload["epg_source_id"])
            channel_xmltv_id = str(payload.get("epg_channel_id", payload.get("channel_xmltv_id", "")))
            mapping = store.add_channel_epg_mapping(
                channel_id,
                epg_source_id,
                int(payload.get("priority", 0)),
                channel_xmltv_id,
            )
            icon_url = service._extract_epg_source_icon(epg_source_id, channel_xmltv_id)
            if icon_url:
                store.set_channel_logo_url(channel_id, icon_url)
            job = service._start_job("mapping-create-rebuild", lambda: service.rebuild_all_public_outputs("mapping-create"))
            return _json_response({"mapping": _mapping_payload(mapping), "job": job, "icon_url": icon_url}, status=201)
        if len(parts) == 3 and parts[1] == "mappings":
            mapping_id = int(parts[2])
            if method == "PATCH":
                mapping = store.update_channel_epg_mapping(channel_id, mapping_id, payload)
                job = service._start_job("mapping-update-rebuild", lambda: service.rebuild_all_public_outputs("mapping-update"))
                return _json_response({"mapping": _mapping_payload(mapping), "job": job}, status=202)
            if method == "DELETE":
                store.delete_channel_epg_mapping(channel_id, mapping_id)
                return _json_response(service._start_job("mapping-delete-rebuild", lambda: service.rebuild_all_public_outputs("mapping-delete")), status=202)
        if len(parts) == 2 and parts[1] == "epgpw-map" and method == "POST":
            result = service.auto_add_epgpw_mapping(
                channel_id,
                str(payload["epgpw_channel_id"]),
                str(payload.get("display_name", "")),
            )
            return _json_response(result, status=201)

    if method == "GET" and path == "/api/epgpw/search":
        q = query.get("q", [""])[0].strip()
        lang = query.get("lang", ["en"])[0]
        results = search_epgpw_channels(q, lang=lang)
        return _json_response({"results": results})

    if method == "GET" and path == "/api/epg-sources":
        return _json_response({"epg_sources": [_source_payload(source) for source in store.list_epg_sources()]})

    if method == "POST" and path == "/api/epg-sources":
        source = store.add_epg_source(payload)
        job = service.start_reload_epg_source_job(source.id)
        return _json_response({"epg_source": _source_payload(source), "job": job}, status=201)

    if path.startswith("/api/epg-sources/"):
        parts = path.removeprefix("/api/epg-sources/").strip("/").split("/")
        if parts == [""]:
            return _json_response({"epg_sources": [_source_payload(source) for source in store.list_epg_sources()]})
        source_id = int(parts[0])
        if len(parts) == 1:
            if method == "PATCH":
                source = store.update_epg_source(source_id, payload)
                job = service.start_reload_epg_source_job(source.id)
                return _json_response({"epg_source": _source_payload(source), "job": job}, status=202)
            if method == "DELETE":
                return _json_response(service._start_job("source-delete-rebuild", lambda: service.delete_epg_source_and_rebuild(source_id)), status=202)
        if len(parts) == 2 and parts[1] == "channels" and method == "GET":
            rows = store.search_epg_channel_cache(source_id, query.get("q", [""])[0])
            return _json_response({"channels": rows})

    if method == "POST" and path == "/api/jobs/validate-all":
        return _json_response(service.start_validate_all_job("manual"), status=202)
    if method == "POST" and path.startswith("/api/jobs/validate-channel/"):
        return _json_response(service.start_validate_channel_job(int(path.rsplit("/", 1)[1])), status=202)
    if method == "POST" and path.startswith("/api/jobs/validate-stream/"):
        return _json_response(service.start_validate_stream_job(int(path.rsplit("/", 1)[1])), status=202)
    if method == "POST" and path.startswith("/api/jobs/validate-stream-extended/"):
        return _json_response(service.start_stream_stability_job(int(path.rsplit("/", 1)[1])), status=202)
    if method == "POST" and path == "/api/jobs/reload-epg-sources":
        return _json_response(service._start_job("reload-epg-sources", lambda: _reload_all_sources(service, store)), status=202)
    if method == "POST" and path.startswith("/api/jobs/reload-epg-source/"):
        return _json_response(service.start_reload_epg_source_job(int(path.rsplit("/", 1)[1])), status=202)
    if method == "POST" and path == "/api/jobs/rebuild-playlist":
        return _json_response(service.start_rebuild_playlist_job("manual"), status=202)
    if method == "POST" and path == "/api/jobs/rebuild-epg":
        return _json_response(service.start_rebuild_epg_job("manual"), status=202)
    if method == "GET" and path.startswith("/api/jobs/"):
        return _json_response(service.get_job(path.rsplit("/", 1)[1]))

    if method == "POST" and path == "/api/channels/validate":
        return _json_response(service.start_validate_all_job("manual"), status=202)
    if method == "GET" and path == "/api/runs":
        return _json_response({"runs": store.list_runs(limit=20)})
    if method == "GET" and path == "/api/runs/":
        return _json_response({"runs": store.list_runs(limit=20)})
    if method == "GET" and path == "/api/system/status":
        return _json_response({"status": "ok", "channels": len(store.list_channels())})
    if method == "GET" and path == "/api/system/status/":
        return _json_response({"status": "ok", "channels": len(store.list_channels())})

    if method == "GET" and path == "/ui/channels":
        epg_icons = service.get_all_epg_icons()
        return _html_response(render_channels_page(store.list_channels(), epg_icons))
    if method == "GET" and path in {"/ui/channels/new", "/ui/channels/new/"}:
        return _html_response(render_new_channel_page())
    if method == "GET" and path.startswith("/ui/channels/"):
        tail = path.rsplit("/", 1)[1]
        if not tail:
            epg_icons = service.get_all_epg_icons()
            return _html_response(render_channels_page(store.list_channels(), epg_icons))
        channel_id = int(tail)
        return _html_response(
            render_channel_editor_page(
                store.get_channel(channel_id),
                store.list_stream_variants(channel_id),
                store.list_channel_epg_mappings(channel_id),
                store.list_epg_sources(),
                {"items": [], "empty_message": ""},
                service.get_epg_icon_for_channel(channel_id),
            )
        )
    if method == "GET" and path == "/ui/epg-sources":
        return _html_response(render_epg_sources_page(store.list_epg_sources()))
    if method == "GET" and path == "/ui/epg-sources/":
        return _html_response(render_epg_sources_page(store.list_epg_sources()))
    if method == "GET" and path == "/ui/runs":
        return _html_response(render_runs_page(store.list_runs(limit=20)))
    if method == "GET" and path == "/ui/runs/":
        return _html_response(render_runs_page(store.list_runs(limit=20)))

    return _text_response("not found", status=404)


def _reload_all_sources(service, store) -> dict[str, object]:
    results = [service.reload_epg_source(source.id) for source in store.list_epg_sources() if source.enabled]
    rebuild = service.rebuild_all_public_outputs("epg-source-reload")
    return {"status": "ok", "sources": results, "rebuild": rebuild}


def build_test_server(store, service):
    def call(method: str, path: str, payload: object | None):
        return _dispatch_request(store, service, method=method, raw_path=path, body=payload)

    return call


class AdminRequestHandler(BaseHTTPRequestHandler):
    store = None
    service = None

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length else None
        status, headers, payload = _dispatch_request(
            self.store,
            self.service,
            method=method,
            raw_path=self.path,
            body=body,
        )
        self._write_text(payload, content_type=headers.get("Content-Type", "text/plain; charset=utf-8"), status=status)

    def _write_text(self, body: str, *, content_type: str, status: int) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)


def serve(*, bind_host: str, bind_port: int, store, service) -> None:
    AdminRequestHandler.store = store
    AdminRequestHandler.service = service
    httpd = ThreadingHTTPServer((bind_host, bind_port), AdminRequestHandler)
    httpd.serve_forever()
