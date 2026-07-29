"""Double-click launcher for Claude Usage Monitor.

This is the friendly front door to `tray.main()`. `run.pyw` starts the app and
nothing else; this adds the three things you want when starting it by
double-clicking a shortcut instead of from a terminal:

  1. Single instance. Starting a second copy is actively bad — the two tray
     icons are indistinguishable, the second HTTP server lands on a different
     port in the ladder, and both windows fight over the shared WebView2
     user-data dir (that collision shows up as "resource is in use",
     0x800700AA — see window.py). So probe the local server first and bow out
     if the app is already up.
  2. Visible failures. Under `pythonw` there is no console, so a missing
     dependency or an unhandled crash otherwise vanishes without a trace. Both
     get a message box and a log file instead of silence.
  3. Runs from anywhere. Python puts this file's directory on sys.path, so the
     shortcut's working directory doesn't matter.

Run:  pythonw launcher.pyw
"""
import ctypes
import os
import sys
import tempfile
import traceback
import urllib.error
import urllib.request

TITLE = "Claude Usage Monitor"

# Must match the ladder server.py binds (PORT..PORT+3), so we detect an
# instance that had to fall past 8787.
PORTS = (8787, 8788, 8789, 8790)

LOG_DIR = os.path.join(tempfile.gettempdir(), "ClaudeUsageMonitor")
LOG_FILE = os.path.join(LOG_DIR, "launcher-error.log")

MB_ICONERROR = 0x10
MB_ICONINFORMATION = 0x40


def alert(text, flags=MB_ICONINFORMATION):
    """Message box. This is the only way to say anything under pythonw."""
    try:
        ctypes.windll.user32.MessageBoxW(0, text, TITLE, flags)
    except Exception:
        pass


def already_running():
    """True if an instance is answering on the port ladder."""
    for port in PORTS:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:{}/health".format(port), timeout=1
            ) as resp:
                if b'"ok"' in resp.read(64):
                    return True
        except (urllib.error.URLError, OSError, ValueError):
            continue
    return False


def log_crash(exc_text):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(exc_text)
        return LOG_FILE
    except OSError:
        return None


def main():
    if already_running():
        alert("Claude Usage Monitor is already running.\n\n"
              "Look for the % icon in your system tray (click the ^ arrow if "
              "it's hidden) and choose “Open dashboard”.")
        return 0

    try:
        import tray
    except ImportError as exc:
        alert("Missing a dependency: {}\n\n"
              "Install the requirements, then try again:\n\n"
              "    pip install -r \"{}\"".format(
                  exc, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "requirements.txt")),
              MB_ICONERROR)
        return 1

    try:
        tray.main()
    except Exception:
        path = log_crash(traceback.format_exc())
        alert("Claude Usage Monitor stopped unexpectedly.\n\n{}{}".format(
            traceback.format_exc(limit=3),
            "\nFull details: " + path if path else ""), MB_ICONERROR)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
