#!/usr/bin/env python3
"""Drive the Google Admin console with a real signed-in browser.

Why this exists
---------------
Bulk-uploading 49 users through the console is a click path that changes between
console builds, and the local docs describe it in prose rather than selectors. So
this opens a persistent Chromium profile, lets the operator sign in once as the
admin, then inspects the real page instead of guessing at it. Once the control is
located it stays in the profile, so later runs skip sign-in entirely.

    ./tools/admin_drive.py --mode recon                 # look, do not touch
    ./tools/admin_drive.py --mode stage --csv accounts/cloudidentity/users.csv
    ./tools/admin_drive.py --mode submit --csv accounts/cloudidentity/users.csv

`stage` picks the CSV and stops before anything is committed; `submit` is the one
that actually creates the accounts. Two phases on purpose: user creation is a real
mutation of the tenant and should be seen before it happens.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

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

INSPECT_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const txt = el => (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 60);
  return {
    url: location.href,
    title: document.title,
    buttons: [...new Set([...document.querySelectorAll('button,[role="button"],a[role="button"]')]
              .filter(vis).map(txt).filter(Boolean))].slice(0, 40),
    links: [...new Set([...document.querySelectorAll('a[href]')]
            .filter(vis).map(txt).filter(Boolean))].slice(0, 40),
    files: [...document.querySelectorAll('input[type=file]')].map(e => ({
      name: e.name, id: e.id, accept: e.accept, visible: vis(e),
    })),
    bulkish: [...new Set([...document.querySelectorAll('*')]
              .filter(vis).filter(e => /bulk/i.test(e.innerText || ''))
              .map(e => (e.tagName + '.' + (e.className || '').toString().split(' ')[0] + ' ' + txt(e)).slice(0, 90)))].slice(0, 12),
  };
}
"""


def inspect(page, out: Path, tag: str) -> dict:
    try:
        info = page.evaluate(INSPECT_JS)
    except Exception as exc:
        info = {"url": page.url, "error": str(exc)[:120]}
    page.screenshot(path=str(out / f"{tag}.png"), full_page=False)
    print(f"\n--- {tag} ---")
    print(f"  url      {info.get('url', '')[:120]}")
    print(f"  title    {info.get('title', '')[:100]}")
    if info.get("buttons"):
        print(f"  buttons  {' | '.join(info['buttons'])[:400]}")
    if info.get("files"):
        print(f"  files    {json.dumps(info['files'])[:300]}")
    if info.get("bulkish"):
        print("  bulk-ish:")
        for b in info["bulkish"]:
            print(f"    · {b}")
    return info


def on_admin_console(url: str) -> bool:
    """True only for a real admin-console page.

    Substring matching is a trap here: the sign-in URL carries the console as a
    `continue=` parameter, so `"admin.google.com/ac" in url` is true while you are
    still staring at the password box. Parse it.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (
        parsed.hostname == "admin.google.com"
        and parsed.path.startswith("/ac")
        and "checkout" not in parsed.path
    )


def wait_for_login(page, limit: int) -> bool:
    print("waiting for a signed-in admin session (this window is yours to use)...")
    start = time.time()
    last = None
    while time.time() - start < limit:
        try:
            url = page.url
        except Exception:
            url = "?"
        if url != last:
            last = url
            print(f"  …{int(time.time() - start):>3}s  {url[:110]}")
        if on_admin_console(url):
            return True
        page.wait_for_timeout(2000)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["recon", "stage", "submit"], default="recon")
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--csv", default="accounts/cloudidentity/users.csv")
    ap.add_argument("--url", default="https://admin.google.com/ac/users")
    ap.add_argument("--wait", type=int, default=900, help="seconds to wait for sign-in")
    args = ap.parse_args()

    out = ROOT / "debug" / "admin" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    profile = ROOT / args.profile
    profile.mkdir(parents=True, exist_ok=True)
    csv_path = (ROOT / args.csv).resolve()
    if args.mode != "recon" and not csv_path.exists():
        print(f"csv missing: {csv_path}")
        return 2

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            args=LAUNCH_ARGS,
            locale="en-US",
            viewport={"width": 1440, "height": 1000},
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)

        if not wait_for_login(page, args.wait):
            print("never reached a signed-in admin page")
            inspect(page, out, "stuck")
            ctx.close()
            return 1

        # land on the target page explicitly; the login redirect may stop anywhere
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        except Exception as exc:
            print(f"  navigation to users page: {str(exc)[:80]}")
        page.wait_for_timeout(5000)
        info = inspect(page, out, "users_page")
        (out / "inspect.json").write_text(json.dumps(info, indent=2))

        if args.mode == "recon":
            print(f"\nsaved -> {out}")
            print("holding open 2 min so you can look too")
            page.wait_for_timeout(120000)
            ctx.close()
            return 0

        # the control lives behind "More options" in the current console build,
        # not on the toolbar -- that is why it could not be found by eye
        for opener in ("More options",):
            try:
                b = page.get_by_role("button", name=opener)
                if b.count() and b.first.is_visible():
                    b.first.click()
                    page.wait_for_timeout(1500)
                    print(f"opened '{opener}'")
            except Exception as exc:
                print(f"  {opener}: {str(exc)[:60]}")

        # find the bulk-update entry point
        opened = False
        for name in ("Bulk update users", "Bulk update", "Upload users"):
            try:
                loc = page.get_by_role("menuitem", name=name)
                if not (loc.count() and loc.first.is_visible()):
                    loc = page.get_by_role("button", name=name)
                if not (loc.count() and loc.first.is_visible()):
                    loc = page.get_by_role("link", name=name)
                if not (loc.count() and loc.first.is_visible()):
                    loc = page.get_by_text(name, exact=False)
                if loc.count() and loc.first.is_visible():
                    loc.first.click()
                    print(f"clicked '{name}'")
                    opened = True
                    break
            except Exception:
                continue
        if not opened:
            print("could not find a bulk-update control; inspect.json has the page's controls")
            inspect(page, out, "no_bulk_control")
            page.wait_for_timeout(120000)
            ctx.close()
            return 1

        page.wait_for_timeout(3000)
        inspect(page, out, "bulk_dialog")

        # attach the CSV to whichever file input the dialog exposes
        attached = False
        for sel in ("input[type=file]",):
            loc = page.locator(sel)
            if loc.count():
                try:
                    loc.first.set_input_files(str(csv_path))
                    print(f"attached {csv_path.name} to {sel}")
                    attached = True
                    break
                except Exception as exc:
                    print(f"attach failed: {str(exc)[:100]}")
        if not attached:
            print("no file input found; the dialog may need the button that reveals it")
            inspect(page, out, "no_file_input")

        page.wait_for_timeout(5000)
        inspect(page, out, "staged")

        if args.mode == "submit":
            for name in ("Upload", "Continue", "Next", "Add users", "Confirm"):
                try:
                    btn = page.get_by_role("button", name=name)
                    if btn.count() and btn.first.is_visible():
                        btn.first.click()
                        print(f"submitted via '{name}'")
                        break
                except Exception:
                    continue
            page.wait_for_timeout(8000)
            inspect(page, out, "after_submit")
        else:
            print("\nstaged only -- nothing committed. re-run with --mode submit to push it.")

        print(f"\nsaved -> {out}")
        print("holding open 2 min")
        page.wait_for_timeout(120000)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
