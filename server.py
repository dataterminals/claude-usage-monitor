"""Tiny local HTTP server for the dashboard.

Serves the static dashboard and two JSON endpoints backed by the engine:
    GET /            -> dashboard.html
    GET /api/usage   -> engine.snapshot()
    GET /api/quota   -> experimental plan-quota (only if enabled)
    GET /health      -> {"ok": true}

Bound to 127.0.0.1 only. dashboard.html is read from disk per request, so you
can edit it and refresh the browser without restarting the tray app.
"""
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# When frozen by PyInstaller, dashboard.html is unpacked under sys._MEIPASS.
_HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))


def make_server(state, host="127.0.0.1", port=8787):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # keep the console quiet

        def _send(self, code, body, ctype="application/json"):
            payload = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html"):
                try:
                    with open(os.path.join(_HERE, "dashboard.html"), encoding="utf-8") as f:
                        self._send(200, f.read(), "text/html; charset=utf-8")
                except OSError:
                    self._send(500, "dashboard.html not found", "text/plain")
            elif path == "/api/usage":
                self._send(200, json.dumps(state.engine.snapshot()))
            elif path == "/api/quota":
                self._send(200, json.dumps(state.quota_snapshot()))
            elif path == "/health":
                self._send(200, '{"ok":true}')
            else:
                self._send(404, '{"error":"not found"}')

    last_err = None
    for candidate in (port, 0):  # fall back to an ephemeral port if taken
        try:
            srv = ThreadingHTTPServer((host, candidate), Handler)
            srv.daemon_threads = True
            return srv
        except OSError as exc:
            last_err = exc
    raise RuntimeError("could not bind a local port: {}".format(last_err))
