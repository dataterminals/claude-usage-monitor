"""Experimental live plan-quota reader.

Hits the same endpoint Claude Code's `/usage` command uses to fetch the
5-hour / weekly rate-limit bars. Endpoint + headers + refresh flow were
reverse-engineered from the installed Claude Code binary.

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <accessToken>
    anthropic-beta: oauth-2025-04-20

Response: { five_hour, seven_day, seven_day_sonnet, seven_day_opus }, each a
{ "utilization": 0-100, "resets_at": ISO8601|null } (or absent/null), plus a
`limits` array that is the newer shape for the same thing. The two flat weekly
per-model keys read null on a current Max plan; the model-scoped cap now lives
in that array as {kind: "weekly_scoped", scope: {model: {display_name}}, ...},
so _normalize() reads both and emits `seven_day_scoped_<model>` for the latter.

The response carries more than this module surfaces — `extra_usage`/`spend`
(overage credits), `seven_day_breakdown` (which surface spent the week), and
several codenamed windows. `raw` is returned untouched so a caller can reach
them without another round trip.

READ-ONLY BY DEFAULT: this only reads your credentials and never writes them
unless the caller passes allow_refresh=True. A token refresh rotates the
refresh token, so the file is backed up to `.credentials.json.bak` before the
first write.

Staleness: the cache is keyed on the credentials file's mtime as well as a TTL,
so when Claude Code (or `claude /login`) rewrites the token, the next poll picks
it up immediately instead of serving a stale failure for the rest of the TTL.

This is UNDOCUMENTED and may break on a Claude Code update. All failures
degrade gracefully to {available: false}; it never raises to the caller.
"""
import json
import os
import shutil
import ssl
import threading
import time
import urllib.error
import urllib.request

CREDENTIALS = os.path.expanduser("~/.claude/.credentials.json")
BACKUP = CREDENTIALS + ".bak"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
# console.anthropic.com no longer routes this path — it returns a hard 404 for
# every request shape. The host was renamed: Claude Code 2.1.220 ships
# platform.claude.com/v1/oauth/token and does not mention the old host at all.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
USER_AGENT = "claude-code/2.1.220"
BETA = "oauth-2025-04-20"

# No `scope` on a refresh_token grant. RFC 6749 §6 makes it optional and reads
# an omitted scope as "identical to the original grant" — which is what we want,
# because the credentials file carries five scopes (file_upload and mcp_servers
# included) and naming a subset here would write a *down-scoped* token back over
# Claude Code's own credentials.

_lock = threading.RLock()
_cache = {"epoch": 0.0, "data": None, "ttl": 0.0, "creds_mtime": None}

# Why we don't trust the Windows certificate store for this.
#
# The two endpoints sit behind different CAs: api.anthropic.com chains through
# Google Trust Services, platform.claude.com through Let's Encrypt
# (leaf -> E7 -> ISRG Root X1). Python's ssl.create_default_context() calls
# load_default_certs(), which pulls every cert Windows has cached in
# CurrentUser\CA in as a *trust anchor* — expired ones included. This machine
# had an ISRG Root X2 there that expired 2025-09-15, so OpenSSL anchored on it
# and aborted the Let's Encrypt chain with "certificate has expired". The usage
# GET kept working and every single refresh POST died before a byte went out,
# which is exactly the shape of the bug this module spent months not finding:
# the request was never sent, so the refresh token was never spent, so
# _write_creds() never ran and .credentials.json.bak was never created.
#
# Measured: with the system store, platform.claude.com and console.anthropic.com
# both fail "certificate has expired" while api.anthropic.com is fine; with
# certifi's bundle all three negotiate TLSv1.3. Claude Code itself never had the
# problem because Bun ships its own roots rather than reading the Windows store.
#
# certifi is optional — without it we fall back to the default context and are
# no worse off than before.
_ssl_ctx = None


def _context():
    """A verifying SSL context that doesn't inherit the Windows store's stale
    intermediates. Built once; falls back to the default on any failure."""
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


