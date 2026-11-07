#!/usr/bin/env python3
"""Walk the Cloud Identity signup while already signed into a Google account.

Question: does an authenticated session skip `crossflowverification` on the
Workspace/Cloud Identity funnel? Consumer signup does not — measured
2026-11-06, signed in, same QR.

    ./tools/cloud_identity_probe.py --domain marekfarm.duckdns.org
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

URL = "https://workspace.google.com/gcpidentity/signup?sku=identitybasic"
ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run",
        "--no-default-browser-check", "--disable-dev-shm-usage",
        "--window-size=1366,900"]


def dump(page, out, tag, log):
    try:
        text = (page.evaluate("document.body.innerText") or "")[:1500]
    except Exception:
        text = ""
    inputs = page.evaluate(
        "()=>[...document.querySelectorAll('input,select,button')].map(e=>"
        "`${e.tagName.toLowerCase()} name=${e.getAttribute('name')||''} "
        "type=${e.getAttribute('type')||''} "
        "label=${(e.getAttribute('aria-label')||'').slice(0,40)} "
        "text=${(e.innerText||'').trim().slice(0,40)}`).slice(0,60)")
    rec = {"tag": tag, "url": page.url, "text": text, "inputs": inputs}
    log.append(rec)
    try:
        page.screenshot(path=str(out / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    print(f"\n--- {tag} ---")
    print("url ", page.url[:150])
    print("text", text[:400].replace("\n", " | "))
    for i in inputs[:14]:
        print("   ", i)
    return rec


def fill_any(page, selectors, value):
    for sel in selectors:
        try:
            loc = page.locator(sel)
            if loc.count():
                loc.first.fill(value)
                return sel
        except Exception:
            continue
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain", default="marekfarm.duckdns.org")
    ap.add_argument("--business", default="Marek Farm")
    ap.add_argument("--first", default="Marek")
    ap.add_argument("--last", default="Reid")
    ap.add_argument("--admin", default="admin")
    ap.add_argument("--password", default="Th3-Quiet-Depth-94")
    ap.add_argument("--profile", default=".profiles/signedin_probe")
    ap.add_argument("--hold-minutes", type=int, default=25)
    args = ap.parse_args()

    out = ROOT / "debug" / "cloud_identity" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    log = []

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str((ROOT / args.profile).resolve()),
            headless=False, locale="en-US", args=ARGS,
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)
        info = dump(page, out, "01_landing", log)

        # generic walk: fill whatever the page asks for, press the primary CTA
        for step in range(10):
            try:
                text = (page.evaluate("document.body.innerText") or "").lower()
            except Exception:
                text = ""
            if "crossflowverification" in page.url or "scan the qr" in text:
                print("\n== PHONE GATE ==")
                break

            filled = []
            f = fill_any(page, ['input[name="businessName"]',
                                'input[aria-label*="business" i]',
                                'input[id*="business" i]'], args.business)
            if f: filled.append(f"business<-{f}")
            f = fill_any(page, ['input[name="domain"]',
                                'input[aria-label*="domain" i]',
                                'input[id*="domain" i]'], args.domain)
            if f: filled.append(f"domain<-{f}")
            f = fill_any(page, ['input[name="username"]',
                                'input[aria-label*="username" i]',
                                'input[id*="username" i]'], args.admin)
            if f: filled.append(f"admin<-{f}")
            for sel in ('input[name="password"]', 'input[type="password"]'):
                try:
                    loc = page.locator(sel)
                    if loc.count():
                        loc.first.fill(args.password)
                        filled.append(f"password<-{sel}")
                        break
                except Exception:
                    pass
            # account page: username + password + confirm + agree checkbox
            if "create a username" in text:
                for i in range(page.locator('input[type="text"]').count()):
                    try:
                        el = page.locator('input[type="text"]').nth(i)
                        if el.is_visible() and not el.input_value():
                            el.fill(args.admin)
                            filled.append(f"admin<-text[{i}]")
                            break
                    except Exception:
                        continue
                n = page.locator('input[type="password"]').count()
                for i in range(n):
                    try:
                        el = page.locator('input[type="password"]').nth(i)
                        if el.is_visible():
                            el.fill(args.password)
                    except Exception:
                        continue
                filled.append(f"passwords<-{n}")
                try:
                    cb = page.locator('input[type="checkbox"]')
                    for i in range(cb.count()):
                        if cb.nth(i).is_visible():
                            cb.nth(i).check(force=True)
                            filled.append("agree<-checked")
                            break
                except Exception:
                    pass

            # domain page: one unlabelled text input, placeholder "Enter a domain name"
            if "domain name" in text:
                try:
                    sel = page.locator('input[type="text"]')
                    if sel.count() >= 1:
                        sel.first.fill(args.domain)
                        filled.append("domain<-index")
                except Exception:
                    pass

            # contact page: two unlabelled text inputs are first then last name
            if "contact info" in text:
                try:
                    texts = page.locator('input[type="text"]')
                    if texts.count() >= 2:
                        texts.nth(0).fill(args.first)
                        texts.nth(1).fill(args.last)
                        filled.append("first/last<-index")
                except Exception:
                    pass

            # recovery email
            f = fill_any(page, ['input[type="email"]'], "user@example.com")
            if f: filled.append(f"recovery<-{f}")
            # required radio groups (employee count) must be set before Next
            try:
                checked = page.evaluate(
                    "()=>[...document.querySelectorAll('input[type=radio]')]"
                    ".some(e=>e.checked)")
                if not checked:
                    radios = page.locator('input[type="radio"]')
                    if radios.count():
                        radios.first.check(force=True)
                        filled.append("radio<-first")
            except Exception:
                pass

            print(f"\nstep {step}: filled {filled}")

            clicked = False
            for name in ("Agree and continue", "Get started", "Sign up",
                         "Next", "Continue"):
                try:
                    loc = page.get_by_role("button", name=name, exact=False)
                    if loc.count() and loc.first.is_enabled():
                        loc.first.click(timeout=5000)
                        clicked = True
                        print(f"  clicked {name}")
                        break
                except Exception:
                    continue
            if not clicked:
                print("  no CTA found")
                break
            page.wait_for_timeout(4000)
            info = dump(page, out, f"02_step_{step}", log)

        result = {"url": page.url, "text": info["text"][:600],
                  "gate": "PHONE_GATE" if "crossflow" in page.url else "OTHER"}
        (out / "result.json").write_text(json.dumps(result, indent=2))
        (out / "trace.json").write_text(json.dumps(log, indent=2))
        print("\n=== RESULT ===")
        print(json.dumps(result, indent=2)[:900])
        print(f"artifacts: {out}")
        deadline = time.time() + args.hold_minutes * 60
        while time.time() < deadline:
            page.wait_for_timeout(4000)
            try:
                if not ctx.pages:
                    break
            except Exception:
                break
        try:
            ctx.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
