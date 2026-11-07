#!/usr/bin/env python3
"""Search the Admin console for a control and report what it finds.

Why this exists
---------------
Bulk user upload is not on the Users toolbar and not in the "More options" menu
in the current Cloud Identity console build, so guessing at menu paths is wasted
effort. The console has a global search that indexes its own pages; this types a
query into it and dumps every result with its destination, which is how you find
a page Google moved without publishing a new address.

    ./tools/admin_find.py --query "bulk update users"
    ./tools/admin_find.py --query "users" --url https://admin.google.com/ac/users
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

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1440,1000",
]

RESULTS_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const txt = el => (el.innerText || el.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 90);
  const results = [...document.querySelectorAll(
      '[role="option"],[role="menuitem"],li,[role="listbox"] > div, a[href*="/ac/"]')]
    .filter(vis).map(el => ({
      text: txt(el),
      href: el.getAttribute('href') || (el.querySelector('a[href]') || {}).href || null,
    }))
    .filter(r => r.text && r.text.length > 3);
  const seen = new Set();
  return results.filter(r => {
    const k = r.text + "|" + (r.href || "");
    if (seen.has(k)) return false;
    seen.add(k);
    return true;
  }).slice(0, 40);
}
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="bulk update users")
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--url", default="https://admin.google.com/ac/users")
    ap.add_argument("--hold", type=int, default=60, help="seconds to keep the window open")
    args = ap.parse_args()

    out = ROOT / "debug" / "admin_find" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile),
            headless=False,
            args=LAUNCH_ARGS,
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)

        if "accounts.google.com" in page.url:
            print("not signed in -- run tools/admin_drive.py --mode recon first")
            ctx.close()
            return 1

        # the console search is the first textbox in the header
        box = page.locator('input[type="text"], input[type="search"]').first
        if not box.count():
            print("no search box found")
            page.screenshot(path=str(out / "no_search.png"))
            ctx.close()
            return 1

        box.click()
        box.press_sequentially(args.query, delay=45)
        page.wait_for_timeout(3500)
        page.screenshot(path=str(out / "results.png"))

        results = page.evaluate(RESULTS_JS)
        (out / "results.json").write_text(json.dumps(results, indent=2))
        print(f"query: {args.query!r}  -> {len(results)} results")
        for r in results:
            print(f"  · {r['text'][:70]:<72} {('-> ' + str(r['href'])[:70]) if r['href'] else ''}")

        print(f"\nsaved -> {out}")
        page.wait_for_timeout(args.hold * 1000)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
