#!/usr/bin/env python3
"""Create Roblox accounts through Google SSO, unattended.

Why this works
--------------
A fresh Cloud Identity account is challenged on its first sign-in -- "Verify it's
you" -- and the only door Google offers is a phone number. Enforcing 2-Step
Verification does not help: the account has no enrolled method, so the policy
deadlocks instead of driving enrollment.

What does work is the admin-generated *backup verification code*. The Admin SDK
mints ten of them per user with `verificationCodes.generate`, they are returned as
plain JSON, and Google accepts one at the challenge screen under "Try another
way -> Enter one of your backup codes". No phone, no TOTP enrollment, no admin
console, and each code is single-use so a burned identity cannot be replayed.

Per identity this costs two API calls and one browser sign-in. Once Roblox mints
`.ROBLOSECURITY` the Google identity is spent -- the Roblox account never needs it
again -- so a temporary credential is all we ever needed.

    ./tools/sso_fleet.py --limit 3                  # three identities, watch it work
    ./tools/sso_fleet.py --calibrate                # one identity, stop on any stall
    ./tools/sso_fleet.py --start 10 --limit 20

The browser profile persists across identities; google.com cookies are dropped
between runs so each sign-in starts at the identifier page instead of reusing an
account chooser.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from cloud_identity import load_key_file, service_for
from tools.oauth_capture import SIGNUP, MAC_UA, COOKIE, validate
from roblox_creator.identity import make_username
from tools.roblox_http import finish as http_finish

OUT_DEFAULT = "accounts/sso/accounts.jsonl"
LEDGER_DEFAULT = "accounts/cloudidentity/users.json"
TOKEN_DEFAULT = "accounts/cloudidentity/token.json"
CODE_DIR = "accounts/cloudidentity/codes"

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1366,900",
]


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- codes

def mint_codes(service, email: str) -> list[str]:
    """Ten fresh backup codes. Generating invalidates the previous set."""
    service.verificationCodes().generate(userKey=email).execute()
    listing = service.verificationCodes().list(userKey=email).execute()
    return [item["verificationCode"] for item in listing.get("items", [])]


def save_codes(email: str, codes: list[str]) -> Path:
    out = ROOT / CODE_DIR / f"{email.split('@')[0]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "user": email, "codes": codes,
        "minted_at": time.strftime("%Y-%m-%dT%H:%M:%S")}, indent=2))
    out.chmod(0o600)
    return out


# ------------------------------------------------------------------------- browser

def clear_domain(ctx, needle: str) -> None:
    """Drop cookies for one domain family, leaving the other side intact."""
    try:
        for ck in ctx.cookies():
            if needle in ck["domain"]:
                try:
                    ctx.clear_cookies(name=ck["name"], domain=ck["domain"])
                except Exception:
                    pass
    except Exception:
        pass


# Returns coordinates instead of clicking: `element.click()` from evaluate is a
# synthetic click with no user activation, so Chrome opens the popup without a
# real opener and Google's redirect can never complete the Roblox signup. A
# trusted click at the same point behaves like a person pressing the button.
GOOGLE_BUTTON_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    return r.width > 1 && r.height > 1;
  };
  const els = [...document.querySelectorAll('button,a,[role=button],div[role=button]')]
      .filter(vis);
  const cands = els.filter(e => /google/i.test(
      (e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')));
  return cands.map(e => {
    const r = e.getBoundingClientRect();
    return {
      label: ((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || ''))
                 .replace(/\s+/g, ' ').trim().slice(0, 70),
      tag: e.tagName,
      href: (e.getAttribute('href') || '').slice(0, 160),
      x: r.left + r.width / 2, y: r.top + r.height / 2,
      w: Math.round(r.width), h: Math.round(r.height),
    };
  });
}
"""

VARIANT_JS = r"""
() => {
  const names = [...document.querySelectorAll('button,a,[role=button]')]
    .map(e => ((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')).toLowerCase());
  return names.some(n => n.includes('google'));
}
"""


