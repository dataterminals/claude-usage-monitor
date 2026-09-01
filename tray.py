"""Claude Usage Monitor — system-tray entry point.

Tray icon shows how close you are to your plan caps (highest live utilization %),
with a limits-first menu and a full dashboard in a native window. Local
transcript accounting lives under the dashboard's "Details" tab.

Data is local; the only network call is the plan-quota reader.

Two things about this loop are load-bearing and easy to undo by accident:

  * Rebuilding the tray icon is expensive. pystray's Win32 backend serializes
    the PIL image to a temp .ico on disk and re-registers it with the shell via
    Shell_NotifyIcon — a synchronous round-trip to explorer.exe. So the icon,
    the tooltip, and the menu are each rewritten only when their *content*
    actually changes, not on every pass.
  * The watchdog observer sets `_dirty` on every transcript write, and Claude
    Code writes continuously while you work. Without MIN_INTERVAL the loop
    free-runs at whatever a pass costs (~0.3s), which floods the notification
    area and wedges the tray until you hover it.

Run:  pythonw launcher.pyw   (or)   python tray.py   (or the built .exe)
"""
import os
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime

import pystray
from PIL import Image, ImageDraw, ImageFont

import pacing
import quota
import window as win_mod
from engine import UsageEngine
from server import make_server

PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
PORT = 8787
REFRESH_SECONDS = 5      # idle cadence: poll at least this often
MIN_INTERVAL = 2.0       # floor between passes, whatever the watchdog says
SAVE_SECONDS = 120       # how often to persist the engine's parse cache

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


def reset_str_time(epoch):
    if not epoch:
        return "—"
    return datetime.fromtimestamp(epoch).strftime("%I:%M%p").lstrip("0")


_FONTS = {}


