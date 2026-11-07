#!/usr/bin/env python3
"""One user, created through the admin console UI, to compare with the API.

`users.insert` answers 412 from every network tried so far — WARP and two free
SOCKS5 exits. That leaves one question the API cannot answer: is the block on
the *create path* (Admin SDK) or on the *tenant*?

The console's bulk-add form is a different path into the same operation. If it
succeeds, the block is API-specific and the UI is the workaround. If it answers
the same abuse error, the tenant is closed regardless of route.

    ./tools/admin_ui_insert.py --email ui.probe1 --first Ui --last Probe
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.admin_add_users import (BULKADD_URL, LAUNCH_ARGS, dump, on_console,
                                   read_values, wait_all_added, wait_ready)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="local part only")
    ap.add_argument("--first", default="Ui")
    ap.add_argument("--last", default="Probe")
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--hold", type=int, default=0)
    args = ap.parse_args()

    out = ROOT / "debug" / "admin_ui" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=False,
            args=LAUNCH_ARGS, locale="en-US",
            viewport={"width": 1440, "height": 1100})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BULKADD_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)
        if not on_console(page.url):
            print(f"not signed in ({page.url[:90]})")
            ctx.close()
            return 1

        # the form opens with spare rows; fill only the first and leave the rest
        # blank, which the console accepts
        boxes = page.locator('input[type="text"], input:not([type])')
        filled = 0
        for value in (args.first, args.last, args.email):
            for i in range(boxes.count()):
                box = boxes.nth(i)
                try:
                    if box.is_visible() and not box.input_value():
                        box.click()
                        box.fill(value)
                        filled += 1
                        break
                except Exception:
                    continue
        print(f"fields filled: {filled}")
        dump(page, out, "01_filled")

        if filled < 3:
            print("form did not take every field — stopping")
            page.wait_for_timeout(args.hold * 1000)
            ctx.close()
            return 1

        page.get_by_role("button", name="Continue").first.click()
        page.wait_for_timeout(7000)
        info = dump(page, out, "02_step2")
        print(f"step2 url: {info['url'][:100]}")
        print(f"step2 body: {str(info.get('body'))[:400]}")

        added = wait_all_added(page, 1)
        print(f"users added: {added}")
        dump(page, out, "03_result")
        page.screenshot(path=str(out / "03_result.png"), full_page=True)
        print(f"values now: {read_values(page)}")

        if args.hold:
            page.wait_for_timeout(args.hold * 1000)
        ctx.close()
    print(f"saved -> {out}")
    return 0 if added else 1


if __name__ == "__main__":
    raise SystemExit(main())
