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


class State:
    def __init__(self):
        self.engine = UsageEngine(os.path.expanduser("~/.claude/projects"))
        self.quota_enabled = os.environ.get("CLAUDE_USAGE_QUOTA") == "1"
        self.mock = os.environ.get("CLAUDE_USAGE_MOCK_QUOTA") == "1"

    def quota_snapshot(self):
        if self.mock:
            # canned data (with reset times a few hours out) for UI dev/preview
            import copy
            import time
            m = copy.deepcopy(_MOCK_QUOTA)
            now = time.time()
            m["limits"]["five_hour"]["resets_at"] = _iso(now + 1.6 * 3600)
            for k in ("seven_day", "seven_day_opus", "seven_day_sonnet"):
                m["limits"][k]["resets_at"] = _iso(now + 3.2 * 24 * 3600)
            return m
        if not self.quota_enabled:
            return {"enabled": False}
        data = quota.fetch()
        data["enabled"] = True
        return data


def _iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def main():
    state = State()
    state.engine.refresh()
    port = int(os.environ.get("CLAUDE_USAGE_PORT", "8787"))
    srv = make_server(state, port=port)
    _, bound = srv.server_address
    print("Claude Usage dashboard -> http://127.0.0.1:{}/".format(bound), flush=True)

    def refresher():
        while True:
            time.sleep(5)
            try:
                state.engine.refresh()
            except Exception:
                pass

    threading.Thread(target=refresher, daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
