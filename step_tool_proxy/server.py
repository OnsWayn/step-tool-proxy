"""ThreadingHTTPServer routing for Step Tool Proxy."""

from __future__ import annotations

import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__
from .auth import authenticate
from .config import get_config
from .proxy import handle_chat_completions, handle_models
from .store import TokenStore, UpstreamStore
from .web import handle_admin, read_index


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


class App:
    def __init__(self) -> None:
        self.cfg = get_config()
        # Seeded from STEP_API_KEY / the retired settings value on first run.
        self.upstreams = UpstreamStore(self.cfg.data_dir, seed_key=self.cfg.seed_api_key())
        self.store = TokenStore(
            self.cfg.data_dir, default_upstream_id=self.upstreams.default_id()
        )


_app: App | None = None


def get_app() -> App:
    global _app
    if _app is None:
        _app = App()
    return _app


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log("HTTP " + (fmt % args))

    def _read_body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return b""
        return self.rfile.read(n)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json",
        extra_headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _json_err(self, status: int, msg: str) -> None:
        body = json.dumps({"error": {"message": msg, "type": "proxy_error"}}).encode()
        self._send(status, body)

    def _auth(self):
        app = get_app()
        return authenticate(
            authorization=self.headers.get("Authorization"),
            cookie_header=self.headers.get("Cookie"),
            master_token=app.cfg.master_token,
            store=app.store,
            upstreams=app.upstreams,
        )

    def do_OPTIONS(self) -> None:
        self._send(204, b"")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/health":
            self._send(200, b'{"ok":true}', "application/json")
            return

        if path in ("/", "/index.html"):
            try:
                html = read_index()
            except FileNotFoundError:
                self._json_err(500, "WebUI missing")
                return
            self._send(200, html, "text/html; charset=utf-8")
            return

        if path.startswith("/api/"):
            app = get_app()
            auth = self._auth()
            status, body, ct, extra = handle_admin(
                "GET", path, b"", auth, app.cfg, app.store, self.headers.get("Cookie"), app.upstreams
            )
            self._send(status, body, ct, extra)
            return

        if path in ("/v1/models", "/models"):
            auth = self._auth()
            if not auth.ok_api:
                self._json_err(401, "Unauthorized — use a client or master Bearer token")
                return
            app = get_app()
            api_key = auth.upstream_key or (
                app.upstreams.first_active_key() if auth.is_master else None
            )
            if not api_key:
                self._json_err(503, "No usable upstream key for this client token")
                return
            status, body, ct = handle_models(app.cfg, api_key)
            self._send(status, body, ct)
            return

        self._json_err(404, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        raw = self._read_body()

        if path.startswith("/api/"):
            app = get_app()
            auth = self._auth()
            status, body, ct, extra = handle_admin(
                "POST", path, raw, auth, app.cfg, app.store, self.headers.get("Cookie"), app.upstreams
            )
            self._send(status, body, ct, extra)
            return

        if path.rstrip("/").endswith("chat/completions") or path in (
            "/v1/chat/completions",
            "/chat/completions",
        ):
            auth = self._auth()
            if not auth.ok_api:
                self._json_err(401, "Unauthorized — use a client or master Bearer token")
                return
            app = get_app()
            api_key = auth.upstream_key or (
                app.upstreams.first_active_key() if auth.is_master else None
            )
            if not api_key:
                self._json_err(503, "No usable upstream key for this client token")
                return
            try:
                body = json.loads(raw) if raw else {}
            except Exception:
                self._json_err(400, "invalid JSON body")
                return
            status, out, ct = handle_chat_completions(
                app.cfg, body, raw, log=log, api_key=api_key
            )
            self._send(status, out, ct)
            return

        self._json_err(404, "not found")

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith("/api/"):
            app = get_app()
            auth = self._auth()
            status, body, ct, extra = handle_admin(
                "DELETE", path, b"", auth, app.cfg, app.store, self.headers.get("Cookie"), app.upstreams
            )
            self._send(status, body, ct, extra)
            return
        self._json_err(404, "not found")


def main() -> None:
    app = get_app()
    cfg = app.cfg
    if not app.upstreams.first_active_key():
        log("WARNING: no upstream key configured — proxying will return 503")
    addr = (cfg.host, cfg.port)
    httpd = ThreadingHTTPServer(addr, Handler)
    log(
        f"step-tool-proxy v{__version__} listening http://{cfg.host}:{cfg.port} "
        f"-> {cfg.upstream} FORCE_BUFFER={int(cfg.force_buffer)}"
    )
    log(f"WebUI: http://127.0.0.1:{cfg.port}/  data={cfg.data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
        httpd.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
