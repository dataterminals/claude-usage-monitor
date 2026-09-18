"""Self-check for the second transcript root.  Run:  python test_cowork.py

No framework, no dependencies, offline: a temp tree laid out the way the
desktop app lays out %APPDATA%\\Claude\\local-agent-mode-sessions, next to a
temp ~/.claude/projects, and nothing here reads a real transcript or cache.

Cowork runs the same Claude Code runtime, so its transcripts parse as-is; what
needed pinning is everything around them. Finding them at all — they sit under
a `.claude` dot-directory that a recursive glob walks straight past. Not
counting a session's `audit.jsonl` or whatever lands in its `outputs`. Naming
a session by the title in its sidecar rather than by its `cwd`, which is
always `outputs`, and following that title when the app changes it. And the
cache: a cache written before the second root existed must still load, with
its records reading as Claude Code's, and the Cowork transcripts read on top.
"""
import glob
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import engine

fails = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


tmp = tempfile.mkdtemp(prefix="usage-cowork-")
code = os.path.join(tmp, "projects")
cowork = os.path.join(tmp, "local-agent-mode-sessions")
org = os.path.join(cowork, "user-1", "org-1")
n = 0


def turn(path, minutes_ago, cwd, sid):
    """Append one billable assistant turn; every one is unique."""
    global n
    n += 1
    ts = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat()
    line = {"type": "assistant", "timestamp": ts.replace("+00:00", "Z"),
            "requestId": "req_%d" % n, "sessionId": "sess-" + sid,
            "cwd": cwd,
            "message": {"id": "msg_%d" % n, "model": "claude-sonnet-5",
                        "usage": {"input_tokens": 100, "output_tokens": 10}}}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")


def sidecar(session_dir, meta):
    with open(session_dir + ".json", "w", encoding="utf-8") as f:
        json.dump(meta, f)


def session(name, under=org):
    d = os.path.join(under, name)
    mangled = "C--" + name.replace("_", "-") + "-outputs"
    return d, os.path.join(d, ".claude", "projects", mangled), os.path.join(d, "outputs")


def projects(snap):
    return sorted(p["name"] for p in snap["by_project"])


