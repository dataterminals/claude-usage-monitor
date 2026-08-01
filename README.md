# Claude Usage Monitor

A lightweight Windows system-tray app that tells you, at a glance, **how close you
are to your Claude plan limits** — so you can gauge how hard you can keep going.

The tray icon shows your highest current utilization %. **Open dashboard** pops a
small **native window** (its own chromeless app window you can park in a corner of
a monitor — no browser tab) that leads with your limit gauges (the live `/usage`
bars) plus **pacing** — whether you're ahead of an even spend, and how long to
wait to get back on it — which `/usage` doesn't give you. A second tab has the
full local cost/token accounting if you ever want the detail.

Everything is derived from your local Claude Code transcripts
(`~/.claude/projects/**/*.jsonl`). The only network call is the plan-quota reader,
which is **read-only** unless you explicitly ask it to refresh your token.

## What it shows

**Limits tab (the point):**
- A one-line verdict: your tightest cap and whether to ease up.
- **A pacing card** — the wait that puts you back on an even spend, e.g. *"1h 23m
  — wait until 12:11 PM and the 5-hour budget line catches up to what you've
  already spent."* See [Pacing](#pacing) for what that means.
- Gauges for each live limit — 5-hour session, weekly (all models), weekly Opus,
  weekly Sonnet — each with % used, a bar, and reset countdown. The tick on each
  bar marks where an even spend would sit right now, so overshoot is visible.
- **Today's allowance** under the weekly gauge: `100/7 = 14.3%` per day, how much
  of it you've used, and how far ahead of (or behind) the weekly line you are.
- A self-tracked burn panel (rolling-5h tokens/cost, burn rate, projection,
  cache-hit %, 7-day total) that works even when the live bars aren't connected.

**Details tab:** notional cost KPIs (today / 5h / week / all-time), a 48-hour
activity sparkline, 30-day cost bars, by-model and by-project tables, and recent
sessions.

**Tray icon:** your highest utilization % (green → amber → red). Tooltip shows the
5h and weekly %, reset time, and the pacing line (*"Ahead of pace — resume in
1h 23m"*). Menu: **Open dashboard** (the native window), the same limits, today's
allowance, pacing, cost totals, a Live-quota toggle, an *Attempt token refresh*
action, refresh, and quit.

**The window:** a real desktop window (Edge WebView2, already on Windows) — tall
and narrow by default, dark-themed, resizable so you can size it into a corner. It
opens only when you ask (nothing pops up at login), and **closing it just hides
it** — reopening from the tray is instant. Quit from the tray closes it for good.
If WebView2/pywebview isn't available, *Open dashboard* falls back to your browser.

> On a Max/Pro subscription the dollar figures are **notional** (equivalent-API
> cost) — an intensity gauge, not a bill. Token counts and the % gauges are exact.

## Run it

**A) The launcher (recommended):** double-click **Claude Usage Monitor** on the
Desktop or in the Start menu. Both shortcuts run `launcher.pyw`, which wraps
`tray.main()` with the bits you want when starting from a shortcut: it refuses to
start a *second* copy (two identical tray icons fighting over the same WebView2
profile is a bad time), and — since `pythonw` has no console — it surfaces a
missing dependency or a crash in a message box plus a log under
`%TEMP%\ClaudeUsageMonitor\launcher-error.log` instead of dying silently.

Recreate the shortcuts on another machine with:
```powershell
$repo = "D:\Github Repositories\claude-usage-monitor"
$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) 'Claude Usage Monitor.lnk'))
$sc.TargetPath = (Get-Command pythonw).Source
$sc.Arguments = '"' + (Join-Path $repo 'launcher.pyw') + '"'
$sc.WorkingDirectory = $repo
$sc.IconLocation = (Join-Path $repo 'icons\app.ico') + ',0'
$sc.Save()
```

**B) Auto-launch on login:** drop a copy of that shortcut into your Startup
folder (`shell:startup`). Delete it from that folder to disable.

