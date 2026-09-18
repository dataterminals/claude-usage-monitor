"""Self-check for what GET /health reports.  Run:  python test_health.py

Same shape as test_pacing.py: no framework, prints every check, exits non-zero
if any failed.

/health is where the app says what it has been doing. The updater swallows its
failures and the cache save used to report nothing, so "is it persisting?" got
answered by reading the cache file off disk, from a shell whose %LOCALAPPDATA%
was redirected into an app package. That copy was nine days stale while the
real file was being rewritten every two minutes. These checks pin the report
down instead: load/save outcomes, swallowed tracebacks, and that launcher.pyw's
single-instance probe still finds "ok" in the first 64 bytes.

Offline and self-contained: the engine reads a temp projects dir and writes a
temp cache file, live quota is off before the updater runs, and the server binds
an ephemeral port so it can't collide with a running instance.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import engine
import quota
import server
import tray


def _offline(*_args, **_kwargs):
    raise RuntimeError("test_health.py must stay offline: no credentials, no network")


quota._read_creds = quota._write_creds = quota._call = quota._refresh = _offline

fails = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="usage-health-")
projects = os.path.join(tmp, "projects")
os.makedirs(os.path.join(projects, "demo"))
transcript = os.path.join(projects, "demo", "session.jsonl")


def append_turn(n):
    """One billable assistant turn, half an hour ago."""
    ts = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    line = {"type": "assistant", "timestamp": ts.replace("+00:00", "Z"),
            "requestId": "req_%d" % n, "sessionId": "s1", "cwd": "C:\\proj\\demo",
            "message": {"id": "msg_%d" % n, "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 10, "output_tokens": 5}}}
    with open(transcript, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def fresh_engine(cache_file):
    eng = engine.UsageEngine(projects)
    eng.cache_file = cache_file
    return eng


try:
    print("\n[1] engine: every load and save leaves an outcome behind")
    cache = os.path.join(tmp, "cache", "engine-cache.json")
    eng = fresh_engine(cache)
    check("nothing recorded before any cache call",
          (eng.load_result, eng.last_save, eng.last_written), (None, None, None))
    check("load with no file is a miss", eng.load_cache(), False)
    check("  and says why", eng.load_result.startswith("miss: FileNotFoundError"), True)

    append_turn(1)
    eng.refresh()
    check("first save writes", eng.save_cache(), True)
    check("  result", eng.last_save[1], "saved 1 records")
    check("  last_written is that save's time", eng.last_written, eng.last_save[0])
    written = eng.last_written
    check("a save with nothing new skips the write", eng.save_cache(), False)
    check("  and says so", eng.last_save[1], "unchanged: 1 records")
    check("  without moving last_written", eng.last_written, written)

    check("the next launch starts warm", fresh_engine(cache).load_cache(), True)
    warm = fresh_engine(cache)
    warm.load_cache()
    check("  result", warm.load_result, "hit: 1 records")

    with open(cache, encoding="utf-8") as f:
        doc = json.load(f)
    doc["version"] = engine._CACHE_VERSION - 1
    old = os.path.join(tmp, "old-cache.json")
    with open(old, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    stale = fresh_engine(old)
    check("an older cache version is a miss", stale.load_cache(), False)
    check("  naming both versions", stale.load_result,
          "miss: version %d, want %d" % (engine._CACHE_VERSION - 1, engine._CACHE_VERSION))

    blocker = os.path.join(tmp, "a-file-not-a-dir")
    open(blocker, "w").close()
    doomed = fresh_engine(os.path.join(blocker, "sub", "engine-cache.json"))
    doomed.refresh()
    check("a save that can't create its directory fails quietly", doomed.save_cache(), False)
    check("  and reports the error", doomed.last_save[1].startswith("error: "), True)
    check("  without claiming a write", doomed.last_written, None)

    print("\n[2] tray: the updater's passes and failures reach health()")
    app = tray.App()
    app.engine = fresh_engine(os.path.join(tmp, "tray-cache", "engine-cache.json"))
    app.quota_enabled = False           # no network, no credentials, no pacing
    h = app.health()
    check("before any pass: no ticks", h["updater"]["ticks"], 0)
    check("  no last tick", h["updater"]["last_tick"], None)
    check("  no error", h["updater"]["last_error"], None)
    check("  no save", h["cache"]["last_save"], None)
    check("  file is the engine's own path", h["cache"]["file"], app.engine.cache_file)
    check("  the roots it reads, nothing read yet",
          {k: (v["dir"], v["files"]) for k, v in h["sources"].items()}, {"code": (projects, 0)})

    append_turn(2)
    app._last_save = time.monotonic() - tray.SAVE_SECONDS    # due now, whatever the uptime
    app._tick()
    h = app.health()
    check("a finished pass counts", h["updater"]["ticks"], 1)
    check("  and is stamped", h["updater"]["last_tick"] is not None, True)
    check("  its save is reported", h["cache"]["last_save"]["result"], "saved 2 records")
    check("  and the transcript it read is counted", h["sources"]["code"]["files"], 1)
    check("  and really landed", os.path.exists(app.engine.cache_file), True)

    def save_raises():
        raise RuntimeError("synthetic save failure")

    app.engine.save_cache = save_raises
    app._last_save = time.monotonic() - tray.SAVE_SECONDS
    app._tick()
    h = app.health()
    check("a save that raises still finishes the pass", h["updater"]["ticks"], 2)
    check("  and its traceback is kept",
          "synthetic save failure" in (h["updater"]["last_error"] or {}).get("traceback", ""),
          True)

    def deep(n):
        if n == 0:
            raise KeyError("innermost")
        return deep(n - 1)

    def failing_tick():
        app._stop.set()                 # one pass, then _updater returns
        deep(12)

    app._tick = failing_tick
    app._updater()
    err = app.health()["updater"]["last_error"]
    check("a pass that raises is not counted", app.health()["updater"]["ticks"], 2)
    check("  but its traceback reaches health()", "KeyError: 'innermost'" in err["traceback"], True)
    # The old limit=4 kept _updater and failing_tick and cut the frame that raised.
    check("  including the frame that raised",
          'raise KeyError("innermost")' in err["traceback"], True)
    check("  stamped", err["age_s"] >= 0, True)

    print("\n[3] server: /health leads with ok, whatever the report does")
    check("a state without health() answers exactly as before",
          server.health_body(object()), '{"ok":true}')

    class Raises:
        def health(self):
            raise RuntimeError("report exploded")

    class Unserializable:
        def health(self):
            return {"x": object()}

    for label, state in (("raises", Raises()), ("won't serialize", Unserializable())):
        body = server.health_body(state)
        check("a report that %s still leads with ok" % label,
              body.startswith('{"ok": true'), True)
    check("  and says what broke", json.loads(server.health_body(Raises()))["health_error"],
          "RuntimeError: report exploded")
    check("the real report serializes", json.loads(server.health_body(app))["ok"], True)

    srv = server.make_server(app, port=0)       # ephemeral: never the running app's port
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = "http://127.0.0.1:%d/health" % srv.server_address[1]
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            head = resp.read(64)                # exactly what launcher.already_running() reads
        check("the launcher probe finds ok in the first 64 bytes", b'"ok"' in head, True)
        with urllib.request.urlopen(url, timeout=5) as resp:
            live = json.loads(resp.read())
        check("the full body carries the cache report", live["cache"]["file"],
              app.engine.cache_file)
        check("  and the swallowed traceback", "innermost" in live["updater"]["last_error"]["traceback"],
              True)
    finally:
        srv.shutdown()
        srv.server_close()
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
