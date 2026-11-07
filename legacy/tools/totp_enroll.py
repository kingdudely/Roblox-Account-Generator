#!/usr/bin/env python3
"""Enroll a Google account in TOTP 2-Step Verification, and compute its codes.

Why this exists
---------------
Google offers authenticator-app (TOTP) as a 2SV method that needs no phone number.
That matters because the admin has no phone, and because a TOTP secret is a value
we can store and compute from forever -- unlike an SMS code, which needs a SIM each
time.

The enrollment screen shows a QR code, but it also offers "Can't scan it?", which
reveals the base32 secret as text. Reading text is far more reliable than decoding
a screenshot, so that is the path this takes.

    ./tools/totp_enroll.py --profile .profiles/admin --probe     # reveal, don't finish
    ./tools/totp_enroll.py --profile .profiles/admin --commit    # complete enrollment
    ./tools/totp_enroll.py --code --secret-file accounts/cloudidentity/admin_totp.json

TOTP is HMAC-SHA1 over a time counter -- ~15 lines of stdlib, no dependency, and
the same code path serves the admin and every provisioned user.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_URL = "https://myaccount.google.com/signinoptions/two-step-verification"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1400,1000",
]

SECRET_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  // base32 secrets are shown as 4-char groups, upper case, A-Z2-7
  const groups = [...document.querySelectorAll('*')]
    .filter(vis)
    .map(el => (el.children.length === 0 ? (el.innerText || '') : '').trim())
    .filter(t => /^[A-Z2-7]{4}(\s?[A-Z2-7]{4})+$/.test(t.replace(/\s+/g, ' ')));
  return {
    candidates: [...new Set(groups)].slice(0, 6),
    body: document.body.innerText.replace(/\s+/g, ' ').slice(0, 500),
  };
}
"""


def totp(secret: str, digits: int = 6, period: int = 30, at: float | None = None) -> str:
    key = base64.b32decode(secret.upper().replace(" ", ""), casefold=True)
    counter = int((at if at is not None else time.time()) // period)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(value).zfill(digits)


def click_any(page, labels, timeout: int = 4000) -> str | None:
    for label in labels:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=label)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=timeout)
                    page.wait_for_timeout(1800)
                    return label
            except Exception:
                continue
        try:
            loc = page.get_by_text(label, exact=False)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                page.wait_for_timeout(1800)
                return label
        except Exception:
            continue
    return None


def enroll(args) -> int:
    from playwright.sync_api import sync_playwright

    out = ROOT / "debug" / "totp" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    profile = ROOT / args.profile
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, args=LAUNCH_ARGS,
            locale="en-US", viewport={"width": 1400, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(4000)
        page.screenshot(path=str(out / "01_start.png"))
        print(f"start: {page.url[:110]}")

        # already enrolled?
        body = (page.inner_text("body") or "").lower()
        if "turn off" in body and "2-step" in body:
            print("2SV already appears to be ON for this account")
            page.wait_for_timeout(args.hold * 1000)
            ctx.close()
            return 0

        hit = click_any(page, ["Get started", "Turn on 2-Step Verification", "Turn on 2-step verification"])
        print(f"start clicked: {hit}")
        page.wait_for_timeout(2000)
        page.screenshot(path=str(out / "02_after_start.png"))

        # password re-entry challenge
        try:
            pw_field = page.locator('input[type="password"]')
            if pw_field.count() and pw_field.first.is_visible():
                if args.password:
                    pw_field.first.click()
                    pw_field.first.fill(args.password)
                    click_any(page, ["Next"])
                    page.wait_for_timeout(2500)
                    print("re-entered password")
                else:
                    print("password prompt appeared and --password was not given")
        except Exception:
            pass

        # choose authenticator app; it may sit behind "Show more options"
        for attempt in range(3):
            hit = click_any(page, ["Authenticator app", "Set up authenticator", "Authenticator"])
            if hit:
                print(f"method clicked: {hit}")
                break
            more = click_any(page, ["Show more options", "More options", "Choose another method"])
            print(f"  more options: {more}")
            if not more:
                break
        page.wait_for_timeout(2500)
        page.screenshot(path=str(out / "03_authenticator.png"))

        # reveal the base32 secret instead of decoding a QR image
        reveal = click_any(page, ["Can't scan it?", "Can't scan the QR code?", "Can't scan"])
        print(f"revealed secret control: {reveal}")
        page.wait_for_timeout(1500)
        page.screenshot(path=str(out / "04_secret.png"))

        info = page.evaluate(SECRET_JS)
        print(f"candidates: {info['candidates']}")
        if not info["candidates"]:
            print("no base32 secret found; see screenshot")
            print(f"page says: {info['body'][:300]}")
            page.wait_for_timeout(args.hold * 1000)
            ctx.close()
            return 1

        secret = info["candidates"][0].replace(" ", "")
        code = totp(secret)
        print(f"secret: {secret}")
        print(f"current code: {code}")

        target = ROOT / args.save
        target.parent.mkdir(parents=True, exist_ok=True)
        record = {"label": args.label, "secret": secret, "url": page.url,
                  "enrolled_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
        target.write_text(json.dumps(record, indent=2))
        target.chmod(0o600)
        print(f"saved -> {target}")

        if not args.commit:
            print("\nprobe only -- enrollment not completed. re-run with --commit")
            page.wait_for_timeout(args.hold * 1000)
            ctx.close()
            return 0

        # enter the code
        filled = False
        for selector in ('input[type="tel"]', 'input[type="text"]', 'input[name="code"]'):
            try:
                f = page.locator(selector)
                if f.count() and f.first.is_visible():
                    f.first.click()
                    f.first.fill(totp(secret))
                    filled = True
                    break
            except Exception:
                continue
        print(f"code entered: {filled}")
        if filled:
            hit = click_any(page, ["Next", "Verify", "Done"])
            print(f"submitted: {hit}")
            page.wait_for_timeout(3000)
            page.screenshot(path=str(out / "05_done.png"))
            print(f"final url: {page.url[:110]}")

        page.wait_for_timeout(args.hold * 1000)
        ctx.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--password", default="", help="account password, if Google asks again")
    ap.add_argument("--label", default="admin")
    ap.add_argument("--save", default="accounts/cloudidentity/admin_totp.json")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--commit", action="store_true")
    ap.add_argument("--hold", type=int, default=30)
    ap.add_argument("--code", action="store_true", help="print the current code and exit")
    ap.add_argument("--secret-file", default="accounts/cloudidentity/admin_totp.json")
    args = ap.parse_args()

    if args.code:
        data = json.loads((ROOT / args.secret_file).read_text())
        print(totp(data["secret"]))
        return 0

    return enroll(args)


if __name__ == "__main__":
    raise SystemExit(main())
