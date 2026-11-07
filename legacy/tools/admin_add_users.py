#!/usr/bin/env python3
"""Create Cloud Identity users through the console's bulk-add form.

Why this exists
---------------
Cloud Identity's console has no CSV upload screen -- `More options` holds only
custom attributes, the transfer tool, profile edits and deleted users. But the
"Add new user" button does not open a single-user dialog either: it routes to
`/ac/user/bulkadd`, a multi-row form with an "Add another user" repeater. That is
the bulk path, and it is 49 accounts in five passes.

    ./tools/admin_add_users.py --mode probe
    ./tools/admin_add_users.py --mode fill --start 0  --batch 10    # stop at step 2
    ./tools/admin_add_users.py --mode fill --start 10 --batch 10

Each row is [First name, Last name, Primary email, Secondary email]; the domain is
fixed by the console, so the email field takes the local part only. `fill` stops
after pressing Continue so step two can be inspected before anything is created.
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
    "--window-size=1440,1100",
]

BULKADD_URL = "https://admin.google.com/ac/user/bulkadd"
DEFAULT_ROWS = 3

FORM_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const label = el => {
    const l = el.id ? document.querySelector(`label[for="${el.id}"]`) : null;
    if (l) return (l.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 40);
    return (el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
            el.name || el.id || '?').slice(0, 40);
  };
  return {
    url: location.href,
    inputs: [...document.querySelectorAll('input')].filter(vis).map((el, i) => ({
      i, label: label(el), id: el.id || null, type: el.type, value: (el.value || '').slice(0, 40),
    })),
    buttons: [...new Set([...document.querySelectorAll('button,[role="button"]')]
              .filter(vis).map(el => (el.innerText || el.getAttribute('aria-label') || '')
              .replace(/\s+/g, ' ').trim().slice(0, 40)).filter(Boolean))],
    body: document.body.innerText.replace(/\s+/g, ' ').slice(0, 700),
  };
}
"""


