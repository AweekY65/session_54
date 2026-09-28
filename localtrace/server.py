"""A minimal local HTTP receiver using only the Python standard library.

Endpoints
---------
``POST /spans``
    Body is a single span object, a list of span objects, or
    ``{"spans": [...]}``. Responds 202 with the ingest result; malformed
    spans produce 400 and are not partially accepted.

``GET /healthz``
    ``{"status": "ok", "spans": N}``.

The server binds to localhost by default and stores everything through the
local :class:`~localtrace.collector.Collector`; nothing leaves the machine.
"""

from __future__ import annotations

import json
import threading
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .collector import Collector
from .model import Span
from .store import SpanStore


class _Handler(BaseHTTPRequestHandler):
    server_version = "localtrace/1.0"

    # Injected onto the server instance.
    @property
    def collector(self) -> Collector:
        return self.server.collector  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default
        if getattr(self.server, "verbose", False):  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    def _send(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        if self.path.split("?")[0] == "/healthz":
            self._send(200, {"status": "ok",
                             "spans": len(self.collector.store)})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path != "/spans":
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send(400, {"error": "invalid JSON body"})
            return
        if isinstance(data, dict) and "spans" in data:
            data = data["spans"]
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            self._send(400, {"error": "expected a span object or list"})
            return
        try:
            spans = [item if isinstance(item, Span) else Span.from_dict(item)
                     for item in data]
        except (ValueError, TypeError) as exc:
            self._send(400, {"error": f"invalid span: {exc}"})
            return
        result = self.collector.receive(spans)
        self._send(202, result.to_dict())


@dataclass
class ServerHandle:
    server: ThreadingHTTPServer
    thread: threading.Thread
    collector: Collector

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    @property
    def spans_url(self) -> str:
        return self.url.rstrip("/") + "/spans"

    def stop(self) -> None:
        self.collector.flush()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def __enter__(self) -> "ServerHandle":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


def start_server(store: SpanStore | str, host: str = "127.0.0.1",
                 port: int = 0, *, verbose: bool = False) -> ServerHandle:
    """Start the receiver in a background daemon thread; returns the handle."""
    collector = store if isinstance(store, Collector) else Collector(
        store if isinstance(store, SpanStore) else SpanStore(store)
    )
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.collector = collector  # type: ignore[attr-defined]
    httpd.verbose = verbose  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    return ServerHandle(httpd, thread, collector)


def post_spans(url: str, spans) -> dict[str, Any]:
    """Client helper: POST Spans/dicts to a local receiver; returns its reply."""
    payload = json.dumps(
        [s.to_dict() if isinstance(s, Span) else s for s in spans]
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))