def _font(size):
    """Cached TrueType load. make_image's fitting loop asks for up to a dozen
    sizes per render, and ImageFont.truetype re-reads the file every call."""
    f = _FONTS.get(size)
    if f is None:
        for path in (r"C:\Windows\Fonts\arialbd.ttf", r"C:\Windows\Fonts\arial.ttf"):
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
        if f is None:
            f = ImageFont.load_default()
        _FONTS[size] = f
    return f


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
        self.pace = {}
        self.quota_enabled = True
        self.auto_refresh = True
        self.url = ""
        self.icon = None
        self.window = None          # DashboardWindow, when pywebview is present
        self.history = pacing.SampleStore()
        self.last_error = None
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._icon_key = None       # (text, accent) currently drawn
        self._title = None          # tooltip currently registered
        self._menu_key = None       # label tuple currently in the menu
        self._last_save = 0.0

    # ---- snapshots served to the HTTP layer -------------------------------
    # Both of these hand back whatever the updater last produced. `snap`,
    # `quota` and `pace` are only ever *replaced* (never mutated in place), so
    # a reader on an HTTP thread always sees one coherent object. The point is
    # that /api/quota must never make a network call on the request thread —
    # an 8s urlopen timeout there is a dashboard that hangs on a cold network.

    def quota_snapshot(self):
        if not self.quota_enabled:
            return {"enabled": False}
        q = self.quota
        if not q:
            return {"enabled": True, "available": False,
                    "reason": "starting up — first reading on its way"}
        data = dict(q)
        data["enabled"] = True
        if data.get("available") and self.pace:
            data["pacing"] = self.pace
        return data

    def usage_snapshot(self):
        return self.snap or self.engine.snapshot()

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

    def _quota_state(self):
        """Short reason the limit labels have nothing to show, or None."""
        if not self.quota_enabled:
            return "live quota off"
        q = self.quota
        if not q:
            return "starting…"
        if not q.get("available"):
            return "not connected"
        return None

    # ---- dynamic menu labels (pystray passes the item arg) ----
    def lbl_5h(self, item=None):
        state = self._quota_state()
        if state:
            return "5-hour limit:  — ({})".format(state)
        u = self._util("five_hour")
        if u is None:
            # Connected fine — the endpoint simply reports no five_hour block
            # until one opens. That is not the same as being disconnected, and
            # calling it "not connected" sent me looking for auth problems that
            # were never there.
            return "5-hour limit:  — (no session window open yet)"
        r = reset_str((self._limits().get("five_hour") or {}).get("resets_at"))
        return "5-hour limit:  {}%{}".format(int(u), "  · resets " + r if r else "")

    def lbl_week(self, item=None):
        state = self._quota_state()
        if state:
            return "Weekly limit:  — ({})".format(state)
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

    def lbl_pace(self, item=None):
        return (self.pace or {}).get("headline") or "Pacing:  —"

    def lbl_day(self, item=None):
        d = (self.pace or {}).get("weekly_day")
        if not d:
            return "Today (limit):  —"
        used = d.get("used_today")
        if used is None:
            return "Today (limit):  {:.1f}% allowance · no history yet".format(d["allowance"])
        line = "Today (limit):  {:.1f}% of {:.1f}%  ·  {:.1f}% left".format(
            used, d["allowance"], d["remaining_today"])
        if not d.get("used_today_exact"):
            # History doesn't reach the 08:00 boundary, so this is a floor, not
            # the day's true total — say where the count actually starts.
            line += "  (since {})".format(reset_str_time(d.get("used_since_epoch")))
        return line

    def lbl_today(self, item=None):
        # `snap` empty means the updater hasn't produced a reading yet, which is
        # the honest test — a warm parse cache has real numbers within a second,
        # a cold one takes as long as your history is long.
        if not self.snap:
            return "Today (cost):  scanning transcripts…"
        return "Today (cost):  " + money(self._win("today").get("cost", 0))

    def lbl_all(self, item=None):
        if not self.snap:
            return "All-time (cost):  —"
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

    def toggle_auto_refresh(self, icon, item):
        self.auto_refresh = not self.auto_refresh
        quota.invalidate()
        self._dirty.set()

    def notify(self, message, title=None):
        if self.icon is None:
            return
        try:
            self.icon.notify(message, title or "Claude Usage")
        except Exception:
            pass

    def attempt_refresh(self, *_):
        def work():
            res = quota.fetch(force=True, allow_refresh=True, force_refresh_token=True)
            if res.get("available"):
                self.quota = res
                self.notify("Live limits are connected again.", "Token refreshed")
            else:
                # Deliberately do NOT overwrite self.quota here: a failed manual
                # attempt used to evict a perfectly good reading and blank the
                # tray for five minutes. quota.fetch() stored this failure with
                # a zero TTL, so the next pass re-reads and restores live data.
                self.notify(res.get("reason") or "unknown error", "Token refresh failed")
            self._dirty.set()
        threading.Thread(target=work, daemon=True).start()

    def force_refresh(self, *_):
        quota.invalidate()
        self._dirty.set()

    def quit(self, *_):
        self._stop.set()
        self._dirty.set()
        try:
            self.engine.save_cache()
        except Exception:
            pass
        if self.icon:
            self.icon.stop()
        # Tear down the pywebview GUI loop (unblocks the main thread) last.
        if self.window is not None:
            self.window.shutdown()

    # ---- background loop ----
    def _updater(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                # A malformed transcript line or a transient Win32 failure must
                # never kill this thread. When it did, the tray silently froze
                # at its last values and only a restart brought it back.
                self.last_error = traceback.format_exc(limit=4)
            if self._stop.is_set():
                break
            # Hard floor between passes. Every watchdog event that lands during
            # the work and this sleep collapses into the single clear() below.
            if self._stop.wait(MIN_INTERVAL):
                break
            self._dirty.clear()
            if self._stop.is_set():
                break       # quit() sets _dirty right after _stop; don't out-race it
            # Then wake on the next change, or on the idle cadence.
            self._dirty.wait(timeout=REFRESH_SECONDS)

    def _tick(self):
        self.engine.refresh()
        self.snap = self.engine.snapshot()
        if self.quota_enabled:
            try:
                self.quota = quota.fetch(allow_refresh=self.auto_refresh)
            except Exception as exc:
                self.quota = {"available": False, "reason": str(exc)}
            self._sample()
        else:
            self.quota = {}
            self.pace = {}
        self._update_icon()
        self._update_menu()
        now = time.monotonic()
        if now - self._last_save >= SAVE_SECONDS:
            self._last_save = now
            try:
                self.engine.save_cache()
            except Exception:
                pass

    def _sample(self):
        """Record the reading and recompute pacing. The updater is the only
        writer of the history file, so there's no cross-process contention."""
        limits = self._limits()
        if not limits:
            self.pace = {}
            return
        try:
            self.history.record(limits)
            self.pace = pacing.compute(limits, store=self.history)
        except Exception:
            self.pace = {}   # pacing is advisory; never break the loop over it

    def _menu_signature(self):
        return (self.lbl_5h(), self.lbl_week(), self.lbl_day(), self.lbl_pace(),
                self.lbl_today(), self.lbl_all(), self.quota_enabled, self.auto_refresh)

    def _update_menu(self):
        """Rebuild the popup only when a label actually reads differently.

        pystray's _update_menu does DestroyMenu + a full rebuild, and it runs on
        this thread while the message-loop thread may be holding that same
        handle inside a live TrackPopupMenuEx. Calling it several times a second
        was both wasteful and a real race.
        """
        if self.icon is None:
            return
        sig = self._menu_signature()
        if sig == self._menu_key:
            return
        self._menu_key = sig
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def _update_icon(self):
        if self.icon is None:
            return
        u = self._worst_util()
        if u is not None:
            text, accent = "{}%".format(int(round(u))), util_accent(u)
            u5, u7 = self._util("five_hour"), self._util("seven_day")
            r5 = reset_str((self._limits().get("five_hour") or {}).get("resets_at"))
            line2 = "5h {}%".format(int(u5)) if u5 is not None else "5h —"
            if u7 is not None:
                line2 += "  ·  Week {}%".format(int(u7))
            title = "Claude Usage  ·  {}% of tightest cap\n{}{}".format(
                int(round(u)), line2, "  ·  resets " + r5 if r5 else "")
            head = (self.pace or {}).get("headline")
            if head:
                title += "\n" + head
        elif not self.snap:
            text, accent = "…", CALM
            title = "Claude Usage  ·  scanning transcripts…"
        else:
            today = self._win("today").get("cost", 0)
            text, accent = money(today), CALM
            reason = self._quota_state() or (self.quota or {}).get("reason") or "no window open yet"
            title = "Claude Usage  ·  Today {}\n(limits: {})".format(money(today), reason)

        # Shell_NotifyIcon's tooltip is a 128-char buffer; anything longer is
        # dropped wholesale rather than clipped, so clip it ourselves.
        if len(title) > 127:
            title = title[:126] + "…"

        # Only touch the shell when the pixels or the text would differ.
        key = (text, accent)
        if key != self._icon_key:
            self._icon_key = key
            self.icon.icon = make_image(text, accent)
        if title != self._title:
            self._title = title
            self.icon.title = title


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
        pystray.MenuItem(app.lbl_day, None, enabled=False),
        pystray.MenuItem(app.lbl_pace, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(app.lbl_today, None, enabled=False),
        pystray.MenuItem(app.lbl_all, None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Live quota", app.toggle_quota, checked=lambda item: app.quota_enabled),
        pystray.MenuItem("Auto-refresh token", app.toggle_auto_refresh,
                         checked=lambda item: app.auto_refresh),
        pystray.MenuItem("Attempt token refresh", app.attempt_refresh),
        pystray.MenuItem("Refresh now", app.force_refresh),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", app.quit),
    )


def main():
    app = App()
    # Warm start from the last run's parse cache. A cold scan of a large
    # history takes over a minute, and it used to run *before* the server, the
    # tray icon or the window existed — so the app was invisible for that whole
    # time, and the launcher's single-instance probe had nothing to answer on.
    # The first real scan now happens on the updater thread instead.
    try:
        app.engine.load_cache()
    except Exception:
        pass

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