def on_console(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return parsed.hostname == "admin.google.com" and parsed.path.startswith("/ac")


def dump(page, out: Path, tag: str) -> dict:
    try:
        info = page.evaluate(FORM_JS)
    except Exception as exc:
        info = {"url": page.url, "error": str(exc)[:120]}
    page.screenshot(path=str(out / f"{tag}.png"))
    print(f"\n--- {tag} ---")
    print(f"  url      {info.get('url', '')[:110]}")
    if info.get("inputs"):
        print(f"  inputs   {len(info['inputs'])}: " +
              ", ".join(f"{x['label'] or '?'}" for x in info["inputs"][:10]))
    if info.get("buttons"):
        print(f"  buttons  {' | '.join(info['buttons'])[:300]}")
    if info.get("body"):
        print(f"  body     {info['body'][:260]}")
    return info


def add_rows(page, target_rows: int) -> int:
    """Grow the form to `target_rows` rows; returns the row count reached."""
    for _ in range(target_rows - DEFAULT_ROWS):
        try:
            btn = page.get_by_role("button", name="Add another user")
            if not (btn.count() and btn.first.is_visible()):
                btn = page.get_by_text("Add another user", exact=False)
            btn.first.click()
            page.wait_for_timeout(250)
        except Exception:
            break
    return count_rows(page)


def count_rows(page) -> int:
    try:
        n = page.evaluate(
            "() => document.querySelectorAll('input[type=text]').length")
        return int(n) // 4
    except Exception:
        return 0


def wait_ready(page, rows: int, limit: int = 120) -> bool:
    """Continue stays disabled until every row's username check comes back.

    The availability lookups are asynchronous per row, so pressing Continue too
    early just re-runs validation and leaves the page where it was. Poll for
    `rows` "Available" markers plus an enabled Continue.
    """
    start = time.time()
    while time.time() - start < limit:
        try:
            available = page.evaluate("() => document.body.innerText.split('Available').length - 1")
            disabled = page.evaluate("""() => {
                const b = [...document.querySelectorAll('button')]
                    .find(el => /Continue|Next/.test(el.innerText || ''));
                if (!b) return true;
                return b.disabled === true || b.getAttribute('aria-disabled') === 'true';
            }""")
            if available >= rows and not disabled:
                return True
        except Exception:
            pass
        page.wait_for_timeout(1500)
    return False


def read_values(page) -> list[str]:
    """Read the grid straight from the DOM.

    Locator handles go stale: the availability check re-renders rows and swaps
    input nodes, so a handle captured before the check can point at a detached
    element and read back empty. Re-read by document order every time.
    """
    try:
        return page.evaluate(
            "() => [...document.querySelectorAll('input[type=text]')].map(e => e.value)")
    except Exception:
        return []


def fill_rows(page, users: list[dict]) -> int:
    """Type into the row grid, then verify and repair. Per row: first, last, email."""
    expected: list[str] = []
    for user in users:
        expected += [user["first"], user["last"], user["email"].split("@")[0], ""]

    fields = page.locator('input[type="text"]')
    total = fields.count()
    for idx, value in enumerate(expected):
        if idx % 4 == 3 or idx >= total:   # secondary email stays empty
            continue
        try:
            field = fields.nth(idx)
            field.click()
            field.fill("")
            field.press_sequentially(value, delay=10)
        except Exception:
            pass

    page.wait_for_timeout(1500)

    for attempt in range(3):
        values = read_values(page)
        bad = [i for i in range(min(len(expected), len(values)))
               if i % 4 != 3 and (values[i] or "").strip() != expected[i]]
        if not bad:
            break
        print(f"  repair pass {attempt + 1}: {len(bad)} field(s) {bad[:6]}")
        fields = page.locator('input[type="text"]')
        for i in bad:
            try:
                field = fields.nth(i)
                field.click()
                field.fill("")
                field.press_sequentially(expected[i], delay=10)
            except Exception:
                pass
        page.wait_for_timeout(1800)

    values = read_values(page)
    filled = 0
    for row in range(len(users)):
        ok = all((values[row * 4 + c] or "").strip() == expected[row * 4 + c] for c in range(3)) \
            if row * 4 + 2 < len(values) else False
        if ok:
            filled += 1
    return filled


def wait_all_added(page, rows: int, limit: int = 240) -> int:
    """Wait until every card says "User added" and none still say "Adding user"."""
    start = time.time()
    while time.time() - start < limit:
        try:
            body = page.evaluate("() => document.body.innerText")
            added = body.count("User added")
            pending = body.count("Adding user")
            if added >= rows and pending == 0:
                return added
        except Exception:
            pass
        page.wait_for_timeout(2500)
    return 0


def capture_passwords(page, users: list[dict]) -> list[dict]:
    """Click each card's copy icon and read the generated password off the clipboard.

    Google shows these passwords exactly once, on this page. Anything not read
    here is unrecoverable and has to be reset afterwards.
    """
    captured: list[dict] = []
    buttons = page.locator('[aria-label="content_copy"]')
    count = buttons.count()
    print(f"  copy buttons on the results page: {count}")
    for idx in range(min(count, len(users))):
        password = ""
        try:
            page.evaluate("() => navigator.clipboard.writeText('')")
            buttons.nth(idx).click()
            page.wait_for_timeout(350)
            password = page.evaluate("() => navigator.clipboard.readText()") or ""
        except Exception as exc:
            print(f"  [{idx}] clipboard failed: {str(exc)[:70]}")
        captured.append({**users[idx], "password": password.strip()})
    return captured


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["probe", "fill"], default="probe")
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--users", default="accounts/cloudidentity/users.json")
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--batch", type=int, default=10)
    ap.add_argument("--hold", type=int, default=60)
    args = ap.parse_args()

    out = ROOT / "debug" / "admin_bulk" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    users = json.loads((ROOT / args.users).read_text())[args.start: args.start + args.batch]

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile),
            headless=False,
            args=LAUNCH_ARGS,
            locale="en-US",
            viewport={"width": 1440, "height": 1100},
        )
        try:
            ctx.grant_permissions(["clipboard-read", "clipboard-write"],
                                  origin="https://admin.google.com")
        except Exception:
            pass
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(BULKADD_URL, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(5000)

        if not on_console(page.url):
            print(f"not signed in ({page.url[:90]})")
            ctx.close()
            return 1

        dump(page, out, "01_form")
        if args.mode == "probe":
            page.wait_for_timeout(args.hold * 1000)
            ctx.close()
            return 0

        rows = add_rows(page, len(users))
        print(f"\nrows on form: {rows} (wanted {len(users)})")
        filled = fill_rows(page, users[:rows])
        print(f"rows filled:  {filled}")

        page.screenshot(path=str(out / "02_filled.png"), full_page=True)
        page.wait_for_timeout(800)

        if filled != rows:
            print("not every row took input -- stopping before Continue")
            dump(page, out, "02_stopped")
            page.wait_for_timeout(args.hold * 1000)
            ctx.close()
            return 1

        ready = wait_ready(page, rows)
        print(f"ready: {ready} (all usernames checked, Continue enabled)")

        for name in ("Continue", "Next"):
            try:
                btn = page.get_by_role("button", name=name)
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    print(f"clicked {name}")
                    break
            except Exception:
                continue

        page.wait_for_timeout(6000)
        info = dump(page, out, "03_step2")
        (out / "step2.json").write_text(json.dumps(info, indent=2))

        added = wait_all_added(page, len(users))
        print(f"users added: {added}/{len(users)}")
        page.screenshot(path=str(out / "04_result.png"), full_page=True)

        if added:
            captured = capture_passwords(page, users)
            with created.open("a") as fh:
                for row in captured:
                    fh.write(json.dumps({**row, "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S")}) + "\n")
            good = sum(1 for r in captured if r["password"])
            print(f"passwords captured: {good}/{len(captured)}")
            if good != len(captured):
                print("  !! some passwords were not captured and must be reset")

        print(f"\nsaved -> {out}")
        page.wait_for_timeout(args.hold * 1000)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
