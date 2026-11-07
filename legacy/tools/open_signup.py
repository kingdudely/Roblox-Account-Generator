"""Open Roblox's signup page on a chosen layout and leave the window up.

The layout is not in the server HTML and not in a query parameter: curl always
receives the classic markup (`auth-ui-data` says control), and the new form is
swapped in client-side after the page hydrates. So the only reliable way to get
the new UI is to load the page in a browser and reload until `#MonthDropdown`
is a <div> instead of a <select>.

    PYTHONPATH=. .venv/bin/python tools/open_signup.py               # new UI
    PYTHONPATH=. .venv/bin/python tools/open_signup.py --classic     # old UI
    PYTHONPATH=. .venv/bin/python tools/open_signup.py --tries 25 --port 9222

The window stays open until you close it. With `--port` you also get a Chrome
DevTools endpoint on 127.0.0.1:9222, so the page can be inspected or attached to
over CDP.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from roblox_creator import signup_form as form
from roblox_creator.config import load_proxies
from roblox_creator.socks_bridge import SocksBridge
from tools.browser_creator import LAUNCH_ARGS

SIGNUP = "https://www.roblox.com/CreateAccount"

# Measured 2026-11-04: the layout follows the reported platform, not a cookie,
# a query parameter or a reload lottery.
#   macOS  -> #MonthDropdown is a <div>  (the new two-step form)
#   Windows/ Linux -> native <select>     (the classic one-step form)
UAS = {
    "mac": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"),
    "windows": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"),
    "linux": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"),
}


def log(msg: str) -> None:
    print(msg, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--want", default="new", choices=("new", "classic"))
    ap.add_argument("--tries", type=int, default=15)
    ap.add_argument("--proxy-file", default=None)
    ap.add_argument("--profile", default="debug/profiles/signup_ui")
    ap.add_argument("--port", type=int, default=9222, help="CDP port; 0 disables")
    ap.add_argument("--ua", default=None, choices=("mac", "windows", "linux"),
                    help="platform to report; default is mac for the new UI, "
                         "windows for the classic one")
    ap.add_argument("--wait", type=float, default=0,
                    help="seconds to keep the window open; 0 = until closed")
    args = ap.parse_args()

    wanted = form.RADIX if args.want == "new" else form.CLASSIC
    ua_key = args.ua or ("mac" if wanted == form.RADIX else "windows")
    proxy = None
    if args.proxy_file:
        pool = load_proxies(Path(args.proxy_file))
        proxy = {"server": SocksBridge(pool[0]).start()}
        log(f"  proxy: {pool[0].split('@')[-1][:40]}")
    log(f"  platform: {ua_key}")

    chrome_args = list(LAUNCH_ARGS)
    if args.port:
        chrome_args.append(f"--remote-debugging-port={args.port}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False, args=chrome_args)
        page = None
        ctx = None
        found = False

        # The layout is rolled once per browser session and then pinned: reloads
        # keep whatever the session got, while a new context re-rolls. So this
        # loops over *contexts*, not reloads.
        for attempt in range(1, args.tries + 1):
            try:
                ctx = browser.new_context(user_agent=UAS[ua_key],
                                          viewport={"width": 1366, "height": 900},
                                          locale="en-US", color_scheme="light",
                                          proxy=proxy)
                page = ctx.new_page()
                page.goto(SIGNUP, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_selector("#signup-username", state="visible", timeout=20000)
                page.wait_for_timeout(1800)
            except Exception as exc:
                log(f"  {attempt:>2}: load failed ({type(exc).__name__})")
                try:
                    ctx.close()
                except Exception:
                    pass
                continue

            tag = page.evaluate(
                "() => { const m = document.querySelector('#MonthDropdown');"
                " return m ? m.tagName : ''; }")
            variant = "radix" if tag.upper() == "DIV" else "classic"
            extra = page.evaluate(
                """() => {
                  const meta = document.querySelector('meta[name=auth-ui-data]');
                  return {
                    auth_ui: meta ? meta.getAttribute('data-auth-ui-variant') : null,
                    pw_on_screen_one: !!document.querySelector('#signup-password'),
                    submit: (document.querySelector('#signup-button')
                              || [...document.querySelectorAll('button')]
                                   .find(b => /continue|sign up/i.test(b.innerText || ''))
                              || {}).innerText || null,
                  };
                }""")
            log(f"  {attempt:>2}: {variant:<7} auth-ui={extra['auth_ui']} "
                f"password-on-step-1={extra['pw_on_screen_one']} submit={extra['submit']!r}")

            if variant == wanted:
                found = True
                break
            try:
                ctx.close()
            except Exception:
                pass
            page = None

        if not found:
            log(f"\n  no {wanted} layout in {args.tries} sessions")

        log("")
        log(f"  window is {'the NEW signup UI (two-step, Add password)' if wanted == form.RADIX else 'the classic UI'}"
            f"{'' if found else ' (best effort)'}")
        if args.port:
            log(f"  CDP: http://127.0.0.1:{args.port}")
        log("  close the window to exit")
        log("")

        if args.wait:
            page.wait_for_timeout(int(args.wait * 1000))
        else:
            while True:
                try:
                    if page is None or page.is_closed():
                        break
                    page.wait_for_timeout(1000)
                except Exception:
                    break
        try:
            browser.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()
