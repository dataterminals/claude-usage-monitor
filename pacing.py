"""Pacing model — "am I ahead of budget, and how long should I wait?"

Both plan windows are FIXED blocks, not rolling. The API hands back a
`resets_at` anchor that holds steady while you work (sampled 25s apart, it
doesn't move) and never a window start, so the start is always
`resets_at - length`. The part that shapes this whole module follows from
"fixed": **waiting does not drain the bar.** At 60% with two hours left you are
still at 60% two hours later.

What waiting *does* do is let the budget line catch up to you. Spending a
window's allowance evenly means sitting at `ceiling * elapsed/length` at any
moment; if you're above that line, the moment it reaches where you already are
is:

    resume_at = window_start + length * (utilization / ceiling)

One formula, both windows (length 5h or 168h), and no burn-rate estimate — which
is the point. A rate derived from `utilization / elapsed` is wild in the first
minutes of a window and lags badly after a burst. `resume_at` is a fixed instant,
so a countdown to it also ticks smoothly between quota fetches instead of
jittering as the estimate is revised.

Two consequences worth knowing:
  * With ceiling=100 the timer can never point past the reset — it asymptotes to
    exactly the reset at 100%. Only a ceiling below 100 can say "this window is
    already spent".
  * You have to satisfy BOTH windows, so the wait that matters is the LONGER of
    the two. Otherwise the 5-hour view would happily green-light spending to 100%
    every five hours, which torches the week.

The weekly window also gets a day-level view. Its reset anchor supplies the phase
for free — a Tuesday 08:00 reset puts every day boundary at 08:00 — so the
`100/7 = 14.29%` daily allowance needs no hardcoded weekday or hour. Day steps
are a flat 24h, matching how the window itself is anchored; across a DST change
the boundary's local clock time shifts by an hour, same as the reset's does.

"Used today" needs utilization *at* the day boundary, which the API doesn't
report, so `SampleStore` keeps a small on-disk history of readings.
"""
import json
import os
import time

WINDOW_HOURS = {
    "five_hour": 5.0,
    "seven_day": 168.0,
    "seven_day_opus": 168.0,
    "seven_day_sonnet": 168.0,
}

LABELS = {
    "five_hour": "5-hour session",
    "seven_day": "This week · all models",
    "seven_day_opus": "This week · Opus",
    "seven_day_sonnet": "This week · Sonnet",
}

DEFAULT_CEILING = 100.0
_KEEP_SECONDS = 8 * 86400      # a little more than the weekly window
_MAX_SAMPLES = 4000
_MIN_SAMPLE_GAP = 300.0        # re-record an unchanged reading at most this often

# A weekly bar cannot fall inside its own window — it only drops at the reset,
# which starts a new generation. So a drop with the generation unchanged means
# the *denominator* moved, not the numerator: a plan change (Max 5x -> 20x)
# rebases every bar downward mid-window. See SampleStore._rebased.
_REBASE_DROP = 0.5


def window_hours(key):
    """Length of the window a limits key names, or None if it isn't one.

    Resolved by prefix rather than from an exhaustive table: the endpoint now
    names model-scoped weekly caps at runtime (`seven_day_scoped_fable`), and a
    fixed table silently dropped every key it had not been taught — which is
    how the only per-model cap still reported went missing from both the gauges
    and the wait driver.
    """
    hours = WINDOW_HOURS.get(key)
    if hours is not None:
        return hours
    if not isinstance(key, str):
        return None
    if key == "five_hour" or key.startswith("five_hour_"):
        return 5.0
    if key == "seven_day" or key.startswith("seven_day_"):
        return 168.0
    return None


