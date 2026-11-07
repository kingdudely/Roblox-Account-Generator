#!/usr/bin/env python3
"""Can Roblox's half of the SSO chain be completed without the browser?

Why this exists
---------------
The sign-in chain is:

    auth.roblox.com/.../sso/oauth/init   → Google OAuth
    accounts.google.com/o/oauth2/v2/auth → sign-in UI
    (sign in)
    auth.roblox.com/.../sso/oauth/callback?state=…&code=…   ← sets .ROBLOSECURITY
    www.roblox.com/sso-complete

Google's part needs a browser because the sign-in RPCs carry JS-minted BotGuard
tokens. Roblox's part carries nothing of the sort: it is a GET with a state and a
code. If that GET works from an HTTP client, the browser can stop at the OAuth
redirect and one lane stops costing 400 MB for work that is two requests.

This probe intercepts the callback, aborts it (so the browser never redeems the
code), then redeems the same URL with curl_cffi and reports what comes back.

    ./tools/sso_callback_probe.py --index 22

Outcome `browser-only` is a real answer too: it means the redirect has to be
followed by the browser, and the split stops at the OAuth step.
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

from playwright.sync_api import sync_playwright

from tools.admin_api_users import build_service
from tools.sso_fleet import (LAUNCH_ARGS, MAC_UA, COOKIE, mint_codes,
                             open_signup, find_google_page, google_signin, log)


def redeem(url: str, cookies: dict[str, str]) -> dict:
    """Follow the callback with curl_cffi, carrying the browser's roblox cookies."""
    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=30)
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".roblox.com")

    out = {"status": None, "cookie": None, "final_url": None, "set_cookie": None}
    try:
        response = session.get(url, allow_redirects=True)
        out["status"] = response.status_code
        out["final_url"] = response.url
        out["set_cookie"] = (response.headers.get("set-cookie") or "")[:200]
        for ck in session.cookies:
            if ck.name == COOKIE and ck.value:
                out["cookie"] = ck.value
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
    return out


def validate(cookie: str) -> dict:
    from curl_cffi import requests as creq

    try:
        s = creq.Session(impersonate="chrome", timeout=25)
        s.cookies.set(COOKIE, cookie, domain=".roblox.com")
        r = s.get("https://users.roblox.com/v1/users/authenticated")
        out = {"status": r.status_code, "body": r.text[:160]}
        if r.status_code == 200:
            d = r.json()
            out.update({"id": d.get("id"), "name": d.get("name")})
        return out
    except Exception as exc:
        return {"status": 0, "error": str(exc)[:100]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", type=int, required=True, help="1-based ledger index")
    ap.add_argument("--ledger", default="accounts/cloudidentity/users.json")
    ap.add_argument("--token", default="accounts/cloudidentity/token.json")
    ap.add_argument("--profile", default=".profiles/probe")
    args = ap.parse_args()

    users = json.loads((ROOT / args.ledger).read_text())
    user = users[args.index - 1]
    out_dir = ROOT / "debug" / "probe" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    service = build_service(ROOT / args.token)
    codes = mint_codes(service, user["email"])
    log(f"{user['email']}: {len(codes)} codes, spending {codes[0]}")

    caught: dict = {}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=True,
            args=LAUNCH_ARGS, user_agent=MAC_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US", color_scheme="light")

        def on_callback(route):
            caught["url"] = route.request.url
            log(f"    callback intercepted: {route.request.url[:110]}")
            route.abort("aborted")

        # Match the host explicitly: a glob on the path also matches Google's
        # CheckCookie URL, which carries the callback address in `continue=`.
        def watch(req):
            if "auth.roblox.com" in req.url or "/sso/" in req.url:
                log(f"    seen: {req.method} {req.url[:130]}")

        ctx.on("request", watch)
        ctx.route(
            lambda url: ("/sso/oauth/callback" in str(url)
                         and "accounts.google.com" not in str(url)),
            on_callback)

        try:
            page = open_signup(ctx)
            if page is None:
                log("no google button")
                return 1
            popup = find_google_page(ctx, page)
            if popup is None:
                log("google never opened")
                return 1
            # context-level routing does not catch this redirect; route on the
            # popup page itself as well and let whichever fires first win
            def is_callback(url):
                return ("/sso/oauth/callback" in str(url)
                        and "accounts.google.com" not in str(url))

            try:
                popup.route(is_callback, on_callback)
                log("    popup-level routing armed")
            except Exception as exc:
                log(f"    popup route failed: {str(exc)[:80]}")

            popup.wait_for_timeout(2000)
            outcome = google_signin(popup, user["email"], user["password"],
                                    codes[0], out_dir, args.index,
                                    deadline=150, stop_flag=caught)
            log(f"    browser outcome: {outcome}")

            cookies = {c["name"]: c["value"] for c in ctx.cookies()
                       if "roblox" in c["domain"]}
            log(f"    browser roblox cookies: {sorted(cookies)}")
        finally:
            ctx.close()

    if not caught.get("url"):
        log("callback never reached — nothing to redeem")
        return 1

    (out_dir / "callback_url.txt").write_text(caught["url"])
    log("\n  redeeming the same callback with curl_cffi (no browser)…")
    result = redeem(caught["url"], cookies)
    log(f"    status: {result.get('status')}  final: {str(result.get('final_url'))[:90]}")
    log(f"    set-cookie: {str(result.get('set_cookie'))[:120]}")

    if not result.get("cookie"):
        log("\n  => browser-only: the callback did not mint a session over HTTP")
        return 2

    log(f"    cookie: {len(result['cookie'])} chars")
    check = validate(result["cookie"])
    log(f"    session: {check}")
    if check.get("status") == 200:
        log("\n  => SPLIT WORKS: browser for Google, HTTP for Roblox")
        return 0
    log("\n  => inconclusive: cookie issued but not accepted")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
