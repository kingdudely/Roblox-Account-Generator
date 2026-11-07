#!/usr/bin/env python3
"""Does "Use your existing email" avoid the phone/QR gate?

The desktop signup now ends in `crossflowverification`: scan a QR, then send an
outbound SMS from a real phone. That killed the web route for volume.

But the username step offers a second door — binding the new Google Account to an
address you already own instead of minting a Gmail. If that door skips
crossflowverification, then a temp inbox is enough to make Google Accounts, and
identity supply becomes mintable again.

This drives that exact branch and reports the gate it lands on.

    python3 tools/google_existing_email_probe.py
    python3 tools/google_existing_email_probe.py --keep-open

Artifacts under debug/google_existing/<stamp>/.
Exit 0 if the flow reached something other than PHONE_GATE / crossflow.
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from playwright.sync_api import sync_playwright

from tools.google_signup_probe import (
    LAUNCH_ARGS, SIGNUP_URL, analyse_gate, click_next, fill, pick, survey,
)

MAIL_API = "https://api.mail.tm"


# ---------------------------------------------------------------- temp inbox

def create_inbox() -> tuple[str, str]:
    domains = requests.get(f"{MAIL_API}/domains", timeout=20).json()["hydra:member"]
    domain = next(d["domain"] for d in domains if d.get("isActive"))
    local = "g" + secrets.token_hex(6)
    address = f"{local}@{domain}"
    password = secrets.token_urlsafe(16)
    r = requests.post(f"{MAIL_API}/accounts", timeout=20,
                      json={"address": address, "password": password})
    r.raise_for_status()
    tok = requests.post(f"{MAIL_API}/token", timeout=20,
                        json={"address": address, "password": password})
    tok.raise_for_status()
    return address, tok.json()["token"]


def messages(token: str) -> list[dict]:
    r = requests.get(f"{MAIL_API}/messages", timeout=20,
                     headers={"Authorization": f"Bearer {token}"})
    if r.status_code != 200:
        return []
    return r.json().get("hydra:member", [])


def message_body(token: str, mid: str) -> dict:
    r = requests.get(f"{MAIL_API}/messages/{mid}", timeout=20,
                     headers={"Authorization": f"Bearer {token}"})
    return r.json() if r.status_code == 200 else {}


def wait_for_code(token: str, timeout: float = 90) -> tuple[str | None, str]:
    """Return (6-digit code, raw text). Google sends either a code or a link."""
    deadline = time.time() + timeout
    seen = set()
    while time.time() < deadline:
        for m in messages(token):
            mid = m.get("id")
            if not mid or mid in seen:
                continue
            seen.add(mid)
            detail = message_body(token, mid)
            text = " ".join([
                detail.get("subject", ""),
                " ".join(detail.get("text", []) if isinstance(detail.get("text"), list)
                         else [str(detail.get("text") or "")]),
            ])
            html = " ".join(detail.get("html", []) if isinstance(detail.get("html"), list)
                            else [str(detail.get("html") or "")])
            blob = f"{text} {html}"
            match = re.search(r"\b(\d{6})\b", blob)
            if match:
                return match.group(1), blob[:1200]
            if "accounts.google.com" in blob:
                link = re.search(r"https://accounts\.google\.com/[^\s\"'<>]+", blob)
                if link:
                    return None, link.group(0)[:800]
        time.sleep(3)
    return None, ""


# ---------------------------------------------------------------- flow

def click_text(page, *patterns: str, timeout: float = 8) -> bool:
    deadline = time.time() + timeout
    rx = re.compile("|".join(patterns), re.I)
    while time.time() < deadline:
        for loc in (page.get_by_role("button"), page.locator("button, a, [role=button]")):
            try:
                for i in range(min(loc.count(), 40)):
                    el = loc.nth(i)
                    label = (el.inner_text() or el.get_attribute("aria-label") or "").strip()
                    if label and rx.search(label) and el.is_visible():
                        el.click()
                        return True
            except Exception:
                continue
        page.wait_for_timeout(500)
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=".profiles/google_existing")
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--proxy", default=None, help="e.g. socks5://host:port")
    ap.add_argument("--first", default="Elliot")
    ap.add_argument("--last", default="Marsh")
    args = ap.parse_args()

    out = ROOT / "debug" / "google_existing" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (ROOT / args.profile).mkdir(parents=True, exist_ok=True)

    address, token = create_inbox()
    print(f"inbox    {address}")
    (out / "inbox.txt").write_text(f"{address}\n{token}\n")

    surveys: list = []
    final_gate = "UNKNOWN"

    with sync_playwright() as pw:
        proxy = {"server": args.proxy} if args.proxy else None
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=False,
            args=LAUNCH_ARGS, proxy=proxy, locale="en-US",
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)

        info = survey(page, out, surveys, "landing")
        gate = analyse_gate(info)
        print(f"  gate     {gate}")

        if gate == "NAME":
            fill(page, 'input[name="firstName"]', args.first)
            fill(page, 'input[name="lastName"]', args.last)
            click_next(page)
            info = survey(page, out, surveys, "after_name")
            gate = analyse_gate(info)
            print(f"  gate     {gate}")

        if gate == "BIRTHDAY":
            for attempt in range(3):
                fill(page, 'input[name="day"]', "9")
                fill(page, 'input[name="year"]', "1991")
                pick(page, "month", "June")
                pick(page, "gender", "Male")
                click_next(page)
                page.wait_for_timeout(2800)
                if "birthdaygender" not in page.url:
                    break
                print(f"  birthday retry {attempt + 1}")
            info = survey(page, out, surveys, "after_birthday")
            gate = analyse_gate(info)
            print(f"  gate     {gate}")

        # Two possible shapes here:
        #   US: username step with a "Use your existing email" button
        #   EU: collectemailphone with a direct emailPhone field
        typed = False
        if page.locator('input[name="emailPhone"]').count():
            print("\nEU shape: filling emailPhone directly")
            typed = fill(page, 'input[name="emailPhone"]', address)
            info = survey(page, out, surveys, "collectemailphone")
            print(f"  gate     {analyse_gate(info)}")
        else:
            print("\nclicking 'Use your existing email'")
            if not click_text(page, r"use your existing email", r"existing email"):
                print("  !! button not found; dumping screen and stopping")
                survey(page, out, surveys, "no_email_button")
                ctx.close()
                return 3
            page.wait_for_timeout(2500)
            info = survey(page, out, surveys, "existing_email_form")
            print(f"  gate     {analyse_gate(info)}")
            for sel in ('input[name="Email"]', 'input[type="email"]',
                        'input[name="email"]', 'input[autocomplete="email"]'):
                if fill(page, sel, address):
                    typed = True
                    break
        if not typed:
            print("  !! no email input recognised")
        click_next(page)
        page.wait_for_timeout(2500)
        info = survey(page, out, surveys, "after_email")
        gate = analyse_gate(info)
        print(f"  gate     {gate}")

        # password if it appears
        if gate == "PASSWORD" or "Passwd" in " ".join(info.get("inputs") or []):
            pw_value = "Th3-Quiet-Depth-94"
            fill(page, 'input[name="Passwd"]', pw_value)
            fill(page, 'input[name="PasswdAgain"]', pw_value)
            click_next(page)
            page.wait_for_timeout(3000)
            info = survey(page, out, surveys, "after_password")
            gate = analyse_gate(info)
            print(f"  gate     {gate}")

        # what came next
        password_done = False
        for step in range(10):
            info = survey(page, out, surveys, f"gate_{step}")
            gate = analyse_gate(info)
            url = info.get("url", "")
            body = (info.get("body") or "").lower()
            inputs = " ".join(info.get("inputs") or [])
            print(f"  gate     {gate}")

            if "crossflowverification" in url or "scan the qr" in body:
                final_gate = "CROSSFLOW_QR"
                print("\nRESULT: existing-email path still hits the QR/phone gate")
                break

            if gate == "PASSWORD" or "Passwd" in inputs:
                pw_value = "Th3-Quiet-Depth-94"
                fill(page, 'input[name="Passwd"]', pw_value)
                fill(page, 'input[name="PasswdAgain"]', pw_value)
                click_next(page)
                password_done = True
                page.wait_for_timeout(3500)
                continue

            # email verification: pull the code from the inbox
            if "code" in inputs and (
                    "check your" in body or "sent" in body
                    or "verify your email" in body or "enter the code" in body):
                print("  waiting for Google's email ...")
                code, raw = wait_for_code(token, timeout=90)
                if code:
                    print(f"  code {code}")
                    entered = False
                    for sel in ('input[name="code"]', 'input[type="text"]',
                                'input[type="tel"]', 'input[inputmode="numeric"]'):
                        if fill(page, sel, code):
                            entered = True
                            break
                    if not entered:
                        try:
                            page.keyboard.type(code, delay=60)
                            entered = True
                        except Exception:
                            pass
                    if entered:
                        click_next(page)
                        page.wait_for_timeout(3000)
                        continue
                elif raw:
                    print(f"  link {raw[:120]}")
                    try:
                        page.goto(raw, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2500)
                        continue
                    except Exception as exc:
                        print(f"  link open failed: {type(exc).__name__}")
                else:
                    print("  no email arrived")
                    final_gate = "EMAIL_NOT_RECEIVED"
                    break

            if ("welcome" in body or "you're all set" in body or "i agree" in body
                    or "/signup/consent" in url or "/signup/welcome" in url
                    or "/signup/complete" in url or "/signup/created" in url):
                final_gate = "CREATED_OR_CONSENT"
                print("\nRESULT: reached the end without a phone gate")
                break

            if not click_next(page):
                final_gate = gate
                print(f"\nRESULT: stopped at "
                      f"{gate if gate != 'UNKNOWN' else 'an unrecognised screen'}")
                break
            page.wait_for_timeout(3000)

        (out / "survey.json").write_text(json.dumps(surveys, indent=2))
        print(f"\nfinal gate: {final_gate}")

        # prove the session is real: an unauthenticated myaccount redirects to login
        if password_done:
            try:
                page.goto("https://myaccount.google.com/", wait_until="domcontentloaded",
                          timeout=60000)
                page.wait_for_timeout(2500)
                title = page.title()
                landed = page.url
                signed_in = "myaccount.google.com" in landed and "signin" not in landed
                print(f"session  {'SIGNED IN' if signed_in else 'not signed in'}")
                print(f"  title  {title[:90]}")
                print(f"  url    {landed[:110]}")
                (out / "session.json").write_text(json.dumps(
                    {"address": address, "signed_in": signed_in,
                     "url": landed, "title": title}, indent=2))
                if signed_in:
                    final_gate = "ACCOUNT_CREATED"
            except Exception as exc:  # noqa: BLE001
                print(f"session check failed: {type(exc).__name__}")

        (out / "result.json").write_text(json.dumps(
            {"address": address, "final_gate": final_gate,
             "password_done": password_done}, indent=2))
        print(f"address  {address}")
        print(f"artifacts -> {out}")

        if args.keep_open:
            print("holding 90s for a visual check")
            page.wait_for_timeout(90000)
        ctx.close()

    return 0 if final_gate in ("CREATED_OR_CONSENT", "ACCOUNT_CREATED") else 1


if __name__ == "__main__":
    raise SystemExit(main())
