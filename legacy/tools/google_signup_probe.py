#!/usr/bin/env python3
"""Probe Google's own account signup and report exactly which gates appear.

Why this exists
---------------
The Roblox side is solved: Google SSO creates accounts with no captcha. What is
left is identity supply, and the question is empirical -- does Google still let a
fresh signup finish from this connection without a paid SMS number? Every claim
about "the phone step is skippable" online is stale or region-specific, so the
only honest answer comes from driving the flow and recording what it asks for.

This tool walks the signup wizard, screenshots every screen, and dumps the visible
interactive elements at each step. It stops at the phone-verification screen and
prints any control that looks like an escape hatch. It never submits a phone
number.

    ./tools/google_signup_probe.py --mode desktop
    ./tools/google_signup_probe.py --mode mobile
    ./tools/google_signup_probe.py --mode desktop --profile .profiles/google1

Output goes to debug/google_signup/<timestamp>/:
    step_<n>_<name>.png    one screenshot per screen
    survey.json            headings, buttons, links, inputs, body text per screen
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

SIGNUP_URL = ("https://accounts.google.com/signup/v2/createaccount"
              "?flowName=GlifWebSignIn&flowEntry=SignUp")

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1366,900",
]

# text on any control that would let the flow continue without a phone number
ESCAPE_HINTS = ("skip", "not now", "later", "verify later", "do this later",
                "remind me", "no thanks", "continue without", "next")

SURVEY_JS = r"""
() => {
  const vis = el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 1 && r.height > 1 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  const txt = el => (el.innerText || el.value || el.getAttribute('aria-label') || '')
      .replace(/\s+/g, ' ').trim().slice(0, 70);
  const uniq = a => [...new Set(a.filter(Boolean))];
  return {
    url: location.href,
    title: document.title,
    headings: uniq([...document.querySelectorAll('h1,h2,[role="heading"]')].filter(vis).map(txt)).slice(0, 8),
    buttons: uniq([...document.querySelectorAll('button,[role="button"],div[jsname][role="button"]')]
                   .filter(vis).map(txt)).slice(0, 25),
    links: uniq([...document.querySelectorAll('a[href]')].filter(vis).map(txt)).slice(0, 20),
    inputs: [...document.querySelectorAll('input,select,textarea')].filter(vis)
              .map(e => `${e.getAttribute('name') || e.id || '?'}:${e.type || e.tagName.toLowerCase()}`),
    selects: [...document.querySelectorAll('select')].map(s => ({
      id: s.id, name: s.name || null, visible: vis(s),
      opts: [...s.options].slice(0, 4).map(o => o.text).filter(Boolean),
    })),
    combos: [...document.querySelectorAll('[role="combobox"],[role="listbox"],[aria-haspopup]')]
              .filter(vis).map(e => (e.id || e.getAttribute('aria-label') || e.getAttribute('role'))
                                    + (e.getAttribute('aria-haspopup') ? '/popup' : '')).slice(0, 10),
    body: document.body.innerText.replace(/\s+/g, ' ').trim().slice(0, 900),
  };
}
"""


def survey(page, out: Path, surveys: list, step: str) -> dict:
    page.wait_for_timeout(1500)
    try:
        info = page.evaluate(SURVEY_JS)
    except Exception as exc:  # navigation raced the evaluate
        info = {"url": page.url, "error": str(exc)[:120]}
    info["step"] = step
    surveys.append(info)
    page.screenshot(path=str(out / f"step_{len(surveys):02d}_{step}.png"))
    print(f"\n--- {step} ---")
    print(f"  url      {info.get('url', '')[:110]}")
    if info.get("headings"):
        print(f"  heading  {' | '.join(info['headings'])[:150]}")
    if info.get("inputs"):
        print(f"  inputs   {', '.join(info['inputs'])[:150]}")
    if info.get("selects"):
        print(f"  selects  {json.dumps(info['selects'])[:190]}")
    if info.get("combos"):
        print(f"  combos   {', '.join(info['combos'])[:150]}")
    if info.get("buttons"):
        print(f"  buttons  {' | '.join(info['buttons'])[:190]}")
    if info.get("links"):
        print(f"  links    {' | '.join(info['links'])[:190]}")
    return info


def click_next(page) -> bool:
    """Google labels the primary button 'Next'; jsname varies between builds."""
    for loc in (
        page.get_by_role("button", name="Next"),
        page.locator('button:has-text("Next")'),
        page.locator('[jsname="V67aGc"]'),
        page.locator('div[jsname="V67aGc"]'),
    ):
        try:
            if loc.count() and loc.first.is_visible():
                loc.first.click()
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Enter")
        return True
    except Exception:
        return False


def fill(page, selector: str, value: str) -> bool:
    """Set a field and prove the value survived.

    Google's Material components re-render on blur and will silently drop a value
    written too early in the flow, which then fails validation on submit. So: type
    with real key events, read the field back, retry on mismatch.
    """
    for _ in range(3):
        try:
            loc = page.locator(selector).first
            if not (loc.count() and loc.is_visible()):
                return False
            loc.click()
            loc.fill("")
            loc.press_sequentially(value, delay=35)
            if (loc.input_value() or "").strip() == value:
                return True
            page.wait_for_timeout(400)
        except Exception:
            page.wait_for_timeout(300)
    return False


def analyse_gate(info: dict) -> str:
    """Classify the current screen: what is Google asking for?"""
    body = (info.get("body") or "").lower()
    inputs = " ".join(info.get("inputs") or [])
    buttons = [b.lower() for b in (info.get("buttons") or [])]
    if "phoneNumber" in inputs or "phone number" in body or "verify your phone" in body:
        return "PHONE_GATE"
    if "Passwd" in inputs or "confirmpasswd" in inputs.lower():
        return "PASSWORD"
    if "Username" in inputs or "usernameRadio" in inputs:
        return "USERNAME"
    if "year" in inputs or "birthday" in body and "day" in inputs:
        return "BIRTHDAY"
    if "firstName" in inputs:
        return "NAME"
    if "welcome" in body or "privacy" in body and "terms" in body:
        return "CONSENT_OR_WELCOME"
    if any("skip" in b or "not now" in b for b in buttons):
        return "GATE_WITH_SKIP"
    return "UNKNOWN"


def pick(page, label: str, option_text: str) -> bool:
    """Choose an option from one of Google's Material dropdowns.

    These are `<select>` elements covered by a styled div, so Playwright's normal
    actionability checks can reject them. Layered fallbacks, cheapest first.
    """
    # 1. native select by id or accessible name
    for sel in (f"#{label}", f'select[name="{label}"]'):
        try:
            loc = page.locator(sel).first
            if loc.count():
                try:
                    page.select_option(sel, label=option_text)
                    return True
                except Exception:
                    pass
                # 2. force the value through React and fire change
                ok = page.evaluate(
                    """([sel, want]) => {
                        const s = document.querySelector(sel);
                        if (!s) return false;
                        const opt = [...s.options].find(o => o.text.trim().toLowerCase() === want.toLowerCase())
                                 || [...s.options].find(o => o.text.trim().toLowerCase().startsWith(want.toLowerCase()));
                        if (!opt) return false;
                        s.value = opt.value;
                        s.dispatchEvent(new Event('input', { bubbles: true }));
                        s.dispatchEvent(new Event('change', { bubbles: true }));
                        return true;
                    }""", [sel, option_text])
                if ok:
                    return True
        except Exception:
            pass
    # 3. click the styled control open, then click the text
    try:
        page.locator(f"#{label}").first.click(force=True)
        page.wait_for_timeout(500)
        page.get_by_role("option", name=option_text, exact=False).first.click()
        return True
    except Exception:
        pass
    try:
        page.get_by_text(option_text, exact=True).first.click()
        return True
    except Exception:
        return False


def verify_field(page, selector: str, want: str) -> bool:
    try:
        loc = page.locator(selector).first
        return loc.count() and (loc.input_value() or "").strip().lower() == want.lower()
    except Exception:
        return False


def capture_qr(page, out: Path) -> str | None:
    """Pull the QR image out of the gate screen and decode it to a URL."""
    import base64

    data = page.evaluate("""() => {
        const img = [...document.querySelectorAll('img')]
            .find(i => (i.src || '').startsWith('data:image') && i.naturalWidth > 80);
        if (img) return { kind: 'img', src: img.src };
        const c = [...document.querySelectorAll('canvas')].find(c => c.width > 80);
        if (c) return { kind: 'canvas', src: c.toDataURL('image/png') };
        return null;
    }""")
    if not data:
        print("  no QR element found on the gate screen")
        return None

    raw = base64.b64decode(data["src"].split(",", 1)[1])
    png = out / "gate_qr.png"
    png.write_bytes(raw)

    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
    detector = cv2.QRCodeDetector()
    url = ""
    # Google renders the gate QR as a 240px 1-bit PNG; the bare image often will
    # not decode, but inverted and upscaled it does.
    for variant in (
        img,
        255 - img,
        cv2.resize(img, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST),
        cv2.resize(255 - img, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST),
    ):
        url, *_ = detector.detectAndDecode(variant)
        if url:
            break
    if not url:
        print("  QR captured but would not decode")
        return None
    (out / "gate_qr_url.txt").write_text(url)
    print(f"  QR -> {url[:150]}")
    return url


def follow_qr(pw, source_ctx, url: str, out: Path, surveys: list) -> None:
    """Open the QR target as a phone and record what it asks for.

    This is the question that decides whether the Android route is worth the
    install: if the cross-device step resolves in a mobile browser, the gate is
    emulable; if it demands Play Services or a SIM, it is not.
    """
    from playwright.sync_api import sync_playwright as _  # noqa: F401  (type clarity)
    device = pw.devices["Pixel 7"]
    browser = pw.chromium.launch(headless=False, args=LAUNCH_ARGS)
    mobile = browser.new_context(**device, locale="en-US")
    try:
        mobile.add_cookies([{k: c[k] for k in ("name", "value", "domain", "path")}
                            for c in source_ctx.cookies()
                            if "google" in c["domain"] or c["domain"].startswith(".")])
    except Exception as exc:
        print(f"  cookie copy partial: {str(exc)[:60]}")
    page = mobile.new_page()
    print("\n=== opening QR target as Pixel 7 ===")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        print(f"  navigation failed: {str(exc)[:100]}")
    survey(page, out, surveys, "qr_target")
    print(f"  body     {(page.evaluate('document.body.innerText') or '')[:400].replace(chr(10), ' | ')}")
    page.wait_for_timeout(2500)
    page.screenshot(path=str(out / "qr_target_full.png"), full_page=True)
    browser.close()


def escape_controls(info: dict) -> list[str]:
    found = []
    for b in info.get("buttons") or []:
        low = b.lower()
        if any(h in low for h in ("skip", "not now", "later", "verify later", "remind", "without")):
            found.append(b)
    for l in info.get("links") or []:
        low = l.lower()
        if any(h in low for h in ("skip", "not now", "later", "without")):
            found.append(f"link:{l}")
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["desktop", "mobile"], default="desktop")
    ap.add_argument("--profile", default=".profiles/google_probe")
    ap.add_argument("--keep-open", action="store_true",
                    help="hold the browser open after the survey (default: hold)")
    ap.add_argument("--follow-qr", action="store_true",
                    help="decode the gate QR and open it as a phone")
    ap.add_argument("--proxy", default=None,
                    help="route the browser through this exit, e.g. socks5://host:port")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = ROOT / "debug" / "google_signup" / stamp
    out.mkdir(parents=True, exist_ok=True)
    profile = ROOT / args.profile
    profile.mkdir(parents=True, exist_ok=True)

    tag = f"{random.randint(1000, 9999)}"
    first, last = "Marcus", "Delaney"
    username = f"marcus.delaney{tag}"

    surveys: list = []
    with sync_playwright() as pw:
        proxy = {"server": args.proxy} if args.proxy else None
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            args=LAUNCH_ARGS,
            proxy=proxy,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
            user_agent=None if args.mode == "desktop" else (
                "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36"),
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)

        info = survey(page, out, surveys, "landing")
        kind = analyse_gate(info)
        print(f"  gate     {kind}")

        # 1. name
        if kind == "NAME":
            if not fill(page, 'input[name="firstName"]', first):
                print("  !! first name would not take")
            fill(page, 'input[name="lastName"]', last)
            click_next(page)
            info = survey(page, out, surveys, "after_name")
            kind = analyse_gate(info)
            print(f"  gate     {kind}")

        # 2. birthday / gender
        if kind == "BIRTHDAY":
            for attempt in range(3):
                fill(page, 'input[name="day"]', "14")
                fill(page, 'input[name="year"]', "1994")
                if not pick(page, "month", "March"):
                    print("  !! month would not set")
                if not pick(page, "gender", "Male"):
                    print("  !! gender would not set")
                print(f"  try {attempt + 1}: month={verify_field(page, '#month', '3') or '?'}"
                      f" day={verify_field(page, 'input[name=\"day\"]', '14')}"
                      f" year={verify_field(page, 'input[name=\"year\"]', '1994')}")
                click_next(page)
                page.wait_for_timeout(2500)
                if "birthdaygender" not in page.url:
                    break
                print("  still on birthday step, retrying")
            info = survey(page, out, surveys, "after_birthday")
            kind = analyse_gate(info)
            print(f"  gate     {kind}")

        # 3. username -- either a text field or a list of suggested addresses
        if kind == "USERNAME":
            if not fill(page, 'input[name="Username"]', username):
                radios = page.locator('input[name="usernameRadio"]')
                if radios.count():
                    radios.first.check(force=True)
                    print(f"  picked suggested address #{1} of {radios.count()}")
            click_next(page)
            info = survey(page, out, surveys, "after_username")
            kind = analyse_gate(info)
            print(f"  gate     {kind}")

        # 4. password -- this is the last step we can complete without a phone
        if kind == "PASSWORD":
            pw_value = "Th3-Quiet-Depth-94"
            if not fill(page, 'input[name="Passwd"]', pw_value):
                print("  !! password would not set")
            if not (fill(page, 'input[name="PasswdAgain"]', pw_value)
                    or fill(page, 'input[name="ConfirmPasswd"]', pw_value)):
                print("  !! confirm password would not set")
            page.wait_for_timeout(600)
            click_next(page)
            page.wait_for_timeout(3000)
            info = survey(page, out, surveys, "after_password")
            kind = analyse_gate(info)
            print(f"  gate     {kind}")

        # 5. whatever comes next is the answer
        for extra in ("gate_0", "gate_1", "gate_2"):
            if kind in ("PHONE_GATE", "GATE_WITH_SKIP", "CONSENT_OR_WELCOME", "UNKNOWN"):
                info = survey(page, out, surveys, extra)
                kind = analyse_gate(info)
                print(f"  gate     {kind}")
                if kind == "PHONE_GATE":
                    qr_url = capture_qr(page, out)
                    if qr_url and args.follow_qr:
                        follow_qr(pw, ctx, qr_url, out, surveys)
                    break
                escapes = escape_controls(info)
                if escapes:
                    print(f"  ESCAPES  {' | '.join(escapes)}")
                if kind == "PHONE_GATE" or not escapes:
                    break
                # try the escape hatch and see what follows
                target = escapes[0].replace("link:", "")
                try:
                    page.get_by_text(target, exact=False).first.click()
                    print(f"  clicked  {target}")
                except Exception as exc:
                    print(f"  click failed {str(exc)[:80]}")
                    break

        (out / "survey.json").write_text(json.dumps({
            "mode": args.mode,
            "profile": str(profile),
            "final_url": page.url,
            "attempted_username": username,
            "screens": surveys,
        }, indent=2))
        print(f"\nsaved -> {out}")
        print(f"final -> {page.url[:120]}")

        if args.keep_open:
            print("holding browser open; ctrl-c to exit")
            try:
                while True:
                    page.wait_for_timeout(2000)
            except KeyboardInterrupt:
                pass
        else:
            page.wait_for_timeout(3000)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