def _anchor(iso):
    """A window's `resets_at` as a stable epoch, rounded to the nearest minute.

    The endpoint re-serializes the same reset with a different sub-second part
    on every call (…T17:10:00.889179Z, then …T17:10:00.062571Z), so the raw
    value is unusable as an identity: it would make each day boundary land a
    fraction before the hour and print as 7:59, and — worse — it's the
    generation marker `SampleStore` uses to tell one week's readings from the
    next, which would then never match itself.

    Rounding, not flooring: the jitter is centred ON the whole minute, not
    parked above it, so it lands on both sides — observed live as
    …T05:59:59.620062Z one poll and …T06:00:00.060670Z the next. Flooring turns
    that half-second of noise into a whole minute of disagreement, which prints
    as a rollover time flickering 1:59 AM / 2:00 AM and, worse, fractures the
    very generation marker this function exists to keep stable. Real boundaries
    sit on whole minutes, so the nearest minute is also the truer answer.
    """
    if not iso:
        return None
    try:
        from datetime import datetime
        e = datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError, TypeError):
        return None
    return float(int((e + 30) // 60) * 60)


def five_hour_start(limits):
    """Epoch the current 5-hour block opened, or None when none is open.

    The endpoint reports only `resets_at`; the block is fixed, so its start is
    the reset less the window length. The engine uses this to anchor its own
    5-hour view to the same instant the gauge is measuring.
    """
    lim = (limits or {}).get("five_hour")
    if not isinstance(lim, dict):
        return None
    reset_e = _anchor(lim.get("resets_at"))
    return None if reset_e is None else reset_e - WINDOW_HOURS["five_hour"] * 3600.0


def _app_dir():
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_DATA_HOME")
            or os.path.join(os.path.expanduser("~"), ".local", "share"))
    return os.path.join(base, "ClaudeUsageMonitor")


class SampleStore:
    """Tiny append-mostly history of quota readings, persisted as JSON.

    Only used to answer "what was utilization at the start of today?", which the
    usage endpoint doesn't report. Every sample carries the weekly window's reset
    epoch as a generation marker so readings from a *previous* week are never
    subtracted from this week's (that would read as negative usage).

    Single-writer by design: the tray's updater records, everything else reads.
    All failures degrade to an empty history — pacing then just omits the
    day-level numbers rather than breaking.
    """

    def __init__(self, path=None):
        self.path = path or os.path.join(_app_dir(), "quota-history.json")
        self._samples = None

    # ---- persistence ----
    def load(self):
        if self._samples is not None:
            return self._samples
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            raw = doc.get("samples") if isinstance(doc, dict) else doc
            self._samples = [s for s in raw if isinstance(s, dict) and "t" in s]
        except (OSError, ValueError, TypeError):
            self._samples = []
        return self._samples

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"samples": self._samples}, f)
            os.replace(tmp, self.path)
        except OSError:
            pass  # history is a nice-to-have; never break the caller

    # ---- write ----
    @staticmethod
    def _rebased(samples, gen, utils):
        """True when this weekly reading sits below one already in `gen`.

        Utilization is monotone inside a fixed window, so the only way it falls
        without the generation changing is that the *plan* changed underneath
        it — a Max 5x -> 20x upgrade rebases every bar downward mid-window while
        `resets_at`, and therefore the generation marker, stays put.

        Left alone those pre-upgrade samples keep serving as today's baseline,
        and since `used_today` clamps at zero it then reports a confident 0.0
        used with the full allowance still free — flagged *exact*, because a
        sample really does exist at the day boundary. Dropping the generation's
        history instead costs one day of "since HH:MM" partial reporting, which
        is the honest answer while a fresh baseline builds.

        Compared against the generation's maximum, not its last sample, so a
        reading that already landed post-rebase can't mask the discontinuity.
        """
        u = utils.get("seven_day")
        if u is None:
            return False
        prior = [s["u"]["seven_day"] for s in samples
                 if s.get("w") == gen and "seven_day" in (s.get("u") or {})]
        return bool(prior) and (max(prior) - u) > _REBASE_DROP

    def record(self, limits, now=None):
        """Record one reading. Cheap no-op when nothing has changed."""
        now = now or time.time()
        utils = {}
        for key, v in (limits or {}).items():
            if window_hours(key) is None:
                continue
            if isinstance(v, dict) and v.get("utilization") is not None:
                utils[key] = float(v["utilization"])
        if not utils:
            return
        week = limits.get("seven_day") or {}
        gen = _anchor(week.get("resets_at"))

        samples = self.load()
        if self._rebased(samples, gen, utils):
            samples = [s for s in samples if s.get("w") != gen]
            last = None
        else:
            last = samples[-1] if samples else None
        if (last is not None
                and last.get("w") == gen
                and last.get("u") == utils
                and (now - last.get("t", 0)) < _MIN_SAMPLE_GAP):
            return

        samples.append({"t": now, "u": utils, "w": gen})
        cutoff = now - _KEEP_SECONDS
        if samples[0].get("t", now) < cutoff:
            samples = [s for s in samples if s.get("t", 0) >= cutoff]
        if len(samples) > _MAX_SAMPLES:
            samples = samples[-_MAX_SAMPLES:]
        self._samples = samples
        self._save()

    # ---- read ----
    def baseline(self, key, at_epoch, generation):
        """Utilization for `key` at or before `at_epoch`, within one window
        generation. Returns (utilization, sample_epoch, exact) — `exact` is False
        when the history doesn't reach back to `at_epoch` and the earliest
        in-generation sample was used instead, so callers can say "since 10:24"
        rather than overstating it as a full day.
        """
        rows = [s for s in self.load()
                if s.get("w") == generation and key in (s.get("u") or {})]
        if not rows:
            return None, None, False
        before = [s for s in rows if s["t"] <= at_epoch]
        if before:
            s = before[-1]
            return s["u"][key], s["t"], True
        s = rows[0]
        return s["u"][key], s["t"], False


