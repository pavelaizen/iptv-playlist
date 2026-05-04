from __future__ import annotations

import json
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def render_channels_page(channels) -> str:
    rows = "\n".join(
        (
            "<tr>"
            f"<td>{channel.display_order}</td>"
            f"<td>{escape(channel.status)}</td>"
            f"<td>{escape(channel.name)}</td>"
            f"<td>{escape(channel.group_name)}</td>"
            "</tr>"
        )
        for channel in channels
    )
    return (
        "<!DOCTYPE html>"
        "<html lang=\"en\">"
        "<head><meta charset=\"utf-8\"><title>Playlist Admin</title></head>"
        "<body>"
        "<nav>"
        "<a href=\"/ui/channels\">Channels</a> "
        "<a href=\"/ui/epg-sources\">EPG Sources</a> "
        "<a href=\"/ui/runs\">Runs</a>"
        "</nav>"
        "<form method=\"post\" action=\"/api/channels/validate\">"
        "<button type=\"submit\">Validate all channels</button>"
        "</form>"
        "<table>"
        "<thead><tr><th>Order</th><th>Status</th><th>Name</th><th>Group</th></tr></thead>"
        f"<tbody>{rows}</tbody>"
        "</table>"
        "</body></html>"
    )


def render_epg_sources_page(sources) -> str:
    items = "".join(
        "<li>"
        f"{escape(source.display_name)} - {escape(source.source_url)}"
        "</li>"
        for source in sources
    )
    return f"<html><body><h1>EPG Sources</h1><ul>{items}</ul></body></html>"


def render_runs_page(runs) -> str:
    items = "".join(
        "<li>"
        f"{escape(str(run['trigger_type']))}: valid={int(run['valid_count'])} invalid={int(run['invalid_count'])}"
        "</li>"
        for run in runs
    )
    return f"<html><body><h1>Runs</h1><ul>{items}</ul></body></html>"


def render_channel_editor_page(channel, mappings) -> str:
    items = "".join(
        "<li>"
        f"source={int(mapping['epg_source_id'])} channel_id={escape(str(mapping['channel_xmltv_id']))}"
        "</li>"
        for mapping in mappings
    )
    return (
        "<html><body>"
        f"<h1>{escape(channel.name)}</h1>"
        f"<p>{escape(channel.stream_url)}</p>"
        f"<ul>{items}</ul>"
        "</body></html>"
    )


def _dispatch_request(store, service, method: str, path: str):
    if method == "GET" and path == "/api/channels":
        body = json.dumps(
            {
                "channels": [
                    {
                        "id": channel.id,
                        "name": channel.name,
                        "group_name": channel.group_name,
                        "status": channel.status,
                        "draft_differs_from_live": channel.draft_differs_from_live,
                    }
                    for channel in store.list_channels()
                ]
            }
        )
        return 200, {"Content-Type": "application/json"}, body

    if method == "GET" and path == "/api/epg-sources":
        body = json.dumps(
            {
                "epg_sources": [
                    {
                        "id": source.id,
                        "display_name": source.display_name,
                        "source_url": source.source_url,
                        "enabled": source.enabled,
                        "priority": source.priority,
                    }
                    for source in store.list_epg_sources()
                ]
            }
        )
        return 200, {"Content-Type": "application/json"}, body

    if method == "GET" and path == "/api/runs":
        body = json.dumps({"runs": store.list_runs(limit=20)})
        return 200, {"Content-Type": "application/json"}, body

    if method == "GET" and path == "/api/system/status":
        body = json.dumps({"status": "ok", "channels": len(store.list_channels())})
        return 200, {"Content-Type": "application/json"}, body

    if method == "GET" and path == "/ui/channels":
        return 200, {"Content-Type": "text/html; charset=utf-8"}, render_channels_page(store.list_channels())

    if method == "GET" and path == "/ui/epg-sources":
        return 200, {"Content-Type": "text/html; charset=utf-8"}, render_epg_sources_page(store.list_epg_sources())

    if method == "GET" and path == "/ui/runs":
        return 200, {"Content-Type": "text/html; charset=utf-8"}, render_runs_page(store.list_runs(limit=20))

    if method == "GET" and path.startswith("/ui/channels/"):
        try:
            channel_id = int(path.rsplit("/", 1)[1])
        except ValueError:
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, "bad channel id"
        try:
            channel = store.get_channel(channel_id)
        except KeyError:
            return 404, {"Content-Type": "text/plain; charset=utf-8"}, "channel not found"
        mappings = store.list_channel_epg_mappings(channel_id)
        return 200, {"Content-Type": "text/html; charset=utf-8"}, render_channel_editor_page(channel, mappings)

    if method == "POST" and path == "/api/channels/validate":
        body = json.dumps(service.validate_all(trigger_type="manual"))
        return 202, {"Content-Type": "application/json"}, body

    if method == "POST" and path.startswith("/api/channels/") and path.endswith("/validate"):
        raw_channel_id = path.removeprefix("/api/channels/").removesuffix("/validate")
        try:
            channel_id = int(raw_channel_id.strip("/"))
        except ValueError:
            return 400, {"Content-Type": "text/plain; charset=utf-8"}, "bad channel id"
        body = json.dumps(service.validate_channel(channel_id))
        return 202, {"Content-Type": "application/json"}, body

    return 404, {"Content-Type": "text/plain; charset=utf-8"}, "not found"


def build_test_server(store, service):
    def call(method: str, path: str, payload: dict[str, object] | None):
        del payload
        return _dispatch_request(store, service, method=method, path=path)

    return call


class AdminRequestHandler(BaseHTTPRequestHandler):
    store = None
    service = None

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        status, headers, body = _dispatch_request(
            self.store,
            self.service,
            method="GET",
            path=path,
        )
        if headers.get("Content-Type", "").startswith("application/json"):
            self._write_json(json.loads(body), status=status)
            return
        self._write_text(body, content_type=headers.get("Content-Type", "text/plain; charset=utf-8"), status=status)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        status, headers, body = _dispatch_request(
            self.store,
            self.service,
            method="POST",
            path=path,
        )
        if headers.get("Content-Type", "").startswith("application/json"):
            self._write_json(json.loads(body), status=status)
            return
        self._write_text(body, content_type=headers.get("Content-Type", "text/plain; charset=utf-8"), status=status)

    def _write_json(self, payload: dict[str, object], status: int = HTTPStatus.OK) -> None:
        self._write_text(
            json.dumps(payload),
            content_type="application/json",
            status=status,
        )

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