_TTL_OK = 45.0      # re-fetch a good result at most this often
_TTL_NET = 10.0     # transient (DNS/socket): the network is usually just not up
                    # yet at login — a long penalty box here is what made the
                    # app look dead for minutes after boot.
_TTL_AUTH = 60.0    # token problem; an mtime change re-checks sooner anyway
_TTL_FAIL = 300.0   # server told us to back off (429)
_TTL_REFRESH_MAX = 900.0    # ceiling for the doubling wait after refused refreshes


class RefreshError(Exception):
    """A token refresh failed. Carries a human-readable reason and, when the
    token endpoint answered with an HTTP error, its `status` and Retry-After
    seconds (`retry_after`). Both stay None when no error response came back:
    the network or TLS failed, the answer was unusable, or there was no refresh
    token to send."""

    def __init__(self, reason, status=None, retry_after=None):
        super().__init__(reason)
        self.status = status
        self.retry_after = retry_after


def _creds_mtime():
    try:
        return os.path.getmtime(CREDENTIALS)
    except OSError:
        return None


def _read_creds():
    with open(CREDENTIALS, encoding="utf-8") as f:
        doc = json.load(f)
    return (doc.get("claudeAiOauth") or {}) if isinstance(doc, dict) else {}


def _write_creds(oauth):
    with open(CREDENTIALS, encoding="utf-8") as f:
        doc = json.load(f)
    # One-time safety net: rotating a refresh token is the one operation here
    # that can lock you out of Claude Code, so keep the last known-good file.
    if not os.path.exists(BACKUP):
        try:
            shutil.copyfile(CREDENTIALS, BACKUP)
        except OSError:
            pass
    doc["claudeAiOauth"] = oauth
    tmp = CREDENTIALS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, CREDENTIALS)


def _http_detail(exc):
    """Best-effort human reason out of an HTTPError body.

    The token endpoint answers in two shapes: OAuth's {"error", "error_description"}
    and Anthropic's {"error": {"type", "message"}}.
    """
    detail = None
    try:
        body = json.loads(exc.read())
        err = body.get("error")
        if isinstance(err, dict):
            detail = err.get("message") or err.get("type")
        else:
            detail = body.get("error_description") or err
    except Exception:
        pass
    return "HTTP {}{}".format(exc.code, ": " + str(detail) if detail else "")


def _retry_after(headers):
    """Retry-After in whole seconds, or None when it is absent, not positive, or
    in the HTTP-date form — all of which _rate_limit_ttl reads as _TTL_FAIL."""
    try:
        secs = int(headers.get("retry-after") or 0)
    except (AttributeError, TypeError, ValueError):
        return None
    return secs if secs > 0 else None


def _rate_limit_ttl(retry_after):
    """How long to sit out a 429: the server's Retry-After, clamped to between
    _TTL_FAIL and an hour. Both endpoints wait under this one clamp."""
    return max(_TTL_FAIL, min(retry_after or _TTL_FAIL, 3600))


