"""Self-check for the pacing math.  Run:  python test_pacing.py

No test framework and no dependencies — same spirit as `python quota.py`, which
prints a live snapshot. Runs every check, prints each one, and exits non-zero if
any failed, so it works as a pre-commit sanity gate.

The catch-up formula is small but easy to get subtly wrong (window phase, the
ceiling's effect on whether "spent" is even reachable, and the endpoint's
jittery reset anchor), so each block here pins one property of it.
"""
import os
import sys
import tempfile
from datetime import datetime, timezone

import pacing


fails = []


def check(name, got, want, tol=1e-6):
    ok = abs(got - want) <= tol if isinstance(want, float) else got == want
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

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
