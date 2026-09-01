"""Experimental live plan-quota reader.

Hits the same endpoint Claude Code's `/usage` command uses to fetch the
5-hour / weekly rate-limit bars. Endpoint + headers + refresh flow were
reverse-engineered from the installed Claude Code binary.

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <accessToken>
    anthropic-beta: oauth-2025-04-20

Response: { five_hour, seven_day, seven_day_sonnet, seven_day_opus }, each a
{ "utilization": 0-100, "resets_at": ISO8601|null } (or absent/null).

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

_TTL_OK = 45.0      # re-fetch a good result at most this often
_TTL_NET = 10.0     # transient (DNS/socket): the network is usually just not up
                    # yet at login — a long penalty box here is what made the
                    # app look dead for minutes after boot.
_TTL_AUTH = 60.0    # token problem; an mtime change re-checks sooner anyway
_TTL_FAIL = 300.0   # server told us to back off (429)


class RefreshError(Exception):
    """A token refresh failed. Carries a human-readable reason."""


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
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise RefreshError(_http_detail(exc)) from exc
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
    scope = data.get("scope")
    if isinstance(scope, str) and scope:
        updated["scopes"] = scope.split()
    _write_creds(updated)
    return updated


def _expired(oauth, now, skew=300.0):
    """True when the access token is at or within `skew` seconds of expiry."""
    expires = oauth.get("expiresAt")
    return bool(expires) and (now * 1000 + skew * 1000) >= expires


def _normalize(raw):
    out = {}
    for key in ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet"):
        v = raw.get(key)
        out[key] = {"utilization": v.get("utilization"), "resets_at": v.get("resets_at")} \
            if isinstance(v, dict) else None
    return out


def _call(token):
    req = urllib.request.Request(USAGE_URL, method="GET", headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": BETA,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def fetch(force=False, allow_refresh=False, force_refresh_token=False):
    """Return the current plan-quota snapshot.

    force=True bypasses the cache. allow_refresh=True permits a token refresh
    (which writes credentials) on expiry/401; default False is strictly
    read-only. force_refresh_token=True mints a fresh token up front regardless
    of the current one's state — the tray's "Attempt token refresh".
    Honors HTTP 429 Retry-After so we don't hammer a rate-limited endpoint.

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
                return _store(now, mtime,
                              {"available": False,
                               "reason": "token expired; refresh failed ({}) — run /login "
                                         "in a terminal".format(exc)},
                              ttl=_TTL_AUTH)
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
                    return _store(now, mtime,
                                  {"available": False,
                                   "reason": "token rejected (401); refresh failed ({})".format(exc2)},
                                  ttl=_TTL_AUTH)
            if exc.code == 429:
                try:
                    ra = int(exc.headers.get("retry-after") or 0)
                except (TypeError, ValueError):
                    ra = 0
                ra = max(_TTL_FAIL, min(ra or _TTL_FAIL, 3600))
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