def _refresh(oauth):
    """Refresh the access token and persist it. Raises RefreshError on failure
    (the caller keeps the old token)."""
    if not oauth.get("refreshToken"):
        raise RefreshError("no refresh token in the credentials file")
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth.get("refreshToken"),
        "client_id": CLIENT_ID,
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    try:
        with urllib.request.urlopen(req, timeout=10, context=_context()) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RefreshError(_http_detail(exc), status=exc.code,
                           retry_after=_retry_after(exc.headers)) from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise RefreshError(str(exc)) from exc

    if not isinstance(data, dict) or not (data.get("access_token") or data.get("accessToken")):
        raise RefreshError("no access token in the refresh response")

    updated = dict(oauth)
    for src, dst in (("access_token", "accessToken"),
                     ("refresh_token", "refreshToken"),
                     ("expires_at", "expiresAt")):
        if data.get(src) is not None:
            updated[dst] = data[src]
    for k in ("accessToken", "refreshToken", "expiresAt"):
        if data.get(k) is not None:
            updated[k] = data[k]
    # The endpoint answers with expires_in — seconds from now — and not with any
    # absolute expires_at, so the loops above find nothing and expiresAt keeps
    # the value it already had. Left alone that is a slow-motion disaster once
    # the TLS fix lets a refresh through: the stale expiresAt is in the past,
    # _expired() stays true, and every poll refreshes again, rotating the one
    # refresh token every 45 seconds until a rotation lands mid-flight against
    # Claude Code's and the grant is invalidated for both of us. Claude Code
    # 2.1.250 does the same arithmetic (Date.now() + expires_in * 1000).
    for src, dst in (("expires_in", "expiresAt"),
                     ("refresh_token_expires_in", "refreshTokenExpiresAt")):
        secs = data.get(src)
        if isinstance(secs, (int, float)) and not isinstance(secs, bool) and secs > 0:
            updated[dst] = int(time.time() * 1000 + secs * 1000)
    scope = data.get("scope")
    if isinstance(scope, str) and scope:
        updated["scopes"] = scope.split()
    _write_creds(updated)
    return updated


def _expired(oauth, now, skew=300.0):
    """True when the access token is at or within `skew` seconds of expiry."""
    expires = oauth.get("expiresAt")
    return bool(expires) and (now * 1000 + skew * 1000) >= expires


# Consecutive automatic refreshes the token endpoint has refused, counted against
# the credentials file as it stood (its mtime). Any rewrite of that file — our
# own successful refresh, Claude Code refreshing itself, `/login` — starts over.
_refusals = {"creds_mtime": None, "count": 0}


def _refresh_backoff(exc, mtime):
    """How long a failed *automatic* refresh holds off the next attempt.

    Every failure used to wait a flat _TTL_AUTH, so an expired token behind a
    rate-limited endpoint re-POSTed once a minute for as long as the app ran —
    ~1,400 refresh POSTs a day. On 2026-09-17 the endpoint was answering 429
    while the refresh token itself still had weeks left. A 429 now waits out
    Retry-After under the usage endpoint's clamp; any other HTTP refusal doubles
    from _TTL_AUTH up to _TTL_REFRESH_MAX. A failure with no HTTP status (network
    or TLS down, nothing to send) never reached the endpoint, so it keeps the
    flat _TTL_AUTH and recovers as soon as the network does.

    None of this delays a fresh login: a credentials rewrite invalidates the
    cached failure on the very next poll. The manual "Attempt token refresh"
    doesn't come through here and always tries at once.
    """
    if exc.status == 429:
        return _rate_limit_ttl(exc.retry_after)
    if exc.status is None:
        return _TTL_AUTH
    if _refusals["creds_mtime"] != mtime:
        _refusals.update(creds_mtime=mtime, count=0)
    _refusals["count"] += 1
    return min(_TTL_AUTH * 2 ** (_refusals["count"] - 1), _TTL_REFRESH_MAX)


def _retry_hint(ttl):
    return "retrying in ~{}m".format(max(1, round(ttl / 60)))


# The flat per-model weekly keys are the older shape, and on a current Max plan
# `seven_day_opus` and `seven_day_sonnet` both come back null. The scoped cap
# did not go away — it moved into the response's `limits` array, arriving as
#   {kind: "weekly_scoped", scope: {model: {display_name: "Fable"}}, percent: N}
# with the model named at runtime rather than baked into a key. A fixed
# whitelist therefore dropped the only per-model cap still reported, so it
# reached neither a gauge nor pacing's wait driver: that bar could have run to
# 100% with nothing in the tray ever mentioning it.
_FLAT_KEYS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")

# `kind: session` and `kind: weekly_all` in the same array duplicate five_hour
# and seven_day, which we already read from the flat keys — only scoped rows
# carry anything new.
_SCOPED_KIND = "weekly_scoped"

