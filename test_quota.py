"""Self-check for quota.py's token-refresh backoff.  Run:  python test_quota.py

No test framework and no dependencies, same as test_pacing.py: every check
prints, and the exit code is non-zero if any failed.

Offline by construction, because a refresh token is single-use and shared with
Claude Code: one stray real POST can sign both of them out. Before quota is
imported the home directory points at a throwaway temp dir, and quota's
credentials paths are then pinned inside it and verified. urlopen is a scripted
fake that raises on any request it wasn't told to expect, and socket connects
raise as a backstop. quota's clock is fake too, so an hour of 5-second polling
takes milliseconds.
"""
import atexit
import email.message
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import time as real_time
import urllib.error
import urllib.request

TMP = tempfile.mkdtemp(prefix="test_quota_")
atexit.register(shutil.rmtree, TMP, True)
os.environ["USERPROFILE"] = TMP     # what expanduser("~") reads on Windows
os.environ["HOME"] = TMP            # ...and everywhere else


def _offline(*args, **kwargs):
    raise RuntimeError("test_quota.py tried to open a real network connection")


socket.create_connection = _offline
socket.socket.connect = _offline
socket.socket.connect_ex = _offline

import quota  # noqa: E402  (only after the redirects above)

quota.CREDENTIALS = os.path.join(TMP, ".claude", ".credentials.json")
quota.BACKUP = quota.CREDENTIALS + ".bak"
for _path in (quota.CREDENTIALS, quota.BACKUP):
    if os.path.normcase(os.path.commonpath([_path, TMP])) != os.path.normcase(TMP):
        sys.exit("refusing to run: %s is outside the temp dir" % _path)
os.makedirs(os.path.dirname(quota.CREDENTIALS))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")   # reasons carry an em dash


fails = []


def check(name, got, want, tol=1e-6):
    if isinstance(want, float) and isinstance(got, (int, float)) and not isinstance(got, bool):
        ok = abs(got - want) <= tol
    else:
        ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


# ---- fakes ----------------------------------------------------------------

class Clock:
    """Stands in for the `time` module inside quota: time() is whatever we set."""

    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def __getattr__(self, name):
        return getattr(real_time, name)


clock = Clock(1789650000.0)
quota.time = clock


class Reply(io.BytesIO):
    """A 200 from urlopen, usable as a context manager like the real one."""

    def __init__(self, payload):
        super().__init__(json.dumps(payload).encode("utf-8"))
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def answer(payload):
    return lambda req: Reply(payload)


def refuse(code, retry_after=None, body=None):
    def reply(req):
        headers = email.message.Message()
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        raise urllib.error.HTTPError(req.full_url, code, "refused", headers,
                                     io.BytesIO(json.dumps(body or {}).encode("utf-8")))
    return reply


def unreachable(req):
    raise urllib.error.URLError("network is unreachable")


class Endpoints:
    """Scripted token and usage endpoints. A script is a list of replies taken in
    order, the last one repeating. Any other URL, or an endpoint with no script,
    raises: nothing may fall through to a real request."""

    def __init__(self):
        self.reset()

    def reset(self, token=(), usage=()):
        self.scripts = {quota.TOKEN_URL: list(token), quota.USAGE_URL: list(usage)}
        self.hits = {quota.TOKEN_URL: 0, quota.USAGE_URL: 0}

    def urlopen(self, req, timeout=None, context=None):
        url = req.full_url
        if not self.scripts.get(url):
            raise RuntimeError("unscripted request to " + url)
        self.hits[url] += 1
        script = self.scripts[url]
        return (script.pop(0) if len(script) > 1 else script[0])(req)

    @property
    def token_posts(self):
        return self.hits[quota.TOKEN_URL]

    @property
    def usage_gets(self):
        return self.hits[quota.USAGE_URL]


net = Endpoints()
urllib.request.urlopen = net.urlopen

_stamp = [1700000000]


