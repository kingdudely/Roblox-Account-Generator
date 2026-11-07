#!/usr/bin/env python3
"""Sign into a Google account, then try a signup from that signed-in session.

The hypothesis under test: the phone gate is a stranger's tax. If the browser is
already authenticated, the signup may inherit the session's trust and skip
`crossflowverification`. Nobody in this repo has tested it with a real account.

Credentials are read from a file, never argv, so they stay out of process lists:

    printf '%s\\n%s\\n' 'you@gmail.com' 'password' > /tmp/opencode/g.creds
    chmod 600 /tmp/opencode/g.creds

    ./tools/google_signedin_probe.py --creds /tmp/opencode/g.creds
    ./tools/google_signedin_probe.py --creds ... --also-cloud-identity

Stops and holds the window open whenever Google asks for a verification the tool
cannot answer (2FA, phone, "verify it's you"), so the flow can be completed by
hand instead of guessed at.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.google_signup_probe import pick

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1366,900",
]

SIGNUP_URL = ("https://accounts.google.com/signup/v2/createaccount"
              "?flowName=GlifWebSignIn&flowEntry=SignUp"
              "&continue=https://myaccount.google.com/")
CLOUD_IDENTITY_URL = ("https://workspace.google.com/gcpidentity/signup"
                      "?sku=identitybasic")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_creds(path: Path) -> tuple[str, str]:
    lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    if len(lines) < 2:
        raise SystemExit("creds file needs two lines: email, then password")
    return lines[0], lines[1]


def body(page) -> str:
    try:
        return (page.evaluate("document.body.innerText") or "")[:1200]
    except Exception:
        return ""


def click_text(page, *names: str, timeout: int = 6000) -> bool:
    for name in names:
        for role in ("button", "link"):
            try:
                loc = page.get_by_role(role, name=name, exact=False)
                if loc.count():
                    loc.first.click(timeout=timeout)
                    return True
            except Exception:
                continue
    return False


def dump(page, out: Path, tag: str, log: list) -> dict:
    info = {
        "t": now(),
        "tag": tag,
        "url": page.url,
        "title": page.title(),
        "text": body(page),
        "inputs": page.evaluate(
            "()=>[...document.querySelectorAll('input,select,button')]"
            ".map(e=>`${e.tagName.toLowerCase()}"
            " name=${e.getAttribute('name')||''}"
            " type=${e.getAttribute('type')||''}"
            " text=${(e.innerText||e.getAttribute('aria-label')||'').trim().slice(0,40)}`)"
            ".slice(0,40)"),
    }
    log.append(info)
    try:
        page.screenshot(path=str(out / f"{tag}.png"), full_page=True)
    except Exception:
        pass
    print(f"\n--- {tag} ---")
    print("url   ", page.url[:140])
    print("text  ", info["text"][:500].replace("\n", " | "))
    print("inputs", info["inputs"][:12])
    return info


def signed_in(page) -> bool:
    try:
        page.goto("https://myaccount.google.com/", wait_until="domcontentloaded",
                  timeout=45000)
        page.wait_for_timeout(3000)
        return "accounts.google.com" not in page.url
    except Exception:
        return False


def try_signin(page, email: str, password: str, out: Path, log: list) -> str:
    page.goto("https://accounts.google.com/ServiceLogin?hl=en",
              wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    dump(page, out, "01_login_page", log)

    # identifier
    for sel in ('input[type="email"]', "#identifierId"):
        try:
            if page.locator(sel).count():
                page.locator(sel).first.fill(email)
                break
        except Exception:
            continue
    click_text(page, "Next")
    page.wait_for_timeout(3500)
    info = dump(page, out, "02_after_email", log)

    # password
    if page.locator('input[type="password"]').count():
        page.locator('input[type="password"]').first.fill(password)
        click_text(page, "Next")
        page.wait_for_timeout(6000)
    info = dump(page, out, "03_after_password", log)

    # any challenge we cannot answer?
    text = info["text"].lower()
    challenge = any(k in text for k in (
        "verify it's you", "verify its you", "2-step", "two-step",
        "enter a phone", "tap yes", "check your phone", "code",
        "couldn't find your google account", "wrong password",
        "try again", "unusual activity"))
    if challenge:
        return "challenge"
    return "ok"


def walk_signup(page, out: Path, log: list) -> dict:
    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    info = dump(page, out, "10_signup_landing", log)

    text = info["text"].lower()
    if "crossflow" in page.url or "scan the qr" in text:
        return {"gate": "PHONE_GATE", "where": "landing"}

    # the signed-in variant may ask "whose account" first
    click_text(page, "Add another account", "Use another account")
    page.wait_for_timeout(2500)
    info = dump(page, out, "11_after_add_account", log)

    steps = 0
    name_filled = False
    while steps < 8:
        steps += 1
        text = body(page).lower()
        url = page.url
        if "crossflowverification" in url or "scan the qr" in text:
            dump(page, out, f"20_gate_{steps}", log)
            return {"gate": "PHONE_GATE", "where": url[:120]}

        if page.locator('input[name="firstName"]').count():
            page.locator('input[name="firstName"]').first.fill("Marek")
            if page.locator('input[name="lastName"]').count():
                page.locator('input[name="lastName"]').first.fill("Reid")
            click_text(page, "Next")
            page.wait_for_timeout(3000)
            dump(page, out, f"12_name_{steps}", log)
            name_filled = True
            continue

        if page.locator('input[name="day"]').count():
            for attempt in range(3):
                page.locator('input[name="day"]').first.fill("12")
                page.locator('input[name="year"]').first.fill("1992")
                pick(page, "month", "July")
                pick(page, "gender", "Male")
                click_text(page, "Next")
                page.wait_for_timeout(3200)
                if "birthdaygender" not in page.url:
                    break
                print(f"  birthday retry {attempt + 1}")
            dump(page, out, f"13_birthday_{steps}", log)
            continue

        if page.locator('input[name="usernameRadio"]').count():
            page.locator('input[name="usernameRadio"]').first.check(force=True)
            click_text(page, "Next")
            page.wait_for_timeout(3000)
            dump(page, out, f"14_username_{steps}", log)
            continue

        if page.locator('input[name="Passwd"]').count():
            page.locator('input[name="Passwd"]').first.fill("Th3-Quiet-Depth-94")
            if page.locator('input[name="PasswdAgain"]').count():
                page.locator('input[name="PasswdAgain"]').first.fill("Th3-Quiet-Depth-94")
            click_text(page, "Next")
            page.wait_for_timeout(5000)
            dump(page, out, f"15_password_{steps}", log)
            continue

        dump(page, out, f"16_unknown_{steps}", log)
        break

    return {"gate": "UNKNOWN", "url": page.url[:160],
            "text": body(page)[:300], "name_filled": name_filled}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--creds", type=Path, required=True)
    ap.add_argument("--profile", default=".profiles/signedin_probe")
    ap.add_argument("--hold-minutes", type=int, default=20)
    ap.add_argument("--also-cloud-identity", action="store_true")
    args = ap.parse_args()

    email, password = read_creds(args.creds)
    out = ROOT / "debug" / "google_signedin" / \
        datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    log: list = []

    print(f"account: {email}")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str((ROOT / args.profile).resolve()),
            headless=False, locale="en-US", args=LAUNCH_ARGS,
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        # a warm profile may already hold the session; reuse it so the signup
        # inherits the same trusted browser history that the test is about.
        if signed_in(page):
            state = "already"
            dump(page, out, "00_already_signed_in", log)
        else:
            state = try_signin(page, email, password, out, log)
        print(f"\nsignin: {state}")

        result = {"signin": state, "email": email}
        if state in ("ok", "already"):
            result["signup"] = walk_signup(page, out, log)
            if args.also_cloud_identity:
                page.goto(CLOUD_IDENTITY_URL, wait_until="domcontentloaded",
                          timeout=60000)
                page.wait_for_timeout(4000)
                info = dump(page, out, "30_cloud_identity", log)
                text = info["text"].lower()
                result["cloud_identity"] = {
                    "url": page.url[:160],
                    "gate": ("PHONE_GATE"
                             if ("crossflow" in page.url or "scan the qr" in text)
                             else "NO_GATE_SEEN"),
                    "text": info["text"][:400],
                }

        (out / "result.json").write_text(json.dumps(result, indent=2))
        (out / "trace.json").write_text(json.dumps(log, indent=2))
        print("\n=== RESULT ===")
        print(json.dumps(result, indent=2)[:1500])
        print(f"\nartifacts: {out}")
        print(f"holding window {args.hold_minutes} min — close it or wait")
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
