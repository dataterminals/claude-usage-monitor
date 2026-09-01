"""Usage engine.

Parses Claude Code transcripts (~/.claude/projects/**/*.jsonl), keeps an
in-memory list of per-message usage records, and produces JSON-serializable
snapshots aggregated by window / model / project / session / time.

Reads incrementally (byte offsets per file) so the growing active-session
transcript is cheap to re-scan, and dedupes on the API message id + requestId
so a re-logged line is never double-counted.

Those offsets are also persisted to disk (see load_cache/save_cache), because
a cold scan is O(everything you have ever done): a 333 MB / 622-file history
takes ~72 s to parse from scratch, and paying that at every launch is what made
starting the app feel like it wasn't counting anything yet.

Parsing is deliberately paranoid about record shape. A single malformed line
used to raise straight through the updater thread and freeze the tray until a
restart, so anything unexpected here is skipped, never raised.
"""
import glob
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

from pricing import cost_for_record

_TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

_CACHE_VERSION = 1
# Sanity bounds for a transcript timestamp. A bogus epoch (0, or a far-future
# value from a corrupt line) makes datetime.fromtimestamp raise deep inside
# snapshot(), so reject it at parse time instead.
_MIN_EPOCH = 946684800.0                    # 2000-01-01
_MAX_SKEW = 366 * 24 * 3600.0               # a year ahead of now


def _blank():
    acc = {k: 0 for k in _TOKEN_KEYS}
    acc["cost"] = 0.0
    acc["count"] = 0
    acc["web_search"] = 0
    acc["web_fetch"] = 0
    return acc


def _add(acc, rec):
    acc["cost"] += rec["cost"]
    acc["count"] += 1
    t = rec["tokens"]
    for k in _TOKEN_KEYS:
        acc[k] += t[k]
    acc["web_search"] += rec["web_search"]
    acc["web_fetch"] += rec["web_fetch"]


def _serialize(acc, **extra):
    out = dict(acc)
    out["tokens_total"] = sum(acc[k] for k in _TOKEN_KEYS)
    out.update(extra)
    return out