def write_creds(access="access-1", refresh="refresh-1", valid_for=-3600):
    """A throwaway credentials file whose access token expires `valid_for` seconds
    from the fake now (negative: already expired). Every write gets its own
    mtime, so quota's rewrite detection sees it even within one clock tick."""
    doc = {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int((clock.now + valid_for) * 1000),
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
    }}
    with open(quota.CREDENTIALS, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    _stamp[0] += 1
    os.utime(quota.CREDENTIALS, (_stamp[0], _stamp[0]))


def saved_creds():
    with open(quota.CREDENTIALS, encoding="utf-8") as f:
        return json.load(f)["claudeAiOauth"]


def scenario(token=(), usage=(), **creds):
    """Start clean: nothing cached, no refusal streak, no backup, new credentials."""
    quota._cache.update(epoch=0.0, data=None, ttl=0.0, creds_mtime=None)
    quota._refusals.update(creds_mtime=None, count=0)
    if os.path.exists(quota.BACKUP):
        os.remove(quota.BACKUP)
    net.reset(token=token, usage=usage)
    write_creds(**creds)


def poll():
    """One tray pass with auto-refresh on, the tray's default."""
    return quota.fetch(allow_refresh=True)


def held():
    """How long quota will sit on what it just stored."""
    return quota._cache["ttl"]


RATE_LIMITED = {"error": {"type": "rate_limit_error",
                          "message": "Rate limited. Please try again later."}}
RATE_MSG = "HTTP 429: Rate limited. Please try again later."
REVOKED = {"error": "invalid_grant", "error_description": "refresh token revoked"}
USAGE = {"five_hour": {"utilization": 12.0, "resets_at": "2026-09-17T15:00:00+00:00"},
         "seven_day": {"utilization": 30.0, "resets_at": "2026-09-20T08:00:00+00:00"}}
TOKEN = {"access_token": "access-2", "refresh_token": "refresh-2",
         "expires_in": 28800, "token_type": "Bearer"}

print("credentials under test:", quota.CREDENTIALS)

# ---- checks ---------------------------------------------------------------

print("\n[1] expired token, refresh answered 429 with Retry-After: 900")
scenario(token=[refuse(429, retry_after=900, body=RATE_LIMITED)])
start = clock.now
r = poll()
print("     reason:", r.get("reason"))
check("unavailable", r.get("available"), False)
check("held for Retry-After", held(), 900.0)
check("reason says when it retries", "retrying in ~15m" in r.get("reason", ""), True)
for t in range(5, 900, 5):
    clock.now = start + t
    poll()
check("no POST inside the window (179 polls)", net.token_posts, 1)
clock.now = start + 900
poll()
check("one POST when the window ends", net.token_posts, 2)
check("never asks for usage with a dead token", net.usage_gets, 0)

print("\n[2] Retry-After is clamped exactly as on the usage endpoint")
for label, ra, want in (("absent", None, 300.0),
                        ("30s", 30, 300.0),
                        ("20m", 1200, 1200.0),
                        ("a day", 86400, 3600.0),
                        ("HTTP-date", "Thu, 17 Sep 2026 12:00:00 GMT", 300.0)):
    scenario(token=[refuse(429, retry_after=ra, body=RATE_LIMITED)])
    poll()
    refresh_wait = held()
    scenario(usage=[refuse(429, retry_after=ra)], valid_for=3600)
    poll()
    usage_wait = held()
    check("%-9s refresh waits" % label, refresh_wait, want)
    check("%-9s usage waits the same" % label, usage_wait, refresh_wait)

print("\n[3] RefreshError carries what the endpoint said")


def refresh_error(reply, oauth=None):
    net.reset(token=[reply])
    try:
        quota._refresh(oauth or {"accessToken": "a", "refreshToken": "r"})
    except quota.RefreshError as exc:
        return exc
    return None


err = refresh_error(refuse(429, retry_after=120, body=RATE_LIMITED))
check("429: status", getattr(err, "status", None), 429)
check("429: retry_after", getattr(err, "retry_after", None), 120)
check("429: message unchanged", str(err), RATE_MSG)
err = refresh_error(refuse(400, body=REVOKED))
check("400: status", getattr(err, "status", None), 400)
check("400: no retry_after", getattr(err, "retry_after", "missing"), None)
err = refresh_error(unreachable)
check("network: no status", getattr(err, "status", "missing"), None)
err = refresh_error(unreachable, oauth={"accessToken": "a"})
check("no refresh token: no status, nothing sent",
      (getattr(err, "status", "missing"), net.token_posts), (None, 0))

print("\n[4] other refusals double the wait: 1, 2, 4, 8, then 15 minutes at most")
scenario(token=[refuse(400, body=REVOKED)])
waits = []
for i in range(7):
    r = poll()
    waits.append(held())
    if i == 0:
        print("     reason:", r.get("reason"))
        clock.now += held() - 1
        poll()
        check("no retry a second before the first wait ends", net.token_posts, 1)
        clock.now += 1
    else:
        clock.now += held()
check("waits", waits, [60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 900.0])
check("one POST per wait", net.token_posts, 7)
check("reason keeps the endpoint's words", "HTTP 400: refresh token revoked" in r.get("reason", ""), True)

print("\n[5] failures that never reach the endpoint keep the flat minute")
scenario(token=[unreachable])
waits = []
for _ in range(4):
    poll()
    waits.append(held())
    clock.now += held()
check("waits", waits, [60.0, 60.0, 60.0, 60.0])

print("\n[6] a credentials rewrite is picked up on the next poll and starts over")
scenario(token=[refuse(400, body=REVOKED)])
for _ in range(3):
    poll()
    clock.now += held()
poll()
check("escalated to 8 minutes", held(), 480.0)
posts = net.token_posts
clock.now += 10
write_creds(access="access-3", refresh="refresh-3", valid_for=-60)     # rewritten, still expired
poll()
check("rewrite retried without waiting out the 8 minutes", net.token_posts, posts + 1)
check("and the doubling started over", held(), 60.0)
clock.now += 10
net.scripts[quota.USAGE_URL] = [answer(USAGE)]
write_creds(access="access-4", refresh="refresh-4", valid_for=8 * 3600)   # what /login leaves
r = poll()
check("a fresh login connects on the next poll", r.get("available"), True)
check("without spending the refresh token", net.token_posts, posts + 1)

print("\n[7] a refresh that works connects and clears the count")
scenario(token=[refuse(400, body=REVOKED), refuse(400, body=REVOKED), answer(TOKEN),
                refuse(400, body=REVOKED)],
         usage=[answer(USAGE)])
for _ in range(2):
    poll()
    clock.now += held()
r = poll()
check("connected", r.get("available"), True)
check("marked refreshed", r.get("refreshed"), True)
creds = saved_creds()
check("new access token written to the temp file", creds.get("accessToken"), "access-2")
check("rotated refresh token written", creds.get("refreshToken"), "refresh-2")
check("expiresAt from expires_in", creds.get("expiresAt"), int(clock.now * 1000 + 28800 * 1000))
check("backup taken before the first write", os.path.exists(quota.BACKUP), True)
clock.now += 8 * 3600
poll()
check("the next refusal starts again at 1 minute", held(), 60.0)

print("\n[8] Attempt token refresh still tries at once, even mid-wait")
scenario(token=[refuse(429, retry_after=900, body=RATE_LIMITED)])
poll()
clock.now += 30
r = quota.fetch(force=True, allow_refresh=True, force_refresh_token=True)
check("POSTed immediately", net.token_posts, 2)
check("failure text unchanged", r.get("reason"), "token refresh failed (%s)" % RATE_MSG)
check("still a zero TTL, so good gauges are never evicted", held(), 0.0)
net.scripts[quota.TOKEN_URL] = [answer(TOKEN)]
net.scripts[quota.USAGE_URL] = [answer(USAGE)]
clock.now += 5
r = quota.fetch(force=True, allow_refresh=True, force_refresh_token=True)
check("and connects when it works", r.get("available"), True)

print("\n[9] a 401 from the usage endpoint refreshes under the same backoff")
scenario(token=[refuse(429, retry_after=600, body=RATE_LIMITED)], usage=[refuse(401)],
         valid_for=3600)
r = poll()
check("reason", r.get("reason"),
      "token rejected (401); refresh failed (%s) — retrying in ~10m" % RATE_MSG)
check("held for Retry-After", held(), 600.0)
clock.now += 300
poll()
check("no second POST inside the window", net.token_posts, 1)
check("no second usage call either", net.usage_gets, 1)

print("\n[10] the usage endpoint's own 429 reads as before")
scenario(usage=[refuse(429, retry_after=120)], valid_for=3600)
r = poll()
check("reason", r.get("reason"), "rate-limited by the usage endpoint — retry in ~5m")
check("waits 5 minutes", held(), 300.0)
check("no refresh for a live token", net.token_posts, 0)

print("\n[11] an hour of 5-second polls with an expired token (was 60 tries each)")
for label, reply, want in (("429, no Retry-After", refuse(429, body=RATE_LIMITED), 12),
                           ("429, Retry-After 900", refuse(429, retry_after=900, body=RATE_LIMITED), 4),
                           ("400 invalid_grant", refuse(400, body=REVOKED), 7),
                           ("network down", unreachable, 60)):
    scenario(token=[reply])
    start = clock.now
    while clock.now < start + 3600:
        poll()
        clock.now += 5
    check("%-21s tries" % label, net.token_posts, want)

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
