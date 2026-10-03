"""The handful of tray toggles that have to outlive a restart.

Everything else the tray owns is a runtime choice — Live quota, Auto-refresh
token — and forgetting it at quit is fine, because the default is the one you
want on a fresh start. A window frame is not like that: "borderless" is a
property of where you keep the window, not of this run of the app, and having
to re-pick it every launch would make the setting worse than not having one.

Deliberately not a config format. One flat JSON object beside the parse cache
and the reading history, read once at startup and rewritten on a change, with
every failure degrading to the defaults — a corrupt file costs you a toggle,
never a launch.
"""
import json
import os

DEFAULTS = {
    # The window's home is a narrow strip against a screen edge, where the
    # caption is ~30px of title bar for a title you already know and three
    # buttons you reach for once a week. Off by default would mean shipping the
    # feature switched off for its own use case.
    "borderless": True,
}


def _path():
    base = (os.environ.get("LOCALAPPDATA")
            or os.environ.get("XDG_CONFIG_HOME")
            or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, "ClaudeUsageMonitor", "settings.json")


def load():
    """The stored settings over the defaults. Never raises."""
    out = dict(DEFAULTS)
    try:
        with open(_path(), encoding="utf-8") as f:
            doc = json.load(f)
        if isinstance(doc, dict):
            # Only keys we know, and only at the type we expect: a hand-edited
            # `"borderless": "yes"` should fall back, not reach Win32 as truthy.
            for key, default in DEFAULTS.items():
                if key in doc and isinstance(doc[key], type(default)):
                    out[key] = doc[key]
    except (OSError, ValueError, TypeError):
        pass
    return out


def save(values):
    """Persist `values` (merged over what's stored). True if it landed."""
    merged = load()
    merged.update({k: v for k, v in (values or {}).items() if k in DEFAULTS})
    path = _path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=2)
        os.replace(tmp, path)
        return True
    except OSError:
        return False
