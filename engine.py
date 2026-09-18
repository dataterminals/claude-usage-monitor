"""Usage engine.

Parses Claude Code transcripts (~/.claude/projects/**/*.jsonl) and Cowork's
(see cowork_sessions_dir), keeps an in-memory list of per-message usage
records, and produces JSON-serializable snapshots aggregated by window / model
/ project / session / time.

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
import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timezone

from pricing import cost_for_record

_TOKEN_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

# Bumped to 2 when pricing.json's Sonnet 5 rate was corrected and the model
# lookup learned to strip dated suffixes: `cost` is baked into every cached
# record, so without a version bump the old numbers would outlive the fix.
_CACHE_VERSION = 2
# Sanity bounds for a transcript timestamp. A bogus epoch (0, or a far-future
# value from a corrupt line) makes datetime.fromtimestamp raise deep inside
# snapshot(), so reject it at parse time instead.
_MIN_EPOCH = 946684800.0                    # 2000-01-01
_MAX_SKEW = 366 * 24 * 3600.0               # a year ahead of now
# The "right now" rates (see _velocity): what landed in the last five, fifteen
# and thirty minutes, each as an hourly figure. Three widths rather than one
# because the shape is the information — five reading above thirty means you
# are speeding up, five below it means you are easing off, and a single number
# can say neither. The five-minute box is the "right now" one and leads.
_VELOCITY_WINDOWS = (300.0, 900.0, 1800.0)
_VELOCITY_HOURS = 48

# Where transcripts are. Cowork — the desktop app's agent mode — runs the same
# Claude Code runtime, so its transcripts have exactly this shape, but it files
# them under its own tree: one `.claude/projects` per session, inside
# %APPDATA%\Claude\local-agent-mode-sessions, and nothing under ~/.claude ever
# points at them. Verified 2026-09-18: a week in which the weekly gauge rose in
# 37 hours, 16 of them with no transcript spend at all. Chat is the rest of
# that gap and has no transcript anywhere — it is claude.ai in a web view —
# so only the live gauges see it (pacing.gauge_velocity is the answer there).
_TRANSCRIPT_MARK = os.sep + ".claude" + os.sep + "projects" + os.sep


def code_projects_dir():
    return os.path.expanduser("~/.claude/projects")


def cowork_sessions_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, "Claude", "local-agent-mode-sessions")


def _transcripts(root, nested):
    """Every transcript under `root`, in one walk.

    Not glob: `**` skips dot-directories, and Cowork keeps its transcripts
    under a `.claude` one, so `glob("**/*.jsonl")` over that tree finds
    nothing at all — measured, zero of 56. With `nested` the walk descends
    into a `.claude` only for its `projects`, and keeps only files under one:
    a session's `audit.jsonl` and whatever lands in its `outputs` are not
    transcripts. The whole Cowork tree walks in ~60 ms; pruned, well under.
    A root that does not exist yields nothing.
    """
    for dp, dn, fn in os.walk(root):
        if nested:
            if os.path.basename(dp) == ".claude":
                dn[:] = [d for d in dn if d == "projects"]
            if _TRANSCRIPT_MARK not in dp + os.sep:
                continue
        for f in fn:
            if f.endswith(".jsonl"):
                yield os.path.join(dp, f)


def _cowork_label(meta):
    """A Cowork session's name for the by-project table.

    The transcript's own `cwd` is the session's `outputs` folder, every time,
    so the label comes from the sidecar `<session>.json` the app keeps beside
    the session directory: its `title` ("Dropbox connector setup"). The
    Chat-side agent session has none, only a `sessionType`.
    """
    meta = meta if isinstance(meta, dict) else {}
    title = meta.get("title")
    if isinstance(title, str) and title.strip():
        return "Cowork \u00b7 " + title.strip()
    kind = meta.get("sessionType")
    return "Cowork \u00b7 " + (kind if isinstance(kind, str) and kind else "untitled")


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


def _velocity(recent, now_e, window=_VELOCITY_WINDOWS[0]):
    """The rate right now, as distinct from the block's average.

    `rolling_5h.burn_cost_per_hour` divides the block's spend by the block's
    age, which is the right number for "what will these five hours cost" and
    the wrong one for "how fast am I going": an hour after the last request it
    has barely moved, because its numerator is frozen while its denominator
    grows a minute per minute. This is the other number: what landed in the
    last `window` seconds — five, fifteen or thirty minutes — as an hourly
    rate. In the five-minute box a lone $2 request reads as $24/h the moment it
    lands and for the five minutes after, then drops out; steady spending at
    $R/h reads R once the box is full, whatever the box's width; a box-length
    after the last request the reading is zero. A plain box rather than a
    decaying kernel because it says exactly what it measures.

    Also the peak of that reading across the 48-hour window, so a gauge has a
    scale that is your own fastest stretch of that width rather than a magic
    number. A wider box can never peak higher than a narrower one, since its
    rate is an average of the narrower boxes inside it. The
    reading only rises when a request lands and falls as older ones age out,
    so its maximum sits at a request time, and one ordered pass with a sliding
    box finds it. `recent` is (epoch, cost, tokens) tuples in any order.
    """
    recent = sorted(recent, key=lambda x: x[0])
    per_h = 3600.0 / window
    cost_sum = tok_sum = peak = 0.0
    peak_at = None
    head = 0                        # oldest record still inside the box
    for e, cost, tok in recent:
        cost_sum += cost
        tok_sum += tok
        while recent[head][0] <= e - window:
            cost_sum -= recent[head][1]
            tok_sum -= recent[head][2]
            head += 1
        if cost_sum > peak:
            peak, peak_at = cost_sum, e
    # The reading now, summed afresh over the whole list: `head` was advanced
    # relative to the *last record*, and a record stamped after "now" (clock
    # skew) would have carried it past requests still inside now's box. That
    # skewed record itself counts as just landed.
    cost_now = tok_now = 0.0
    for e, cost, tok in recent:
        if e > now_e - window:
            cost_now += cost
            tok_now += tok
    return {
        "cost_per_hour": cost_now * per_h,
        "tokens_per_hour": tok_now * per_h,
        "peak_cost_per_hour": peak * per_h,
        "peak_epoch": peak_at,
        "window_seconds": window,
        "window_hours": _VELOCITY_HOURS,
    }


def _cache_path(projects_dir):
    base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    key = hashlib.sha1(os.path.abspath(projects_dir).encode("utf-8")).hexdigest()[:12]
    return os.path.join(base, "ClaudeUsageMonitor", "engine-cache-{}.json".format(key))


def _why(exc):
    return "{}: {}".format(type(exc).__name__, exc)


class UsageEngine:
    def __init__(self, projects_dir, cowork_dir=None):
        self.projects_dir = projects_dir
        self.cowork_dir = cowork_dir
        self._offsets = {}      # path -> byte offset already consumed
        self._seen = set()      # dedup keys
        self._records = []      # list of record dicts
        self._labels = {}       # cowork session key -> (sidecar mtime, label)
        self._lock = threading.Lock()
        self.last_scan_epoch = None
        self.first_scan_done = False
        self.cache_file = _cache_path(projects_dir)
        self._saved_count = 0
        # What the cache calls actually did, for the tray's GET /health. Both
        # calls return a bare bool that every caller ignores, so without these a
        # save that never lands is indistinguishable from one that does.
        # last_save is one (epoch, result) tuple, replaced whole, so a reader on
        # an HTTP thread can't pair one call's time with another call's result.
        self.load_result = None     # "hit: …" / "miss: …"; None = never tried
        self.last_save = None       # (epoch, "saved …" / "unchanged …" / "error: …")
        self.last_written = None    # epoch of the last write that landed

    # ---- persistence ------------------------------------------------------

    def _miss(self, why):
        self.load_result = "miss: " + why
        return False

    def load_cache(self):
        """Restore offsets/records from the last run. Returns True on a hit.

        Any problem at all falls through to a full rescan — the cache is an
        optimization, never a source of truth.
        """
        try:
            with open(self.cache_file, encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as exc:
            return self._miss(_why(exc))
        if not isinstance(doc, dict) or doc.get("version") != _CACHE_VERSION:
            return self._miss("version {!r}, want {}".format(
                doc.get("version") if isinstance(doc, dict) else None, _CACHE_VERSION))
        if doc.get("projects_dir") != self.projects_dir:
            return self._miss("written for another projects_dir")
        # A cache from before the Cowork root has no `cowork_dir` at all: take
        # it, and the Cowork transcripts simply have no offsets yet, so the
        # first refresh reads them (23 MB, seconds) instead of everything
        # (a full rescan is over a minute). A cache for a *different* Cowork
        # root would carry that root's records, so that one is a miss.
        if doc.get("cowork_dir", self.cowork_dir) != self.cowork_dir:
            return self._miss("written for another cowork_dir")
        offsets, seen, records = doc.get("offsets"), doc.get("seen"), doc.get("records")
        if not isinstance(offsets, dict) or not isinstance(seen, list) \
                or not isinstance(records, list):
            return self._miss("malformed")
        labels = doc.get("labels")
        if not isinstance(labels, dict):
            labels = {}
        for r in records:
            if isinstance(r, dict):
                r.setdefault("source", "code")    # pre-Cowork cache
        with self._lock:
            self._offsets = {k: v for k, v in offsets.items() if isinstance(v, int)}
            self._seen = set(seen)
            self._records = records
            self._saved_count = len(records)
            # mtime None: re-read the sidecars on the first refresh, but the
            # names are right from the first snapshot rather than a tick later.
            self._labels = {k: (None, v) for k, v in labels.items() if isinstance(v, str)}
        self.load_result = "hit: {} records".format(len(records))
        return True

    def save_cache(self):
        """Write offsets/records so the next launch starts warm. Never raises."""
        with self._lock:
            if len(self._records) == self._saved_count:
                self.last_save = (time.time(), "unchanged: {} records".format(self._saved_count))
                return False
            # Drop offsets for transcripts that no longer exist, so the file
            # doesn't grow a tail of dead paths forever.
            offsets = {p: o for p, o in self._offsets.items() if os.path.exists(p)}
            doc = {
                "version": _CACHE_VERSION,
                "projects_dir": self.projects_dir,
                "cowork_dir": self.cowork_dir,
                "saved_epoch": time.time(),
                "offsets": offsets,
                "seen": list(self._seen),
                "records": list(self._records),
                "labels": {k: v[1] for k, v in self._labels.items()},
            }
            count = len(self._records)
        tmp = self.cache_file + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(doc, f, separators=(",", ":"))
            os.replace(tmp, self.cache_file)
        except (OSError, ValueError, TypeError) as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.last_save = (time.time(), "error: " + _why(exc))
            return False
        with self._lock:
            self._saved_count = count
        self.last_written = time.time()
        self.last_save = (self.last_written, "saved {} records".format(count))
        return True

    # ---- ingest -----------------------------------------------------------

    def roots(self):
        """(root, source) pairs, the Cowork one only when configured."""
        out = [(self.projects_dir, "code")]
        if self.cowork_dir:
            out.append((self.cowork_dir, "cowork"))
        return out

    def sources(self):
        """Per source: its root, whether it is there, and the transcripts
        read so far. For GET /health, so "is Cowork being counted?" has an
        answer that does not involve reading the cache off disk."""
        with self._lock:
            paths = list(self._offsets)
        out = {}
        for root, source in self.roots():
            prefix = os.path.join(root, "")
            out[source] = {
                "dir": root,
                "exists": os.path.isdir(root),
                "files": sum(1 for p in paths if p.startswith(prefix)),
            }
        return out

    def refresh(self):
        with self._lock:
            sessions = set()
            for root, source in self.roots():
                try:
                    paths = list(_transcripts(root, nested=(source == "cowork")))
                except OSError:
                    paths = []
                for path in paths:
                    if source == "cowork":
                        sessions.add(path.split(_TRANSCRIPT_MARK, 1)[0])
                    try:
                        self._read_file(path, source)
                    except Exception:
                        # One unreadable / malformed transcript must not abort the scan
                        # (and must not reach the updater thread, which dies on it).
                        continue
            self._relabel(sessions)
            self.last_scan_epoch = datetime.now(timezone.utc).timestamp()
            self.first_scan_done = True

    def _relabel(self, session_dirs):
        """Refresh the Cowork session names from their sidecars.

        Read at snapshot time rather than baked into each record, because the
        app retitles a session after its first exchange (the transcript logs
        an `ai-title` event) and the records parsed before that would keep
        the placeholder forever. One stat per session per pass; the file is
        re-read only when its mtime moves.
        """
        for sd in session_dirs:
            key = os.path.basename(sd)
            sidecar = sd + ".json"
            try:
                mtime = os.path.getmtime(sidecar)
            except OSError:
                mtime = None
            cur = self._labels.get(key)
            if cur is not None and cur[0] == mtime:
                continue
            meta = None
            if mtime is not None:
                try:
                    with open(sidecar, encoding="utf-8") as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    meta = None
            self._labels[key] = (mtime, _cowork_label(meta))

    def _read_file(self, path, source="code"):
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
                rec = self._parse(obj, path, source)
            except Exception:
                continue        # malformed record shape — skip the line
            if rec is not None:
                self._records.append(rec)

    def _parse(self, obj, path, source="code"):
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
        if source == "cowork":
            # The session directory's name; snapshot() swaps in the title.
            # The record's own cwd is the session's `outputs` folder, always.
            project = os.path.basename(path.split(_TRANSCRIPT_MARK, 1)[0]) or "(cowork)"
        else:
            cwd = obj.get("cwd")
            cwd = cwd.rstrip("/\\") if isinstance(cwd, str) else ""
            project = os.path.basename(cwd) or os.path.basename(os.path.dirname(path)) or "(unknown)"

        def _str(v):
            return v if isinstance(v, str) else ""

        return {
            "epoch": epoch,
            "model": model,
            "project": project,
            "source": source,
            "session": _str(obj.get("sessionId")),
            "branch": _str(obj.get("gitBranch")),
            "tokens": tokens,
            "web_search": _num(stu.get("web_search_requests")),
            "web_fetch": _num(stu.get("web_fetch_requests")),
            "cost": cost_for_record(model, tokens),
        }

    # ---- aggregate --------------------------------------------------------

    def snapshot(self, now=None, five_hour_start=None):
        """Aggregate the records. `five_hour_start` is the plan's real 5-hour
        block start (quota's `resets_at - 5h`) when it's known.

        Without it the 5-hour view is a trailing window whose burn rate divides
        by the time since the *first record in it*, which is wrong twice over: a
        burst that just began divides by minutes and projects an absurd figure
        (measured: 0.28h elapsed -> $93 projected on a window actually 2.08h
        old), and the trailing window straddles the previous block, counting
        spend the live gauge has already reset past. Given the anchor, both the
        window and the divisor come off the real block and the two halves of the
        dashboard finally describe the same five hours.
        """
        now = now or datetime.now(timezone.utc)
        now_e = now.timestamp()
        local_midnight = now.astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        midnight_e = local_midnight.timestamp()
        w5_e = now_e - 5 * 3600
        anchored = bool(five_hour_start) and w5_e <= five_hour_start <= now_e
        if anchored:
            w5_e = float(five_hour_start)
        w7_e = now_e - 7 * 24 * 3600
        d30_e = now_e - 30 * 24 * 3600
        h48_e = now_e - _VELOCITY_HOURS * 3600

        today, last5, week, allt = _blank(), _blank(), _blank(), _blank()
        by_model, by_project, by_day, by_session = {}, {}, {}, {}
        by_source = {}          # 7-day window: how much of it was Cowork
        n_source = {}           # all-time record count per source
        hourly = {}
        recent = []             # (epoch, cost, tokens) within 48h, for _velocity
        first5_e = None
        latest = None
        latest_proj = None

        with self._lock:
            recs = list(self._records)
            labels = {k: v[1] for k, v in self._labels.items()}

        for r in recs:
            e = r["epoch"]
            src = r.get("source", "code")
            proj = r["project"]
            if src == "cowork":
                # Unlabelled means the sidecar has not been read yet (or was
                # never there); one shared bucket beats a raw session id.
                proj = labels.get(proj) or "Cowork \u00b7 session"
            n_source[src] = n_source.get(src, 0) + 1
            _add(allt, r)
            if latest is None or e > latest["epoch"]:
                latest, latest_proj = r, proj
            if e >= midnight_e:
                _add(today, r)
            if e >= w5_e:
                _add(last5, r)
                if first5_e is None or e < first5_e:
                    first5_e = e
            if e >= w7_e:
                _add(week, r)
                _add(by_source.setdefault(src, _blank()), r)
                s = by_session.get(r["session"])
                if s is None:
                    s = by_session[r["session"]] = _blank()
                    s["project"] = proj
                    s["model"] = r["model"]
                    s["last"] = e
                _add(s, r)
                # Records arrive in glob order, not time order, so "the model
                # this session is on" has to be the latest by timestamp — set
                # unconditionally it was just whichever file was walked last.
                if e >= s["last"]:
                    s["model"] = r["model"]
                s["last"] = max(s["last"], e)
            _add(by_model.setdefault(r["model"], _blank()), r)
            _add(by_project.setdefault(proj, _blank()), r)
            if e >= d30_e:
                day = datetime.fromtimestamp(e).strftime("%Y-%m-%d")
                _add(by_day.setdefault(day, _blank()), r)
            if e >= h48_e:
                tk = sum(r["tokens"].values())
                hk = int(e // 3600 * 3600)
                h = hourly.setdefault(hk, {"cost": 0.0, "tokens": 0})
                h["cost"] += r["cost"]
                h["tokens"] += tk
                recent.append((e, r["cost"], tk))

        # 5h burn / projection
        if anchored:
            elapsed_h = max((now_e - w5_e) / 3600.0, 1 / 60.0)
        elif first5_e is not None:
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
            anchored=anchored,
        )

        # 48h hourly series, gap-filled
        series = []
        base = int(now_e // 3600 * 3600)
        for hk in range(base - 47 * 3600, base + 3600, 3600):
            h = hourly.get(hk, {"cost": 0.0, "tokens": 0})
            series.append({"epoch": hk, "cost": h["cost"], "tokens": h["tokens"]})

        # One entry per width, narrowest first, so the dashboard can draw a dial
        # each. Sorted once here rather than three times: _velocity sorts what it
        # is given, and sorting an already-sorted list is linear.
        recent.sort(key=lambda r: r[0])
        velocity = [_velocity(recent, now_e, w) for w in _VELOCITY_WINDOWS]

        idle = (now_e - latest["epoch"]) if latest else None
        active = {
            "active": bool(latest and idle is not None and idle < 300),
            "idle_seconds": int(idle) if idle is not None else None,
            "session": latest["session"] if latest else None,
            "project": latest_proj,
            "source": latest.get("source", "code") if latest else None,
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
                "cowork_dir": self.cowork_dir,
                # What each root has contributed, so the dashboard can say
                # "Cowork: 1,957 messages" rather than leave you to infer it.
                "sources": {source: {"dir": root, "records": n_source.get(source, 0)}
                            for root, source in self.roots()},
                "models": sorted(by_model.keys()),
                "scanning": not self.first_scan_done,
            },
            "windows": {
                "today": _serialize(today, label="Today"),
                "rolling_5h": {**rolling,
                               "label": "Current 5h block" if anchored else "Rolling 5h"},
                "week_7d": _serialize(week, label="Last 7 days"),
                "all": _serialize(allt, label="All time"),
            },
            "by_model": rank(by_model, "model"),
            "by_project": rank(by_project, "project"),
            "by_source": rank(by_source, "source"),
            "by_day": days,
            "hourly_48h": series,
            "velocity": velocity,
            "sessions": sessions[:12],
            "active": active,
        }
