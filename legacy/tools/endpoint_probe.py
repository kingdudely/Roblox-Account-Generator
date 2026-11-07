#!/usr/bin/env python3
"""Which auth endpoints actually sit behind the challenge middleware?

The Cloud API index lists every public auth route, including the deprecated
ones Roblox keeps serving for old clients. The challenge (Arkose) gate is
installed per-route, not per-host, so a deprecated sibling of `/v2/signup`
can still be reachable with no captcha at all.

The probe never creates an account: it posts a deliberately invalid body and
reads how far the request got.

    CHALLENGED  403 + rblx-challenge-type      -> gate is installed
    VALIDATED   400 + field errors             -> gate is absent, body reached
                                                  the validator
    CSRF        403 + x-csrf-token, no type    -> needs a token first, retry
    MISSING     404/405                        -> route not mounted for this
                                                  method

    ./tools/endpoint_probe.py
    ./tools/endpoint_probe.py --proxy http://user:pass@host:port
"""

from __future__ import annotations

import argparse
import json
import sys
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

# Invalid on purpose: every field fails validation, so a route without the
# challenge gate answers with a 400 and names the bad field. A route with the
# gate answers 403 before it ever looks at the body.
BAD_BODY = {
    "username": "!!",
    "password": "x",
    "birthday": "1990-01-01",
    "gender": 2,
}

ROUTES = [
    ("POST", "/v2/signup"),                 # control: known challenge gate
    ("POST", "/v1/signup"),                 # deprecated sibling
    ("POST", "/v1/signup/linked"),
    ("POST", "/v1/external/signup"),        # SSO signup, no cookie per docs
    ("POST", "/v1/external/access"),
    ("POST", "/v1/account/upgrade"),
    ("GET", "/v1/account-creation/metadata"),
]


def classify(resp) -> tuple[str, str]:
    ctype = resp.headers.get("rblx-challenge-type")
    if ctype:
        return "CHALLENGED", f"type={ctype}"
    if resp.status_code == 403 and resp.headers.get("x-csrf-token"):
        return "CSRF", "token handed out, no challenge type"
    if resp.status_code in (400, 422):
        return "VALIDATED", "body reached the validator"
    if resp.status_code in (404, 405):
        return "MISSING", "route not mounted for this method"
    if resp.status_code in (200, 201):
        return "ACCEPTED", "route answered without a challenge"
    return f"HTTP{resp.status_code}", ""


def probe(session, method: str, path: str) -> dict:
    url = BASE + path
    out = {"path": path, "method": method}
    try:
        if method == "GET":
            r = session.get(url, headers=HEADERS, timeout=25)
        else:
            r = session.post(url, json=BAD_BODY, headers=HEADERS, timeout=25)
    except Exception as exc:
        out.update(verdict="TRANSPORT", detail=type(exc).__name__)
        return out

    # a fresh session sometimes gets the CSRF handshake before the real answer
    if r.status_code == 403 and r.headers.get("x-csrf-token") and not r.headers.get("rblx-challenge-type"):
        h = dict(HEADERS)
        h["x-csrf-token"] = r.headers["x-csrf-token"]
        try:
            r = (session.get(url, headers=h, timeout=25) if method == "GET"
                 else session.post(url, json=BAD_BODY, headers=h, timeout=25))
        except Exception as exc:
            out.update(verdict="TRANSPORT", detail=type(exc).__name__)
            return out
        out["after_csrf"] = True

    verdict, detail = classify(r)
    out.update(verdict=verdict, status=r.status_code, detail=detail,
               body=r.text[:200])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=30)
    if args.proxy:
        session.proxies = {"http": args.proxy, "https": args.proxy}

    rows = [probe(session, m, p) for m, p in ROUTES]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    width = max(len(r["path"]) for r in rows)
    for r in rows:
        print(f"  {r['method']:<4} {r['path']:<{width}}  "
              f"{r.get('verdict', '?'):<10} {r.get('status', ''):<4} "
              f"{r.get('detail', '')}")
        if r.get("verdict") == "VALIDATED":
            print(f"        body: {r.get('body', '')[:160]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
