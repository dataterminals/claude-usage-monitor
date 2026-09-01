"""Tiny local HTTP server for the dashboard.

Serves the static dashboard and two JSON endpoints backed by the engine:
    GET /            -> dashboard.html
    GET /api/usage   -> the updater's latest engine snapshot
    GET /api/quota   -> experimental plan-quota (only if enabled)
    GET /health      -> {"ok": true}

Both API handlers serve what the tray's updater thread last computed rather
than recomputing (or, worse, making a network call) on the request thread — a
poll that blocks on an 8s urlopen timeout is a dashboard that hangs.

Bound to 127.0.0.1 only. dashboard.html is read from disk per request, so the
native window (or a browser) reflects edits on refresh without a restart.
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
                self._send(200, json.dumps(state.usage_snapshot()))
            elif path == "/api/quota":
                self._send(200, json.dumps(state.quota_snapshot()))
            elif path == "/health":
                self._send(200, '{"ok":true}')
            else:
                self._send(404, '{"error":"not found"}')

    # A stable origin matters here: the PWA's service worker is scoped to
    # host:port, so a drifting port would orphan its cache and point the
    # installed app at a dead origin. Try a short *deterministic* ladder near
    # the preferred port and stop — never fall back to a random ephemeral port.
    last_err = None
    for candidate in (port, port + 1, port + 2, port + 3):
        try:
            srv = ThreadingHTTPServer((host, candidate), Handler)
            srv.daemon_threads = True
            return srv
        except OSError as exc:
            last_err = exc
    raise RuntimeError(
        "could not bind {}:{}-{} (all in use): {}".format(host, port, port + 3, last_err))
