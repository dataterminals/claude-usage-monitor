"""Run only the dashboard + JSON API (no tray icon, standard library only).

    python serve.py          ->  http://127.0.0.1:8787/

Useful on a headless box, or if you just want the web dashboard without the
system-tray piece. Live quota is OFF here unless CLAUDE_USAGE_QUOTA=1 is set
(so this never touches your credentials file on its own).

Env: CLAUDE_USAGE_PORT (default 8787), CLAUDE_USAGE_QUOTA=1 to enable quota.
"""
import os
import tempfile
import threading
import time

import pacing
import quota
from engine import UsageEngine, code_projects_dir, cowork_sessions_dir
from server import make_server


# Shaped like a current Max plan, not like the one this mock was written for:
# both flat per-model keys read null there now, and the per-model cap arrives
# as a runtime-named scoped row. A mock still carrying seven_day_opus could not
# preview the per-model card at all — the one part of the Limits tab whose
# layout is hardest to reason about without seeing it.
_MOCK_QUOTA = {
    "enabled": True, "available": True,
    "limits": {
        "five_hour": {"utilization": 68, "resets_at": None},
        "seven_day": {"utilization": 41, "resets_at": None},
        "seven_day_opus": None,
        "seven_day_sonnet": None,
        "seven_day_scoped_fable": {"utilization": 77, "resets_at": None,
                                   "scope": "Fable", "label": "This week · Fable"},
    },
}
# Reset offsets chosen so the mock lands *over* pace on the 5-hour window
# (2h into 5h at 68% — the budget line is at 40%), since previewing the pacing
# UI is most of what the mock is for. Fable is over its weekly line too, and far
# enough over to be the binding cap: 77% against the week's 41%.
_MOCK_RESET_IN = {"five_hour": 3.0 * 3600, "seven_day": 3.2 * 86400,
                  "seven_day_scoped_fable": 3.2 * 86400}


class State:
    def __init__(self):
        self.engine = UsageEngine(code_projects_dir(), cowork_dir=cowork_sessions_dir())
        self.quota_enabled = os.environ.get("CLAUDE_USAGE_QUOTA") == "1"
        self.mock = os.environ.get("CLAUDE_USAGE_MOCK_QUOTA") == "1"
        self.history = pacing.SampleStore()

    def quota_snapshot(self):
        if self.mock:
            # canned data (with reset times a few hours out) for UI dev/preview
            import copy
            m = copy.deepcopy(_MOCK_QUOTA)
            now = time.time()
            for k, dt in _MOCK_RESET_IN.items():
                m["limits"][k]["resets_at"] = _iso(now + dt)
            # A throwaway store rather than none at all. Without one there is no
            # climb rate and no day baseline, so the per-model card can only ever
            # preview its "no reading history yet" fallback — which is the
            # branch that needs previewing least. In memory and never saved: the
            # mock must not write over the real history.
            m["pacing"] = pacing.compute(m["limits"], store=_mock_store(now, m["limits"]))
            return m
        if not self.quota_enabled:
            return {"enabled": False}
        data = dict(quota.fetch())
        data["enabled"] = True
        if data.get("available"):
            data["pacing"] = pacing.compute(data.get("limits") or {},
                                            store=self.history)
        return data

    def usage_snapshot(self):
        # The tray serves its updater's cached snapshot here; headless has no
        # updater, so aggregate on the request thread. Still no network call —
        # quota.fetch() is cached, and the refresher thread keeps it warm.
        limits = {}
        if self.quota_enabled and not self.mock:
            try:
                data = quota.fetch()
                limits = (data.get("limits") or {}) if data.get("available") else {}
            except Exception:
                limits = {}
        return self.engine.snapshot(five_hour_start=pacing.five_hour_start(limits))

    def sample(self):
        """Record a reading so "used today" has a baseline. Mirrors the tray's
        updater — this process is the only writer when running headless."""
        if self.mock or not self.quota_enabled:
            return
        data = quota.fetch()
        if data.get("available"):
            self.history.record(data.get("limits") or {})


def _mock_store(now, limits):
    """A day and a half of readings that arrive at the mock's numbers.

    A straight ramp from zero over 36 hours, one sample every ten minutes, all
    stamped with the weekly generation the way the tray's updater stamps them.
    Enough for the day baseline and for every climb box up to 24h.
    """
    # A path under TEMP, not the real history's: nothing here writes, but the
    # default path would aim a future _save() at eight days of real readings.
    store = pacing.SampleStore(path=os.path.join(
        tempfile.gettempdir(), "ClaudeUsageMonitor", "mock-history.json"))
    span, step = 36 * 3600.0, 600.0
    gen = pacing._anchor(limits["seven_day"]["resets_at"])
    store._samples = [
        {"t": now - span + i * step,
         "u": {k: round(v["utilization"] * (i * step) / span, 1)
               for k, v in limits.items() if isinstance(v, dict)},
         "w": gen}
        for i in range(int(span / step) + 1)]
    return store


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def main():
    state = State()
    # Warm start from the last run's parse cache; a cold scan of a large
    # transcript history takes over a minute.
    try:
        state.engine.load_cache()
    except Exception:
        pass
    state.engine.refresh()
    port = int(os.environ.get("CLAUDE_USAGE_PORT", "8787"))
    srv = make_server(state, port=port)
    _, bound = srv.server_address
    print("Claude Usage dashboard -> http://127.0.0.1:{}/".format(bound), flush=True)

    def refresher():
        last_save = time.monotonic()
        while True:
            time.sleep(5)
            try:
                state.engine.refresh()
                state.sample()
                if time.monotonic() - last_save >= 120:
                    last_save = time.monotonic()
                    state.engine.save_cache()
            except Exception:
                pass

    threading.Thread(target=refresher, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
