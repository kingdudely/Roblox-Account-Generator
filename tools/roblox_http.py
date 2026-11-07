#!/usr/bin/env python3
"""Roblox account operations over plain HTTP, using a captured session cookie.

Why this exists
---------------
The "One last thing" dialog looks like a form and behaves like one to a robot:
Radix comboboxes, a terms modal, a username field. It is not a form. It is a
wrapper around a single call, captured from a live signup:

    POST https://auth.roblox.com/v1/account/upgrade
    {"upgradeType":"OAuth","birthday":"1996-05-15T00:00:00.000Z","gender":3}
    -> 200

So finalising an account needs no browser, no comboboxes and no screenshots. This
module does that, plus the session check, with `curl_cffi` so the TLS fingerprint
is a real Chrome's.

CSRF: auth.roblox.com requires `X-CSRF-TOKEN` on POST. Sending the request without
one answers 403 with the token in the `x-csrf-token` response header; the call is
then retried once with it. That handshake is the normal pattern on this host.
"""

from __future__ import annotations

from typing import Any

from curl_cffi import requests as creq

COOKIE = ".ROBLOSECURITY"
BASE = "https://auth.roblox.com"
AUTHENTICATED = "https://users.roblox.com/v1/users/authenticated"
UPGRADE = f"{BASE}/v1/account/upgrade"


def session_for(cookie: str):
    s = creq.Session(impersonate="chrome", timeout=30)
    s.cookies.set(COOKIE, cookie, domain=".roblox.com")
    return s


def _post_with_csrf(session, url: str, payload: dict) -> Any:
    """POST, learning the CSRF token from the 403 that asks for it."""
    headers = {"content-type": "application/json;charset=UTF-8",
               "origin": "https://www.roblox.com",
               "referer": "https://www.roblox.com/"}

    response = session.post(url, json=payload, headers=headers)
    if response.status_code == 403:
        token = response.headers.get("x-csrf-token")
        if token:
            headers["x-csrf-token"] = token
            response = session.post(url, json=payload, headers=headers)
    return response


def finalise(cookie: str, birthday: str = "1996-05-15", gender: int = 3) -> dict:
    """Turn a pending SSO account into a usable one.

    Until this runs every authenticated call answers `403 User is moderated`,
    which reads like a ban and is not one.
    """
    session = session_for(cookie)
    payload = {"upgradeType": "OAuth",
               "birthday": f"{birthday}T00:00:00.000Z",
               "gender": gender}
    try:
        response = _post_with_csrf(session, UPGRADE, payload)
        return {"status": response.status_code, "body": response.text[:200]}
    except Exception as exc:
        return {"status": 0, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


def validate(cookie: str) -> dict:
    try:
        response = session_for(cookie).get(AUTHENTICATED)
    except Exception as exc:
        return {"status": 0, "error": f"{type(exc).__name__}: {str(exc)[:120]}"}
    out = {"status": response.status_code, "body": response.text[:200]}
    if response.status_code == 200:
        data = response.json()
        out.update({"id": data.get("id"), "name": data.get("name")})
    return out


def finish(cookie: str, birthday: str = "1996-05-15", gender: int = 3) -> dict:
    """Finalise if needed, then report the session. Idempotent."""
    check = validate(cookie)
    if check.get("status") == 200:
        return {"finalised": False, "session": check}
    upgraded = finalise(cookie, birthday, gender)
    check = validate(cookie)
    return {"finalised": True, "upgrade": upgraded, "session": check}


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) < 2:
        print("usage: roblox_http.py <cookie-file>")
        raise SystemExit(2)
    value = Path(sys.argv[1]).read_text().strip()
    print(finish(value))
