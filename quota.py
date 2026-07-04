"""Experimental live plan-quota reader.

Hits the same endpoint Claude Code's `/usage` command uses to fetch the
5-hour / weekly rate-limit bars. Endpoint + headers + refresh flow were
reverse-engineered from the installed Claude Code v2.0.65 binary.

    GET https://api.anthropic.com/api/oauth/usage
    Authorization: Bearer <accessToken>
    anthropic-beta: oauth-2025-04-20

Response: { five_hour, seven_day, seven_day_sonnet, seven_day_opus }, each a
{ "utilization": 0-100, "resets_at": ISO8601|null } (or absent/null).

This is UNDOCUMENTED and may break on a Claude Code update — it's the opt-in
"experimental" surface. All failures degrade gracefully to {available: false}.
Never raises to the caller.
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
_CACHE_TTL = 45.0


def _read_creds():
    with open(CREDENTIALS, encoding="utf-8") as f:
        return (json.load(f).get("claudeAiOauth") or {})


def _write_creds(oauth):
    with open(CREDENTIALS, encoding="utf-8") as f:
        doc = json.load(f)
    doc["claudeAiOauth"] = oauth
    tmp = CREDENTIALS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.replace(tmp, CREDENTIALS)


def _refresh(oauth):
    """Refresh an expiring access token. Returns the updated oauth dict.

    Non-destructive on failure: raises, and the caller keeps the old token.
    """
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
    # some responses use camelCase already
    for k in ("accessToken", "refreshToken", "expiresAt"):
        if data.get(k) is not None:
            updated[k] = data[k]
    _write_creds(updated)
    return updated


def _normalize(raw):
    out = {}
    for key in ("five_hour", "seven_day", "seven_day_sonnet", "seven_day_opus"):
        v = raw.get(key)
        if isinstance(v, dict):
            out[key] = {
                "utilization": v.get("utilization"),
                "resets_at": v.get("resets_at"),
            }
        else:
            out[key] = None
    return out


def _call(token):
    """Make the usage GET. Returns (raw_dict, None) on 200, or (None, HTTPError/Exception)."""
    req = urllib.request.Request(USAGE_URL, method="GET", headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": BETA,
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read())


def fetch(force=False):
    now = time.time()
    if not force and _cache["data"] and (now - _cache["epoch"]) < _CACHE_TTL:
        return _cache["data"]

    oauth = _read_creds()
    token = oauth.get("accessToken")
    warn = None
    if not token:
        result = {"available": False,
                  "reason": "not signed in — run /login in Claude Code"}
        _cache.update(epoch=now, data=result)
        return result

    # proactive refresh if within 5 min of the recorded expiry
    expires = oauth.get("expiresAt")
    if expires and (now * 1000 + 300_000) >= expires and oauth.get("refreshToken"):
        try:
            oauth = _refresh(oauth)
            token = oauth.get("accessToken")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            warn = "token near expiry; refresh failed ({})".format(exc)

    result = None
    for attempt in (1, 2):
        try:
            raw = _call(token)
            result = {"available": True, "fetched_epoch": now,
                      "limits": _normalize(raw), "raw": raw}
            if warn:
                result["warning"] = warn
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and attempt == 1 and oauth.get("refreshToken"):
                # token rejected — try one refresh-and-retry
                try:
                    oauth = _refresh(oauth)
                    token = oauth.get("accessToken")
                    continue
                except (urllib.error.URLError, OSError, ValueError) as rexc:
                    result = {"available": False,
                              "reason": "token rejected (401) and refresh failed — "
                                        "run /login in Claude Code ({})".format(rexc)}
                    break
            reason = "token rejected (401) — run /login in Claude Code" if exc.code == 401 \
                else "HTTP {} from usage endpoint".format(exc.code)
            result = {"available": False, "reason": reason}
            break
        except (urllib.error.URLError, OSError, ValueError) as exc:
            result = {"available": False, "reason": str(exc)}
            break

    _cache.update(epoch=now, data=result)
    return result


if __name__ == "__main__":
    print(json.dumps(fetch(force=True), indent=2))
