#!/usr/bin/env python3
"""Turn off the login challenge for one user, temporarily, from the admin console.

Why this exists
---------------
Every managed identity in this tenant is challenged on its first sign-in with
"Verify it's you -> enter a phone number". That challenge has no API and no
policy switch: enforcing 2-Step Verification is documented to displace risk-based
challenges, but with enforcement on and no method enrolled the account deadlocks
("your sign-in settings don't meet your organization's 2SV policy") because the
method Google wants to offer is the one the policy forbids.

Google does ship one escape hatch, and it is meant for exactly this situation:

    Admin console -> Users -> <user> -> Security -> Login challenge
                  -> "Turn Off For 10 Minutes"

    https://knowledge.workspace.google.com/admin/security/
        temporarily-turn-off-login-challenges-for-a-user

No API is documented for it, so this drives the console and records every RPC the
page fires in reply. That recording is the point: if the call is replayable we can
loop it over a fleet instead of clicking 50 times, and the same recording tells us
the request shape if we later need to reproduce it outside a browser.

    ./tools/admin_login_challenge.py --email admin@example.duckdns.org
    ./tools/admin_login_challenge.py --email ... --mode off

`recon` (default) only looks and dumps. `off` clicks through and still dumps.
Nothing here touches the Roblox side; it unblocks the sign-in, which is upstream.
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

from tools.admin_api_users import build_service

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1440,1000",
]

DUMP_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const txt = el => (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 80);
  const uniq = arr => [...new Set(arr)].filter(Boolean);
  return {
    url: location.href,
    title: document.title,
    tabs: uniq([...document.querySelectorAll('[role="tab"],a[role="tab"]')].filter(vis).map(txt)).slice(0, 20),
    buttons: uniq([...document.querySelectorAll('button,[role="button"]')].filter(vis).map(txt)).slice(0, 50),
    text: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 4000),
  };
}
"""


