"""화면 페이지와 동작 API를 127.0.0.1에서만 내준다.

다른 프로그램이나 웹 페이지가 업로드 API를 부르지 못하게
- Host 머리글이 정확히 127.0.0.1:포트인지 본다(DNS 리바인딩 막기).
- API는 실행마다 새로 만드는 토큰(X-App-Token)이 맞아야 한다. 사용자 정의 머리글이라 다른 사이트의 요청은 CORS 사전 확인에서 막힌다.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .controller import UserError

log = logging.getLogger("gphoto")

_CTYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
           ".js": "application/javascript; charset=utf-8", ".svg": "image/svg+xml",
           ".png": "image/png", ".ico": "image/x-icon"}


def _routes():
    get = {
        "/api/state": lambda c, p: c.snapshot(),
        "/api/albums": lambda c, p: c.albums(),
        "/api/confirm": lambda c, p: c.confirm_info(),
        "/api/failures": lambda c, p: c.failures(),
    }
    post = {
        "/api/client": lambda c, p: c.choose_client(p.get("path")),
        "/api/guide": lambda c, p: c.open_guide(p.get("page")),
        "/api/guide_step": lambda c, p: c.set_guide_step(p.get("step")),
        "/api/login": lambda c, p: c.login(),
        "/api/login_cancel": lambda c, p: c.cancel_login(),
        "/api/logout": lambda c, p: c.logout(),
        "/api/root": lambda c, p: c.choose_root(p.get("path")),
        "/api/options": lambda c, p: c.set_options(p.get("include_root"), p.get("include_videos")),
        "/api/scan": lambda c, p: c.scan(),
        "/api/select": lambda c, p: c.select(p.get("keys"), p.get("on", True), p.get("all")),
        "/api/start": lambda c, p: c.start(),
        "/api/stop": lambda c, p: c.stop(),
        "/api/forget_album": lambda c, p: c.forget_album(p.get("key", "")),
        "/api/retry_failed": lambda c, p: c.retry_failed(),
        "/api/open": lambda c, p: c.open_target(p.get("what"), p.get("key")),
        "/api/quit": lambda c, p: c.quit(),
    }
    return get, post


class AppServer:
    def __init__(self, controller, ui_dir, port=0):
        self.ctl = controller
        self.ui_dir = Path(ui_dir).resolve()
        self.token = secrets.token_urlsafe(24)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/?t={self.token}"
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _handler(self):
        server = self
        get_routes, post_routes = _routes()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # pythonw에는 stderr가 없다
                pass

            def _send(self, code, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            def _allowed(self, api: bool) -> bool:
                if self.headers.get("Host") != f"127.0.0.1:{server.port}":
                    self._send(403, b"forbidden", "text/plain")
                    return False
                if api and not secrets.compare_digest(self.headers.get("X-App-Token", ""),
                                                      server.token):
                    self._json(403, {"ok": False, "error": "토큰이 맞지 않습니다"})
                    return False
                return True

            def _call(self, fn, params):
                try:
                    data = fn(server.ctl, params)
                    self._json(200, {"ok": True, "data": data})
                except UserError as e:
                    self._json(200, {"ok": False, "error": str(e)})
                except Exception as e:  # noqa: BLE001
                    log.exception("API 오류")
                    self._json(500, {"ok": False, "error": f"오류: {e}"})

            def do_GET(self):  # noqa: N802
                path = urlparse(self.path).path
                if path.startswith("/api/"):
                    if not self._allowed(True):
                        return
                    fn = get_routes.get(path)
                    if fn is None:
                        return self._json(404, {"ok": False, "error": "없는 API"})
                    return self._call(fn, {})
                if not self._allowed(False):
                    return
                if path == "/":
                    path = "/index.html"
                f = (server.ui_dir / path.lstrip("/")).resolve()
                if server.ui_dir not in f.parents or not f.is_file():
                    return self._send(404, b"not found", "text/plain")
                self._send(200, f.read_bytes(),
                           _CTYPES.get(f.suffix.lower(), "application/octet-stream"))

            def do_POST(self):  # noqa: N802
                path = urlparse(self.path).path
                if not self._allowed(True):
                    return
                fn = post_routes.get(path)
                if fn is None:
                    return self._json(404, {"ok": False, "error": "없는 API"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n > 1_000_000:
                    self.close_connection = True  # 본문을 읽지 않았으니 연결을 끊는다
                    return self._json(413, {"ok": False, "error": "요청이 너무 큽니다"})
                raw = self.rfile.read(n) if n else b"{}"
                try:
                    params = json.loads(raw.decode("utf-8") or "{}")
                    if not isinstance(params, dict):
                        raise ValueError
                except ValueError:
                    return self._json(400, {"ok": False, "error": "요청 형식 오류"})
                return self._call(fn, params)

        return Handler
