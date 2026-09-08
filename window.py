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
# Deliberately below anything the full layout tolerates, so hand-dragging is
# governed by the CSS tiers rather than by this tuple. Note it is NOT the real
# floor: Windows refuses to shrink a captioned, resizable window past roughly
# 136px wide (the caption buttons set that limit), and a resize() asking for
# less is silently clamped up to it. Measured, not assumed — resize(110, 800)
# comes back as 136 while resize(400, 600) is exact, so it is a width clamp and
# not DPI scaling. The clamp has a name: SM_CXMINTRACK, measured at 136 here,
# and it is not lowerable — forcing WM_GETMINMAXINFO's ptMinTrackSize to (1, 1)
# still came back as 136. Only dropping WS_CAPTION moves it, which we won't.
_MIN = (72, 240)

# Docked-strip width — the width you can SEE, not the window rect. Those differ
# by the invisible resize border (see _frame_insets): asking for a visible 136
# means a 150px window rect, which is comfortably over the 136 SM_CXMINTRACK
# floor and so is honoured exactly. Before this was a window-rect number, so a
# "136px" dock actually showed 122px and rendered at a 120px CSS viewport;
# now it shows 136 and renders at 134. Both sit inside the dashboard's
# <140px "edge" tier, so the docked layout is unchanged — label over
# percentage, bar, and the rollover clock. There are only 5px of slack there:
# a visible width of 142+ (client 140) would tip into the <170px "sliver" tier
# and quietly change what the strip looks like.
_DOCK_W = 136


# Every Win32 helper below follows the same contract as _work_area always has:
# it returns None (or a neutral value) on any failure, so a machine without
# ctypes, without these entry points, or simply having a bad day degrades to
# the old behaviour instead of raising into a tray callback.
#
# All of them open their OWN ctypes.WinDLL("user32") rather than reaching for
# ctypes.windll.user32. That singleton is cached process-wide and pywebview
# calls it too — winforms.move() passes Python None for SetWindowPos's cx/cy
# and relies on there being no argtypes. Setting .argtypes on the shared handle
# makes every subsequent win.move() die with "argument 5: TypeError: 'NoneType'
# object cannot be interpreted as an integer". Measured the hard way; our own
# handles carry our own prototypes and nobody else's calls notice.
_MONITOR_DEFAULTTONEAREST = 2


def _user32():
    """A private user32 handle, or None. See the note above on why it's private."""
    try:
        import ctypes
        return ctypes.WinDLL("user32")
    except (ImportError, AttributeError, OSError):   # pragma: no cover - non-Windows
        return None


def _hwnd(win):
    """The Win32 handle behind a pywebview window, or None if there isn't one."""
    try:
        return int(win.native.Handle.ToInt64())
    except Exception:
        return None