# If both shapes ever ship at once, one bar per cap: a scoped row naming Opus
# defers to a populated seven_day_opus rather than rendering beside it.
_LEGACY_SCOPE = {"opus": "seven_day_opus", "sonnet": "seven_day_sonnet"}


def _slug(text):
    return "".join(c if c.isalnum() else "_" for c in str(text).strip().lower()).strip("_")


def _scoped(raw, out):
    """Add model/surface-scoped weekly caps from the `limits` array to `out`."""
    rows = raw.get("limits")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") != _SCOPED_KIND:
            continue
        if row.get("percent") is None:
            continue
        scope = row.get("scope")
        scope = scope if isinstance(scope, dict) else {}
        model = scope.get("model")
        model = model if isinstance(model, dict) else {}
        name = model.get("display_name") or scope.get("surface")
        slug = _slug(name) if name else ""
        if not slug or out.get(_LEGACY_SCOPE.get(slug)):
            continue
        key = "seven_day_scoped_" + slug
        if out.get(key):
            continue
        out[key] = {"utilization": row.get("percent"),
                    "resets_at": row.get("resets_at"),
                    "label": "This week · {}".format(name)}


def _normalize(raw):
    out = {}
    for key in _FLAT_KEYS:
        v = raw.get(key)
        out[key] = {"utilization": v.get("utilization"), "resets_at": v.get("resets_at")} \
            if isinstance(v, dict) else None
    try:
        _scoped(raw, out)
    except Exception:
        pass    # a shape change in the array must not cost us the flat keys
    return out