try:
    # ---- the tree -------------------------------------------------------
    turn(os.path.join(code, "demo", "s0.jsonl"), 40, "C:\\proj\\demo", "demo")

    aaa, aaa_projects, aaa_out = session("local_aaa")
    turn(os.path.join(aaa_projects, "s1.jsonl"), 30, aaa_out, "aaa")
    turn(os.path.join(aaa_projects, "s1.jsonl"), 20, aaa_out, "aaa")
    sidecar(aaa, {"title": "Dropbox connector setup", "model": "claude-opus-4-8"})
    # Not transcripts, even though they are .jsonl with assistant-shaped lines.
    turn(os.path.join(aaa, "audit.jsonl"), 25, aaa_out, "audit")
    turn(os.path.join(aaa_out, "notes.jsonl"), 25, aaa_out, "notes")

    ditto, ditto_projects, ditto_out = session("local_ditto_org-1", under=os.path.join(org, "agent"))
    turn(os.path.join(ditto_projects, "s2.jsonl"), 15, ditto_out, "ditto")
    sidecar(ditto, {"sessionType": "agent"})                     # no title, like the real one

    bbb, bbb_projects, bbb_out = session("local_bbb")
    turn(os.path.join(bbb_projects, "s3", "subagents", "agent-x.jsonl"), 10, bbb_out, "bbb")
    # no sidecar at all

    print("\n[1] finding them: through the dot-directory, and only the transcripts")
    found = sorted(engine._transcripts(cowork, nested=True))
    check("three transcripts, and nothing from audit.jsonl or outputs/",
          [os.path.relpath(p, cowork).replace(os.sep, "/") for p in found],
          ["user-1/org-1/agent/local_ditto_org-1/.claude/projects/C--local-ditto-org-1-outputs/s2.jsonl",
           "user-1/org-1/local_aaa/.claude/projects/C--local-aaa-outputs/s1.jsonl",
           "user-1/org-1/local_bbb/.claude/projects/C--local-bbb-outputs/s3/subagents/agent-x.jsonl"])
    globbed = glob.glob(os.path.join(cowork, "**", "*.jsonl"), recursive=True)
    check("glob's recursive ** finds exactly the two that are not transcripts, which is why this walks",
          sorted(os.path.basename(p) for p in globbed), ["audit.jsonl", "notes.jsonl"])
    check("the Claude Code root walks the same as it globbed",
          list(engine._transcripts(code, nested=False)),
          glob.glob(os.path.join(code, "**", "*.jsonl"), recursive=True))
    check("a missing root yields nothing",
          list(engine._transcripts(os.path.join(tmp, "nope"), nested=True)), [])

    print("\n[2] both roots in one engine: sources, titles, totals")
    eng = engine.UsageEngine(code, cowork_dir=cowork)
    eng.cache_file = os.path.join(tmp, "cache-1.json")
    eng.refresh()
    snap = eng.snapshot()
    check("five records: one Code, four Cowork", snap["meta"]["record_count"], 5)
    check("  per source",
          {k: v["records"] for k, v in snap["meta"]["sources"].items()}, {"code": 1, "cowork": 4})
    check("  each says where it came from",
          sorted(r["source"] for r in eng._records), ["code"] + ["cowork"] * 4)
    check("projects: the title, the agent's kind, 'untitled' for the one with no sidecar, and Code's own",
          projects(snap),
          ["Cowork \u00b7 Dropbox connector setup", "Cowork \u00b7 agent", "Cowork \u00b7 untitled", "demo"])
    check("  not the cwd, which is always `outputs`", "outputs" in projects(snap), False)
    by_src = {x["name"]: x for x in snap["by_source"]}
    check("the week's split", {k: v["count"] for k, v in by_src.items()}, {"code": 1, "cowork": 4})
    check("  in tokens too", by_src["cowork"]["tokens_total"], 4 * 110)
    check("the latest request is a Cowork one", snap["active"]["source"], "cowork")
    check("  under its name", snap["active"]["project"], "Cowork \u00b7 untitled")
    sess = {s["session"]: s["project"] for s in snap["sessions"]}
    check("the sessions table carries the title", sess["sess-aaa"], "Cowork \u00b7 Dropbox connector setup")
    check("  and Code's project name", sess["sess-demo"], "demo")
    check("sources(): both there, files counted per root",
          {k: (v["exists"], v["files"]) for k, v in eng.sources().items()},
          {"code": (True, 1), "cowork": (True, 3)})

    print("\n[3] the title follows the sidecar")
    sidecar(aaa, {"title": "Chime monitor"})
    later = time.time() + 5
    os.utime(aaa + ".json", (later, later))          # a same-second rewrite must still count
    eng.refresh()
    check("retitled on the next pass", "Cowork \u00b7 Chime monitor" in projects(eng.snapshot()), True)
    check("  and the old name is gone", "Cowork \u00b7 Dropbox connector setup" in projects(eng.snapshot()), False)
    os.remove(aaa + ".json")
    eng.refresh()
    check("sidecar gone: 'untitled', not a crash", "Cowork \u00b7 untitled" in projects(eng.snapshot()), True)
    sidecar(aaa, {"title": "Dropbox connector setup"})
    os.utime(aaa + ".json", (later + 5, later + 5))
    eng.refresh()

    print("\n[4] the cache: round trip, and a cache from before the second root")
    check("saved", eng.save_cache(), True)
    warm = engine.UsageEngine(code, cowork_dir=cowork)
    warm.cache_file = eng.cache_file
    check("a fresh engine starts warm", warm.load_cache(), True)
    check("  with the names already right, before any refresh", projects(warm.snapshot()), projects(eng.snapshot()))
    check("  and the sources", warm.snapshot()["meta"]["sources"], eng.snapshot()["meta"]["sources"])

    with open(eng.cache_file, encoding="utf-8") as f:
        doc = json.load(f)
    old = os.path.join(tmp, "cache-old.json")
    doc.pop("cowork_dir")
    doc.pop("labels")
    doc["records"] = [r for r in doc["records"] if r["source"] == "code"]
    for r in doc["records"]:
        r.pop("source")
    doc["offsets"] = {p: o for p, o in doc["offsets"].items() if p.startswith(code)}
    doc["seen"] = [k for k in doc["seen"] if k.endswith("|req_1")]
    with open(old, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    up = engine.UsageEngine(code, cowork_dir=cowork)
    up.cache_file = old
    check("a pre-Cowork cache still loads", up.load_cache(), True)
    check("  its records read as Claude Code's", [r["source"] for r in up._records], ["code"])
    up.refresh()
    check("  and the Cowork transcripts are read on top", up.snapshot()["meta"]["record_count"], 5)
    check("  with their names", projects(up.snapshot()), projects(eng.snapshot()))

    other = engine.UsageEngine(code, cowork_dir=os.path.join(tmp, "elsewhere"))
    other.cache_file = eng.cache_file
    check("a cache for another Cowork root is a miss", other.load_cache(), False)
    check("  and says so", other.load_result, "miss: written for another cowork_dir")

    print("\n[5] without a Cowork root, nothing changes")
    solo = engine.UsageEngine(code)
    solo.cache_file = os.path.join(tmp, "cache-solo.json")
    solo.refresh()
    check("one root, one record", solo.snapshot()["meta"]["record_count"], 1)
    check("  sources: just Code", list(solo.sources()), ["code"])
    check("  and the project name is the cwd's, as before", projects(solo.snapshot()), ["demo"])
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
