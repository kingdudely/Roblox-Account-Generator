#!/usr/bin/env python3
"""Sweep every documented auth route and report which sit behind the gate.

`rblx-challenge-type` is emitted by the challenge middleware and nothing else.
A route that answers anything at all without that header has no captcha in
front of it — the question is then whether the route is alive (validation
error) or tombstoned (429/501/static 400).

    ./tools/route_sweep.py
    ./tools/route_sweep.py --alive      # only routes past the tombstones
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE = "https://auth.roblox.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
HEADERS = {
    "user-agent": UA,
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://www.roblox.com",
    "referer": "https://www.roblox.com/CreateAccount",
    "sec-ch-ua": '"Google Chrome";v="141", "Chromium";v="141", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}

SIGNUP_BODY = {"username": "!!", "password": "x",
               "birthday": "1990-01-01", "gender": 2}

# (method, path, body) — bodies are invalid on purpose so a live route answers
# with a validation error instead of doing the thing.
ROUTES: list[tuple[str, str, dict | None]] = [
    ("POST", "/v2/signup", SIGNUP_BODY),
    ("POST", "/v2/signup/linked", {}),
    ("POST", "/v1/signup", SIGNUP_BODY),
    ("POST", "/v1/signup/linked", {}),
    ("POST", "/v1/external/signup", {}),
    ("POST", "/v1/external/access", {}),
    ("POST", "/v1/external/login", {}),
    ("POST", "/v1/external/loginAndLink", {}),
    ("POST", "/v1/external/unlink", {}),
    ("GET",  "/v1/external/1/sso/oauth/init", None),
    ("GET",  "/v1/external/1/sso/oauth/callback", None),
    ("POST", "/v1/external/1/sso/oauth/callback", {}),
    ("POST", "/v1/external/1/sso/native/nonce", {}),
    ("POST", "/v1/account/upgrade", {}),
    ("GET",  "/v1/account-creation/metadata", None),
    ("GET",  "/v1/client-assertion", None),
    ("POST", "/v2/login", {"username": "a", "password": "b", "ctype": "Username"}),
    ("POST", "/v1/login", {"username": "a", "password": "b", "ctype": "Username"}),
    ("POST", "/v2/login/linked", {}),
    ("POST", "/v1/login/linked", {}),
    ("POST", "/v1/session/refresh", {}),
    ("POST", "/v1/logout", {}),
    ("GET",  "/v2/passwords/validate?username=a", None),
    ("POST", "/v2/passwords/validate", {"username": "a", "password": "b"}),
    ("GET",  "/v2/revert/account", None),
    ("POST", "/v2/revert/account", {}),
    ("POST", "/v1/identity/initialize-login", {"identifier": "a"}),
    ("POST", "/v2/identity-verification/login", {}),
    ("POST", "/v1/identity-verification/login", {}),
    ("POST", "/v3/users/1/two-step-verification/login",
     {"code": "000000", "rememberDevice": False}),
    ("GET",  "/v1/account/pin", None),
    ("POST", "/v1/account/pin", {"pin": "1234"}),
    ("POST", "/v1/account/pin/lock", {}),
    ("POST", "/v1/account/pin/unlock", {}),
    ("POST", "/v2/usernames/validate",
     {"username": "!!", "birthday": "1990-01-01"}),
    ("GET",  "/v1/validators/email?email=not-an-email", None),
    ("GET",  "/v1/validators/username?username=!!", None),
    ("POST", "/v1/validators/username", {"username": "!!"}),
    ("GET",  "/v2/usernames/validate?username=!!&birthday=1990-01-01", None),
    ("POST", "/v1/passkey/StartRegistration", {}),
    ("POST", "/v1/passkey/start-preauth-registration", {}),
    ("POST", "/v1/passkey/finish-preauth-registration", {}),
    ("POST", "/v1/passkey/finish-ar-preauth-registration", {}),
    ("POST", "/v1/passkey/StartAuthentication", {"username": "a"}),
    ("POST", "/v1/passkey/start-authentication-by-user", {"username": "a"}),
    ("GET",  "/v1/passkey/su-eligibility", None),
    ("GET",  "/v1/social/connected-providers", None),
    ("GET",  "/v1/xbox/connection", None),
    ("POST", "/v1/xbox/translate", {}),
    ("GET",  "/v2/auth/metadata", None),
    ("GET",  "/v2/recovery/metadata", None),
]

# Static 400s that name a required field are torn tombstones, not live routes.
TOMBSTONE_MESSAGES = {
    "Unspecified identity provider/platform",
}


def classify(resp) -> str:
    ctype = resp.headers.get("rblx-challenge-type")
    if ctype:
        return "GATED"
    if resp.status_code == 403:
        return "CSRF"
    if resp.status_code == 429:
        return "TOMB-429"
    if resp.status_code == 501:
        return "TOMB-501"
    if resp.status_code == 401:
        return "AUTH-401"
    if resp.status_code in (400, 422):
        try:
            msg = resp.json()["errors"][0].get("message", "")
        except Exception:
            return "LIVE"
        if msg in TOMBSTONE_MESSAGES:
            return "TOMB-400"
        return "LIVE"
    if resp.status_code in (200, 201, 204):
        return "LIVE"
    return f"HTTP{resp.status_code}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--alive", action="store_true", help="only LIVE/GATED rows")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=25)
    rows = []

    def send(method, path, body, headers):
        if method == "GET":
            return session.get(BASE + path, headers=headers)
        return session.post(BASE + path, json=body or {}, headers=headers)

    for method, path, body in ROUTES:
        try:
            r = send(method, path, body, HEADERS)
            # Roblox hands back a CSRF token on the first 403 of a session and
            # expects the same request replayed with it. Without this step every
            # live route reads as a 403 wall.
            csrf = r.headers.get("x-csrf-token")
            if r.status_code == 403 and csrf and not r.headers.get("rblx-challenge-type"):
                r = send(method, path, body, {**HEADERS, "x-csrf-token": csrf})
            verdict = classify(r)
            rows.append({"method": method, "path": path, "verdict": verdict,
                         "status": r.status_code,
                         "challenge": r.headers.get("rblx-challenge-type"),
                         "body": r.text[:160]})
        except Exception as exc:
            rows.append({"method": method, "path": path,
                         "verdict": f"TRANSPORT {type(exc).__name__}",
                         "status": None, "body": ""})
        time.sleep(0.15)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    shown = [r for r in rows if not args.alive or r["verdict"] in ("LIVE", "GATED", "AUTH-401")]
    width = max(len(r["path"]) for r in shown)
    for r in shown:
        print(f"  {r['method']:<4} {r['path']:<{width}}  {r['verdict']:<11} "
              f"{str(r['status'] or ''):<4} {r['body'][:100]}")

    tally: dict[str, int] = {}
    for r in rows:
        tally[r["verdict"]] = tally.get(r["verdict"], 0) + 1
    print("\n  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