def open_signup(ctx, tries: int = 14):
    """Reload the signup page until this session's layout offers Google."""
    for attempt in range(1, tries + 1):
        clear_domain(ctx, "roblox")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.goto("about:blank")
            page.goto(SIGNUP, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_selector("#signup-username", state="visible", timeout=20000)
            page.wait_for_timeout(1500)
            offered = page.evaluate(VARIANT_JS)
        except Exception as exc:
            log(f"    load {attempt}: {type(exc).__name__}")
            continue
        log(f"    layout {attempt}: google={offered}")
        if offered:
            return page
    return None


def find_google_page(ctx, page):
    """Click through to the Google sign-in, which may be a popup or same-tab."""
    def google_page():
        for p in ctx.pages:
            try:
                if "accounts.google.com" in p.url:
                    return p
            except Exception:
                continue
        try:
            if "accounts.google.com" in page.url:
                return page
        except Exception:
            pass
        return None

    try:
        cands = page.evaluate(GOOGLE_BUTTON_JS) or []
    except Exception as exc:
        log(f"    google scan failed: {str(exc)[:100]}")
        cands = []
    for c in cands:
        log(f"      candidate: {c['label']!r} <{c['tag']}> "
            f"({c['w']}x{c['h']}) href={c['href'][:80]!r}")

    # The signup CTA is the one that carries the SSO handshake. A bare "Google"
    # button is a different, older login flow -- clicking it yields an
    # sso-complete?sso-type=login with no partner params.
    def rank(c):
        label = c["label"].lower()
        href = c["href"].lower()
        score = 0
        if "sign up with google" in label:
            score += 100
        if "signup" in label or "create" in label:
            score += 40
        if "sso" in href or "oauth" in href or "auth.roblox" in href:
            score += 60
        if c["w"] * c["h"] > 4000:
            score += 10
        return score

    box = max(cands, key=rank) if cands else None
    if box:
        log(f"    clicking: {box['label']!r} at ({box['x']:.0f},{box['y']:.0f})")
        try:
            page.mouse.click(box["x"], box["y"])
        except Exception as exc:
            log(f"    click failed: {str(exc)[:100]}")
    else:
        log("    google button not found")
    page.wait_for_timeout(3000)

    got = google_page()
    if got:
        return got

    # some layouts open an interstitial that carries its own Google button
    for name in ("Sign up with Google", "Continue with Google", "Sign in with Google"):
        try:
            loc = page.get_by_role("button", name=name)
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                log(f"    clicked: {name}")
                page.wait_for_timeout(3500)
                got = google_page()
                if got:
                    return got
        except Exception:
            continue
    return None


def body_text(page) -> str:
    try:
        return (page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
    except Exception:
        return ""


def visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return bool(loc.count()) and loc.first.is_visible()
    except Exception:
        return False


def fill(page, selector: str, value: str) -> bool:
    try:
        loc = page.locator(selector)
        if loc.count() and loc.first.is_visible():
            loc.first.click()
            loc.first.fill(value)
            return True
    except Exception:
        pass
    return False


def press(page, labels) -> str | None:
    for label in labels:
        for kind in ("button", "link"):
            try:
                loc = page.get_by_role(kind, name=label, exact=False)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=4000)
                    page.wait_for_timeout(2500)
                    return label
            except Exception:
                continue
    return None


def type_code(page, selector: str, code: str) -> bool:
    """Type a code rather than filling it.

    Google's code field enables its Next button from keystroke events, so a
    programmatic `fill()` leaves the value visible but the button inert.
    """
    try:
        loc = page.locator(selector)
        if not (loc.count() and loc.first.is_visible()):
            return False
        loc.first.click()
        page.wait_for_timeout(250)
        loc.first.fill("")
        page.keyboard.type(code, delay=90)
        return True
    except Exception:
        return False


def press_next(page) -> str | None:
    """Click the visible Next button by coordinates.

    Role-based clicking keeps missing it: the control is present in the DOM but
    Playwright's accessible-name lookup does not settle on it. A coordinate click
    is also a trusted gesture, which is what the popup needs anyway.
    """
    try:
        box = page.evaluate(r"""
        () => {
          const b = [...document.querySelectorAll('button,[role="button"]')]
            .find(e => /^next$/i.test((e.innerText || '').trim()));
          if (!b) return null;
          const r = b.getBoundingClientRect();
          return { x: r.left + r.width / 2, y: r.top + r.height / 2,
                   disabled: !!b.disabled ||
                             b.getAttribute('aria-disabled') === 'true' };
        }""")
    except Exception:
        return None
    if not box:
        return None
    if box.get("disabled"):
        return "disabled"
    try:
        page.mouse.click(box["x"], box["y"])
        page.wait_for_timeout(2000)
        return "clicked"
    except Exception:
        return None


def code_field(page) -> str | None:
    """The backup-code box: numeric, short, and not the password field."""
    try:
        found = page.evaluate(r"""
        () => {
          const vis = el => {
            const r = el.getBoundingClientRect();
            return r.width > 1 && r.height > 1;
          };
          const inputs = [...document.querySelectorAll('input')].filter(vis);
          for (const i of inputs) {
            const type = (i.type || 'text').toLowerCase();
            if (type === 'password' || type === 'email') continue;
            const label = (i.getAttribute('aria-label') || i.name || '') .toLowerCase();
            const hints = label + ' ' + (i.getAttribute('placeholder') || '').toLowerCase();
            const short = (i.maxLength > 0 && i.maxLength <= 12);
            if (/code|backup|verify|otp/.test(hints) || (type === 'tel') ||
                (short && type === 'text' && i.autocomplete !== 'username')) {
              return i.id ? '#' + CSS.escape(i.id) : null;
            }
          }
          return null;
        }
        """)
        return found
    except Exception:
        return None


def google_signin(popup, email: str, password: str, code: str,
                  out_dir: Path, index: int, deadline: float = 210.0,
                  stop_flag: dict | None = None) -> str:
    """Walk the Google sign-in. Returns a short outcome string."""
    last_url = ""
    start = time.time()
    tried_another_way = False
    code_submitted = False
    dumped_code_screen = False
    last_action = ""

    while time.time() - start < deadline:
        try:
            url = popup.url
        except Exception:
            # popup closed: the main page should be carrying the redirect
            return f"closed after {last_url[:140]}"
        low = body_text(popup)
        last_url = url

        # a probe can halt the flow mid-redirect to test redeeming it elsewhere
        if stop_flag and stop_flag.get("url"):
            return "interrupted"

        if "accounts.google.com" not in url:
            return f"left-google {url[:140]}"

        # identifier
        if visible(popup, 'input[type="email"]') or visible(popup, "#identifierId"):
            if fill(popup, 'input[type="email"]', email) or fill(popup, "#identifierId", email):
                last_action = f"identifier -> {press(popup, ['Next'])}"
                log(f"      {last_action}")
            continue

        # password
        if visible(popup, 'input[type="password"]'):
            if fill(popup, 'input[type="password"]', password):
                last_action = f"password -> {press(popup, ['Next'])}"
                log(f"      {last_action}")
            continue

        # account chooser
        if "use another account" in low:
            hit = press(popup, ["Use another account"])
            log(f"      chooser -> {hit}")
            continue

        # backup code entry
        if ("backup code" in low or "one of your backup codes" in low) and not code_submitted:
            if not dumped_code_screen:
                dumped_code_screen = True
                try:
                    popup.screenshot(path=str(out_dir / f"{index:03d}_codescreen.png"))
                    controls = popup.evaluate(r"""() => {
                      const vis = el => { const r = el.getBoundingClientRect();
                        return r.width > 1 && r.height > 1; };
                      return {
                        inputs: [...document.querySelectorAll('input')].filter(vis)
                          .map(i => ({ id: i.id, name: i.name, type: i.type,
                                       maxLength: i.maxLength,
                                       label: i.getAttribute('aria-label') || '' })),
                        buttons: [...document.querySelectorAll('button,[role=button],a')]
                          .filter(vis).map(b => ((b.innerText || b.getAttribute('aria-label') || '')
                          .replace(/\s+/g, ' ').trim().slice(0, 40))).filter(Boolean),
                      };
                    }""")
                    log(f"      code screen inputs: {controls['inputs']}")
                    log(f"      code screen buttons: {controls['buttons'][:12]}")
                except Exception as exc:
                    log(f"      code screen dump failed: {str(exc)[:80]}")
            sel = code_field(popup) or "#backupCodePin"
            ok = type_code(popup, sel, code)
            if ok:
                log(f"      backup code filled into {sel or 'fallback input'}")
                # Google usually submits itself once the last digit lands, which
                # destroys the button mid-click -- so treat a vanished field as
                # submitted and only press when the field is still there.
                popup.wait_for_timeout(2200)
                still_there = visible(popup, sel)
                if still_there:
                    hit = press_next(popup)
                    if hit in (None, "disabled"):
                        try:
                            popup.keyboard.press("Enter")
                            hit = f"Enter ({hit})"
                        except Exception:
                            pass
                    log(f"      code submit -> {hit}")
                else:
                    log("      code auto-submitted")
                code_submitted = True
            else:
                log("      backup code screen, but no field matched")
            continue

        # get from the phone challenge to the method list
        if not tried_another_way and "try another way" in low:
            hit = press(popup, ["Try another way", "More ways to verify", "Other ways to verify"])
            tried_another_way = True
            log(f"      challenge -> {hit}")
            continue

        # method list: pick backup codes explicitly
        if "backup" in low:
            hit = press(popup, ["Enter one of your backup codes", "Backup codes",
                                "Use a backup code", "Backup code"])
            log(f"      method list -> {hit}")
            continue

        # consent / interstitial
        hit = press(popup, ["Continue", "Allow", "I understand", "Got it"])
        if hit:
            log(f"      consent -> {hit}")
            continue

        try:
            popup.wait_for_timeout(1200)
        except Exception:
            # the popup closes the moment the flow completes -- that is success
            return "closed"

    return "timeout"


def dismiss_tos(page) -> bool:
    """Roblox gates account creation behind an updated-agreements modal."""
    for label in ("I Agree", "I agree", "Agree", "Accept"):
        try:
            loc = page.get_by_role("button", name=label)
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def pick_combo(page, label: str, text: str) -> bool:
    """Choose a value from a Radix combobox.

    The birthday controls are not <select> elements: each is a
    `button[role=combobox]` with an `aria-label`, backed by a hidden native select
    the page keeps out of view. Setting the select changes nothing the form reads
    -- the combobox has to be opened and an item clicked. The listbox is rendered
    in a portal on body, and its items are plain buttons carrying
    `data-radix-collection-item`, not `role=option`.
    """
    try:
        combo = page.locator(f'[role="dialog"] [role="combobox"][aria-label="{label}"]')
        if not combo.count():
            combo = page.get_by_role("combobox", name=label)
        combo.first.click(timeout=8000)
        page.wait_for_timeout(800)

        # A click by coordinates lands on the overlay when the portal is
        # positioned elsewhere, which dismisses the whole dialog. Clicking the
        # item element itself cannot miss.
        hit = page.evaluate(r"""(text) => {
          const lb = document.querySelector('[role="listbox"]');
          if (!lb) return false;
          const items = [...lb.querySelectorAll(
              'button,[role="option"],[data-radix-collection-item]')];
          const match = items.find(e => (e.innerText || '').trim() === text);
          if (!match) return false;
          match.click();
          return true;
        }""", text)

        if not hit:
            log(f"    no option {text!r} for {label}")
            try:
                page.keyboard.press("Escape")
                page.wait_for_timeout(400)
            except Exception:
                pass
            return False

        page.wait_for_timeout(600)
        return True
    except Exception as exc:
        log(f"    combobox {label}: {str(exc)[:90]}")
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False


def finish_account(ctx, page, out_dir: Path, index: int) -> bool:
    """Complete the "One last thing" form that turns a pending account live.

    An SSO signup creates the account straight away, but Roblox leaves it
    un-approved until birthday, username and the terms modal are done. Until then
    every authenticated call answers `403 User is moderated`, which reads like a
    ban and is not one.
    """
    try:
        page.goto("https://www.roblox.com/home", wait_until="domcontentloaded",
                  timeout=45000)
        page.wait_for_timeout(3500)
    except Exception as exc:
        log(f"    finalise nav failed: {str(exc)[:80]}")
        return False

    # The real signal is the birthday combobox, not the presence of a dialog:
    # the terms modal and other overlays count as dialogs too, and treating those
    # as "form present" makes an approved account look like it needs finalising.
    if not page.locator('[role="combobox"][aria-label="Month"]').count():
        log(f"    account already approved ({page.url.split('/')[-1] or 'home'})")
        return True

    for attempt in range(3):
        dismiss_tos(page)
        page.wait_for_timeout(600)

        got = [pick_combo(page, "Month", "May"),
               pick_combo(page, "Day", "15"),
               pick_combo(page, "Year", "1996")]
        log(f"    birthday: {got}")

        for label in ("Female", "Male"):
            try:
                b = page.get_by_role("button", name=label, exact=True)
                if b.count() and b.first.is_visible():
                    b.first.click()
                    page.wait_for_timeout(400)
                    break
            except Exception:
                continue

        # keep the generated username Roblox already proposed unless it collides
        try:
            if visible(page, "#birthday-compliance-username"):
                if attempt:
                    fill(page, "#birthday-compliance-username",
                         make_username(random.Random(), attempt))
                    page.wait_for_timeout(600)
        except Exception:
            pass

        hit = press(page, ["Create account", "Create Account", "Sign Up", "Continue"])
        log(f"    finalise submit -> {hit}")
        page.wait_for_timeout(5000)

        if "/home" in page.url or "/discover" in page.url:
            log("    account approved")
            return True

        body = body_text(page)
        if "taken" in body or "not available" in body or "already" in body:
            log("    username rejected, retrying")
            continue
        if "one last thing" in body:
            continue
        break

    try:
        page.screenshot(path=str(out_dir / f"{index:03d}_finalise.png"))
        (out_dir / f"{index:03d}_finalise.txt").write_text(
            f"url: {page.url}\n\n" + body_text(page))
        log(f"    finalise dump -> {out_dir}")
    except Exception:
        pass
    return "/home" in page.url or "/discover" in page.url


def validate_in_browser(ctx, page, cookie: str) -> dict:
    """Check the session the way the site does: from page context.

    `curl_cffi` returns 403 with an empty body for valid SSO cookies -- that is
    Cloudflare rejecting the client, not the cookie -- so anything that treats a
    non-200 as "bad account" throws away accounts that were created fine.
    """
    try:
        ctx.add_cookies([{"name": COOKIE, "value": cookie,
                          "domain": ".roblox.com", "path": "/"}])
    except Exception:
        pass
    try:
        page.goto("https://www.roblox.com/home", wait_until="domcontentloaded",
                  timeout=45000)
        page.wait_for_timeout(2500)
        raw = page.evaluate("""async () => {
          try {
            const r = await fetch('https://users.roblox.com/v1/users/authenticated',
                                  {credentials: 'include'});
            return r.status + ' ' + (await r.text());
          } catch (e) { return '0 ' + String(e); }
        }""")
    except Exception as exc:
        return {"status": 0, "error": str(exc)[:120]}
    code, _, body = (raw or "0 ").partition(" ")
    out = {"status": int(code) if code.isdigit() else 0, "body": body[:200]}
    if out["status"] == 200:
        try:
            data = json.loads(body)
            out.update({"id": data.get("id"), "name": data.get("name")})
        except Exception:
            pass
    return out


def grab_cookie(ctx, seconds: float = 45.0) -> str | None:
    """Take the session cookie, preferring the domain-wide one.

    `ctx.cookies()` can hold several cookies with the same name: a site-scoped one
    left by the signup page and the real domain-wide session. Taking whichever
    appears first picks the wrong one roughly half the time, and a wrong cookie
    looks exactly like a moderated account when it is validated.
    """
    deadline = time.time() + seconds
    seen = {}
    while time.time() < deadline:
        try:
            for ck in ctx.cookies():
                if ck["name"] == COOKIE and ck.get("value"):
                    seen[(ck["domain"], len(ck["value"]))] = ck
            if seen:
                best = sorted(
                    seen.values(),
                    key=lambda c: (c["domain"].startswith("."), len(c["value"])),
                    reverse=True)[0]
                if len(seen) > 1:
                    log(f"    cookie candidates: "
                        f"{[(c['domain'], len(c['value'])) for c in seen.values()]}")
                return best["value"]
        except Exception:
            pass
        time.sleep(1.0)
    return None


# ---------------------------------------------------------------------------- run

def run_identity(ctx, service, user: dict, index: int, total: int,
                 calibrate: bool, out_dir: Path,
                 no_finalise: bool = False) -> dict | None:
    email = user["email"]
    password = user["password"]
    log(f"\n[{index}/{total}] {email}")

    try:
        codes = mint_codes(service, email)
    except Exception as exc:
        log(f"    mint failed: {str(exc)[:160]}")
        return None
    if not codes:
        log("    no codes returned")
        return None
    save_codes(email, codes)
    log(f"    codes minted: {len(codes)} (spending {codes[0]})")

    clear_domain(ctx, "google")
    page = open_signup(ctx)
    if page is None:
        log("    no Google button in any layout")
        return None

    popup = find_google_page(ctx, page)
    if popup is None:
        log("    google sign-in never opened")
        page.screenshot(path=str(out_dir / f"{index:03d}_no_google.png"))
        return None
    popup.wait_for_timeout(2500)
    log(f"    google: {popup.url[:80]}")

    outcome = google_signin(popup, email, password, codes[0], out_dir, index)
    log(f"    outcome: {outcome}")
    for extra in ctx.pages:
        try:
            log(f"    page: {extra.url[:140]}")
        except Exception:
            pass

    # "closed" is the normal success case: the popup disappears once Google
    # hands control back, so the cookie is the only real signal.
    cookie = grab_cookie(ctx)
    if not cookie:
        log("    no .ROBLOSECURITY appeared")
        for target, name in ((page, "main"), (popup, "popup")):
            try:
                target.screenshot(path=str(out_dir / f"{index:03d}_{name}.png"))
                (out_dir / f"{index:03d}_{name}.txt").write_text(
                    f"url: {target.url}\n\n" + body_text(target))
            except Exception:
                pass
        log(f"    dumped -> {out_dir}")
        return None

    if no_finalise:
        target = out_dir / f"{index:03d}_cookie.txt"
        target.write_text(cookie)
        log(f"    cookie saved, not finalised -> {target}")
        return None

    # The account exists but Roblox leaves it pending. Finalise over HTTP first:
    # the whole "One last thing" dialog is one POST to /v1/account/upgrade, so the
    # browser only needs to be involved if that call ever stops working.
    result = http_finish(cookie)
    check = result.get("session") or {}
    if check.get("status") != 200:
        log(f"    http finalise: {result.get('upgrade')} | session {check.get('status')}")
        log("    falling back to the browser form")
        finish_account(ctx, page, out_dir, index)
        check = validate_in_browser(ctx, page, cookie)
    else:
        log(f"    http finalise ok: {check.get('name')} (id {check.get('id')})")
    log(f"    session: {check.get('status')} "
        f"{check.get('name') or check.get('body') or check.get('error') or ''}")
    if check.get("status") != 200:
        try:
            (out_dir / f"{index:03d}_cookie.txt").write_text(cookie)
            log(f"    cookie kept -> {out_dir / f'{index:03d}_cookie.txt'}")
        except Exception:
            pass
        for target, name in ((page, "main"), (popup, "popup")):
            try:
                target.screenshot(path=str(out_dir / f"{index:03d}_{name}_badcookie.png"))
                (out_dir / f"{index:03d}_{name}_badcookie.txt").write_text(
                    f"url: {target.url}\n\n" + body_text(target))
            except Exception:
                pass
        log(f"    dumped -> {out_dir}")
        return None

    record = {
        "username": check.get("name"),
        "password": None,
        "birthday": None,
        "gender": None,
        "user_id": check.get("id"),
        "display_name": check.get("name"),
        "cookie": cookie,
        "status": "created",
        "note": f"Google SSO {email} via backup verification code",
        "proxy": None,
        "captcha": "sso-backup-code",
        "identity": email,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
    }
    out = ROOT / OUT_DEFAULT
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    log(f"    saved -> {out}")
    return record


def launch_context(pw, profile: str, headless: bool = False,
                   proxy: str | None = None):
    """One persistent Chromium profile, reused across every identity."""
    launch: dict = {"headless": headless}
    if headless:
        # channel="chromium" selects the new headless implementation; the
        # default headless build is a separate, more detectable shell.
        launch["channel"] = "chromium"
    if proxy:
        # Chromium ignores SOCKS5 auth, so credentials only work on http
        # proxies here; unauthenticated socks5 is fine.
        from urllib.parse import unquote, urlparse

        parts = urlparse(proxy)
        launch["proxy"] = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
        if parts.username:
            launch["proxy"]["username"] = unquote(parts.username)
            launch["proxy"]["password"] = unquote(parts.password or "")
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(ROOT / profile), **launch,
        args=LAUNCH_ARGS, user_agent=MAC_UA,
        viewport={"width": 1366, "height": 900},
        locale="en-US", color_scheme="light")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default=LEDGER_DEFAULT)
    ap.add_argument("--token", default=TOKEN_DEFAULT)
    ap.add_argument("--profile", default=".profiles/ss")
    ap.add_argument("--start", type=int, default=1, help="1-based index into the ledger")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--calibrate", action="store_true",
                    help="stop on any stall and dump the screen")
    ap.add_argument("--no-finalise", action="store_true",
                    help="stop after capturing the cookie (leaves the account pending)")
    ap.add_argument("--record", action="store_true",
                    help="dump every google/roblox request and response to "
                         "requests.jsonl for HTTP reimplementation")
    ap.add_argument("--proxy", default=None,
                    help="socks5://user:pass@host:port or http://host:port — routes "
                         "both the browser and the Admin SDK through this exit, so "
                         "Google sees one consistent residential address")
    ap.add_argument("--headless", action="store_true",
                    help="new-headless Chromium; prefer xvfb-run, which is headed "
                         "on a virtual display and leaves no fingerprint difference")
    args = ap.parse_args()

    users = json.loads((ROOT / args.ledger).read_text())
    batch = users[args.start - 1: args.start - 1 + args.limit]
    if not batch:
        log("nothing to do")
        return 2

    out_dir = ROOT / "debug" / "fleet" / time.strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)

    if args.proxy:
        # the identity and the sign-in must leave from the same address, or the
        # account is created on one network and used from another
        log(f"exit: {args.proxy.split('@')[-1]}")
    service = service_for(load_key_file(ROOT / args.token), args.proxy)
    made = 0
    started = time.time()

    with sync_playwright() as pw:
        ctx = launch_context(pw, args.profile, args.headless, args.proxy)
        if args.record:
            rec_path = out_dir / "requests.jsonl"

            def on_request(req):
                if not any(k in req.url for k in ("google", "roblox", "rbxcdn")):
                    return
                if req.resource_type not in ("document", "xhr", "fetch"):
                    return
                try:
                    headers = {k: v for k, v in req.all_headers().items()
                               if k.lower() in ("content-type", "origin", "referer",
                                                "x-client-data", "x-same-domain",
                                                "authorization")}
                except Exception:
                    headers = {}
                try:
                    body = req.post_data or ""
                except Exception:
                    body = "<binary>"
                rec = {"dir": "req", "method": req.method, "url": req.url,
                       "type": req.resource_type, "headers": headers,
                       "body": body[:4000]}
                with rec_path.open("a") as fh:
                    fh.write(json.dumps(rec) + "\n")

            def on_response(res):
                if not any(k in res.url for k in ("google", "roblox", "rbxcdn")):
                    return
                if res.request.resource_type not in ("document", "xhr", "fetch"):
                    return
                try:
                    heads = res.all_headers()
                except Exception:
                    heads = {}
                picked = {k: v for k, v in heads.items()
                          if k.lower() in ("location", "set-cookie", "content-type")}
                rec = {"dir": "res", "status": res.status, "url": res.url,
                       "headers": picked[:1] if isinstance(picked, list) else picked}
                with rec_path.open("a") as fh:
                    fh.write(json.dumps(rec) + "\n")

            ctx.on("request", on_request)
            ctx.on("response", on_response)
            log(f"    recording -> {rec_path}")

        try:
            for i, user in enumerate(batch, start=args.start):
                # One identity must never take the batch down: a closed window,
                # a Google hiccup or a layout that never offers SSO all end the
                # same way -- log it and move to the next identity.
                try:
                    record = run_identity(ctx, service, user, i, len(users),
                                          args.calibrate, out_dir,
                                          args.no_finalise)
                except Exception as exc:
                    log(f"    aborted: {type(exc).__name__}: {str(exc)[:140]}")
                    record = None
                    try:
                        if not ctx.pages:
                            log("    browser gone; stopping")
                            break
                    except Exception:
                        break
                if record:
                    made += 1
                if args.calibrate:
                    break
        finally:
            ctx.close()

    elapsed = int(time.time() - started)
    rate = (made / elapsed * 3600) if elapsed else 0
    log(f"\ncreated {made} in {elapsed}s  ({rate:.1f}/hour)")
    log(f"artifacts -> {out_dir}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(main())
