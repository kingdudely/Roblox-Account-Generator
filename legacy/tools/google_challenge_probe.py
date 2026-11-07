#!/usr/bin/env python3
"""Sign in as a Cloud Identity admin and enumerate the challenge alternatives.

The Workspace signup lands on `challenge/iap/qrcode` — and unlike the consumer
crossflow page, this one has a **Try another way** control. This tool lists what
that control offers, which decides whether a new admin can be verified without a
phone (recovery email, backup codes, etc.).

    ./tools/google_challenge_probe.py --email admin@example.duckdns.org
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

ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run",
        "--no-default-browser-check", "--window-size=1366,900"]


def txt(page):
    try:
        return page.evaluate("document.body.innerText") or ""
    except Exception:
        return ""


def dump(page, out, tag, log):
    try:
        controls = page.evaluate(
            "()=>[...document.querySelectorAll('button,a,[role=button],input')]"
            ".filter(e=>{const r=e.getBoundingClientRect();return r.width>1&&r.height>1})"
            ".map(e=>`${e.tagName.toLowerCase()} | "
            "${(e.innerText||e.getAttribute('aria-label')||e.value||'').trim().slice(0,60)} | "
            "${(e.getAttribute('href')||'').slice(0,80)}`).slice(0,40)")
    except Exception:
        controls = []
    rec = {"tag": tag, "url": page.url, "text": txt(page)[:1400], "controls": controls}
    log.append(rec)
    try:
        page.screenshot(path=str(out / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    print(f"\n--- {tag} ---")
    print("url ", page.url[:150])
    print("text", rec["text"][:500].replace("\n", " | "))
    for c in controls[:25]:
        print("   ", c)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--password", default="Th3-Quiet-Depth-94")
    ap.add_argument("--profile", default=".profiles/challenge_probe")
    ap.add_argument("--hold-minutes", type=int, default=25)
    args = ap.parse_args()

    out = ROOT / "debug" / "google_challenge" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    log = []

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str((ROOT / args.profile).resolve()),
            headless=False, locale="en-US", args=ARGS,
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://accounts.google.com/signin", wait_until="domcontentloaded",
                  timeout=60000)
        page.wait_for_timeout(2500)

        # always use a fresh identifier field
        try:
            if page.locator('input[type="email"]').count() == 0:
                page.get_by_text("Use another account", exact=False).first.click()
                page.wait_for_timeout(2000)
        except Exception:
            pass

        for sel in ('input[type="email"]', "#identifierId"):
            try:
                if page.locator(sel).count():
                    page.locator(sel).first.fill(args.email)
                    break
            except Exception:
                continue
        for name in ("Next",):
            try:
                page.get_by_role("button", name=name, exact=False).first.click()
            except Exception:
                pass
        page.wait_for_timeout(3500)

        if page.locator('input[type="password"]').count():
            page.locator('input[type="password"]').first.fill(args.password)
            try:
                page.get_by_role("button", name="Next", exact=False).first.click()
            except Exception:
                pass
            page.wait_for_timeout(6000)

        dump(page, out, "01_challenge", log)

        # the whole point: what alternatives exist?
        clicked = False
        try:
            b = page.get_by_role("button", name="Try another way", exact=False)
            if b.count():
                b.first.click()
                clicked = True
                page.wait_for_timeout(3500)
        except Exception as exc:
            print("try-another-way click failed:", exc)
        info = dump(page, out, "02_try_another_way", log)

        # if it rendered a list of options, click each and record where it goes
        options = []
        try:
            options = page.evaluate(
                "()=>[...document.querySelectorAll('[role=button],button,li,a')]"
                ".filter(e=>{const r=e.getBoundingClientRect();return r.width>1&&r.height>1})"
                ".map(e=>(e.innerText||'').trim()).filter(Boolean).slice(0,25)")
        except Exception:
            pass
        result = {"url": page.url, "text": info["text"][:800],
                  "clicked": clicked, "options": options}
        (out / "result.json").write_text(json.dumps(result, indent=2))
        (out / "trace.json").write_text(json.dumps(log, indent=2))
        print("\n=== RESULT ===")
        print(json.dumps(result, indent=2)[:1200])
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
