"""Native dashboard window (pywebview / Edge WebView2).

The app's data always comes from the local server on 127.0.0.1; this module
just hosts the same dashboard.html in a real, chromeless desktop window instead
of a browser tab — so "Claude Usage" behaves like a small self-contained app you
can park in a corner of a monitor.

Lifecycle (the tricky part) — one persistent window that hides instead of dies:
    * pywebview's GUI loop MUST own the main thread; `webview.start()` is called
      exactly once and blocks. So the tray icon and the background updater run
      OFF the main thread, started from the `on_start` callback pywebview runs
      once its loop is live.
    * The window is created HIDDEN, so nothing pops up at login.
    * Closing the window doesn't destroy it — a vetoable `closing` handler HIDES
      it instead and cancels the close. That keeps ≥1 window alive at all times,
      which (a) keeps the GUI loop from exiting when the user closes the window
      and (b) makes "Open dashboard" a cheap `show()` with no window churn.
    * Only `shutdown()` (from Quit) flips a flag that lets the close go through,
      which ends the GUI loop and unblocks the main thread.

If pywebview (or the WebView2 runtime) isn't available, callers should fall
back to opening `url` in the default browser — see `available`.
"""
import os
import tempfile
import threading

try:
    import webview  # pywebview
    available = True
except ImportError:  # pragma: no cover - optional dependency
    webview = None
    available = False

# Private WebView2 user-data dir so our window never contends with another
# WebView2 app (or a second copy of this app) over the shared default profile —
# that collision surfaces as "resource is in use (0x800700AA)".
_STORAGE = os.path.join(tempfile.gettempdir(), "ClaudeUsageMonitor", "webview")


# Chromeless-ish, tall-and-narrow default suited to a monitor corner.
_TITLE = "Claude Usage"
_WIDTH = 480
_HEIGHT = 900
_BG = "#0e1014"          # matches dashboard --bg so there's no white flash
_MIN = (360, 480)


def _nudge_to_front(win):
    """Best-effort bring-to-front. Never raises. Note pywebview's `on_top` is a
    *property* (a bool), not a method."""
    try:
        win.restore()               # in case it was minimized
    except Exception:
        pass
    try:
        win.on_top = True           # flash to the top...
        win.on_top = False          # ...without pinning it there
    except Exception:
        pass


class DashboardWindow:
    """Owns the single native window and the pywebview lifecycle.

    Usage from the tray process:
        win = DashboardWindow(url, icon_path)
        win.run(on_start=<start server + tray + updater>)   # blocks (main thread)
        # ... from a tray callback, on another thread:
        win.open()      # show (or re-show) the window
        win.shutdown()  # let the window close for real, ending the loop (Quit)
    """

    def __init__(self, url, icon_path=None):
        self.url = url
        self.icon_path = icon_path
        self._window = None
        self._started = threading.Event()
        self._quitting = False

    # ---- close interception: hide instead of destroy ----
    def _on_closing(self):
        # Vetoable: returning False cancels the close. We hide instead, unless
        # we're actually quitting (shutdown() set the flag), so the window
        # persists and the GUI loop stays alive across close/reopen.
        if self._quitting:
            return True             # allow the real close -> loop can exit
        try:
            self._window.hide()
        except Exception:
            pass
        return False                # cancel the close

    # ---- window creation ----
    def _make_window(self, hidden):
        win = webview.create_window(
            _TITLE, self.url,
            width=_WIDTH, height=_HEIGHT, min_size=_MIN,
            background_color=_BG, hidden=hidden, focus=not hidden,
            resizable=True, text_select=False, confirm_close=False,
        )
        win.events.closing += self._on_closing
        return win

    # ---- lifecycle ----
    def run(self, on_start=None):
        """Create the hidden window and start the (blocking) GUI loop.

        `on_start` runs once, after the loop is live, on a pywebview worker
        thread — the right place to start the HTTP server, tray icon, and
        updater so they never contend for the main thread.
        """
        self._window = self._make_window(hidden=True)

        def _bootstrap():
            self._started.set()
            if on_start:
                on_start()

        try:
            os.makedirs(_STORAGE, exist_ok=True)
        except OSError:
            pass
        webview.start(_bootstrap, icon=self.icon_path,
                      private_mode=False, storage_path=_STORAGE)

    def open(self):
        """Show (or re-show) the window. Thread-safe; safe to call repeatedly."""
        if not self._started.wait(timeout=10):
            return  # GUI loop never came up; caller may fall back to browser
        win = self._window
        if win is None:
            return
        try:
            win.show()
            _nudge_to_front(win)
        except Exception:
            pass

    def shutdown(self):
        """Allow the window to close for real, ending the GUI loop. Used by Quit."""
        self._quitting = True
        try:
            if self._window is not None:
                self._window.destroy()
        except Exception:
            pass