def on_admin_console(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    return (parsed.hostname == "admin.google.com"
            and parsed.path.startswith("/ac")
            and "checkout" not in parsed.path)


def resolve_user_id(email: str, token_path: Path) -> str | None:
    """Numeric directory ID, which is what /ac/users/<id> deep links want."""
    try:
        service = build_service(token_path)
        user = service.users().get(userKey=email).execute()
        return user.get("id")
    except Exception as exc:
        print(f"  users.get failed: {str(exc)[:120]}")
        return None


def dump(page, out: Path, tag: str) -> dict:
    try:
        info = page.evaluate(DUMP_JS)
    except Exception as exc:
        info = {"url": page.url, "error": str(exc)[:120]}
    try:
        page.screenshot(path=str(out / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    print(f"\n--- {tag} ---")
    print(f"  url      {info.get('url', '')[:120]}")
    print(f"  title    {info.get('title', '')[:100]}")
    if info.get("tabs"):
        print(f"  tabs     {' | '.join(info['tabs'])[:300]}")
    if info.get("buttons"):
        print(f"  buttons  {' | '.join(info['buttons'])[:600]}")
    if info.get("text"):
        print(f"  text     {info['text'][:600]}")
    return info


def click_label(page, labels, timeout: int = 5000) -> str | None:
    """Click the first visible control matching any label, by role then by text."""
    for label in labels:
        for kind in ("tab", "button", "link", "menuitem"):
            try:
                loc = page.get_by_role(kind, name=label, exact=False)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=timeout)
                    page.wait_for_timeout(2200)
                    return f"{label} ({kind})"
            except Exception:
                continue
        try:
            loc = page.get_by_text(label, exact=False)
            if loc.count() and loc.first.is_visible():
                loc.first.click(timeout=timeout)
                page.wait_for_timeout(2200)
                return f"{label} (text)"
        except Exception:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True, help="user to unblock")
    ap.add_argument("--mode", choices=["recon", "off", "codes"], default="recon")
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--token", default="accounts/cloudidentity/token.json")
    ap.add_argument("--wait", type=int, default=600, help="seconds to wait for admin sign-in")
    ap.add_argument("--hold", type=int, default=90)
    args = ap.parse_args()

    out = ROOT / "debug" / "challenge" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    user_id = resolve_user_id(args.email, ROOT / args.token)
    print(f"user id: {user_id}")
    target = (f"https://admin.google.com/ac/users/{user_id}" if user_id
              else "https://admin.google.com/ac/users")

    rpc_log = out / "rpc.jsonl"

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=False,
            args=LAUNCH_ARGS, locale="en-US",
            viewport={"width": 1440, "height": 1000})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # Record every admin-console XHR plus its body. The 10-minute toggle is
        # undocumented; this log is how we learn its call.
        def on_request(req):
            if "admin.google.com" not in req.url and "batchexecute" not in req.url:
                return
            if req.method == "GET":
                return
            try:
                body = req.post_data
            except Exception:
                body = None
            rec = {"url": req.url[:220], "method": req.method,
                   "body": (body or "")[:1200]}
            with rpc_log.open("a") as fh:
                fh.write(json.dumps(rec) + "\n")

        page.on("request", on_request)

        page.goto(target, wait_until="domcontentloaded", timeout=90000)
        start = time.time()
        while time.time() - start < args.wait and not on_admin_console(page.url):
            if int(time.time() - start) % 5 == 0:
                print(f"  waiting for admin session … {page.url[:100]}")
            page.wait_for_timeout(1500)
        if not on_admin_console(page.url):
            dump(page, out, "01_not_signed_in")
            print("\nadmin session not established; sign in in this window")
            page.wait_for_timeout(args.hold * 1000)
            ctx.close()
            return 1

        page.wait_for_timeout(6000)
        dump(page, out, "02_user_page")

        # The Security tab holds the Login challenge control.
        hit = click_label(page, ["Security"])
        print(f"security tab: {hit}")
        page.wait_for_timeout(4000)
        info = dump(page, out, "03_security")

        text = (info.get("text") or "").lower()
        if "login challenge" not in text and "login challenges" not in text:
            print("\n!! no 'Login challenge' control on this page -- see screenshot")

        if args.mode == "codes":
            # Stage 1: the inline link on the 2SV card, which opens the panel.
            hit = click_label(page, ["Get backup verification codes",
                                     "backup verification codes"])
            print(f"card link: {hit}")
            page.wait_for_timeout(3000)
            dump(page, out, "06_panel")

            # Stage 2: the button *inside* the panel. Same words as the link, so
            # match the role and take the last one -- the dialog renders after.
            try:
                btn = page.get_by_role("button", name="Get backup verification codes",
                                       exact=True)
                print(f"panel buttons: {btn.count()}")
                btn.last.click(timeout=8000)
                print("clicked panel button")
            except Exception as exc:
                print(f"panel button: {str(exc)[:120]}")
            page.wait_for_timeout(4000)
            dump(page, out, "06b_backup_codes")

            # Backup codes are 8 digits, or 4x4 alphanumeric groups depending on
            # the console build. Scrape both shapes out of the dialog.
            found = page.evaluate(r"""() => {
              const t = document.body.innerText || '';
              const digits = t.match(/\b\d{8}\b/g) || [];
              const groups = t.match(/\b[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}\b/g) || [];
              const dialog = [...document.querySelectorAll('[role="dialog"]')]
                  .map(d => (d.innerText || '').replace(/\s+/g,' ').slice(0, 1500));
              return { digits: [...new Set(digits)].slice(0,20),
                       groups: [...new Set(groups)].slice(0,10),
                       dialogs: dialog };
            }""")
            print(f"\n  digit codes ({len(found['digits'])}): {found['digits']}")
            print(f"  grouped codes ({len(found['groups'])}): {found['groups']}")
            for d in found["dialogs"]:
                print(f"  dialog: {d[:700]}")

            # scroll the security card to its tail: Login challenge / sign-in cookies
            try:
                page.mouse.wheel(0, 1400)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            tail = page.evaluate(r"""() => {
              const t = document.body.innerText || '';
              const i = t.toLowerCase().indexOf('login challenge');
              return i > -1 ? t.slice(Math.max(0, i - 200), i + 600).replace(/\s+/g,' ')
                            : t.slice(-800).replace(/\s+/g,' ');
            }""")
            print(f"\n  login-challenge area: {tail}")
            dump(page, out, "07_tail")

        if args.mode == "off":
            hit = click_label(page, ["Login challenge", "Login challenges"])
            print(f"login challenge: {hit}")
            page.wait_for_timeout(3000)
            dump(page, out, "04_login_challenge")
            hit = click_label(page, ["Turn Off For 10 Minutes", "Turn off for 10 minutes",
                                     "Turn Off", "Turn off"])
            print(f"toggle: {hit}")
            page.wait_for_timeout(4000)
            dump(page, out, "05_after_off")

        print(f"\nsaved -> {out}")
        print(f"rpc log -> {rpc_log}")
        if rpc_log.exists():
            lines = rpc_log.read_text().strip().splitlines()
            print(f"recorded {len(lines)} console RPCs")
        print(f"holding {args.hold}s")
        page.wait_for_timeout(args.hold * 1000)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