**C) The standalone `.exe`:**
```
dist\ClaudeUsageMonitor.exe
```
Self-contained — no Python needed to run it. Build artifacts are gitignored, so a
fresh checkout has no `dist\`; build it yourself (below).

**D) From source:**
```powershell
cd "D:\Github Repositories\claude-usage-monitor"
pip install -r requirements.txt
pythonw run.pyw          # tray + native window, no console, no launcher guards
# or:  python serve.py   # dashboard API only, no tray/window, stdlib only -> http://127.0.0.1:8787/
```

The native window needs `pywebview` (in `requirements.txt`) and the Edge WebView2
runtime, which ships with Windows 11 / current Edge. Without it the app still runs
and *Open dashboard* just uses your default browser.

## Build the exe

The `.spec` file bundles the dashboard, icons, and the pywebview backends. Use it:

```powershell
cd "D:\Github Repositories\claude-usage-monitor"
pip install pyinstaller
python -m PyInstaller ClaudeUsageMonitor.spec --noconfirm
# -> dist\ClaudeUsageMonitor.exe
```

If you regenerate the app icon, re-run `python make_icons.py` first (writes
`icons\app.ico` + `icons\icon-256.png`).

## Pacing

Both plan windows are **fixed blocks, not rolling** — the API gives a `resets_at`
anchor that holds steady while you work, so a window's start is
`resets_at − length`. The consequence that shapes everything: **waiting doesn't
drain the bar.** At 60% with two hours left you're still at 60% two hours later.

What waiting *does* do is let the budget line catch up. Spending a window evenly
means sitting at `elapsed/length` of the cap at any moment, so if you're above
that line, the moment it reaches where you already are is:

```
resume_at = window_start + length × (utilization / 100)
```

One formula, both windows, and **no burn-rate estimate** — which is the point. A
rate derived from `utilization / elapsed` is wild in the first minutes of a window
and lags badly after a burst. `resume_at` is a fixed instant, so the countdown
also ticks smoothly instead of jittering as an estimate gets revised.

Worked example — a 5-hour window that started at 12:00, and you're at 50% by 13:00:

```
resume_at = 12:00 + 5h × 0.50 = 14:30   ->  wait 1h 30m
```

At 14:30 you're 50% elapsed and 50% used. Resume then and spend at the budget
rate, and you arrive at exactly 100% when it resets at 17:00.

A few things fall out of the model:

- **The two windows are combined, not shown separately** — you have to satisfy
  both, so the binding constraint is the *longer* wait. Otherwise the 5-hour view
  would happily green-light spending to 100% every five hours, which torches your
  week.
- **They answer at different time scales, so they're phrased differently.** The
  5-hour window drives the actionable countdown. An overspent *week* would produce
  a "wait" of a day or more, which isn't something anyone does between prompts —
  so that's expressed as today's allowance and how far ahead of the line you are.
- **The weekly day boundary is derived, not hardcoded.** Its reset anchor supplies
  the phase: a Tuesday 08:00 reset puts every day boundary at 08:00.
- **Near a reset, the advice inverts.** Past the reset you get a *fresh* window
  anchored to whenever you resume, which usually beats resuming at the catch-up
  time — so the card offers that instead.

"Used today" needs utilization *at* the day boundary, which the endpoint doesn't
report, so the app keeps a small rolling history of readings in
`%LOCALAPPDATA%\ClaudeUsageMonitor\quota-history.json` (8 days, pruned; nothing
but timestamps and percentages). Until it spans a full day the figure is labelled
*partial* rather than passed off as a day's total.

Check the math with `python test_pacing.py`.

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
| `pacing.py` | The catch-up model above: window phase, `resume_at`, day allowances, and the on-disk reading history. |
| `test_pacing.py` | Self-check for that math (`python test_pacing.py`). No framework, no dependencies. |
| `pricing.py` / `pricing.json` | Per-model notional cost rates. Edit the JSON; reloaded on restart. |
| `server.py` + `dashboard.html` | Local dashboard on `127.0.0.1` (`/`, `/api/usage`, `/api/quota`, `/health`). Binds a fixed port ladder (8787–8790) so the URL is stable. |
| `window.py` | Native desktop window (pywebview / Edge WebView2) hosting the dashboard. Owns the GUI loop; hides-on-close so reopen is instant. |
| `tray.py` | Tray icon, tooltip, menu, live file-watch refresh; opens/owns the window. |
| `serve.py` | Dashboard/API without the tray or window (stdlib only). `CLAUDE_USAGE_MOCK_QUOTA=1` serves canned gauge data — deliberately over-pace — for UI work. |
| `launcher.pyw` | Double-click entry point: single-instance guard + message-box reporting for missing deps/crashes, then `tray.main()`. |
| `make_icons.py` | Regenerates the app/window icon (`icons\app.ico`) from the tray mark. |

## Privacy

100% local except the opt-in quota call to Anthropic's own API with your own
token. No telemetry. The dashboard binds to loopback only.
