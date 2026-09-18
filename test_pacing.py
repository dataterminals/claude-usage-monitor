"""Self-check for the pacing math.  Run:  python test_pacing.py

No test framework and no dependencies — same spirit as `python quota.py`, which
prints a live snapshot. Runs every check, prints each one, and exits non-zero if
any failed, so it works as a pre-commit sanity gate.

The catch-up formula is small but easy to get subtly wrong (window phase, the
ceiling's effect on whether "spent" is even reachable, and the endpoint's
jittery reset anchor), so each block here pins one property of it.
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timezone

import pacing
import quota


# Only quota._normalize runs here, fed hand-built responses. Anything that would
# read the real credentials or reach the network fails loudly instead.
def _offline(*_args, **_kwargs):
    raise RuntimeError("test_pacing.py must stay offline: no credentials, no network")


quota._read_creds = quota._write_creds = quota._call = quota._refresh = _offline

fails = []


def check(name, got, want, tol=1e-6):
    # Tolerance only between numbers: a regression that returns None where a
    # float belongs should print FAIL, not stop the run with a TypeError.
    close = isinstance(want, float) and isinstance(got, (int, float))
    ok = abs(got - want) <= tol if close else got == want
    print(("  ok   " if ok else "  FAIL ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


def iso(e):
    return datetime.fromtimestamp(e, timezone.utc).isoformat()


BASE = 1785499800.0  # 2026-07-31T12:10:00Z

print("\n[1] 5h window, 50% at 1h elapsed -> resume at 2.5h elapsed")
start, reset = BASE, BASE + 5 * 3600
now = start + 3600
p = pacing.compute({"five_hour": {"utilization": 50.0, "resets_at": iso(reset)}}, now=now)
w = p["windows"]["five_hour"]
check("wait == 1.5h", w["wait_seconds"], 1.5 * 3600)
check("resume at start+2.5h", w["resume_at_epoch"], start + 2.5 * 3600)
check("ideal == 20%", w["ideal_utilization"], 20.0)
check("delta == +30", w["delta"], 30.0)
check("state", w["state"], "over")

print("\n[2] resuming at resume_at and burning at budget rate lands on 100 at reset")
rate = 100.0 / 5.0                      # points per hour = the budget rate
hours_left = (reset - w["resume_at_epoch"]) / 3600.0
check("50 + rate*hours_left == 100", 50.0 + rate * hours_left, 100.0, 1e-9)

print("\n[3] under pace -> no wait, positive slack")
p = pacing.compute({"five_hour": {"utilization": 8.0, "resets_at": iso(reset)}},
                   now=start + 1.667 * 3600)
w = p["windows"]["five_hour"]
check("wait == 0", w["wait_seconds"], 0.0)
check("slack > 0", w["slack_seconds"] > 0, True)
check("state", w["state"], "under")

print("\n[4] ceiling=100 never points past the reset; 100% lands exactly on it")
p = pacing.compute({"five_hour": {"utilization": 100.0, "resets_at": iso(reset)}},
                   now=start + 3600)
w = p["windows"]["five_hour"]
check("resume == reset", w["resume_at_epoch"], reset)
check("state", w["state"], "capped")

print("\n[5] sub-100 ceiling can declare the window spent")
p = pacing.compute({"five_hour": {"utilization": 95.0, "resets_at": iso(reset)}},
                   now=start + 3600, ceiling=90.0)
w = p["windows"]["five_hour"]
check("resume past reset", w["resume_at_epoch"] > reset, True)
check("state", w["state"], "spent")

print("\n[6] combined wait is the LONGER of the two windows")
week_reset = BASE + 4 * 86400
lims = {
    "five_hour": {"utilization": 10.0, "resets_at": iso(reset)},        # under
    "seven_day": {"utilization": 80.0, "resets_at": iso(week_reset)},   # way over
}
p = pacing.compute(lims, now=start + 3600)
check("driver is the week", p["wait_driver"], "seven_day")
check("wait == week's wait", p["wait_seconds"], p["windows"]["seven_day"]["wait_seconds"])
check("wait > 5h's wait", p["wait_seconds"] > p["windows"]["five_hour"]["wait_seconds"], True)

print("\n[7] weekly day boundaries phase off the reset anchor (08:00 local here)")
# Real anchor from the live API: resets Tue 2026-08-04 12:00Z == 08:00 -04:00
wk_reset = datetime.fromisoformat("2026-08-04T12:00:00+00:00").timestamp()
now = datetime.fromisoformat("2026-07-31T13:50:00+00:00").timestamp()
p = pacing.compute({"seven_day": {"utilization": 26.0, "resets_at": iso(wk_reset)}}, now=now)
d = p["weekly_day"]
w = p["windows"]["seven_day"]
check("week start == reset - 7d", w["start_epoch"], wk_reset - 7 * 86400)
check("day index (4th day)", d["day_index"], 3)
check("allowance == 100/7", d["allowance"], 100.0 / 7.0)
check("day_start is a 24h step", (d["day_start_epoch"] - w["start_epoch"]) % 86400, 0.0)
print("     week start :", datetime.fromtimestamp(w["start_epoch"]).astimezone().isoformat())
print("     day  start :", datetime.fromtimestamp(d["day_start_epoch"]).astimezone().isoformat())
print("     banked     : %.2f days" % d["banked_days"])
check("banked ~1.26d", round(d["banked_days"], 2), 1.26)
check("no wait", w["wait_seconds"], 0.0)

print("\n[8] SampleStore: baseline respects the week generation")
path = os.path.join(tempfile.mkdtemp(), "hist.json")
store = pacing.SampleStore(path)
prev_reset = wk_reset - 7 * 86400
day_start = wk_reset - 7 * 86400 + 3 * 86400
# a stale sample from LAST week at 90% — must never be used as this week's base
store.record({"seven_day": {"utilization": 90.0, "resets_at": iso(prev_reset)}},
             now=day_start - 7200)
store.record({"seven_day": {"utilization": 17.0, "resets_at": iso(wk_reset)}},
             now=day_start - 600)
store.record({"seven_day": {"utilization": 26.0, "resets_at": iso(wk_reset)}},
             now=now)
base, at, exact = store.baseline("seven_day", day_start, wk_reset)
check("baseline from this week", base, 17.0)
check("baseline is exact", exact, True)
p = pacing.compute({"seven_day": {"utilization": 26.0, "resets_at": iso(wk_reset)}},
                   now=now, store=store)
check("used today == 26-17", p["weekly_day"]["used_today"], 9.0)
check("remaining today", round(p["weekly_day"]["remaining_today"], 2), 5.29)

print("\n[9] history that starts mid-day is reported as partial, not overstated")
store2 = pacing.SampleStore(os.path.join(tempfile.mkdtemp(), "h2.json"))
store2.record({"seven_day": {"utilization": 22.0, "resets_at": iso(wk_reset)}},
              now=day_start + 3600)
p = pacing.compute({"seven_day": {"utilization": 26.0, "resets_at": iso(wk_reset)}},
                   now=now, store=store2)
check("used == 26-22", p["weekly_day"]["used_today"], 4.0)
check("flagged inexact", p["weekly_day"]["used_today_exact"], False)

print("\n[10] degrades on junk input")
for bad in ({}, {"five_hour": None}, {"five_hour": {"utilization": None, "resets_at": None}},
            {"seven_day": {"utilization": 5.0, "resets_at": "not-a-date"}}):
    p = pacing.compute(bad)
    check("no windows for %r" % (bad,), len(p["windows"]), 0)
    check("  headline is blank", p["headline"], "")

print("\n[11] persistence round-trip + dedupe")
store3 = pacing.SampleStore(os.path.join(tempfile.mkdtemp(), "h3.json"))
lim = {"seven_day": {"utilization": 26.0, "resets_at": iso(wk_reset)}}
for i in range(20):
    store3.record(lim, now=now + i * 5)      # unchanged, 5s apart
check("deduped to 1 sample", len(store3.load()), 1)
store3.record({"seven_day": {"utilization": 27.0, "resets_at": iso(wk_reset)}}, now=now + 100)
check("change appends", len(store3.load()), 2)
check("reload from disk", len(pacing.SampleStore(store3.path).load()), 2)

print("\n[12] headline text")
p = pacing.compute({"five_hour": {"utilization": 50.0, "resets_at": iso(reset)}},
                   now=start + 3600)
print("     over :", p["headline"])
p = pacing.compute({"five_hour": {"utilization": 8.0, "resets_at": iso(reset)}},
                   now=start + 3600)
print("     under:", p["headline"])

print("\n[13] sub-second jitter in resets_at must not fracture the week generation")
# The endpoint re-serializes the same reset with a different fraction each call.
# The last one is the case that used to break: the jitter dips BELOW the whole
# minute, so flooring anchored it a minute early and split the generation.
jit = ["2026-08-04T12:00:00.889179+00:00", "2026-08-04T12:00:00.062571+00:00",
       "2026-08-04T12:00:00.799527+00:00", "2026-08-04T11:59:59.620062+00:00"]
check("all three anchor identically", len({pacing._anchor(s) for s in jit}), 1)
check("anchor is on the minute", pacing._anchor(jit[0]) % 60, 0.0)
check("sub-boundary jitter anchors up, not back",
      pacing._anchor("2026-08-04T11:59:59.620062+00:00"),
      pacing._anchor("2026-08-04T12:00:00.062571+00:00"))

store4 = pacing.SampleStore(os.path.join(tempfile.mkdtemp(), "h4.json"))
t_day = pacing._anchor(jit[0]) - 4 * 86400          # a boundary inside the week
store4.record({"seven_day": {"utilization": 17.0, "resets_at": jit[0]}}, now=t_day - 60)
for i, s in enumerate(jit):                          # unchanged reading, jittery anchor
    store4.record({"seven_day": {"utilization": 17.0, "resets_at": s}}, now=t_day + i)
check("jitter alone doesn't append", len(store4.load()), 1)
store4.record({"seven_day": {"utilization": 26.0, "resets_at": jit[1]}}, now=t_day + 3600)
base, at, exact = store4.baseline("seven_day", t_day, pacing._anchor(jit[2]))
check("baseline found across jitter", base, 17.0)
check("and it's exact", exact, True)

print("\n[14] day boundaries land on the clock minute, not a fraction before it")
p = pacing.compute({"seven_day": {"utilization": 26.0, "resets_at": jit[0]}}, now=now)
d, w = p["weekly_day"], p["windows"]["seven_day"]
check("week start on the minute", w["start_epoch"] % 60, 0.0)
check("day start on the minute", d["day_start_epoch"] % 60, 0.0)
print("     day start :", datetime.fromtimestamp(d["day_start_epoch"]).astimezone().strftime("%a %I:%M:%S %p"))
check("prints as 08:00:00",
      datetime.fromtimestamp(d["day_start_epoch"]).astimezone().strftime("%H:%M:%S"), "08:00:00")

wk_start = wk_reset - 7 * 86400


def weekly(util, reset_e=wk_reset):
    """Just the weekly window, in the shape quota.fetch() hands to pacing."""
    return {"seven_day": {"utilization": util, "resets_at": iso(reset_e)}}


print("\n[15] a drop inside one window is a plan rebase: prune the week, no confident 0.0")
# A plan change (Max 5x -> 20x) rebases every bar DOWN mid-window while resets_at,
# the generation marker, stays put. Unpruned, the pre-upgrade 40 at the day
# boundary served as today's baseline and used_today clamped to 0.0 — flagged
# exact, because that sample really does sit at the boundary.
store5 = pacing.SampleStore(os.path.join(tempfile.mkdtemp(), "h5.json"))
store5.record(weekly(40.0), now=day_start - 600)
store5.record(weekly(46.0), now=day_start + 3600)
t_up = day_start + 7200                        # the upgrade: same resets_at, lower bar
store5.record(weekly(12.0), now=t_up)
check("this week's pre-upgrade samples pruned",
      [s["u"]["seven_day"] for s in store5.load() if s["w"] == wk_reset], [12.0])
store5.record(weekly(13.0), now=t_up + 1800)
d = pacing.compute(weekly(13.0), now=t_up + 1800, store=store5)["weekly_day"]
check("used today == 13-12, not a clamped 0.0", d["used_today"], 1.0)
check("  flagged partial", d["used_today_exact"], False)
check("  counted from the first post-upgrade reading", d["used_since_epoch"], t_up)

print("\n[16] a drop already on disk: readers report unknown, the next record prunes it")
# History as a build without the rebase check wrote it: the upgrade's 12 stored
# straight after the pre-upgrade 46. serve.py computes pacing without recording
# first, so a reader can meet that history before the writer prunes it.
path6 = os.path.join(tempfile.mkdtemp(), "h6.json")
with open(path6, "w", encoding="utf-8") as f:
    json.dump({"samples": [{"t": day_start - 600, "u": {"seven_day": 40.0}, "w": wk_reset},
                           {"t": day_start + 3600, "u": {"seven_day": 46.0}, "w": wk_reset},
                           {"t": t_up, "u": {"seven_day": 12.0}, "w": wk_reset}]}, f)
store6 = pacing.SampleStore(path6)
d = pacing.compute(weekly(12.5), now=t_up + 1800, store=store6)["weekly_day"]
check("baseline above the reading -> used today unknown, not 0.0", d["used_today"], None)
check("  and not flagged exact", d["used_today_exact"], False)
store6.record(weekly(12.5), now=t_up + 1800)
check("drop measured from the week's max (46), not its last sample (12)",
      [s["u"]["seven_day"] for s in store6.load() if s["w"] == wk_reset], [12.5])

print("\n[17] only a drop inside ONE window counts: a reset or a small dip isn't a rebase")
store7 = pacing.SampleStore(os.path.join(tempfile.mkdtemp(), "h7.json"))
store7.record(weekly(88.0, wk_start), now=wk_start - 3600)    # last week, near its cap
day1 = wk_start + 86400
store7.record(weekly(3.0), now=day1 - 600)                    # this week, far below it
store7.record(weekly(5.0), now=day1 + 3600)
d = pacing.compute(weekly(5.0), now=day1 + 3600, store=store7)["weekly_day"]
check("the new week's baseline survives the reset", d["used_today"], 2.0)
check("  exact after the reset", d["used_today_exact"], True)
store7.record(weekly(4.8), now=day1 + 7200)                   # 0.2pt dip, under _REBASE_DROP
d = pacing.compute(weekly(4.8), now=day1 + 7200, store=store7)["weekly_day"]
check("a dip under the threshold keeps it too", d["used_today"], 1.8)
check("  exact after the dip", d["used_today_exact"], True)

print("\n[18] day 0 counts from the window start, where utilization is 0: no history needed")
# The measured case: the week's first stored sample came late on day one, and the
# store answered 1.0 "since 11:41 PM" against a true 3.0.
t0 = wk_start + 20 * 3600
d = pacing.compute(weekly(3.0), now=t0)["weekly_day"]
check("day index 0", d["day_index"], 0)
check("used today == the week so far, with no store at all", d["used_today"], 3.0)
check("  exact", d["used_today_exact"], True)
store8 = pacing.SampleStore(os.path.join(tempfile.mkdtemp(), "h8.json"))
store8.record(weekly(2.0), now=wk_start + 16 * 3600)
store8.record(weekly(3.0), now=t0)
d = pacing.compute(weekly(3.0), now=t0, store=store8)["weekly_day"]
check("history that starts mid-day can't understate it", d["used_today"], 3.0)
check("  still exact", d["used_today_exact"], True)

print("\n[19] the day index floors at 0 when the clock is behind the window start")
# Right at a rollover, a local clock a few seconds behind the server's puts `now`
# before the new window's start. A bare floor-divide made that day -1.
d = pacing.compute(weekly(0.4), now=wk_start - 90)["weekly_day"]
check("day index 0, not -1", d["day_index"], 0)
check("day starts at the window start, not a day before", d["day_start_epoch"], wk_start)
check("budget at day start 0, not negative", d["budget_at_day_start"], 0.0)
d = pacing.compute(weekly(50.0), now=wk_reset + 90)["weekly_day"]
check("the other end still stops at day 7", d["day_index"], 6)

print("\n[20] scoped caps: window length by key prefix, label from the limit dict")
# quota names the per-model weekly cap at runtime (seven_day_scoped_<model>), so no
# table holds its length or its label. A fixed table kept it out of both the
# gauges and the wait driver.
check("seven_day_scoped_* is a week", pacing.window_hours("seven_day_scoped_fable"), 168.0)
check("other keys aren't windows", pacing.window_hours("extra_usage"), None)
lims = {
    "five_hour": {"utilization": 10.0, "resets_at": iso(reset)},        # under
    "seven_day": {"utilization": 30.0, "resets_at": iso(wk_reset)},     # under
    "seven_day_scoped_fable": {"utilization": 70.0, "resets_at": iso(wk_reset),
                               "label": "This week · Fable"},           # way over
}
p = pacing.compute(lims, now=now)
w = p["windows"].get("seven_day_scoped_fable") or {}
check("scoped cap gets a 168h window", w.get("window_hours"), 168.0)
check("  labelled from its limit dict", w.get("label"), "This week · Fable")
check("  and it can drive the wait", p["wait_driver"], "seven_day_scoped_fable")
check("a table key with no label still reads LABELS", p["windows"]["seven_day"]["label"],
      "This week · all models")

print("\n[21] quota._normalize keeps a weekly_scoped `limits` row (hand-built response)")
# Current Max plans report null for both flat per-model keys; the scoped cap comes
# only in the `limits` array, which the four-key whitelist never looked at.
raw = {
    "five_hour": {"utilization": 12.0, "resets_at": iso(reset)},
    "seven_day": {"utilization": 30.0, "resets_at": iso(wk_reset)},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "limits": [
        {"kind": "session", "percent": 12.0, "resets_at": iso(reset)},
        {"kind": "weekly_all", "percent": 30.0, "resets_at": iso(wk_reset)},
        {"kind": "weekly_scoped", "percent": 41.0, "resets_at": iso(wk_reset),
         "scope": {"model": {"display_name": "Fable"}}},
    ],
}
flat = ["five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"]
lims = quota._normalize(raw)
check("scoped row kept as seven_day_scoped_fable", lims.get("seven_day_scoped_fable"),
      {"utilization": 41.0, "resets_at": iso(wk_reset), "label": "This week · Fable"})
check("session/weekly_all rows add nothing", sorted(lims), sorted(flat + ["seven_day_scoped_fable"]))
w = pacing.compute(lims, now=now)["windows"].get("seven_day_scoped_fable") or {}
check("pacing gauges it under that label", (w.get("utilization"), w.get("label")),
      (41.0, "This week · Fable"))
# One bar per cap: a scoped Opus row defers to a populated seven_day_opus, but
# with that key null — today's real shape — the row is the only report and stays.
opus = {"kind": "weekly_scoped", "percent": 55.0, "resets_at": iso(wk_reset),
        "scope": {"model": {"display_name": "Opus"}}}
populated = dict(raw, limits=[opus],
                 seven_day_opus={"utilization": 55.0, "resets_at": iso(wk_reset)})
check("scoped Opus defers to a populated seven_day_opus",
      "seven_day_scoped_opus" in quota._normalize(populated), False)
check("  but not to a null one",
      "seven_day_scoped_opus" in quota._normalize(dict(raw, limits=[opus])), True)
for why, row in (("no percent", {"kind": "weekly_scoped",
                                 "scope": {"model": {"display_name": "Fable"}}}),
                 ("no model name", {"kind": "weekly_scoped", "percent": 5.0,
                                    "scope": {"model": {}}})):
    check("a scoped row with %s adds no key" % why,
          sorted(quota._normalize(dict(raw, limits=[row]))), flat)

print("\n[22] five_hour_start: the block opened at resets_at - 5h, on the minute")
# The engine anchors its 5-hour burn here, so it must be the instant the gauge measures.
check("block start", pacing.five_hour_start(
    {"five_hour": {"utilization": 3.0, "resets_at": iso(reset)}}), start)
check("jitter just below the minute still lands on it", pacing.five_hour_start(
    {"five_hour": {"utilization": 3.0, "resets_at": iso(reset - 0.38)}}), start)
for bad in ({}, {"five_hour": None}, {"five_hour": {"utilization": 0.0, "resets_at": None}}):
    check("no block for %r" % (bad,), pacing.five_hour_start(bad), None)

print("\n[23] headline: a per-model cap over its line, or full, never reads 'On pace'")
# Nothing before the fallback looked past five_hour and seven_day, so a scoped cap
# over its line reached "On pace": min() took its negative slack and
# format_duration clamped it to "0s of budget banked", even at 100%.
lims = {
    "five_hour": {"utilization": 10.0, "resets_at": iso(reset)},        # under
    "seven_day": {"utilization": 30.0, "resets_at": iso(wk_reset)},     # under
    "seven_day_scoped_fable": {"utilization": 70.0, "resets_at": iso(wk_reset),
                               "label": "This week · Fable"},           # over
}
p = pacing.compute(lims, now=now)
h, fable = p["headline"], p["windows"]["seven_day_scoped_fable"]
print("     over  :", h)
check("over its line: not 'On pace'", h.startswith("On pace"), False)
check("  names the cap and how far over it is",
      ("Fable" in h, pacing.format_duration(fable["wait_seconds"]) in h), (True, True))
lims["seven_day_scoped_fable"]["utilization"] = 100.0
h = pacing.compute(lims, now=now)["headline"]
print("     full  :", h)
check("full: not 'On pace'", h.startswith("On pace"), False)
check("  names the cap and when it resets",
      ("Fable" in h, pacing.format_duration(wk_reset - now) in h), (True, True))
lims["seven_day_opus"] = {"utilization": 60.0, "resets_at": iso(wk_reset)}  # over, by less
lims["seven_day_scoped_fable"]["utilization"] = 90.0
h = pacing.compute(lims, now=now)["headline"]
check("two caps over: the longer wait is the one named", ("Fable" in h, "Opus" in h), (True, False))
del lims["seven_day_opus"]
lims["seven_day_scoped_fable"]["utilization"] = 5.0
h = pacing.compute(lims, now=now)["headline"]
print("     under :", h)
check("every line under: still 'On pace'", h.startswith("On pace"), True)

print("\n[24] headline: a full weekly cap comes before the 5-hour window")
# A full week blocks every model until it resets, but it was checked after the
# 5-hour window and read as merely over its line: "next allowance in 22h" at 100%,
# "resume in 50m" with the 5-hour line over, "resets in 3h 20m" with that cap full
# too. None of those moments frees anything up.
to_reset = pacing.format_duration(wk_reset - now)
for label, five_u in (("5h under", 10.0), ("5h over pace", 50.0), ("5h full too", 100.0)):
    lims = dict(weekly(100.0), five_hour={"utilization": five_u, "resets_at": iso(reset)})
    h = pacing.compute(lims, now=now)["headline"]
    print("     week full, %-12s: %s" % (label, h))
    check("week full, %s: names the weekly reset and nothing sooner" % label,
          (to_reset in h, any(s in h for s in ("allowance", "resume", "5-hour"))), (True, False))
# Late in a week the 5-hour block can outlast it, and then that reset is the wait.
late = wk_reset - 3600
lims = dict(weekly(100.0), five_hour={"utilization": 100.0, "resets_at": iso(late + 3 * 3600)})
h = pacing.compute(lims, now=late)["headline"]
check("both full, the 5h block resetting later: that reset is named",
      (h.startswith("5-hour window spent"), pacing.format_duration(3 * 3600) in h), (True, True))
lims = dict(weekly(80.0), five_hour={"utilization": 50.0, "resets_at": iso(reset)})
check("week over its line but not full: the 5-hour countdown still leads",
      pacing.compute(lims, now=now)["headline"].startswith("Ahead of pace"), True)

print("\n[gauge] the gauge's own climb rate, from the reading history")
# The transcript dials cannot see Chat; the bar can. This is that bar's rate:
# points gained over the readings inside each box, as an hourly figure.
G = pacing.GAUGE_WINDOWS                              # (900, 1800) s
t0 = BASE
hist = pacing.SampleStore(path=os.path.join(tempfile.gettempdir(), "usage-gauge-never-written.json"))
hist._samples = []
v = pacing.gauge_velocity(hist, now=t0)
check("no history: a box per width, none with a reading",
      [(x["window_seconds"], x["samples"], x["points_per_hour"]) for x in v],
      [(G[0], 0, 0.0), (G[1], 0, 0.0)])
lims = {"five_hour": {"utilization": 10.0, "resets_at": iso(t0 + 3600)}}
check("  compute() carries it when given the store",
      pacing.compute(lims, now=t0, store=hist)["gauge_rate"], v)
check("  and not without one", pacing.compute(lims, now=t0)["gauge_rate"], None)


def reading(t, u):
    return {"t": t, "u": {"five_hour": u}, "w": 1}


# Two unchanged readings, then a point a minute for ten minutes, read at the last.
hist._samples = ([reading(t0 - 1500, 10.0), reading(t0 - 1000, 10.0)]
                 + [reading(t0 - 600 + 60 * k, 10.0 + k) for k in range(11)])
v15, v30 = pacing.gauge_velocity(hist, now=t0)
check("ten points in ten minutes: 40 pts/h on the fifteen", v15["points_per_hour"], 40.0)
check("  which is twice the even-spend rate (20 pts/h)", v15["pace_multiple"], 2.0)
check("  the points themselves", v15["points"], 10.0)
check("  from the readings the box held", v15["samples"], 11)
check("  and 20 pts/h on the thirty, exactly pace", (v30["points_per_hour"], v30["pace_multiple"]), (20.0, 1.0))
check("  which held one more reading", v30["samples"], 12)
shuffled = list(reversed(hist._samples))
hist._samples, keep = shuffled, hist._samples
check("reading order does not matter", pacing.gauge_velocity(hist, now=t0), [v15, v30])
hist._samples = keep

# The block resets: the bar drops to zero, then climbs three points.
hist._samples += [reading(t0 + 60, 0.0), reading(t0 + 120, 3.0)]
v15 = pacing.gauge_velocity(hist, now=t0 + 120)[0]
check("a reset is not a negative rate: ten before it and three after, 52 pts/h",
      v15["points_per_hour"], 13.0 * 3600 / G[0])
# A box later the climb before the reset has aged out; the reading that
# showed the three drops out on the box edge, like a request does on a dial.
v15 = pacing.gauge_velocity(hist, now=t0 + 120 + G[0] - 1)[0]
check("  a second short of a box later, only the three remain", v15["points"], 3.0)
check("  from the one reading still inside", v15["samples"], 1)
v15 = pacing.gauge_velocity(hist, now=t0 + 120 + G[0])[0]
check("  a box later on the dot: nothing in the box", (v15["samples"], v15["points_per_hour"]), (0, 0.0))

check("a bar the history never carried has no reading",
      [x["samples"] for x in pacing.gauge_velocity(hist, key="seven_day", now=t0)], [0, 0])
hist._samples = [reading(t0 - 60, 10.0), {"t": t0 - 30, "u": {"five_hour": None}},
                 {"t": "soon", "u": {"five_hour": 12.0}}, reading(t0, 11.0)]
check("malformed readings are skipped, not summed",
      pacing.gauge_velocity(hist, now=t0)[0]["points"], 1.0)
check("a lower ceiling scales the pace multiple",
      pacing.gauge_velocity(hist, now=t0, ceiling=50.0)[0]["pace_multiple"],
      (1.0 * 3600 / G[0]) / 10.0)

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
