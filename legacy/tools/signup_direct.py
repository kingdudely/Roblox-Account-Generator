#!/usr/bin/env python3
"""Does a fully browser-shaped signup request ever avoid the captcha entirely?

Every signup attempt in this project so far has been two bare POSTs to
`/v2/signup`: no page load, no cookies from the site, no `sec-ch-ua`, no
`x-csrf-token` until the first 403 hands one over. Roblox answered 403 +
`Challenge is required to authorize the request` every single time, and that was
read as "a captcha is mandatory".

It is not. Roblox decides per request whether to challenge, and the decision is
fed by how much the request looks like the real page: session cookies from the
CreateAccount GET, the right header order and client hints, a CSRF token minted
by that page, a matching TLS fingerprint. A thin request looks like a script.

If warming the session yields even a few percent straight-through 200s, then at
40k exits the captcha stops being the constraint — which is worth far more than
any solver.

    ./tools/signup_direct.py --proxy-file proxies.txt --tries 10
    ./tools/signup_direct.py --proxy-file proxies.txt --tries 10 --bare
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.config import SIGNUP_POST, load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse as parse_proxy

CREATE_PAGE = "https://www.roblox.com/CreateAccount"

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/155.0.0.0 Safari/537.36")

# What Chrome sends for a normal top-level navigation.
PAGE_HEADERS = {
    "user-agent": UA,
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,image/apng,*/*;q=0.8"),
    "accept-language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Linux"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def signup_headers(csrf: str | None = None, referer: str = CREATE_PAGE) -> dict:
    headers = {
        "user-agent": UA,
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/json;charset=UTF-8",
        "origin": "https://www.roblox.com",
        "referer": referer,
        "sec-ch-ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
    }
    if csrf:
        headers["x-csrf-token"] = csrf
    return headers


def classify(resp) -> dict:
    kind = resp.headers.get("rblx-challenge-type", "")
    out = {"status": resp.status_code, "challenge": kind or None}
    if resp.status_code in (200, 201):
        try:
            doc = resp.json()
            out["username"] = doc.get("username") or doc.get("name")
        except Exception:
            out["username"] = None
        out["verdict"] = "CREATED"
        return out
    if resp.status_code == 403 and kind:
        out["verdict"] = "CHALLENGED"
        raw = resp.headers.get("rblx-challenge-metadata", "")
        if raw:
            try:
                import base64
                meta = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
                out["blob"] = bool(meta.get("dataExchangeBlob"))
                out["mode"] = meta.get("mode")
            except Exception:
                pass
        return out
    out["verdict"] = f"HTTP{resp.status_code}"
    out["body"] = resp.text[:120]
    return out


def one_attempt(proxy_url: str, warm: bool) -> dict:
    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=30)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    result: dict = {"warm": warm, "cookies": 0}

    csrf = None
    if warm:
        try:
            page = session.get(CREATE_PAGE, headers=PAGE_HEADERS, timeout=30)
            result["page"] = page.status_code
            result["cookies"] = len(session.cookies)
            # the page embeds a csrf token for the signup form
            text = page.text
            for marker in ('"csrfToken":"', "csrfToken: '"):
                idx = text.find(marker)
                if idx != -1:
                    start = idx + len(marker)
                    end = text.find(marker[-2], start)
                    csrf = text[start:end]
                    break
        except Exception as exc:
            result["page"] = f"{type(exc).__name__}"

    identity = make_identity(random.Random())
    body = {"username": identity.username, "password": identity.password,
            "birthday": "1990-01-01"}
    result["username"] = identity.username

    try:
        first = session.post(SIGNUP_POST, json=body,
                             headers=signup_headers(csrf), timeout=30)
    except Exception as exc:
        result["verdict"] = f"TRANSPORT {type(exc).__name__}"
        return result

    if first.status_code == 403 and first.headers.get("x-csrf-token"):
        csrf = first.headers["x-csrf-token"]
        first = session.post(SIGNUP_POST, json=body,
                             headers=signup_headers(csrf), timeout=30)

    result.update(classify(first))
    result["csrf_from_page"] = bool(csrf and result.get("page") == 200)
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--tries", type=int, default=10)
    ap.add_argument("--bare", action="store_true",
                    help="skip the page warm-up (the old behaviour, as control)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--direct", action="store_true",
                    help="no proxy: use this machine's own egress")
    args = ap.parse_args()

    if args.direct:
        pool = [None]
    else:
        pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)
    warm = not args.bare
    results = []

    for i in range(args.tries):
        proxy = random.choice(pool)
        target = "" if proxy is None else parse_proxy(proxy).url()
        outcome = one_attempt(target, warm)
        results.append(outcome)
        if args.json:
            print(json.dumps(outcome))
        else:
            detail = (f"challenge={outcome.get('challenge')} "
                      f"blob={outcome.get('blob')} mode={outcome.get('mode')}")
            print(f"  [{i:>2}] {outcome.get('verdict'):<12} {detail}"
                  f"  cookies={outcome.get('cookies')}")
        time.sleep(0.4)

    created = [r for r in results if r.get("verdict") == "CREATED"]
    print(f"\n  {len(created)}/{len(results)} straight through"
          f"  ({'warmed' if warm else 'bare'})")
    if created:
        for r in created:
            print(f"    {r.get('username')}")
    return 0 if created else 1


if __name__ == "__main__":
    raise SystemExit(main())
