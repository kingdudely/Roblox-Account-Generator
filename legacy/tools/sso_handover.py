#!/usr/bin/env python3
"""Hand a live Roblox signup to the operator, and record everything they do.

Built for one question the code cannot answer on its own: when a Roblox account
was created through Google SSO, what happens if the same Google identity comes
back a second time? The answer depends on what Roblox stores — the provider's
`sub`, the email, or nothing at all — and the only way to see it is to drive the
real flow and watch the requests.

    ./tools/sso_handover.py                       # opens CreateAccount, clicks Google
    ./tools/sso_handover.py --minutes 60 --profile .profiles/manual

The window is yours. Every navigation is echoed with a timestamp, and every
request into roblox.com / google.com / appleid.apple.com is written to
`debug/handover/<stamp>/requests.jsonl`, so the trail survives the session.

Watch the `sso-complete?sso-type=` parameter when it lands:

    sso-type=signup   a new Roblox account was just created
    sso-type=login    that identity already owned an account, and you are in it

Close the window when done; the run ends with a summary.
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

from tools.browser_creator import LAUNCH_ARGS

SIGNUP = "https://www.roblox.com/CreateAccount"
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

INTERESTING = ("roblox.com", "rbxcdn.com", "google.com", "googleapis.com",
               "gstatic.com", "appleid.apple.com", "apple.com")
SKIP_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".woff",
            ".woff2", ".ttf", ".css", ".mp4", ".avif")
SKIP_URLS = ("gen_204", "google-analytics", "googletagmanager", "doubleclick",
             "e.png?btn=", "play.google", "clients6", "ogs.google")


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=SIGNUP)
    ap.add_argument("--profile", default=".profiles/manual")
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--no-click", action="store_true",
                    help="open the page but do not press the Google button")
    args = ap.parse_args()

    out = ROOT / "debug" / "handover" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    transcript = out / "requests.jsonl"
    started = time.time()

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=False,
            args=LAUNCH_ARGS, user_agent=MAC_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US", color_scheme="light")

        def write(row: dict) -> None:
            row["t"] = round(time.time() - started, 2)
            with transcript.open("a") as fh:
                fh.write(json.dumps(row) + "\n")

        def on_request(req):
            url = req.url
            if url.endswith(SKIP_EXT) or any(s in url for s in SKIP_URLS):
                return
            if not any(k in url for k in INTERESTING):
                return
            if req.resource_type not in ("document", "xhr", "fetch"):
                return
            try:
                body = (req.post_data or "")[:2000]
            except Exception:  # noqa: BLE001 - gzipped bodies
                body = "<binary>"
            write({"dir": "req", "method": req.method, "url": url[:400],
                   "type": req.resource_type, "body": body})

        def on_response(res):
            url = res.url
            if url.endswith(SKIP_EXT) or any(s in url for s in SKIP_URLS):
                return
            if not any(k in url for k in INTERESTING):
                return
            if res.request.resource_type not in ("document", "xhr", "fetch"):
                return
            body = ""
            try:
                if res.request.resource_type in ("xhr", "fetch"):
                    body = res.text()[:1500]
            except Exception:  # noqa: BLE001
                body = "<unreadable>"
            write({"dir": "res", "status": res.status, "url": url[:400], "body": body})

        def on_nav(frame):
            if frame != frame.page.main_frame:
                return
            url = frame.url
            if not url or url == "about:blank":
                return
            marker = ""
            if "sso-complete" in url:
                marker = "   <-- SSO RESULT"
                for kind in ("signup", "login"):
                    if f"sso-type={kind}" in url:
                        marker = f"   <-- sso-type={kind.upper()}"
            log(f"[{round(time.time() - started):>4}s] {url[:130]}{marker}")

        ctx.on("request", on_request)
        ctx.on("response", on_response)
        ctx.on("framenavigated", on_nav)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        try:
            page.wait_for_selector("#signup-username", state="visible", timeout=25000)
        except Exception:  # noqa: BLE001
            log("signup form not detected — leaving the page as loaded")
        page.wait_for_timeout(1500)

        if not args.no_click:
            button = page.get_by_role("button", name="Google")
            if not button.count():
                button = page.get_by_text("Google", exact=False)
            if button.count():
                log("clicking Continue with Google")
                try:
                    button.first.click()
                except Exception as exc:  # noqa: BLE001
                    log(f"click failed: {type(exc).__name__}: {str(exc)[:100]}")
            else:
                log("no Google button found — press it yourself")
        else:
            log("recording; press the button yourself")

        log(f"\nrecording -> {transcript}")
        log("close the window when you are done\n")
        try:
            page.wait_for_event("close", timeout=args.minutes * 60_000)
        except Exception:  # noqa: BLE001 - timeout or teardown both end it
            pass
        try:
            ctx.close()
        except Exception:  # noqa: BLE001
            pass

    rows = [json.loads(l) for l in transcript.read_text().splitlines() if l.strip()] \
        if transcript.exists() else []
    log(f"\nrecorded {len(rows)} rows -> {transcript}")

    kinds = [r["url"] for r in rows if "sso-complete" in r.get("url", "")]
    for u in kinds[-5:]:
        log(f"  sso: {u[:140]}")
    log(f"artifacts -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
