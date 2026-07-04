"""Experimental live plan-quota reader.

Hits the same endpoint Claude Code's `/usage` command uses to fetch the
5-hour / weekly rate-limit bars. Endpoint + headers + refresh flow were
reverse-engineered from the installed Claude Code v2.0.65 binary.

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <accessToken>
    anthropic-beta: oauth-2025-04-20

Response: { five_hour, seven_day, seven_day_sonnet, seven_day_opus }, each a
{ "utilization": 0-100, "resets_at": ISO8601|null } (or absent/null).

SAFE BY DEFAULT: this only READS your credentials and never writes them unless
you explicitly pass allow_refresh=True (the tray's "Attempt token refresh"
action). A token refresh rotates the refresh token, which can force a re-login
of whatever Claude Code login owns it — so it's opt-in, never automatic.

This is UNDOCUMENTED and may break on a Claude Code update. All failures
degrade gracefully to {available: false}; it never raises to the caller.
"""
import json
import os
import time
import urllib.error
import urllib.request

CREDENTIALS = os.path.expanduser("~/.claude/.credentials.json")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REFRESH_SCOPE = "user:profile user:inference user:sessions:claude_code"
USER_AGENT = "claude-code/2.0.65"
BETA = "oauth-2025-04-20"

_cache = {"epoch": 0.0, "data": None}
_TTL_OK = 45.0       # re-fetch a good result at most this often
_TTL_FAIL = 300.0    # back off longer after a failure (avoid hammering)


def _read_creds():
    with open(CREDENTIALS, encoding="utf-8") as f:
        return json.load(f).get("claudeAiOauth") or {}


def _write_creds(oauth):
    with open(CREDENTIALS, encoding="utf-8") as f:
        doc = json.load(f)
    doc["claudeAiOauth"] = oauth
    tmp = CREDENTIALS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, CREDENTIALS)


def _refresh(oauth):
    """Refresh the access token and persist it. Raises on failure (caller keeps old token)."""
    body = json.dumps({
        "grant_type": "refresh_token",
        "refresh_token": oauth.get("refreshToken"),
        "client_id": CLIENT_ID,
        "scope": REFRESH_SCOPE,
    }).encode("utf-8")
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read())
    updated = dict(oauth)
    for src, dst in (("access_token", "accessToken"),
                     ("refresh_token", "refreshToken"),
                     ("expires_at", "expiresAt")):
        if data.get(src) is not None:
            updated[dst] = data[src]
    for k in ("accessToken", "refreshToken", "expiresAt"):
        if data.get(k) is not None:
            updated[k] = data[k]
    _write_creds(updated)
    return updated


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


def fetch(force=False, allow_refresh=False):
    """Return the current plan-quota snapshot.

    force=True bypasses the cache. allow_refresh=True permits ONE token refresh
    (which writes credentials) when the token is expired or rejected; default
    False keeps this strictly read-only.
    """
    now = time.time()
    cached = _cache["data"]
    if not force and cached:
        ttl = _TTL_OK if cached.get("available") else _TTL_FAIL
        if (now - _cache["epoch"]) < ttl:
            return cached

    oauth = _read_creds()
    token = oauth.get("accessToken")
    if not token:
        return _store(now, {"available": False,
                            "reason": "not signed in — run /login in Claude Code"})

    warn = None
    expires = oauth.get("expiresAt")
    expired = bool(expires and (now * 1000 + 300_000) >= expires)
    if expired:
        if allow_refresh and oauth.get("refreshToken"):
            try:
                oauth = _refresh(oauth)
                token = oauth.get("accessToken")
            except (urllib.error.URLError, OSError, ValueError) as exc:
                return _store(now, {"available": False,
                                    "reason": "token expired; refresh failed ({})".format(exc)})
        else:
            warn = "token near/past expiry — enable token refresh or run /login"

    refreshed_once = False
    for _ in (1, 2):
        try:
            raw = _call(token)
            result = {"available": True, "fetched_epoch": now,
                      "limits": _normalize(raw), "raw": raw}
            if warn:
                result["warning"] = warn
            return _store(now, result)
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and allow_refresh and not refreshed_once and oauth.get("refreshToken"):
                refreshed_once = True
                try:
                    oauth = _refresh(oauth)
                    token = oauth.get("accessToken")
                    continue
                except (urllib.error.URLError, OSError, ValueError) as exc2:
                    return _store(now, {"available": False,
                                        "reason": "token rejected (401); refresh failed ({})".format(exc2)})
            reason = ("token rejected (401) — run /login in a terminal, or use "
                      "“Attempt token refresh”") if exc.code == 401 \
                else "HTTP {} from usage endpoint".format(exc.code)
            return _store(now, {"available": False, "reason": reason})
        except (urllib.error.URLError, OSError, ValueError) as exc:
            return _store(now, {"available": False, "reason": str(exc)})


def _store(now, result):
    _cache.update(epoch=now, data=result)
    return result


if __name__ == "__main__":
    import sys
    allow = "--refresh" in sys.argv
    print(json.dumps(fetch(force=True, allow_refresh=allow), indent=2))