def _call(token):
    req = urllib.request.Request(USAGE_URL, method="GET", headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": BETA,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=8, context=_context()) as resp:
        return json.loads(resp.read())


def fetch(force=False, allow_refresh=False, force_refresh_token=False):
    """Return the current plan-quota snapshot.

    force=True bypasses the cache. allow_refresh=True permits a token refresh
    (which writes credentials) on expiry/401; default False is strictly
    read-only. force_refresh_token=True mints a fresh token up front regardless
    of the current one's state — the tray's "Attempt token refresh".
    Honors HTTP 429 Retry-After from both endpoints, and backs off repeated
    failed automatic refreshes (_refresh_backoff), so we don't hammer a
    rate-limited endpoint.

    Serialized on a module lock: the updater thread and any HTTP handler that
    calls in share one in-flight request rather than racing to duplicate it.
    """
    with _lock:
        return _fetch_locked(force, allow_refresh, force_refresh_token)


def _fetch_locked(force, allow_refresh, force_refresh_token):
    now = time.time()
    mtime = _creds_mtime()
    cached = _cache["data"]
    # A credentials rewrite (Claude Code refreshing itself, or `claude /login`)
    # invalidates the cache outright — that's what makes a fresh login show up
    # in the tray within a poll instead of after the whole TTL.
    if (not force and cached
            and (now - _cache["epoch"]) < _cache.get("ttl", _TTL_OK)
            and mtime == _cache.get("creds_mtime")):
        return cached

    try:
        oauth = _read_creds()
    except (OSError, ValueError):
        # No credentials file (or it's unreadable/corrupt) — e.g. a Claude Code
        # login that stores its token elsewhere. Degrade like any other failure.
        return _store(now, mtime, {"available": False,
                                   "reason": "not signed in — run /login in Claude Code"},
                      ttl=_TTL_AUTH)
    token = oauth.get("accessToken")
    if not token:
        return _store(now, mtime, {"available": False,
                                   "reason": "not signed in — run /login in Claude Code"},
                      ttl=_TTL_AUTH)

    refreshed_once = False
    warn = None

    # explicit unconditional refresh (tray "Attempt token refresh")
    if force_refresh_token:
        try:
            oauth = _refresh(oauth)
            token = oauth.get("accessToken")
            mtime = _creds_mtime()
            refreshed_once = True
        except RefreshError as exc:
            # ttl=0: never let a failed *manual* attempt evict a good reading
            # for minutes. The next poll re-reads and restores live limits.
            return _store(now, mtime, {"available": False,
                                       "reason": "token refresh failed ({})".format(exc)},
                          ttl=0.0)

    # proactive refresh when the token is at/near expiry
    if not refreshed_once and _expired(oauth, now):
        if allow_refresh:
            try:
                oauth = _refresh(oauth)
                token = oauth.get("accessToken")
                mtime = _creds_mtime()
                refreshed_once = True
            except RefreshError as exc:
                wait = _refresh_backoff(exc, mtime)
                return _store(now, mtime,
                              {"available": False,
                               "reason": "token expired; refresh failed ({}) — {}, or run "
                                         "/login in a terminal".format(exc, _retry_hint(wait))},
                              ttl=wait)
        else:
            warn = "token near/past expiry — use “Attempt token refresh” or run /login"

    for _ in (1, 2):
        try:
            raw = _call(token)
            result = {"available": True, "fetched_epoch": now,
                      "limits": _normalize(raw), "raw": raw}
            if warn:
                result["warning"] = warn
            if refreshed_once:
                result["refreshed"] = True
            return _store(now, mtime, result)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and allow_refresh and not refreshed_once:
                refreshed_once = True
                # Claude Code may have rotated the token from under us between
                # our read and this call; prefer whatever is on disk now over
                # spending our (single-use) refresh token.
                try:
                    fresh = _read_creds()
                except (OSError, ValueError):
                    fresh = {}
                if fresh.get("accessToken") and fresh["accessToken"] != token \
                        and not _expired(fresh, now, skew=0):
                    oauth, token, mtime = fresh, fresh["accessToken"], _creds_mtime()
                    continue
                try:
                    oauth = _refresh(oauth)
                    token = oauth.get("accessToken")
                    mtime = _creds_mtime()
                    continue
                except RefreshError as exc2:
                    wait = _refresh_backoff(exc2, mtime)
                    return _store(now, mtime,
                                  {"available": False,
                                   "reason": "token rejected (401); refresh failed ({}) — {}".format(
                                       exc2, _retry_hint(wait))},
                                  ttl=wait)
            if exc.code == 429:
                ra = _rate_limit_ttl(_retry_after(exc.headers))
                return _store(now, mtime,
                              {"available": False,
                               "reason": "rate-limited by the usage endpoint — retry in ~{}m".format(
                                   round(ra / 60))},
                              ttl=ra)
            reason = ("token rejected (401) — run /login in a terminal, or use "
                      "“Attempt token refresh”") if exc.code == 401 \
                else "HTTP {} from usage endpoint".format(exc.code)
            return _store(now, mtime, {"available": False, "reason": reason},
                          ttl=_TTL_AUTH if exc.code == 401 else _TTL_NET)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Almost always "the network isn't up yet" right after login/resume.
            return _store(now, mtime, {"available": False, "reason": str(exc)},
                          ttl=_TTL_NET)

    # Unreachable today (the loop's only `continue` sets refreshed_once first),
    # but callers treat the return value as a dict — never hand them None.
    return _store(now, mtime, {"available": False, "reason": "usage endpoint retry exhausted"},
                  ttl=_TTL_NET)


def _store(now, mtime, result, ttl=None):
    if ttl is None:
        ttl = _TTL_OK if result.get("available") else _TTL_NET
    _cache.update(epoch=now, data=result, ttl=ttl, creds_mtime=mtime)
    return result


def invalidate():
    """Drop the cache so the next fetch goes to the network."""
    with _lock:
        _cache.update(epoch=0.0, ttl=0.0)


if __name__ == "__main__":
    import sys
    allow = "--refresh" in sys.argv
    force_tok = "--force-refresh" in sys.argv
    print(json.dumps(fetch(force=True, allow_refresh=allow or force_tok,
                           force_refresh_token=force_tok), indent=2))