def _rc_work(hmon):
    """rcWork of a monitor handle as (left, top, width, height), or None.

    rcWork rather than rcMonitor so the strip's foot stops at the taskbar
    instead of hiding behind it. Note rcWork is not (0, 0)-based even for the
    top edge: one of this desktop's monitors reports rcWork.top = 4, so "full
    height" has to come from the monitor, never from a constant.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):        # pragma: no cover - non-Windows
        # ValueError, not ImportError: `from ctypes import wintypes` on a
        # non-Windows build raises ValueError: _type_ 'v' not supported.
        return None

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD),
                    ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT),
                    ("dwFlags", wintypes.DWORD)]

    user32 = _user32()
    if user32 is None or not hmon:
        return None
    try:
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE,
                                           ctypes.POINTER(_MONITORINFO)]
        mi = _MONITORINFO()
        mi.cbSize = ctypes.sizeof(_MONITORINFO)
        if not user32.GetMonitorInfoW(wintypes.HANDLE(hmon), ctypes.byref(mi)):
            return None
        r = mi.rcWork
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)
    except (AttributeError, OSError, ValueError):
        return None


def _work_area(x=0, y=0):
    """The taskbar-excluded rect of the monitor containing the point (x, y).

    Returns (left, top, width, height), or None if the Win32 call is
    unavailable. Kept for callers that have a coordinate and nothing else; the
    docking path uses _window_work_area instead, because a single point is a
    knife edge (see there).
    """
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):        # pragma: no cover - non-Windows
        # ValueError, not ImportError: `from ctypes import wintypes` on a
        # non-Windows build raises ValueError: _type_ 'v' not supported.
        return None
    user32 = _user32()
    if user32 is None:
        return None
    try:
        user32.MonitorFromPoint.restype = wintypes.HANDLE
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        hmon = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)),
                                       _MONITOR_DEFAULTTONEAREST)
    except (AttributeError, OSError, ValueError):
        return None
    return _rc_work(hmon)


def _window_work_area(win):
    """rcWork of the monitor the window actually lives on, or None.

    MonitorFromWindow, NOT MonitorFromPoint(win.x, win.y). win.x is the *window
    rect* origin, which sits 7px outside the visible frame (see
    _frame_insets), so a strip that Windows itself snapped flush at x = 1920
    reports win.x = 1913 — and MonitorFromPoint(1913, 0) hands back the
    PRIMARY monitor while MonitorFromWindow returns the right-hand one.
    Measured on the live window: point -> 0x20056, window -> 0x20062. That is
    a one-click teleport to the wrong screen, and compensation would have made
    it systematic (each dock sets x to rcWork.left - 7, so the strip would walk
    one monitor left per click). MonitorFromWindow picks the monitor with the
    largest intersection, which is the same rule Aero Snap and maximize use.
    """
    hwnd = _hwnd(win)
    if hwnd:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = _user32()
            if user32 is not None:
                user32.MonitorFromWindow.restype = wintypes.HANDLE
                user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
                area = _rc_work(user32.MonitorFromWindow(wintypes.HWND(hwnd),
                                                         _MONITOR_DEFAULTTONEAREST))
                if area is not None:
                    return area
        except (ImportError, AttributeError, OSError, ValueError):
            pass
    try:                                     # no hwnd: the old point-based path
        return _work_area(win.x, win.y)
    except Exception:
        return None                          # not _work_area(0, 0): that is the
                                             # call that just raised.


def _rect_work_area(x, y, w, h):
    """rcWork of the monitor nearest a rect, or None. Used to rescue geometry
    that names a monitor which has since been unplugged or rearranged."""
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):        # pragma: no cover - non-Windows
        # ValueError, not ImportError: `from ctypes import wintypes` on a
        # non-Windows build raises ValueError: _type_ 'v' not supported.
        return None
    user32 = _user32()
    if user32 is None:
        return None
    try:
        user32.MonitorFromRect.restype = wintypes.HANDLE
        user32.MonitorFromRect.argtypes = [ctypes.POINTER(wintypes.RECT),
                                           wintypes.DWORD]
        rc = wintypes.RECT(int(x), int(y), int(x) + int(w), int(y) + int(h))
        hmon = user32.MonitorFromRect(ctypes.byref(rc), _MONITOR_DEFAULTTONEAREST)
    except (AttributeError, OSError, ValueError):
        return None
    return _rc_work(hmon)


def _frame_insets(win):
    """The invisible resize border around a window: (left, top, right, bottom).

    Windows 10/11 draws a captioned window's frame *inside* a rect that is
    bigger than what you see, and Aero Snap compensates for that — which is
    exactly why a Windows-snapped window's GetWindowRect starts 7px to the left
    of the screen edge and 7px below its bottom. Measured on this machine, on
    both a throwaway window and the live one: (7, 0, 7, 7) while restored. Top
    is 0 because the caption is drawn flush to the window rect.

    Measured per window, never hardcoded and never derived from system metrics:
    SM_CXSIZEFRAME(4) + SM_CXPADDEDBORDER(4) = 8 and AdjustWindowRectEx agrees
    with them, but the real border is that minus SM_CXBORDER(1) = 7. A fix
    built on either would still be a pixel out — harder to see than the 7px
    seam and much harder to explain.

    Returns (0, 0, 0, 0) on any failure, and deliberately also while the window
    is minimized or maximized: DWM does not fail in those states, it lies
    quietly. Minimized it reports (7, 0, 7, 0) — bottom silently zero, so the
    strip would still stop short of the taskbar — and maximized it reports
    (8, 8, 8, 8), a real border but the wrong one for the rect we are about to
    set. Zeros mean the caller places the window the old, uncompensated way
    rather than a confidently wrong way.
    """
    zero = (0, 0, 0, 0)
    hwnd = _hwnd(win)
    if not hwnd:
        return zero
    try:
        import ctypes
        from ctypes import wintypes
    except (ImportError, ValueError):        # pragma: no cover - non-Windows
        # ValueError, not ImportError: `from ctypes import wintypes` on a
        # non-Windows build raises ValueError: _type_ 'v' not supported.
        return zero
    user32 = _user32()
    if user32 is None:
        return zero
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
    except (AttributeError, OSError):        # pragma: no cover - no DWM
        return zero
    try:
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsZoomed.argtypes = [wintypes.HWND]
        h = wintypes.HWND(hwnd)
        if user32.IsIconic(h) or user32.IsZoomed(h):
            return zero                      # the states where DWM lies

        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        rect = wintypes.RECT()
        if not user32.GetWindowRect(h, ctypes.byref(rect)):
            return zero

        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        frame = wintypes.RECT()
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        dwmapi.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                                 ctypes.c_void_p, wintypes.DWORD]
        if dwmapi.DwmGetWindowAttribute(h, DWMWA_EXTENDED_FRAME_BOUNDS,
                                        ctypes.byref(frame),
                                        ctypes.sizeof(frame)) != 0:
            return zero

        insets = (frame.left - rect.left, frame.top - rect.top,
                  rect.right - frame.right, rect.bottom - frame.bottom)
        # Sanity: 7 here, up to ~16 at 240 dpi. Anything outside this is a
        # window we don't understand, and guessing would move it wrongly.
        if any(v < 0 or v > 32 for v in insets):
            return zero
        return insets
    except (AttributeError, OSError, ValueError):
        return zero


def _place(win, x, y, w, h):
    """Set a window's *rect* in one shot. Returns True if it landed.

    One SetWindowPos rather than pywebview's resize()-then-move() pair, for two
    reasons. First it is atomic: resize() issues its SetWindowPos with the
    window's OLD x, so a dock visibly collapses the window to a strip where it
    stands and only then jumps it to the edge — two paints and two WebView2
    relayouts for one click, which is its own share of "doesn't feel flush".
    Second, resize()/move() multiply their arguments by GetDpiForWindow/96
    while rcWork comes back in physical pixels; those agree only because every
    monitor here is 96 dpi, and pywebview is system-DPI-aware so the factor is
    one number for the whole process. Talking to SetWindowPos directly keeps
    the whole calculation in one coordinate space. The pywebview path stays as
    the fallback for when there is no hwnd to talk to.
    """
    hwnd = _hwnd(win)
    if hwnd:
        try:
            import ctypes
            from ctypes import wintypes
            user32 = _user32()
            if user32 is not None:
                user32.SetWindowPos.restype = wintypes.BOOL
                user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                                ctypes.c_int, ctypes.c_int,
                                                ctypes.c_int, ctypes.c_int,
                                                wintypes.UINT]
                SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
                if user32.SetWindowPos(wintypes.HWND(hwnd), None,
                                       int(x), int(y), int(w), int(h),
                                       SWP_NOZORDER | SWP_NOACTIVATE):
                    return True
        except (ImportError, AttributeError, OSError, ValueError):
            pass
    try:
        win.resize(int(w), int(h))
        win.move(int(x), int(y))
        return True
    except Exception:
        return False


def _unstate(win):
    """Take the window out of minimized/maximized BEFORE any geometry is read.

    Not cosmetic — both states poison the numbers we are about to use. A
    minimized window's GetWindowRect is (-32000, -32000, 160, 28): docking would
    pick the far-left monitor whatever screen you're looking at, and _undock
    would remember an off-screen rect that "Restore window size" then obeys.
    A maximized window reports a different border (8 rather than 7) and keeps
    WS_MAXIMIZE set through a SetWindowPos, so the dock survives only until the
    next restore. Note show() does NOT un-minimize a WinForms form — it is
    Show() + Activate() — so this has to be explicit, and it has to run first.
    """
    try:
        win.restore()               # WindowState = Normal; a no-op if already
    except Exception:
        pass


def _clamp_to_screen(win, x, y, w, h):
    """Rescue a window rect that names nowhere. Otherwise leave it alone.

    Clamps what you can SEE, not the window rect, so the invisible border isn't
    mistaken for the window overhanging an edge. Returns the rect unchanged if
    the Win32 calls are unavailable.

    "Stranded" has to mean *unreachable*, not merely "not fully inside the work
    area" — a 900-tall window on a 1032-high work area has 893 visible pixels,
    so a whole-window clamp permits y no greater than 139 and quietly hauls
    anything parked lower up to it. That fires on a window you deliberately put
    near the bottom of the screen, and "Restore window size" is an
    always-enabled tray item, so it fires with no dock involved at all.
    Measured before this guard: parked at y=400, restore moved it to y=139.
    So: if enough of the window overlaps a real work area to grab and drag it
    back, that is reachable, and we don't touch it.
    """
    bl, bt, br, bb = _frame_insets(win)
    vx, vy = int(x) + bl, int(y) + bt
    vw, vh = int(w) - bl - br, int(h) - bt - bb
    if vw <= 0 or vh <= 0:
        return (int(x), int(y), int(w), int(h))
    area = _rect_work_area(vx, vy, vw, vh)
    if area is None:
        return (int(x), int(y), int(w), int(h))
    al, at, aw, ah = area
    # Enough caption to grab: 120px of width and one title bar of height.
    ix = max(0, min(vx + vw, al + aw) - max(vx, al))
    iy = max(0, min(vy + vh, at + ah) - max(vy, at))
    if ix >= min(vw, 120) and iy >= min(vh, 32):
        return (int(x), int(y), int(w), int(h))
    vw = min(vw, aw)
    vh = min(vh, ah)
    vx = max(al, min(vx, al + aw - vw))
    vy = max(at, min(vy, at + ah - vh))
    return (vx - bl, vy - bt, vw + bl + br, vh + bt + bb)


def _nudge_to_front(win, restore=True):
    """Best-effort bring-to-front. Never raises. Note pywebview's `on_top` is a
    *property* (a bool), not a method.

    `restore=False` right after a dock: the window is already Normal by then
    (dock_left calls _unstate first), and an unconditional restore() would be a
    loaded gun if it ever weren't — on a maximized window it throws the freshly
    docked rect away and puts the old size back, so the dock looks like a
    no-op that flashed.
    """
    if restore:
        try:
            win.restore()           # in case it was minimized
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
        self._undock = None         # geometry from before the last dock_left()

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

    # ---- docking ----
    def dock_left(self):
        """Snap the window to the left edge of its monitor as a full-height strip.

        Whichever monitor it is currently on — docking is meant to put the strip
        where you are looking, not always on the primary.

        Flush the way Aero Snap is flush, which is the whole point: Windows
        aligns a window's VISIBLE (DWM) rect to rcWork and pads the window rect
        outward by the invisible border, so we do the same instead of aligning
        the window rect and landing a border-width inside. Measured before, on
        the right-hand monitor (rcWork 1920, 0, 1920x1032): resize(136, 1032)
        then move(1920, 0) put the visible strip at (1927, 0)-(2049, 1025) — a
        7px seam down the left and 7px of desktop showing above the taskbar.
        After: (1920, 0)-(2056, 1032), flush on all three edges. Not the same
        rect Windows' snapper makes — that one is half the screen — but placed
        by the same rule, and the window rect is padded exactly as Windows pads
        it. Measured on the primary monitor via the real dock_left() path:
        window (-7, 0)-(143, 1039), visible (0, 0)-(136, 1032), against rcWork
        (0, 0, 1920, 1032).
        """
        win = self._window
        if win is None:
            return
        try:
            win.show()
            _unstate(win)                   # before ANY geometry is read
            area = _window_work_area(win)
            if area is None:
                return
            left, top, _w, height = area
            if self._undock is None:        # remember the pre-dock geometry once,
                try:                        # so repeated docks don't overwrite it
                    self._undock = (win.x, win.y, win.width, win.height)
                except Exception:
                    self._undock = (None, None, _WIDTH, _HEIGHT)
            # Window rect = visible rect grown by the invisible border. With
            # zeros (no DWM, odd frame) this collapses to exactly the old
            # placement, which is wrong by 7px but never wrong by a screen.
            bl, bt, br, bb = _frame_insets(win)
            _place(win, left - bl, top - bt, _DOCK_W + bl + br, height + bt + bb)
            _nudge_to_front(win, restore=False)
        except Exception:
            pass

    def restore_size(self):
        """Undo a dock: back to the pre-dock geometry, or the default if unknown.

        Clamped onto a monitor that currently exists on the way out. The
        remembered rect is whatever the window happened to be doing before the
        dock, and this desktop spans x = -3840..3840 — unplug or rearrange the
        screen it was on and those coordinates name nowhere. There is no tray
        item that fetches an off-screen window back, so the failure mode is a
        dashboard you can only recover by docking again.
        """
        win = self._window
        if win is None:
            return
        try:
            win.show()
            _unstate(win)
            x, y, w, h = self._undock or (None, None, _WIDTH, _HEIGHT)
            w, h = max(int(w), _MIN[0]), max(int(h), _MIN[1])
            if x is None:                   # never docked: resize where it stands
                try:
                    x, y = win.x, win.y
                except Exception:
                    x = y = None
            if x is None:
                win.resize(w, h)
            else:
                # The remembered numbers are window-rect numbers (win.x/win.width
                # read back the same space move()/resize() write), so they go
                # straight back out uncompensated — only the clamp needs to know
                # about the border.
                _place(win, *_clamp_to_screen(win, int(x), int(y), w, h))
            self._undock = None
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