def _window(key, lim, now, ceiling):
    util = lim.get("utilization")
    reset_e = _anchor(lim.get("resets_at"))
    hours = window_hours(key)
    if util is None or reset_e is None or hours is None:
        return None
    span = hours * 3600.0
    start_e = reset_e - span

    elapsed = now - start_e
    elapsed_frac = min(max(elapsed / span, 0.0), 1.0)
    ideal = ceiling * elapsed_frac

    # The moment the budget line reaches where you already are.
    resume_e = start_e + span * (util / ceiling)
    slack = now - resume_e          # >0 banked, <0 you're ahead of budget
    wait = max(0.0, -slack)

    if util >= 100:
        state = "capped"
    elif resume_e > reset_e:        # only reachable with a sub-100 ceiling
        state = "spent"
    elif wait > 0:
        state = "over"
    else:
        state = "under"

    return {
        "key": key,
        # A scoped cap carries its own label from quota._scoped — the endpoint
        # names the model at runtime, so there is no static entry to look up.
        "label": lim.get("label") or LABELS.get(key, key),
        "window_hours": hours,
        "utilization": util,
        "start_epoch": start_e,
        "reset_epoch": reset_e,
        "elapsed_hours": elapsed / 3600.0,
        "remaining_hours": max(0.0, (reset_e - now) / 3600.0),
        "ideal_utilization": ideal,
        "delta": util - ideal,      # >0 = spending faster than the budget line
        "resume_at_epoch": resume_e,
        "slack_seconds": slack,
        "wait_seconds": wait,
        "state": state,
    }


