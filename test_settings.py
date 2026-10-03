"""Self-check for the persisted tray settings.  Run:  python test_settings.py

No test framework and no dependencies, same as the others. Offline by
construction — settings.py only ever touches one JSON file — but it is pointed
at a temp directory here, because a test that rewrote the real settings would
silently change the window you are looking at.

Small module, but the two things it does wrong are both invisible: a bad file
that takes a preference with it, and a save that drops the keys it wasn't
handed. Both surface as "the app forgot", which is the hardest kind of bug to
believe you have.
"""
import json
import os
import sys
import tempfile

import settings

fails = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="cum-settings-")
os.environ["LOCALAPPDATA"] = tmp          # _path() reads this on every call
path = settings._path()
check("settings live beside the cache and the history",
      os.path.dirname(path).endswith("ClaudeUsageMonitor"), True)


def write(text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


print("\n[1] no file at all is the defaults, not an error")
check("borderless defaults on", settings.load(), {"borderless": True})

print("\n[2] a stored value wins over the default")
check("saved", settings.save({"borderless": False}), True)
check("  and reads back", settings.load()["borderless"], False)
check("  from a file a human can read", json.load(open(path, encoding="utf-8")),
      {"borderless": False})

print("\n[3] a save carries the keys it wasn't given")
# There is one setting today. The moment there are two, a save of one that drops
# the other is a preference that vanishes on an unrelated toggle — cheaper to
# pin now with a stand-in than to find later with a real one.
settings.DEFAULTS["second"] = 7
try:
    write(json.dumps({"borderless": False, "second": 9}))
    settings.save({"borderless": True})
    check("the other known key survives", settings.load(), {"borderless": True, "second": 9})
    # A key nobody claims is not ours to carry: it would have to round-trip
    # through a type check with nothing to check against.
    write(json.dumps({"borderless": False, "stranger": "hi"}))
    check("an unknown key is not loaded", settings.load(), {"borderless": False, "second": 7})
finally:
    del settings.DEFAULTS["second"]

print("\n[4] a file that can't be trusted degrades to the defaults")
for why, text in (("truncated json", '{"borderless": fal'),
                  ("not an object", '["borderless"]'),
                  ("empty", ""),
                  ("wrong type", '{"borderless": "yes"}')):
    write(text)
    check("%-14s -> defaults" % why, settings.load(), {"borderless": True})

print("\n[5] an unwritable path costs a toggle, never a launch")
# A regular file standing where the directory would go: makedirs raises
# NotADirectoryError, which is an OSError, which save() is supposed to eat.
blocker = os.path.join(tmp, "blocker")
open(blocker, "w").close()
os.environ["LOCALAPPDATA"] = blocker
check("save reports failure instead of raising", settings.save({"borderless": False}), False)
check("  and load still answers", settings.load(), {"borderless": True})

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
