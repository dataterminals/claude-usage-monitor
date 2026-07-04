# Claude Usage Monitor

A lightweight Windows system-tray app for thorough, on-the-fly monitoring of your
Claude Code usage. It reads your local session transcripts — no account, no API
key required — and shows today's spend on the tray icon, a live tooltip, and a
full browser dashboard.

Everything is derived from `~/.claude/projects/**/*.jsonl` (the transcripts Claude
Code already writes). The only network call is the **optional, opt-in** experimental
plan-quota reader.

> **On a Max/Pro subscription, dollar figures are *notional* — equivalent-API cost.**
> They're an intensity gauge, not a bill. Token counts are exact.

## What it shows

- **Tray icon** — today's notional cost (`$12`), colored green → amber → red by spend.
- **Tooltip** (hover) — today / rolling-5h / week at a glance.
- **Menu** — today, current 5-hour block + burn rate, 7-day, all-time; a toggle for
  live quota; open dashboard; refresh; quit.
- **Dashboard** (`Open dashboard`, or `http://127.0.0.1:8787/`):
  - KPI cards: Today · Rolling 5h · Last 7 days · All time (cost, tokens, requests)
  - Rolling 5-hour window: spent, burn rate ($/h), projected full-5h, cache-hit %,
    input/output split, active-session status
  - 48-hour activity sparkline and 30-day daily-cost bars
  - Breakdown tables by model and by project (cost, share, tokens, requests)
  - Recent sessions
  - **Plan quota (experimental)** — real 5-hour / weekly `/usage` bars when enabled

Data updates live: a filesystem watch on the transcripts re-parses within a few
seconds of any Claude Code activity (with a 5-second fallback poll).

## Install & run

Requires Python 3.9+ (built and tested on 3.13).

```powershell
cd "H:\Github Repositories\claude-usage-monitor"
pip install -r requirements.txt
pythonw run.pyw        # tray app, no console window
```

- `python tray.py` runs it with a console (handy while tweaking).
- **Start on login:** put a shortcut to `run.pyw` in
  `shell:startup` (Win+R → `shell:startup`), or point it at `pythonw.exe run.pyw`.

### Dashboard only (no tray, no dependencies)

Only needs the standard library — good for a headless box or if you don't want the
tray icon:

```powershell
python serve.py        # -> http://127.0.0.1:8787/
```

Env: `CLAUDE_USAGE_PORT` (default 8787), `CLAUDE_USAGE_QUOTA=1` to enable live quota.

## The experimental "Live quota" toggle

Off by default. When enabled (tray menu → *Live quota (experimental)*), it calls the
**same endpoint Claude Code's `/usage` command uses** to fetch your real rate-limit
bars — the 5-hour rolling window and weekly limits, with reset times:

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <your ~/.claude/.credentials.json token>
anthropic-beta: oauth-2025-04-20
```

This is **undocumented** and may break on a Claude Code update — hence "experimental".
It degrades gracefully: any failure just shows *unavailable* with a reason, and never
affects the local accounting.

- If the stored access token is near expiry (or gets a `401`), the app attempts **one
  token refresh** using the refresh token in `~/.claude/.credentials.json` — the same
  refresh flow Claude Code uses. On success it writes the refreshed token back
  (atomic write). If refresh also fails, it asks you to run `/login` in Claude Code.
- It never prints or transmits your token anywhere except that one Anthropic endpoint.

> **Known gotcha:** if your on-disk `~/.claude/.credentials.json` holds an old or
> under-scoped subscription token, the usage call returns `401`. Run `/login` in
> Claude Code once to mint a current token, then re-enable the toggle.

## How it works

| File | Role |
|---|---|
| `engine.py` | Incrementally parses transcripts → per-message usage records → aggregated snapshots. Dedupes on `message.id`+`requestId` (like `ccusage`), so re-logged streaming messages aren't double-counted. |
| `pricing.py` / `pricing.json` | Per-model rates and cost math. Edit `pricing.json` to adjust; the app reloads it on restart. |
| `server.py` | Tiny stdlib HTTP server: `/` (dashboard), `/api/usage`, `/api/quota`, `/health`. Bound to `127.0.0.1` only. |
| `dashboard.html` | The browser UI. Served from disk each request — edit and refresh, no restart. |
| `quota.py` | The opt-in `/usage` reader + token refresh. |
| `tray.py` | The tray icon, menu, tooltip, and background refresh loop. |
| `serve.py` | Dashboard/API without the tray (stdlib only). |

## Hacking

- **Pricing / new models:** edit `pricing.json` (`_default` covers anything unlisted).
- **Dashboard:** it's one self-contained `dashboard.html` consuming `/api/usage`. Change
  it and hit refresh — the server re-reads it every request.
- **What the tray number shows:** `icon_text()` / `accent_for()` in `tray.py`.
- **Windows / thresholds:** the 5-hour rolling window and colors are simple constants
  in `engine.py` and `tray.py`.

## Privacy

100% local except the opt-in quota call. No telemetry, no external services. The
dashboard binds to loopback only.