def _weekly_day(w, store, now, ceiling):
    """Day-level view of the weekly window, phased off its own reset anchor."""
    span = w["window_hours"] * 3600.0
    # Clamped at both ends. The 6 stops a leap-second-ish overshoot inventing an
    # eighth day; the 0 covers `now` landing before the window start, which a
    # bare floor-divide turns into index -1 and a negative day budget.
    idx = min(max(int((now - w["start_epoch"]) // 86400), 0), 6)
    day_start = w["start_epoch"] + idx * 86400.0
    allowance = ceiling / 7.0

    used = baseline_e = None
    exact = False
    if idx == 0:
        # Day 0's boundary *is* the window start, where utilization is zero by
        # definition — so everything spent this week was spent today and no
        # history is needed. Asking the store instead understated day one
        # whenever the history didn't reach back that far: it would answer with
        # the earliest sample it had and report the remainder "since 23:41".
        used, baseline_e, exact = w["utilization"], day_start, True
    elif store is not None:
        base_util, baseline_e, exact = store.baseline(
            "seven_day", day_start, w["reset_epoch"])
        if base_util is not None:
            if base_util > w["utilization"]:
                # Baseline above today's reading: history written before a plan
                # change that SampleStore._rebased hadn't yet been taught to
                # prune. Clamping would print a confident 0.0 used; unknown is
                # the truthful answer until a post-change baseline exists.
                used = baseline_e = None
                exact = False
            else:
                used = w["utilization"] - base_util

    return {
        "day_index": idx,                       # 0..6 within the week
        "day_number": idx + 1,
        "day_start_epoch": day_start,
        "day_end_epoch": day_start + 86400.0,
        "allowance": allowance,                 # 100/7 = 14.29 points per day
        "used_today": used,
        "used_since_epoch": baseline_e,
        "used_today_exact": exact,              # False -> history started mid-day
        "remaining_today": None if used is None else allowance - used,
        # Budget line expressed in days: >0 means you're that far ahead of it.
        "banked_days": w["slack_seconds"] / 86400.0,
        "budget_at_day_start": ceiling * idx / 7.0,
        "budget_at_day_end": ceiling * (idx + 1) / 7.0,
        "days_left": (w["reset_epoch"] - now) / 86400.0,
    }


def compute(limits, now=None, store=None, ceiling=DEFAULT_CEILING):
    """Pacing for every window present in `limits` (quota.fetch()'s shape).

    Never raises on missing/None entries — a window without a `resets_at` simply
    doesn't appear.
    """
    now = now or time.time()
    limits = limits or {}
    ceiling = float(ceiling) or DEFAULT_CEILING

    windows = {}
    # Driven by what the endpoint actually returned, not by a fixed key list —
    # scoped weekly caps are named at runtime and would otherwise never appear.
    for key in sorted(limits):
        lim = limits.get(key)
        if isinstance(lim, dict):
            w = _window(key, lim, now, ceiling)
            if w is not None:
                windows[key] = w

    # You must satisfy every window, so the binding one is the longest wait.
    driver = None
    for w in windows.values():
        if driver is None or w["wait_seconds"] > driver["wait_seconds"]:
            driver = w

    week = windows.get("seven_day")
    out = {
        "now_epoch": now,
        "ceiling": ceiling,
        "windows": windows,
        "wait_seconds": driver["wait_seconds"] if driver else 0.0,
        "wait_driver": driver["key"] if (driver and driver["wait_seconds"] > 0) else None,
        "resume_at_epoch": (driver["resume_at_epoch"]
                            if (driver and driver["wait_seconds"] > 0) else None),
        "weekly_day": _weekly_day(week, store, now, ceiling) if week else None,
    }
    out["headline"] = headline(out)
    return out


# ---- presentation helpers (shared by the tray; the dashboard has its own) ----

def format_duration(seconds):
    if seconds is None:
        return "—"
    seconds = max(0, int(round(seconds)))
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "{}d {}h".format(d, h) if h else "{}d".format(d)
    if h:
        return "{}h {}m".format(h, m) if m else "{}h".format(h)
    if m:
        return "{}m".format(m)
    return "{}s".format(seconds)


def headline(pace):
    """One short line for the tray tooltip / menu.

    Deliberately *not* driven by `wait_driver`. That's the true binding
    constraint, but when it's the weekly window the wait runs to a day or more,
    and "don't use Claude for 28 hours" isn't advice anyone acts on between
    prompts. The 5-hour window owns the actionable countdown; the weekly one
    speaks in days and allowances.

    A full all-models cap comes before either: nothing frees up until it resets,
    so the headline names that reset, and when both caps are full, the later one.

    A per-model weekly cap (seven_day_opus, seven_day_scoped_fable, ...) has no
    day view of its own, so it speaks only once the all-models lines are fine:
    how far over its line it is, or that it's full. It never reads as on pace.
    """
    windows = pace.get("windows") or {}
    if not windows:
        return ""
    five = windows.get("five_hour")
    week = windows.get("seven_day")
    day = pace.get("weekly_day") or {}
    now = pace.get("now_epoch") or time.time()

    # A full week blocks every model until it resets, so it can't wait behind the
    # 5-hour window: "resume in 50m", "resets in 3h" and the next day's allowance
    # would each point at a moment that frees nothing up. With both caps full,
    # whichever resets later is the real wait.
    full = [w for w in (week, five) if w is not None and w["state"] == "capped"]
    if full:
        block = max(full, key=lambda w: w["reset_epoch"])
        return "{} — resets in {}".format(
            "Weekly cap reached" if block is week else "5-hour window spent",
            format_duration(block["reset_epoch"] - now))
    if five is not None and five["wait_seconds"] > 0:
        return "Ahead of pace — resume in {}".format(
            format_duration(five["wait_seconds"]))

    next_day = format_duration(day["day_end_epoch"] - now) if day else "—"
    if week is not None and week["wait_seconds"] > 0:
        return "Over the weekly line by {} — next allowance in {}".format(
            format_duration(-week["slack_seconds"]), next_day)
    left = day.get("remaining_today")
    if left is not None and left < 0:
        return "Over today's allowance by {:.1f}pt — next in {}".format(-left, next_day)

    # Nothing above looks past five_hour and seven_day, so a per-model cap over
    # its line used to reach "On pace" below: min() picked its negative slack and
    # format_duration clamped that to "0s of budget banked", even at 100%. The
    # longest wait is the binding one, as it is for the wait driver.
    over = [w for w in windows.values() if w["wait_seconds"] > 0]
    if over:
        cap = max(over, key=lambda w: w["wait_seconds"])
        name = "{} {}".format(cap["label"].split("·")[-1].strip(),
                              "weekly" if cap["window_hours"] >= 168 else "5-hour")
        if cap["state"] == "capped":
            return "{} cap reached — resets in {}".format(
                name, format_duration(cap["reset_epoch"] - now))
        return "Over the {} line by {}".format(name, format_duration(-cap["slack_seconds"]))

    slack = min((w["slack_seconds"] for w in windows.values()), default=0.0)
    return "On pace — {} of budget banked".format(format_duration(slack))
