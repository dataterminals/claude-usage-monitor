"""Run only the dashboard + JSON API (no tray icon, standard library only).

    python serve.py          ->  http://127.0.0.1:8787/

Useful on a headless box, or if you just want the web dashboard without the
system-tray piece. Live quota is OFF here unless CLAUDE_USAGE_QUOTA=1 is set
(so this never touches your credentials file on its own).

Env: CLAUDE_USAGE_PORT (default 8787), CLAUDE_USAGE_QUOTA=1 to enable quota.
"""
import os
import threading
import time

import pacing
import quota
from engine import UsageEngine
from server import make_server


_MOCK_QUOTA = {
    "enabled": True, "available": True,
    "limits": {
        "five_hour": {"utilization": 68, "resets_at": None},
        "seven_day": {"utilization": 41, "resets_at": None},
        "seven_day_opus": {"utilization": 55, "resets_at": None},
        "seven_day_sonnet": {"utilization": 12, "resets_at": None},
    },
}
# Reset offsets chosen so the mock lands *over* pace on the 5-hour window
# (2h into 5h at 68% — the budget line is at 40%), since previewing the pacing
# UI is most of what the mock is for.
_MOCK_RESET_IN = {"five_hour": 3.0 * 3600, "seven_day": 3.2 * 86400,
                  "seven_day_opus": 3.2 * 86400, "seven_day_sonnet": 3.2 * 86400}


class State:
    def __init__(self):
        self.engine = UsageEngine(os.path.expanduser("~/.claude/projects"))
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
            # No store: the mock has no real history, so the day-level rows
            # correctly render as "tracking from now" rather than inventing one.
            m["pacing"] = pacing.compute(m["limits"])
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
        # updater, so aggregate on the request thread. Still no network call.
        return self.engine.snapshot()

    def sample(self):
        """Record a reading so "used today" has a baseline. Mirrors the tray's
        updater — this process is the only writer when running headless."""
        if self.mock or not self.quota_enabled:
            return
        data = quota.fetch()
        if data.get("available"):
            self.history.record(data.get("limits") or {})


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
