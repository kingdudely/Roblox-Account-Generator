#!/usr/bin/env python3
"""Drive Google signup to the Android "same device" gate and leave it open.

The desktop gate asks for a QR and an outbound SMS. The Android gate asks for a
device check that hands off to Google Play Services:

    intent://…#Intent;package=com.google.android.gms;
      component=com.google.android.gms/.auth.aang.impl.deeplink.DeepLinkActivity

This tool walks the wizard with an Android user agent, stops on that screen, and
holds the browser open so it can be driven by hand.

    ./tools/google_samedevice_open.py
    ./tools/google_samedevice_open.py --proxy socks5://host:port
    ./tools/google_samedevice_open.py --hold-minutes 120
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.google_signup_probe import (
    SIGNUP_URL, analyse_gate, click_next, fill, pick, survey,
)

ANDROID_DEV = {
    "user_agent": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/156.0.0.0 Mobile Safari/537.36"),
    "viewport": {"width": 412, "height": 915},
    "device_scale_factor": 2.625,
    "is_mobile": True,
    "has_touch": True,
}

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=".profiles/google_samedevice_manual")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--first", default="Marek")
    ap.add_argument("--last", default="Reid")
    ap.add_argument("--day", default="12")
    ap.add_argument("--year", default="1992")
    ap.add_argument("--password", default="Th3-Quiet-Depth-94")
    ap.add_argument("--hold-minutes", type=int, default=180)
    ap.add_argument("--start-only", action="store_true",
                    help="open the signup page and leave it untouched")
    args = ap.parse_args()

    out = ROOT / "debug" / "google_samedevice_open" / \
        datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    surveys: list = []

    with sync_playwright() as pw:
        proxy = {"server": args.proxy} if args.proxy else None
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str((ROOT / args.profile).resolve()),
            headless=False,
            locale="en-US",
            args=LAUNCH_ARGS,
            proxy=proxy,
            **ANDROID_DEV,
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)
        info = survey(page, out, surveys, "landing")
        gate = analyse_gate(info)
        print(f"[gate] {gate}", flush=True)

        if args.start_only:
            gate = None

        if gate == "NAME":
            fill(page, 'input[name="firstName"]', args.first)
            fill(page, 'input[name="lastName"]', args.last)
            click_next(page)
            page.wait_for_timeout(2800)
            info = survey(page, out, surveys, "after_name")
            gate = analyse_gate(info)
            print(f"[gate] {gate}", flush=True)

        if gate == "BIRTHDAY":
            for attempt in range(4):
                fill(page, 'input[name="day"]', args.day)
                fill(page, 'input[name="year"]', args.year)
                pick(page, "month", "July")
                pick(page, "gender", "Male")
                click_next(page)
                page.wait_for_timeout(3000)
                if "birthdaygender" not in page.url:
                    break
                print(f"[birthday] retry {attempt + 1}", flush=True)
            info = survey(page, out, surveys, "after_birthday")
            gate = analyse_gate(info)
            print(f"[gate] {gate}", flush=True)

        radios = page.locator('input[name="usernameRadio"]')
        if radios.count():
            try:
                radios.first.check(force=True)
            except Exception:
                radios.first.click(force=True)
            click_next(page)
            page.wait_for_timeout(3200)
        elif page.locator('input[name="Username"]').count():
            fill(page, 'input[name="Username"]',
                 f"marek.reid{random.randint(1000, 9999)}")
            click_next(page)
            page.wait_for_timeout(3200)

        if page.locator('input[name="Passwd"]').count():
            fill(page, 'input[name="Passwd"]', args.password)
            if page.locator('input[name="PasswdAgain"]').count():
                fill(page, 'input[name="PasswdAgain"]', args.password)
            click_next(page)
            page.wait_for_timeout(5000)

        # if the walk landed on the generic error screen, hand the browser
        # back at the start so it can be driven by hand instead.
        if "/error/" in page.url:
            print("[error] walk ended on the error screen; reopening signup",
                  flush=True)
            page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)

        url = page.url
        (out / "url.txt").write_text(url + "\n")
        body = (page.evaluate("document.body.innerText") or "")[:800]
        (out / "screen.txt").write_text(body)
        page.screenshot(path=str(out / "gate.png"), full_page=True)

        print("\n=== AT THE GATE ===", flush=True)
        print(url, flush=True)
        print("---\n" + body + "\n---", flush=True)
        print(f"browser is open; holding for {args.hold_minutes} min", flush=True)
        print(f"artifacts: {out}", flush=True)

        deadline = time.time() + args.hold_minutes * 60
        while time.time() < deadline:
            page.wait_for_timeout(5000)
            try:
                if not ctx.pages:
                    print("[closed] window closed", flush=True)
                    break
            except Exception:
                print("[closed] browser gone", flush=True)
                break
            if page.url != url:
                url = page.url
                (out / "url.txt").write_text(url + "\n")
                print(f"[nav] {url[:160]}", flush=True)
                try:
                    body = (page.evaluate("document.body.innerText") or "")[:800]
                    (out / "screen.txt").write_text(body)
                    print(body[:400], flush=True)
                except Exception:
                    pass
        try:
            ctx.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
