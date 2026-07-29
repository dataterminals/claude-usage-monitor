"""Claude Usage Monitor — system-tray entry point.

Tray icon shows how close you are to your plan caps (highest live utilization %),
with a limits-first menu and a full dashboard in the browser. Local transcript
accounting lives under the dashboard's "Details" tab.

Data is local; the only network call is the plan-quota reader, which is
read-only unless you explicitly use "Attempt token refresh".

Run:  pythonw run.pyw   (or)   python tray.py   (or the built .exe)
"""
import os
import sys
import threading
import webbrowser
from datetime import datetime

import pystray
from PIL import Image, ImageDraw, ImageFont

import quota
import window as win_mod
from engine import UsageEngine
from server import make_server

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
PORT = 8787
REFRESH_SECONDS = 5

# When frozen by PyInstaller, bundled files unpack under sys._MEIPASS.
_HERE = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
ICON_ICO = os.path.join(_HERE, "icons", "app.ico")

CALM = (90, 162, 255)
GREEN = (67, 201, 139)
AMBER = (242, 179, 75)
RED = (242, 104, 90)


def money(x):
    if x is None:
        return "$0"
    if x >= 100:
        return "${:.0f}".format(x)
    if x >= 10:
        return "${:.1f}".format(x)
    return "${:.2f}".format(x)


def util_accent(u):
    if u is None:
        return CALM
    if u >= 90:
        return RED
    if u >= 70:
        return AMBER
    return GREEN


def reset_str(iso):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%a %I:%M%p").replace(" 0", " ").lstrip("0")
    except (AttributeError, ValueError):
        return ""


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
    d.rounded_rectangle([5, 5, S - 5, S - 5], radius=26, fill=(24, 26, 32, 255), outline=accent, width=7)
    size = 66
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


class App:
    def __init__(self):
        self.engine = UsageEngine(PROJECTS_DIR)
        self.snap = {}
        self.quota = {}
        self.quota_enabled = True
        self.url = ""
        self.icon = None
        self.window = None          # DashboardWindow, when pywebview is present
        self._stop = threading.Event()
        self._dirty = threading.Event()

    def quota_snapshot(self):
        if not self.quota_enabled:
            return {"enabled": False}
        data = quota.fetch()
        data["enabled"] = True
        return data

    # ---- limit helpers ----
    def _limits(self):
        q = self.quota
        return (q.get("limits") or {}) if (q and q.get("available")) else {}

    def _util(self, key):
        v = self._limits().get(key)
        return v.get("utilization") if v else None

    def _worst_util(self):
        vals = [v["utilization"] for v in self._limits().values()
                if v and v.get("utilization") is not None]
        return max(vals) if vals else None

    def _win(self, key):
        return (self.snap.get("windows") or {}).get(key) or {}

    # ---- dynamic menu labels (pystray passes the item arg) ----
    def lbl_5h(self, item=None):
        u = self._util("five_hour")
        if u is None:
            return "5-hour limit:  — (not connected)"
        r = reset_str((self._limits().get("five_hour") or {}).get("resets_at"))
        return "5-hour limit:  {}%{}".format(int(u), "  · resets " + r if r else "")

    def lbl_week(self, item=None):
        u = self._util("seven_day")
        uo = self._util("seven_day_opus")
        if u is None and uo is None:
            return "Weekly limit:  —"
        parts = []
        if u is not None:
            parts.append("all {}%".format(int(u)))
        if uo is not None:
            parts.append("Opus {}%".format(int(uo)))
        return "Weekly limit:  " + " · ".join(parts)

    def lbl_today(self, item=None):
        return "Today (cost):  " + money(self._win("today").get("cost", 0))

    def lbl_all(self, item=None):
        return "All-time (cost):  " + money(self._win("all").get("cost", 0))

    # ---- actions ----
    def open_dashboard(self, *_):
        # Prefer the native window; fall back to the browser if pywebview or
        # its WebView2 runtime isn't available.
        if self.window is not None:
            try:
                self.window.open()
                return
            except Exception:
                pass
        webbrowser.open(self.url)

    def toggle_quota(self, icon, item):
        self.quota_enabled = not self.quota_enabled
        self._dirty.set()

    def attempt_refresh(self, *_):
        def work():
            self.quota = quota.fetch(force=True, allow_refresh=True, force_refresh_token=True)
            self._update_icon()
            if self.icon:
                self.icon.update_menu()
        threading.Thread(target=work, daemon=True).start()

    def force_refresh(self, *_):
        self._dirty.set()

    def quit(self, *_):
        self._stop.set()
        self._dirty.set()
        if self.icon:
            self.icon.stop()
        # Tear down the pywebview GUI loop (unblocks the main thread) last.
        if self.window is not None:
            self.window.shutdown()

    # ---- background loop ----
    def _updater(self):
        while not self._stop.is_set():
            self.engine.refresh()
            self.snap = self.engine.snapshot()
            if self.quota_enabled:
                # Never let a quota hiccup kill the refresh loop — without this
                # the tray would silently freeze at its startup state.
                try:
                    self.quota = quota.fetch()  # read-only, cached
                except Exception as exc:
                    self.quota = {"available": False, "reason": str(exc)}
            else:
                self.quota = {}
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
        u = self._worst_util()
        if u is not None:
            self.icon.icon = make_image("{}%".format(int(round(u))), util_accent(u))
            u5, u7 = self._util("five_hour"), self._util("seven_day")
            r5 = reset_str((self._limits().get("five_hour") or {}).get("resets_at"))
            line2 = "5h {}%".format(int(u5)) if u5 is not None else "5h —"
            if u7 is not None:
                line2 += "  ·  Week {}%".format(int(u7))
            self.icon.title = "Claude Usage  ·  {}% of tightest cap\n{}{}".format(
                int(round(u)), line2, "  ·  resets " + r5 if r5 else "")
        else:
            today = self._win("today").get("cost", 0)
            self.icon.icon = make_image(money(today), CALM)
            reason = (self.quota or {}).get("reason", "live limits off")
            self.icon.title = "Claude Usage  ·  Today {}\n(limits: {})".format(money(today), reason)


