"""Self-check for the "right now" burn rate.  Run:  python test_velocity.py

No framework and no dependencies, like the other self-checks. Offline: the
engine is built on an empty temp projects dir and fed hand-made records, so
nothing here reads a transcript or the cache.

The velocity is a plain ten-minute box (engine._velocity): what landed in the
last ten minutes, as an hourly rate. The properties worth pinning are the ones
a reader would reach for to sanity-check the dial: what one request reads as
the moment it lands, when it drops out, that a full box reads the true rate,
where the peak sits, and — the reason it exists — that it reads zero during an
idle hour while the 5-hour block average barely moves.
"""
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


WIN = engine._VELOCITY_WINDOW       # 600 s
PER_H = 3600.0 / WIN                # a $1 request in the box reads as $6/h


def rec(epoch, cost, tokens=1000, model="claude-opus-5"):
    t = {k: 0 for k in engine._TOKEN_KEYS}
    t["output"] = tokens
    return {"epoch": epoch, "model": model, "project": "p", "session": "s",
            "branch": "main", "tokens": t, "web_search": 0, "web_fetch": 0,
            "cost": cost}


def main():
    now = 1_800_000_000.0

    print("\n[1] one request, and when it drops out")
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

    v = engine._velocity([(now - WIN + 1, 2.0, 1000)], now)
    check("a second short of ten minutes: still $12/h", v["cost_per_hour"], 2.0 * PER_H)
    v = engine._velocity([(now - WIN, 2.0, 1000)], now)
    check("ten minutes on the dot: dropped out", v["cost_per_hour"], 0.0)
    check("  the peak remembers it", v["peak_cost_per_hour"], 2.0 * PER_H)
    check("  and when", v["peak_epoch"], now - WIN)
    v = engine._velocity([(now - 5 * 3600, 2.0, 1000)], now)
    check("five hours on: gone", v["cost_per_hour"], 0.0)

    v = engine._velocity([(now + 60, 2.0, 1000)], now)
    check("a record stamped in the future counts as just landed",
          v["cost_per_hour"], 2.0 * PER_H)
    v = engine._velocity([(now - 500, 1.0, 100), (now + 200, 1.0, 100)], now)
    check("  and does not push a 500s-old request out of now's box",
          v["cost_per_hour"], 2.0 * PER_H)

    print("\n[2] a full box reads the true rate")
    # $1 a minute for three hours is $60/h. Read at the last request the box
    # holds exactly ten of them — no discretization fudge, the point of a box.
    recs = [(now - 60 * k, 1.0, 1000) for k in range(180)]
    v = engine._velocity(recs, now)
    check("$1/min for 3h, read at the last request: $60/h", v["cost_per_hour"], 60.0)
    check("  tokens scale the same way", v["tokens_per_hour"], 60000.0)
    check("  the peak is the same $60/h", v["peak_cost_per_hour"], 60.0)
    v = engine._velocity(recs, now + 30)
    check("half a minute later: still $60/h, nothing has aged out", v["cost_per_hour"], 60.0)
    v = engine._velocity(recs, now + 60)
    check("a minute later: the oldest is out, $54/h", v["cost_per_hour"], 54.0)

    print("\n[3] the peak is the fastest ten minutes in the window, not the latest")
    burst_at = now - 2 * 3600
    recs = [(burst_at, 5.0, 5000), (now, 0.5, 500)]
    v = engine._velocity(recs, now)
    check("peak is the $5 burst two hours ago", v["peak_cost_per_hour"], 5.0 * PER_H)
    check("  stamped when it happened", v["peak_epoch"], burst_at)
    check("  the current rate is the trickle, exactly", v["cost_per_hour"], 0.5 * PER_H)
    check("input order does not matter", engine._velocity(list(reversed(recs)), now), v)

    # Two requests a minute apart share a box: the peak is read after the second.
    recs = [(now - 60, 1.0, 0), (now, 1.0, 0)]
    v = engine._velocity(recs, now)
    check("back-to-back requests stack", v["cost_per_hour"], 2.0 * PER_H)
    check("  and the stacked reading is the peak", v["peak_epoch"], now)
    # Eleven minutes apart they never share one: the peak is either, first wins.
    recs = [(now - 660, 1.0, 0), (now, 1.0, 0)]
    v = engine._velocity(recs, now)
    check("eleven minutes apart they do not stack", v["cost_per_hour"], 1.0 * PER_H)
    check("  peak is a single request", v["peak_cost_per_hour"], 1.0 * PER_H)
    check("  the earlier one, on a tie", v["peak_epoch"], now - 660)

    print("\n[4] through snapshot(): the dial reads zero while the block average sits")
    tmp = tempfile.mkdtemp(prefix="velocity-")
    try:
        eng = engine.UsageEngine(os.path.join(tmp, "projects"))
        block = now - 2 * 3600                              # block opened 2h ago
        # ten $1 requests in the block's first ten minutes, then nothing
        eng._records = [rec(block + 60 * k, 1.0) for k in range(10)]

        at = datetime.fromtimestamp(block + 9 * 60, timezone.utc)   # at the last request
        snap = eng.snapshot(now=at, five_hour_start=block)
        b, v = snap["windows"]["rolling_5h"], snap["velocity"]
        check("at the last request: block average $66.7/h", b["burn_cost_per_hour"], 10.0 / (9 / 60.0), 1e-6)
        check("  velocity holds all ten: $60/h", v["cost_per_hour"], 60.0, 1e-6)
        check("  which is the peak", v["peak_cost_per_hour"], 60.0, 1e-6)

        at = datetime.fromtimestamp(now, timezone.utc)
        snap = eng.snapshot(now=at, five_hour_start=block)
        b, v = snap["windows"]["rolling_5h"], snap["velocity"]
        check("two hours in, idle since: block average still $5/h", b["burn_cost_per_hour"], 5.0, 1e-6)
        check("  velocity reads zero", v["cost_per_hour"], 0.0)
        check("  but the peak is still the burst", v["peak_cost_per_hour"], 60.0, 1e-6)
        check("  window is the 48h one", v["window_hours"], engine._VELOCITY_HOURS)
        check("  the box length is published for the label", v["window_seconds"], WIN)

        # A monster request older than the window must not set the scale.
        eng._records.append(rec(now - 49 * 3600, 100.0))
        v = eng.snapshot(now=at, five_hour_start=block)["velocity"]
        check("a $100 request 49h ago does not set the peak", v["peak_cost_per_hour"], 60.0, 1e-6)
        eng._records.append(rec(now - 47 * 3600, 100.0))
        v = eng.snapshot(now=at, five_hour_start=block)["velocity"]
        check("one at 47h does", v["peak_cost_per_hour"], 100.0 * PER_H, 1e-6)
        check("  without moving the current rate", v["cost_per_hour"], 0.0)
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