def _num(v):
    """Coerce a usage field to a non-negative int; anything odd becomes 0."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return 0
    return int(v) if v > 0 else 0


def _cache_path(projects_dir):
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    key = hashlib.sha1(os.path.abspath(projects_dir).encode("utf-8")).hexdigest()[:12]
    return os.path.join(base, "ClaudeUsageMonitor", "engine-cache-{}.json".format(key))


class UsageEngine:
    def __init__(self, projects_dir):
        self.projects_dir = projects_dir
        self._offsets = {}      # path -> byte offset already consumed
        self._seen = set()      # dedup keys
        self._records = []      # list of record dicts
        self._lock = threading.Lock()
        self.last_scan_epoch = None
        self.first_scan_done = False
        self.cache_file = _cache_path(projects_dir)
        self._saved_count = 0

    # ---- persistence ------------------------------------------------------

    def load_cache(self):
        """Restore offsets/records from the last run. Returns True on a hit.

        Any problem at all falls through to a full rescan — the cache is an
        optimization, never a source of truth.
        """
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError):
            return False
        if not isinstance(doc, dict) or doc.get("version") != _CACHE_VERSION:
            return False
        if doc.get("projects_dir") != self.projects_dir:
            return False
        offsets, seen, records = doc.get("offsets"), doc.get("seen"), doc.get("records")
        if not isinstance(offsets, dict) or not isinstance(seen, list) \
                or not isinstance(records, list):
            return False
        with self._lock:
            self._offsets = {k: v for k, v in offsets.items() if isinstance(v, int)}
            self._seen = set(seen)
            self._records = records
            self._saved_count = len(records)
        return True

    def save_cache(self):
        """Write offsets/records so the next launch starts warm. Never raises."""
        with self._lock:
            if len(self._records) == self._saved_count:
                return False
            # Drop offsets for transcripts that no longer exist, so the file
            # doesn't grow a tail of dead paths forever.
            offsets = {p: o for p, o in self._offsets.items() if os.path.exists(p)}
            doc = {
                "version": _CACHE_VERSION,
                "projects_dir": self.projects_dir,
                "saved_epoch": time.time(),
                "offsets": offsets,
                "seen": list(self._seen),
                "records": list(self._records),
            }
            count = len(self._records)
        tmp = self.cache_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, self.cache_file)
        except (OSError, ValueError, TypeError):
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
        with self._lock:
            self._saved_count = count
        return True

    # ---- ingest -----------------------------------------------------------

    def refresh(self):
        pattern = os.path.join(self.projects_dir, "**", "*.jsonl")
        with self._lock:
            try:
                paths = glob.glob(pattern, recursive=True)
            except OSError:
                paths = []
            for path in paths:
                try:
                    self._read_file(path)
                except Exception:
                    # One unreadable / malformed transcript must not abort the scan
                    # (and must not reach the updater thread, which dies on it).
                    continue
            self.last_scan_epoch = datetime.now(timezone.utc).timestamp()
            self.first_scan_done = True

    def _read_file(self, path):
        size = os.path.getsize(path)
        off = self._offsets.get(path, 0)
        if off > size:          # truncated / rotated — start over
            off = 0
        if off >= size:
            return
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
        nl = data.rfind(b"\n")
        if nl == -1:            # no complete line appended yet
            return
        self._offsets[path] = off + nl + 1
        for raw in data[: nl + 1].split(b"\n"):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            try:
                rec = self._parse(obj, path)
            except Exception:
                continue        # malformed record shape — skip the line
            if rec is not None:
                self._records.append(rec)

    def _parse(self, obj, path):
        if not isinstance(obj, dict) or obj.get("type") != "assistant":
            return None
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return None
        usage = msg.get("usage")
        if not isinstance(usage, dict) or not usage:
            return None
        model = msg.get("model") or "unknown"
        if not isinstance(model, str) or model == "<synthetic>":
            return None

        mid, rid = msg.get("id"), obj.get("requestId")
        key = "{}|{}".format(mid, rid) if (mid or rid) else obj.get("uuid", "")
        if not key or not isinstance(key, str) or key in self._seen:
            return None

        ts_raw = obj.get("timestamp")
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            epoch = ts.timestamp()
        except (AttributeError, TypeError, ValueError, OSError, OverflowError):
            return None
        if not (_MIN_EPOCH < epoch < time.time() + _MAX_SKEW):
            return None

        self._seen.add(key)

        cc = usage.get("cache_creation")
        if not isinstance(cc, dict):
            cc = {}
        c5 = cc.get("ephemeral_5m_input_tokens")
        c1 = cc.get("ephemeral_1h_input_tokens")
        if c5 is None and c1 is None:
            # no ephemeral split available — bill the lump as 5-minute cache
            c5 = usage.get("cache_creation_input_tokens")
            c1 = 0
        tokens = {
            "input": _num(usage.get("input_tokens")),
            "output": _num(usage.get("output_tokens")),
            "cache_read": _num(usage.get("cache_read_input_tokens")),
            "cache_write_5m": _num(c5),
            "cache_write_1h": _num(c1),
        }
        stu = usage.get("server_tool_use")
        if not isinstance(stu, dict):
            stu = {}
        cwd = obj.get("cwd")
        cwd = cwd.rstrip("/\\") if isinstance(cwd, str) else ""
        project = os.path.basename(cwd) or os.path.basename(os.path.dirname(path)) or "(unknown)"

        def _str(v):
            return v if isinstance(v, str) else ""

        return {
            "epoch": epoch,
            "model": model,
            "project": project,
            "session": _str(obj.get("sessionId")),
            "branch": _str(obj.get("gitBranch")),
            "tokens": tokens,
            "web_search": _num(stu.get("web_search_requests")),
            "web_fetch": _num(stu.get("web_fetch_requests")),
            "cost": cost_for_record(model, tokens),
        }

    # ---- aggregate --------------------------------------------------------

    def snapshot(self, now=None):
        now = now or datetime.now(timezone.utc)
        now_e = now.timestamp()
        local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_e = local_midnight.timestamp()
        w5_e = now_e - 5 * 3600
        w7_e = now_e - 7 * 24 * 3600
        d30_e = now_e - 30 * 24 * 3600
        h48_e = now_e - 48 * 3600

        today, last5, week, allt = _blank(), _blank(), _blank(), _blank()
        by_model, by_project, by_day, by_session = {}, {}, {}, {}
        hourly = {}
        first5_e = None
        latest = None

        with self._lock:
            recs = list(self._records)

        for r in recs:
            e = r["epoch"]
            _add(allt, r)
            if latest is None or e > latest["epoch"]:
                latest = r
            if e >= midnight_e:
                _add(today, r)
            if e >= w5_e:
                _add(last5, r)
                if first5_e is None or e < first5_e:
                    first5_e = e
            if e >= w7_e:
                _add(week, r)
                s = by_session.get(r["session"])
                if s is None:
                    s = by_session[r["session"]] = _blank()
                    s["project"] = r["project"]
                    s["model"] = r["model"]
                    s["last"] = e
                _add(s, r)
                s["last"] = max(s["last"], e)
                s["model"] = r["model"]
            _add(by_model.setdefault(r["model"], _blank()), r)
            _add(by_project.setdefault(r["project"], _blank()), r)
            if e >= d30_e:
                day = datetime.fromtimestamp(e).strftime("%Y-%m-%d")
                _add(by_day.setdefault(day, _blank()), r)
            if e >= h48_e:
                hk = int(e // 3600 * 3600)
                h = hourly.setdefault(hk, {"cost": 0.0, "tokens": 0})
                h["cost"] += r["cost"]
                h["tokens"] += sum(r["tokens"].values())

        # rolling 5h burn / projection
        if first5_e is not None:
            elapsed_h = max((now_e - first5_e) / 3600.0, 1 / 60.0)
        else:
            elapsed_h = 0.0
        burn = (last5["cost"] / elapsed_h) if elapsed_h else 0.0
        rolling = _serialize(
            last5,
            elapsed_hours=round(elapsed_h, 3),
            burn_cost_per_hour=burn,
            projected_cost=burn * 5.0,
            window_start_epoch=w5_e,
        )

        # 48h hourly series, gap-filled
        series = []
        base = int(now_e // 3600 * 3600)
        for hk in range(base - 47 * 3600, base + 3600, 3600):
            h = hourly.get(hk, {"cost": 0.0, "tokens": 0})
            series.append({"epoch": hk, "cost": h["cost"], "tokens": h["tokens"]})

        idle = (now_e - latest["epoch"]) if latest else None
        active = {
            "active": bool(latest and idle is not None and idle < 300),
            "idle_seconds": int(idle) if idle is not None else None,
            "session": latest["session"] if latest else None,
            "project": latest["project"] if latest else None,
            "model": latest["model"] if latest else None,
            "since_epoch": first5_e,
        }

        def rank(d, key_name):
            items = []
            for name, acc in d.items():
                items.append(_serialize(acc, name=name))
            items.sort(key=lambda x: x["cost"], reverse=True)
            return items

        sessions = []
        for sid, acc in by_session.items():
            sessions.append(_serialize(
                acc, session=sid, project=acc.get("project"),
                model=acc.get("model"), last_epoch=acc.get("last"),
            ))
        sessions.sort(key=lambda x: x.get("last_epoch") or 0, reverse=True)

        days = [_serialize(acc, day=d) for d, acc in sorted(by_day.items())]

        return {
            "meta": {
                "generated_at": now.isoformat(),
                "generated_epoch": now_e,
                "record_count": len(recs),
                "projects_dir": self.projects_dir,
                "models": sorted(by_model.keys()),
                "scanning": not self.first_scan_done,
            },
            "windows": {
                "today": _serialize(today, label="Today"),
                "rolling_5h": {**rolling, "label": "Rolling 5h"},
                "week_7d": _serialize(week, label="Last 7 days"),
                "all": _serialize(allt, label="All time"),
            },
            "by_model": rank(by_model, "model"),
            "by_project": rank(by_project, "project"),
            "by_day": days,
            "hourly_48h": series,
            "sessions": sessions[:12],
            "active": active,
        }