def start_watcher(app):
    try:
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers import Observer
    except ImportError:
        return None

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


def build_menu(app):
    return pystray.Menu(
        pystray.MenuItem("Open dashboard", app.open_dashboard, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(app.lbl_5h, None, enabled=False),
        pystray.MenuItem(app.lbl_week, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(app.lbl_today, None, enabled=False),
        pystray.MenuItem(app.lbl_all, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Live quota", app.toggle_quota, checked=lambda item: app.quota_enabled),
        pystray.MenuItem("Attempt token refresh", app.attempt_refresh),
        pystray.MenuItem("Refresh now", app.force_refresh),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", app.quit),
    )


def main():
    app = App()
    app.engine.refresh()
    app.snap = app.engine.snapshot()

    srv = make_server(app, port=PORT)
    _, port = srv.server_address
    app.url = "http://127.0.0.1:{}/".format(port)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    start_watcher(app)

    app.icon = pystray.Icon("claude-usage", make_image("…", CALM), "Claude Usage", build_menu(app))
    app._update_icon()

    icon_path = ICON_ICO if os.path.exists(ICON_ICO) else None

    started = threading.Event()  # guards against double-starting the tray/updater

    def start_tray_and_updater(detached):
        if started.is_set():
            return
        started.set()
        threading.Thread(target=app._updater, daemon=True).start()
        if detached:
            app.icon.run_detached()   # tray on a bg thread; caller owns main thread
        else:
            app.icon.run()            # tray owns the (main) thread; blocks

    if win_mod.available:
        # pywebview owns the main thread. Start the tray icon (detached) and the
        # background updater once its GUI loop is live, so nothing contends for
        # the main thread. The window is created hidden — "Open dashboard" shows
        # it — so nothing pops up unbidden.
        app.window = win_mod.DashboardWindow(app.url, icon_path)
        try:
            app.window.run(on_start=lambda: start_tray_and_updater(detached=True))
        except Exception:
            # WebView2 loop couldn't start (e.g. runtime genuinely absent).
            # Fall back to tray-only; "Open dashboard" then uses the browser.
            app.window = None
            start_tray_and_updater(detached=False)
    else:
        # No pywebview: original tray-owns-main-thread flow.
        # "Open dashboard" opens the browser (see App.open_dashboard).
        start_tray_and_updater(detached=False)


if __name__ == "__main__":
    main()
