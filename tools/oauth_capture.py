"""Record a human Google-SSO signup, end to end, and keep the session cookie.

The new signup UI offers Google. If that path never asks for Arkose or the
press-and-hold, then the captcha is not being bypassed by a solver — it is being
skipped by a different account-creation flow, and the interesting question is
which server call mints the session and what authorises it.

This opens the new UI (re-rolled contexts until the two-step form appears), hands
the window to the operator, and records every request, response header and
cookie mutation until `.ROBLOSECURITY` lands. It writes the cookie to
`cookie.txt` next to the capture.

    PYTHONPATH=. .venv/bin/python tools/oauth_capture.py
    PYTHONPATH=. .venv/bin/python tools/oauth_capture.py --port 9222 --minutes 30
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

from roblox_creator import signup_form as form
from tools.browser_creator import LAUNCH_ARGS

SIGNUP = "https://www.roblox.com/CreateAccount"
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")
COOKIE = ".ROBLOSECURITY"

# Everything that could carry an authorization decision.
INTERESTING = ("roblox.com", "rbxcdn.com", "arkoselabs", "funcaptcha", "px-cloud",
               "px-cdn", "perimeterx", "google.com", "googleapis.com", "gstatic.com",
               "accounts.google", "oauth", "captcha", "challenge", "signup")
SKIP_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".woff",
            ".woff2", ".ttf", ".css", ".mp4", ".avif")


def validate(cookie_value: str) -> dict:
    """Does the session actually belong to a usable account?"""
    try:
        from curl_cffi import requests as creq
    except Exception:
        return {"error": "curl_cffi missing"}
    try:
        session = creq.Session(impersonate="chrome", timeout=25)
        session.cookies.set(COOKIE, cookie_value, domain=".roblox.com")
        response = session.get("https://users.roblox.com/v1/users/authenticated")
        body = response.text[:200]
        out = {"status": response.status_code, "body": body}
        if response.status_code == 200:
            data = response.json()
            out.update({"id": data.get("id"), "name": data.get("name")})
        return out
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {str(exc)[:80]}"}


def log(msg: str) -> None:
    print(msg, flush=True)


def interesting(url: str) -> bool:
    low = url.lower().split("?")[0]
    if low.endswith(SKIP_EXT):
        return False
    return any(key in url for key in INTERESTING)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tries", type=int, default=15, help="sessions to re-roll for the new UI")
    ap.add_argument("--port", type=int, default=9222)
    ap.add_argument("--minutes", type=float, default=20)
    ap.add_argument("--follow", type=float, default=120,
                    help="seconds to keep watching after the cookie appears")
    ap.add_argument("--no-require-google", action="store_true",
                    help="accept the new UI even when the Google button is absent")
    ap.add_argument("--out", default="debug/oauth")
    ap.add_argument("--profile", default=None,
                    help="persistent browser profile; keeps Google device state so "
                         "each sign-in is not a brand-new device")
    args = ap.parse_args()

    args.require_google = not args.no_require_google
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / stamp
    out.mkdir(parents=True, exist_ok=True)
    stream = (out / "events.jsonl").open("w", encoding="utf-8")
    events = 0

    def emit(kind: str, url: str, extra: dict | None = None) -> None:
        nonlocal events
        if stream.closed or not interesting(url):
            return
        record = {"t": round(time.time() - started, 2), "kind": kind, "url": url[:220],
                  **(extra or {})}
        stream.write(json.dumps(record) + "\n")
        stream.flush()
        events += 1

    chrome_args = list(LAUNCH_ARGS)
    if args.port:
        chrome_args.append(f"--remote-debugging-port={args.port}")

    started = time.time()
    with sync_playwright() as pw:
        browser = None
        persistent = None
        if args.profile:
            prof = Path(args.profile)
            prof.mkdir(parents=True, exist_ok=True)
            persistent = pw.chromium.launch_persistent_context(
                str(prof), headless=False, args=chrome_args,
                user_agent=MAC_UA, viewport={"width": 1366, "height": 900},
                locale="en-US", color_scheme="light")
            log(f"  persistent profile: {prof}")
        else:
            browser = pw.chromium.launch(headless=False, args=chrome_args)
        ctx = page = None

        for attempt in range(1, args.tries + 1):
            try:
                if persistent is not None:
                    ctx = persistent
                    # Drop only roblox cookies between attempts so the signup
                    # layout re-rolls while the google.com session -- and with it
                    # the device trust -- survives.
                    if attempt > 1:
                        try:
                            for ck in ctx.cookies():
                                if "roblox" in ck["domain"]:
                                    ctx.clear_cookies(name=ck["name"], domain=ck["domain"])
                        except Exception:
                            pass
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    try:
                        page.goto("about:blank")
                    except Exception:
                        page = ctx.new_page()
                else:
                    ctx = browser.new_context(user_agent=MAC_UA,
                                              viewport={"width": 1366, "height": 900},
                                              locale="en-US", color_scheme="light")
                    page = ctx.new_page()
                page.goto(SIGNUP, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector("#signup-username", state="visible", timeout=20000)
                page.wait_for_timeout(1600)
            except Exception as exc:
                log(f"  {attempt:>2}: load failed ({type(exc).__name__})")
                try:
                    ctx.close()
                except Exception:
                    pass
                continue
            info = page.evaluate(
                """() => {
                  const m = document.querySelector('#MonthDropdown');
                  const meta = document.querySelector('meta[name=auth-ui-data]');
                  const names = [...document.querySelectorAll('button,a,[role=button]')]
                    .map(e => ((e.getAttribute('aria-label') || '') + ' '
                               + (e.innerText || '')).trim().toLowerCase());
                  return { month: m ? m.tagName : null,
                           variant: meta ? meta.getAttribute('data-auth-ui-variant') : null,
                           google: names.some(n => n.includes('google')),
                           apple: names.some(n => n.includes('apple')) };
                }""")
            tag = info["month"]
            log(f"  {attempt:>2}: {'NEW UI' if tag == 'DIV' else 'classic':<7} "
                f"variant={info['variant']} google={info['google']}")
            if tag == "DIV" and (not args.require_google or info["google"]):
                break
            if persistent is None:
                try:
                    ctx.close()
                except Exception:
                    pass
            page = None

        if page is None:
            log("  no session offered Google; using the last one anyway")
            if persistent is not None:
                ctx = persistent
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            else:
                ctx = browser.new_context(user_agent=MAC_UA)
                page = ctx.new_page()
            page.goto(SIGNUP, wait_until="domcontentloaded", timeout=45000)

        def wire(target):
            def on_request(request):
                extra = {"method": request.method,
                         "type": request.resource_type}
                try:
                    if request.resource_type in ("xhr", "fetch", "document") \
                            and any(k in request.url for k in ("roblox", "google")):
                        extra["headers"] = {k: v[:200] for k, v in request.headers.items()
                                            if k.lower() not in ("cookie",)}
                        body = request.post_data
                        if body:
                            extra["body"] = body[:400]
                except Exception:
                    pass
                emit("req", request.url, extra)

            def on_response(response):
                extra = {"status": response.status}
                try:
                    wanted = {}
                    for key, value in response.headers.items():
                        low = key.lower()
                        if low.startswith("rblx-") or low in ("location", "set-cookie",
                                                              "content-type"):
                            wanted[key] = value[:220]
                    if wanted:
                        extra["headers"] = wanted
                except Exception:
                    pass
                emit("res", response.url, extra)

            target.on("request", on_request)
            target.on("response", on_response)
            target.on("requestfailed", lambda r: emit("fail", r.url,
                                                      {"why": str(r.failure)[:80]}))

        wire(page)
        ctx.on("page", lambda p: (wire(p), log(f"  popup: {p.url[:60] or 'about:blank'}")))

        log("")
        log("  ============================================================")
        log("   Sign up with Google in this window, all the way through.")
        log("   Do not close the window afterwards.")
        log("  ============================================================")
        log("")

        deadline = time.time() + args.minutes * 60
        cookie = None
        last_note = 0.0
        while time.time() < deadline:
            try:
                page.wait_for_timeout(1000)
            except Exception:
                log("  window closed; stopping capture")
                break
            try:
                cookie = next((c for c in ctx.cookies() if c["name"] == COOKIE), None)
            except Exception:
                cookie = None
            if cookie:
                log(f"\n  .ROBLOSECURITY seen at {int(time.time() - started)}s")
                break
            if time.time() - last_note > 20:
                last_note = time.time()
                log(f"  …{int(time.time() - started)}s  pages={len(ctx.pages)} "
                    f"events={events}  url={page.url[:56] if page else '?'}")

        # The cookie arrives at `sso-complete`, which is a page where the account
        # is finished (username, birthday). Stopping there was the mistake last
        # time: the session exists but the profile may not. Keep watching.
        followed = {"reached": None, "text": None}
        if cookie:
            log("  following sso-complete…")
            follow_deadline = time.time() + args.follow
            while time.time() < follow_deadline:
                page.wait_for_timeout(1500)
                try:
                    url = page.url
                except Exception:
                    break
                if "/home" in url or "/discover" in url:
                    followed["reached"] = url
                    break
                if page.query_selector("#signup-username"):
                    followed["reached"] = "username-form"
                    break
            try:
                followed["url"] = page.url
                followed["text"] = (page.inner_text("body") or "")[:300].replace("\n", " | ")
                page.screenshot(path=str(out / "final.png"))
            except Exception:
                pass
            log(f"  ended on: {followed.get('url', '?')[:70]}")
            log(f"  page says: {str(followed.get('text'))[:160]!r}")

        summary = {
            "outcome": "created" if cookie else "timeout",
            "followed": followed,
            "elapsed": round(time.time() - started, 1),
            "events": events,
            "pages": [p.url[:120] for p in ctx.pages],
            "cookies": [],
        }
        try:
            summary["cookies"] = [{"name": c["name"], "domain": c["domain"],
                                   "httpOnly": c.get("httpOnly"),
                                   "value_len": len(c["value"])}
                                  for c in ctx.cookies()]
        except Exception:
            pass

        if cookie:
            (out / "cookie.txt").write_text(cookie["value"])
            log(f"  cookie saved ({len(cookie['value'])} chars) -> {out / 'cookie.txt'}")
            summary["session"] = validate(cookie["value"])
            log(f"  authenticated check: {summary['session']}")

        hits = []
        with (out / "events.jsonl").open() as handle:
            for line in handle:
                event = json.loads(line)
                if event["kind"] == "res" and any(
                        key in event["url"] for key in ("signup", "captcha", "challenge",
                                                        "oauth", "authorize", "token",
                                                        "external", "sso")):
                    hits.append(event)
        summary["key_responses"] = hits[:30]
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        log(f"  -> {out / 'summary.json'}")

        log("  window stays open; close it to exit")
        while True:
            try:
                if page is None or page.is_closed():
                    break
                page.wait_for_timeout(1000)
            except Exception:
                break
        if not stream.closed:
            stream.close()


if __name__ == "__main__":
    main()
