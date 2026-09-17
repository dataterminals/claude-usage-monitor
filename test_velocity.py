"""Self-check for the "right now" burn rate.  Run:  python test_velocity.py

No framework and no dependencies, like the other self-checks. Offline: the
engine is built on an empty temp projects dir and fed hand-made records, so
nothing here reads a transcript or the cache.

The velocity is an exponentially weighted rate (engine._velocity), and the
properties worth pinning are the ones a reader would reach for to sanity-check
the dial: what one request reads as the moment it lands, how it decays, that
steady spending converges on the true rate, where the peak sits, and — the
reason it exists — that it falls off during an idle hour while the 5-hour
block average barely moves.
"""
import math
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone

import engine

fails = []


def check(name, got, want, tol=1e-6):
    close = isinstance(want, float) and isinstance(got, (int, float))
    ok = abs(got - want) <= tol if close else got == want
    print(("  ok   " if ok else "  FAIL ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


TAU = engine._VELOCITY_TAU          # 600 s
PER_H = 3600.0 / TAU                # a $1 request just landed reads as $6/h


def rec(epoch, cost, tokens=1000, model="claude-opus-5"):
    t = {k: 0 for k in engine._TOKEN_KEYS}
    t["output"] = tokens
    return {"epoch": epoch, "model": model, "project": "p", "session": "s",
            "branch": "main", "tokens": t, "web_search": 0, "web_fetch": 0,
            "cost": cost}


def main():
    now = 1_800_000_000.0

    print("\n[1] one request, and how it ages")
    v = engine._velocity([], now)
    check("nothing recent: no rate", v["cost_per_hour"], 0.0)
    check("  no tokens either", v["tokens_per_hour"], 0.0)
    check("  no peak", v["peak_cost_per_hour"], 0.0)
    check("  no peak time", v["peak_epoch"], None)

    v = engine._velocity([(now, 2.0, 1000)], now)
    check("$2 just landed reads as $12/h", v["cost_per_hour"], 2.0 * PER_H)
    check("  1000 tok just landed reads as 6000 tok/h", v["tokens_per_hour"], 1000 * PER_H)
    check("  and it is its own peak", v["peak_cost_per_hour"], 2.0 * PER_H)
    check("  peaked at its own time", v["peak_epoch"], now)

    v = engine._velocity([(now - TAU, 2.0, 1000)], now)
    check("ten minutes on: a third left", v["cost_per_hour"], 2.0 * PER_H * math.exp(-1))
    check("  the peak remembers full strength", v["peak_cost_per_hour"], 2.0 * PER_H)
    v = engine._velocity([(now - 3 * TAU, 2.0, 1000)], now)
    check("thirty minutes on: a twentieth", v["cost_per_hour"], 2.0 * PER_H * math.exp(-3))
    v = engine._velocity([(now - 5 * 3600, 2.0, 1000)], now)
    check("five hours on: gone", v["cost_per_hour"] < 1e-9, True)

    v = engine._velocity([(now + 60, 2.0, 1000)], now)
    check("a record stamped in the future counts as just landed, not amplified",
          v["cost_per_hour"], 2.0 * PER_H)

    print("\n[2] steady spending converges on the true rate")
    # $1 a minute for three hours is $60/h. Sampled once a minute rather than
    # continuously the kernel sum is a geometric series, so the exact figure
    # is a few percent over — pin that figure, and that it is near $60.
    n = 180
    recs = [(now - 60 * k, 1.0, 1000) for k in range(n)]
    v = engine._velocity(recs, now)
    q = math.exp(-60 / TAU)
    want = PER_H * (1 - q ** n) / (1 - q)
    check("$1/min for 3h, read at the last request", v["cost_per_hour"], want, 1e-6)
    check("  which is within 6% of $60/h", abs(v["cost_per_hour"] - 60) / 60 < 0.06, True)
    check("  tokens scale the same way", v["tokens_per_hour"], 1000 * want, 1e-3)

    print("\n[3] the peak is the fastest moment in the window, not the latest")
    burst_at = now - 2 * 3600
    recs = [(burst_at, 5.0, 5000), (now, 0.5, 500)]
    v = engine._velocity(recs, now)
    check("peak is the $5 burst two hours ago", v["peak_cost_per_hour"], 5.0 * PER_H)
    check("  stamped when it happened", v["peak_epoch"], burst_at)
    # The burst's tail is still in there — two hours is twelve tau, so a few
    # ten-thousandths of a dollar an hour. Pinned exactly rather than waved at.
    check("  the current rate is the trickle plus the burst's tail",
          v["cost_per_hour"], 0.5 * PER_H + 5.0 * PER_H * math.exp(-2 * 3600 / TAU))
    check("input order does not matter", engine._velocity(list(reversed(recs)), now), v)

    # Two requests a minute apart stack: the peak is read after the second.
    recs = [(now - 60, 1.0, 0), (now, 1.0, 0)]
    v = engine._velocity(recs, now)
    check("back-to-back requests stack", v["cost_per_hour"], PER_H * (1 + math.exp(-60 / TAU)))
    check("  and the stacked reading is the peak", v["peak_epoch"], now)

    print("\n[4] through snapshot(): the dial falls while the block average sits")
    tmp = tempfile.mkdtemp(prefix="velocity-")
    try:
        eng = engine.UsageEngine(os.path.join(tmp, "projects"))
        block = now - 2 * 3600                              # block opened 2h ago
        # ten $1 requests in the block's first ten minutes, then nothing
        eng._records = [rec(block + 60 * k, 1.0) for k in range(10)]

        # Read a minute after the last request: ten minutes into the block.
        q = math.exp(-60 / TAU)
        stacked = PER_H * (1 - q ** 10) / (1 - q)      # the reading at the last request
        at = datetime.fromtimestamp(block + 10 * 60, timezone.utc)
        snap = eng.snapshot(now=at, five_hour_start=block)
        b, v = snap["windows"]["rolling_5h"], snap["velocity"]
        check("a minute after the burst: block average $60/h", b["burn_cost_per_hour"], 60.0, 1e-6)
        check("  velocity reads the stacked burst, one decay step on", v["cost_per_hour"], stacked * q, 1e-6)
        check("  the peak is the reading at the last request", v["peak_cost_per_hour"], stacked, 1e-6)

        at = datetime.fromtimestamp(now, timezone.utc)
        snap = eng.snapshot(now=at, five_hour_start=block)
        b, v = snap["windows"]["rolling_5h"], snap["velocity"]
        check("two hours in, idle since: block average still $5/h", b["burn_cost_per_hour"], 5.0, 1e-6)
        check("  velocity has fallen to nothing", v["cost_per_hour"] < 0.01, True)
        check("  but the peak is still the burst", v["peak_cost_per_hour"], stacked, 1e-6)
        check("  window is the 48h one", v["window_hours"], engine._VELOCITY_HOURS)
        check("  tau is published for the label", v["tau_seconds"], TAU)

        # A monster request older than the window must not set the scale.
        eng._records.append(rec(now - 49 * 3600, 100.0))
        v = eng.snapshot(now=at, five_hour_start=block)["velocity"]
        check("a $100 request 49h ago does not set the peak", v["peak_cost_per_hour"], stacked, 1e-6)
        eng._records.append(rec(now - 47 * 3600, 100.0))
        v = eng.snapshot(now=at, five_hour_start=block)["velocity"]
        check("one at 47h does", v["peak_cost_per_hour"], 100.0 * PER_H, 1e-6)
        check("  without moving the current rate", v["cost_per_hour"] < 0.01, True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if fails:
        print("FAILED: %d" % len(fails))
        for f in fails:
            print("  - " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
