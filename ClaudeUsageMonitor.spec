# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build for the tray app -> dist\ClaudeUsageMonitor.exe

One file, no console. The entry point is launcher.pyw rather than run.pyw so the
frozen build keeps the single-instance guard and the message-box crash reporting
(a windowed exe has no console to print to either).

Data files land at the root of sys._MEIPASS, which is where pricing.py,
server.py and tray.py already look when frozen -- keep those two in step.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas = [
    ("dashboard.html", "."),      # server.py reads this per request
    ("pricing.json", "."),        # pricing.py loads at import
    ("icons/app.ico", "icons"),   # tray.py: ICON_ICO, and the window icon
    ("icons/icon-256.png", "icons"),
]
binaries = []
hiddenimports = []

# pywebview's Edge WebView2 backend goes through pythonnet/WinForms, which pulls
# its CLR runtime in as data + binaries -- collect_all, not just hiddenimports.
for _pkg in ("webview", "clr_loader", "pythonnet"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hiddenimports += _h

# Both are resolved dynamically at runtime, so nothing statically references them.
hiddenimports += collect_submodules("pystray")
hiddenimports += [
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    "pystray._win32",
    "clr",
]

# Backends for other platforms/toolkits. pywebview probes for these in a
# try/except, so removing them just makes the probe fall through to WebView2.
excludes = [
    "webview.platforms.cocoa",
    "webview.platforms.gtk",
    "webview.platforms.qt",
    "webview.platforms.cef",
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "matplotlib",
    "numpy",
    "pytest",
]

a = Analysis(
    ["launcher.pyw"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="ClaudeUsageMonitor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX is left off deliberately: it has a habit of corrupting the bundled
    # .NET/CLR DLLs that the WebView2 backend needs.
    upx=False,
    runtime_tmpdir=None,
    console=False,            # tray app -- no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="icons/app.ico",
)
