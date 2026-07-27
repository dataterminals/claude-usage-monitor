# Claude Usage Monitor

A lightweight Windows system-tray app that tells you, at a glance, **how close you
are to your Claude plan limits** — so you can gauge how hard you can keep going.

The tray icon shows your highest current utilization %. **Open dashboard** pops a
small **native window** (its own chromeless app window you can park in a corner of
a monitor — no browser tab) that leads with your limit gauges (the live `/usage`
bars) plus a burn-rate and a **time-to-cap estimate** you don't get from `/usage`
itself. A second tab has the full local cost/token accounting if you ever want the
detail.

Everything is derived from your local Claude Code transcripts
(`~/.claude/projects/**/*.jsonl`). The only network call is the plan-quota reader,
which is **read-only** unless you explicitly ask it to refresh your token.

## What it shows

**Limits tab (the point):**
- A one-line verdict: your tightest cap and whether to ease up.
- Gauges for each live limit — 5-hour session, weekly (all models), weekly Opus,
  weekly Sonnet — each with % used, a bar, and reset countdown.
- **Time-to-cap:** using the live % plus your local burn rate, e.g. *"On pace to
  hit the cap in ~40m · resets in 1h 30m"* vs *"on pace to stay under."*
- A self-tracked burn panel (rolling-5h tokens/cost, burn rate, projection,
  cache-hit %, 7-day total) that works even when the live bars aren't connected.

**Details tab:** notional cost KPIs (today / 5h / week / all-time), a 48-hour
activity sparkline, 30-day cost bars, by-model and by-project tables, and recent
sessions.

**Tray icon:** your highest utilization % (green → amber → red). Tooltip shows the
5h and weekly %, and reset time. Menu: **Open dashboard** (the native window), the
same limits, cost totals, a Live-quota toggle, an *Attempt token refresh* action,
refresh, and quit.

**The window:** a real desktop window (Edge WebView2, already on Windows) — tall
and narrow by default, dark-themed, resizable so you can size it into a corner. It
opens only when you ask (nothing pops up at login), and **closing it just hides
it** — reopening from the tray is instant. Quit from the tray closes it for good.
If WebView2/pywebview isn't available, *Open dashboard* falls back to your browser.

> On a Max/Pro subscription the dollar figures are **notional** (equivalent-API
> cost) — an intensity gauge, not a bill. Token counts and the % gauges are exact.

## Run it

You have three options.

**A) The standalone `.exe` (recommended):**
```
dist\ClaudeUsageMonitor.exe
```
Self-contained — no Python needed to run it. Build it yourself (below) or use the
one already in `dist\`.

**B) Auto-launch on login:** a shortcut to the exe in your Startup folder
(`shell:startup`). One is already installed as *Claude Usage Monitor*; delete it
from that folder to disable.

**C) From source:**
```powershell
cd "H:\Github Repositories\claude-usage-monitor"
pip install -r requirements.txt
pythonw run.pyw          # tray + native window, no console
# or:  python serve.py   # dashboard API only, no tray/window, stdlib only -> http://127.0.0.1:8787/
```

The native window needs `pywebview` (in `requirements.txt`) and the Edge WebView2
runtime, which ships with Windows 11 / current Edge. Without it the app still runs
and *Open dashboard* just uses your default browser.

## Build the exe

The `.spec` file bundles the dashboard, icons, and the pywebview backends. Use it:

```powershell
cd "H:\Github Repositories\claude-usage-monitor"
pip install pyinstaller
python -m PyInstaller ClaudeUsageMonitor.spec --noconfirm
# -> dist\ClaudeUsageMonitor.exe
```

If you regenerate the app icon, re-run `python make_icons.py` first (writes
`icons\app.ico` + `icons\icon-256.png`).

## Live limit gauges (the token bit)

The gauges come from the same endpoint Claude Code's `/usage` uses:
`GET https://api.anthropic.com/api/oauth/usage` with your OAuth token from
`~/.claude/.credentials.json`. Two things to know:

- **It's read-only by default.** The app never writes your credentials on its own.
- If your stored token is stale/rejected (a `401`), the gauges show *not
  connected*. Fix it either way:
  1. **Run `/login` in a Claude Code terminal** (cleanest, zero risk) — mints a
     fresh, correctly-scoped token that the app then reads.
  2. **Tray → *Attempt token refresh*** — the app uses the stored refresh token to
     mint a new one. This *rotates* the refresh token, which can force a re-login
     of whatever Claude Code login owns it, so it's opt-in, never automatic.

Until connected, the self-tracked burn panel still gives you a local read on usage.

## How it works

| File | Role |
|---|---|
| `engine.py` | Incremental transcript parser + aggregation. Dedupes on `message.id`+`requestId` (like `ccusage`). |
| `quota.py` | The `/usage` reader + opt-in token refresh. Read-only unless `allow_refresh=True`. |
| `pricing.py` / `pricing.json` | Per-model notional cost rates. Edit the JSON; reloaded on restart. |
| `server.py` + `dashboard.html` | Local dashboard on `127.0.0.1` (`/`, `/api/usage`, `/api/quota`, `/health`). Binds a fixed port ladder (8787–8790) so the URL is stable. |
| `window.py` | Native desktop window (pywebview / Edge WebView2) hosting the dashboard. Owns the GUI loop; hides-on-close so reopen is instant. |
| `tray.py` | Tray icon, tooltip, menu, live file-watch refresh; opens/owns the window. |
| `serve.py` | Dashboard/API without the tray or window (stdlib only). `CLAUDE_USAGE_MOCK_QUOTA=1` serves canned gauge data for UI work. |
| `make_icons.py` | Regenerates the app/window icon (`icons\app.ico`) from the tray mark. |

## Privacy

100% local except the opt-in quota call to Anthropic's own API with your own
token. No telemetry. The dashboard binds to loopback only.
