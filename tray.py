"""Claude Usage Monitor — system-tray entry point.

Lightweight tray icon that watches ~/.claude/projects transcripts, shows
today's notional cost on the icon + a live tooltip, and opens a full local
dashboard in the browser. All data is local; the only network call is the
opt-in experimental plan-quota reader.

Run:  pythonw run.pyw   (or)   python tray.py
"""
import os
import threading
import webbrowser

import pystray
from PIL import Image, ImageDraw, ImageFont

import quota
from engine import UsageEngine
from server import make_server

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
PORT = 8787
REFRESH_SECONDS = 5


# ---- money / icon helpers -------------------------------------------------

def money(x):
    if x is None:
        return "$0"
    if x >= 100:
        return "${:.0f}".format(x)
    if x >= 10:
        return "${:.1f}".format(x)
    return "${:.2f}".format(x)


def icon_text(cost):
    if cost >= 10:
        return "${:.0f}".format(cost)
    if cost >= 1:
        return "${:.1f}".format(cost)
    return "¢{:.0f}".format(cost * 100) if cost > 0 else "$0"


def accent_for(cost):
    if cost >= 20:
        return (242, 104, 90)     # red
    if cost >= 5:
        return (242, 179, 75)     # amber
    return (90, 162, 255)         # blue


def _font(size):
    for path in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def make_image(text, accent):
    S = 128
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([5, 5, S - 5, S - 5], radius=26,
                        fill=(24, 26, 32, 255), outline=accent, width=7)
    size = 64
    while size > 16:
        f = _font(size)
        l, t, r, b = d.textbbox((0, 0), text, font=f)
        if (r - l) <= S - 26 and (b - t) <= S - 26:
            break
        size -= 4
    f = _font(size)
    l, t, r, b = d.textbbox((0, 0), text, font=f)
    d.text(((S - (r - l)) / 2 - l, (S - (b - t)) / 2 - t), text, font=f, fill=accent)
    return img


# ---- app state ------------------------------------------------------------

class App:
    def __init__(self):
        self.engine = UsageEngine(PROJECTS_DIR)
        self.snap = {}
        self.quota_enabled = False
        self.url = ""
        self.icon = None
        self._stop = threading.Event()
        self._dirty = threading.Event()

    def quota_snapshot(self):
        if not self.quota_enabled:
            return {"enabled": False}
        data = quota.fetch()
        data["enabled"] = True
        return data

    # dynamic menu label helpers
    def _win(self, key):
        return (self.snap.get("windows") or {}).get(key) or {}

    # pystray calls dynamic-text callables with the menu item as an arg
    def lbl_today(self, item=None):
        return "Today:  " + money(self._win("today").get("cost", 0))

    def lbl_block(self, item=None):
        w = self._win("rolling_5h")
        return "5h block:  {}  ({}/h)".format(
            money(w.get("cost", 0)), money(w.get("burn_cost_per_hour", 0)))

    def lbl_week(self, item=None):
        return "7 days:  " + money(self._win("week_7d").get("cost", 0))

    def lbl_all(self, item=None):
        w = self._win("all")
        return "All time:  {}  ({} msgs)".format(
            money(w.get("cost", 0)), (self.snap.get("meta") or {}).get("record_count", 0))

    # actions
    def open_dashboard(self, *_):
        webbrowser.open(self.url)

    def toggle_quota(self, icon, item):
        self.quota_enabled = not self.quota_enabled
        if self.quota_enabled:
            threading.Thread(target=quota.fetch, kwargs={"force": True}, daemon=True).start()

    def force_refresh(self, *_):
        self._dirty.set()

    def quit(self, *_):
        self._stop.set()
        self._dirty.set()
        if self.icon:
            self.icon.stop()

    # background refresh loop
    def _updater(self):
        while not self._stop.is_set():
            self.engine.refresh()
            self.snap = self.engine.snapshot()
            self._update_icon()
            if self.icon is not None:
                try:
                    self.icon.update_menu()
                except Exception:
                    pass
            self._dirty.wait(timeout=REFRESH_SECONDS)
            self._dirty.clear()

    def _update_icon(self):
        if self.icon is None:
            return
        today = self._win("today").get("cost", 0)
        b = self._win("rolling_5h")
        wk = self._win("week_7d").get("cost", 0)
        self.icon.icon = make_image(icon_text(today), accent_for(today))
        self.icon.title = "Claude Usage  ·  Today {}\n5h {}  ·  Week {}".format(
            money(today), money(b.get("cost", 0)), money(wk))


def start_watcher(app):
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None  # periodic refresh still covers updates

    class Handler(FileSystemEventHandler):
        def on_any_event(self, event):
            if str(getattr(event, "src_path", "")).endswith(".jsonl"):
                app._dirty.set()

    obs = Observer()
    try:
        obs.schedule(Handler(), PROJECTS_DIR, recursive=True)
        obs.daemon = True
        obs.start()
        return obs
    except OSError:
        return None


def main():
    app = App()
    app.engine.refresh()
    app.snap = app.engine.snapshot()

    srv = make_server(app, port=PORT)
    host, port = srv.server_address
    app.url = "http://127.0.0.1:{}/".format(port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    start_watcher(app)

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", app.open_dashboard, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(app.lbl_today, None, enabled=False),
        pystray.MenuItem(app.lbl_block, None, enabled=False),
        pystray.MenuItem(app.lbl_week, None, enabled=False),
        pystray.MenuItem(app.lbl_all, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Live quota (experimental)", app.toggle_quota,
                         checked=lambda item: app.quota_enabled),
        pystray.MenuItem("Refresh now", app.force_refresh),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", app.quit),
    )
    app.icon = pystray.Icon("claude-usage", make_image("$0", accent_for(0)),
                            "Claude Usage", menu)
    app._update_icon()

    threading.Thread(target=app._updater, daemon=True).start()
    app.icon.run()


if __name__ == "__main__":
    main()
