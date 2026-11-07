#!/usr/bin/env python3
"""Log into DuckDNS with the saved Google session, create a subdomain, keep the token.

DuckDNS sits behind a reCAPTCHA on the login redirect, so the first run needs a
human click. Everything after that is automatic and cached to a 0600 file:

    /tmp/opencode/duckdns.json   {"domain": "...", "token": "..."}

    ./tools/duckdns_session.py --create example
    ./tools/duckdns_session.py --show
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run",
        "--no-default-browser-check", "--window-size=1366,900"]
STORE = Path("/tmp/opencode/duckdns.json")


def text(page) -> str:
    try:
        return page.evaluate("document.body.innerText") or ""
    except Exception:
        return ""


def dump(page, out: Path, tag: str):
    out.mkdir(parents=True, exist_ok=True)
    try:
        (out / f"{tag}.html").write_text(page.content())
        page.screenshot(path=str(out / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    print(f"\n--- {tag} ---")
    print("url:", page.url[:130])
    print("txt:", text(page)[:700].replace("\n", " | "))


def past_captcha(page) -> bool:
    t = text(page).lower()
    if "please complete the recaptcha" in t:
        return False
    return ("add domain" in t or "currentip" in t or "domains" in t
            and "logout" in t)


def do_login(page, out: Path) -> None:
    page.goto("https://www.duckdns.org/", wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)
    t = text(page)
    if "logout" in t and "logged in with" in t:
        print("already logged in")
        return

    page.click('a[href*="generateRequest=google"]')
    page.wait_for_timeout(5000)

    # account chooser
    try:
        loc = page.get_by_text("skunkboi2012", exact=False)
        if loc.count():
            loc.first.click()
            page.wait_for_timeout(4500)
    except Exception:
        pass

    # consent
    for name in ("Continue", "Allow"):
        try:
            b = page.get_by_role("button", name=name, exact=False)
            if b.count() and b.first.is_visible():
                b.first.click()
                page.wait_for_timeout(5000)
                break
        except Exception:
            continue

    page.wait_for_timeout(3000)
    dump(page, out, "after_login")


def try_captcha(page) -> bool:
    """DuckDNS uses an *invisible* reCAPTCHA behind a button, not a checkbox."""
    try:
        btn = page.locator("button.g-recaptcha")
        if btn.count():
            btn.first.click()
            print("clicked the invisible reCAPTCHA button")
            return True
    except Exception as exc:
        print("invisible captcha click failed:", exc)
    try:
        for fr in page.frames:
            if "recaptcha" in (fr.url or "") and "anchor" in (fr.url or ""):
                box = fr.locator("#recaptcha-anchor")
                if box.count():
                    box.first.click()
                    print("clicked reCAPTCHA checkbox")
                    return True
    except Exception as exc:
        print("captcha click failed:", exc)
    return False


def wait_for_captcha(page, out: Path, max_wait: int) -> bool:
    if past_captcha(page):
        return True
    try_captcha(page)
    page.wait_for_timeout(4000)
    if past_captcha(page):
        return True
    print("\n>>> COMPLETE THE reCAPTCHA IN THE OPEN WINDOW <<<")
    print("    the tool will continue the moment the domains page appears")
    start = time.time()
    while time.time() - start < max_wait:
        page.wait_for_timeout(2000)
        if past_captcha(page):
            dump(page, out, "after_captcha")
            return True
    return False


def read_token(page) -> str:
    import re
    # the visible account token is a UUID printed next to the word "token"
    m = re.search(r"token\s+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
                  text(page), re.I)
    if m:
        return m.group(1)
    # the token sits in a small copy-field near the top of the domains page
    for sel in ('#token', 'input[id="token"]', '[name="token"]'):
        try:
            if page.locator(sel).count():
                v = page.locator(sel).first.input_value()
                if v:
                    return v.strip()
        except Exception:
            continue
    # fall back: the longest hex-looking token on the page
    try:
        vals = page.evaluate(
            "()=>[...document.querySelectorAll('input')].map(e=>e.value)"
            ".filter(v=>v && /^[a-f0-9-]{20,}$/i.test(v))")
        if vals:
            return vals[0].strip()
    except Exception:
        pass
    return ""


def create_domain(page, name: str, out: Path) -> bool:
    t = text(page).lower()
    if name.lower() in t:
        print(f"domain {name} already present")
        return True
    filled = False
    for sel in ('#domain', 'input[name="domain"]', 'input[type="text"]'):
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill(name)
                filled = True
                break
        except Exception:
            continue
    if not filled:
        print("!! could not find the subdomain field")
        dump(page, out, "no_domain_field")
        return False
    clicked = False
    for name_btn in ("add domain", "add", "Add domain"):
        try:
            b = page.get_by_role("button", name=name_btn, exact=False)
            if b.count():
                b.first.click()
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        try:
            page.locator('input[type="submit"]').first.click()
            clicked = True
        except Exception:
            pass
    page.wait_for_timeout(4000)
    dump(page, out, "after_add_domain")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", default=None, help="subdomain label, e.g. example")
    ap.add_argument("--profile", default=".profiles/signedin_probe")
    ap.add_argument("--captcha-wait", type=int, default=300)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    if args.show:
        if STORE.exists():
            print(STORE.read_text())
        else:
            print("no saved domains yet")
        return 0

    out = ROOT / "debug" / "duckdns" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str((ROOT / args.profile).resolve()),
            headless=False, locale="en-US", args=ARGS,
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        do_login(page, out)
        if not wait_for_captcha(page, out, args.captcha_wait):
            print("captcha not cleared; stopping")
            ctx.close()
            return 2
        dump(page, out, "domains_page")

        if args.create:
            create_domain(page, args.create, out)

        token = read_token(page)
        domain = f"{args.create}.duckdns.org" if args.create else ""

        if token:
            STORE.parent.mkdir(parents=True, exist_ok=True)
            STORE.write_text(json.dumps({"domain": domain, "token": token}, indent=2))
            os.chmod(STORE, 0o600)
            print(f"\nsaved {domain or '(existing)'} -> {STORE}")
            print(f"token: {token[:6]}…{token[-4:]}  ({len(token)} chars)")
        else:
            print("\nno token found on the page")
            dump(page, out, "no_token")

        print(f"artifacts: {out}")
        print("window stays open 60s")
        page.wait_for_timeout(60000)
        try:
            ctx.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
