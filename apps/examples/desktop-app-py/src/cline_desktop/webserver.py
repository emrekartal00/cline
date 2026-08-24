"""Local HTTP server for the built Next.js webview (webview/out).

The static export cannot be loaded from file:// — the sidecar's WebSocket
server enforces a trusted-Origin allowlist and file:// pages send
`Origin: null`. Serving from http://127.0.0.1:3125 (a trusted origin) fixes
that, and lets us inject the ws endpoint into every HTML response before the
app boots.
"""

from __future__ import annotations

import logging
import mimetypes
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

log = logging.getLogger("cline_desktop.webserver")

mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("font/woff2", ".woff2")


class _WebviewHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, root: Path, injection: str, **kwargs) -> None:
        self._root = root
        self._injection = injection
        super().__init__(*args, **kwargs)

    def log_message(self, fmt: str, *args) -> None:  # quiet request logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:
        self._serve(head_only=False)

    def do_HEAD(self) -> None:
        self._serve(head_only=True)

    def _resolve(self, request_path: str) -> Path | None:
        path = request_path.split("?", 1)[0].split("#", 1)[0]
        path = unquote(path).lstrip("/")
        candidate = (self._root / path).resolve() if path else self._root / "index.html"
        try:
            candidate.relative_to(self._root)
        except ValueError:
            return None
        if candidate.is_dir():
            candidate = candidate / "index.html"
        if candidate.is_file():
            return candidate
        # Next.js static export emits per-route .html files; fall back for
        # extensionless client-side routes.
        if not candidate.suffix:
            html = candidate.with_suffix(".html")
            if html.is_file():
                return html
            index = self._root / "index.html"
            if index.is_file():
                return index
        return None

    def _serve(self, head_only: bool) -> None:
        target = self._resolve(self.path)
        if target is None:
            self.send_error(404, "Not found")
            return
        body = target.read_bytes()
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        is_html = target.suffix == ".html"
        if is_html and self._injection:
            marker = b"<head>"
            idx = body.find(marker)
            if idx != -1:
                insert_at = idx + len(marker)
                body = body[:insert_at] + self._injection.encode("utf-8") + body[insert_at:]
            else:
                body = self._injection.encode("utf-8") + body
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8" if is_html else content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if is_html else "public, max-age=3600")
        self.end_headers()
        if not head_only:
            self.wfile.write(body)


class StaticWebviewServer:
    def __init__(self, root: Path, port: int, injection: str) -> None:
        if not (root / "index.html").is_file():
            raise FileNotFoundError(
                f"Built webview not found at {root}. Run `bun run build:ui && "
                "(cd webview && bunx next build)` in apps/examples/desktop-app first."
            )
        handler = partial(_WebviewHandler, root=root.resolve(), injection=injection)
        self._server = ThreadingHTTPServer(("127.0.0.1", port), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        threading.Thread(
            target=self._server.serve_forever, daemon=True, name="webview-http"
        ).start()
        log.info("Serving webview at %s", self.url)

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
