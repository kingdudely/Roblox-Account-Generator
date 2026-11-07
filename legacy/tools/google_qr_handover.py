#!/usr/bin/env python3
"""Drive signup to the QR gate, hand it to a phone, then test what the scan bought.

Two questions, one run:

1. Does a real phone complete the `crossflowverification` gate? The screen says
   "Your phone will open an SMS message with a code you need to send" — that is
   an *outbound* SMS, which a normal SIM can send, and which may not consume a
   "new number" the way an inbound code does.

2. Does passing it once trust the browser profile? If the answer is yes, one scan
   covers many accounts and the web path is alive at moderate volume. If every
   signup re-asks, it is one scan per account and the ceiling is your patience.

Flow: fill the form, stop at the QR, print the decoded URL, wait for you to scan.
When the page moves on, record whether the account exists, then immediately start
a second signup in the same profile and report whether the QR reappears.

    python3 tools/google_qr_handover.py
    python3 tools/google_qr_handover.py --minutes 10 --profile .profiles/google_qr
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.google_signup_probe import (
    LAUNCH_ARGS, SIGNUP_URL, analyse_gate, capture_qr, click_next, fill,
    pick, survey,
)


def one_signup(page, out: Path, surveys: list, first: str, last: str,
               username: str, stop_at_qr: bool = True) -> tuple[str, str | None]:
    """Walk the form. Returns (gate, qr_url)."""
    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
    info = survey(page, out, surveys, "landing")
    gate = analyse_gate(info)

    if gate == "NAME":
        fill(page, 'input[name="firstName"]', first)
        fill(page, 'input[name="lastName"]', last)
        click_next(page)
        info = survey(page, out, surveys, "after_name")
        gate = analyse_gate(info)

    if gate == "BIRTHDAY":
        fill(page, 'input[name="day"]', "17")
        fill(page, 'input[name="year"]', "1993")
        pick(page, "month", "August")
        pick(page, "gender", "Female")
        click_next(page)
        page.wait_for_timeout(2500)
        info = survey(page, out, surveys, "after_birthday")
        gate = analyse_gate(info)

    if gate == "USERNAME":
        if not fill(page, 'input[name="Username"]', username):
            radios = page.locator('input[name="usernameRadio"]')
            if radios.count():
                radios.first.check(force=True)
        click_next(page)
        info = survey(page, out, surveys, "after_username")
        gate = analyse_gate(info)

    if gate == "PASSWORD":
        pw_value = "Th3-Quiet-Depth-94"
        fill(page, 'input[name="Passwd"]', pw_value)
        fill(page, 'input[name="PasswdAgain"]', pw_value)
        click_next(page)
        page.wait_for_timeout(3500)
        info = survey(page, out, surveys, "after_password")
        gate = analyse_gate(info)

    qr_url = None
    if gate == "PHONE_GATE":
        qr_url = capture_qr(page, out)
    return gate, qr_url


def wait_for_gate_to_clear(page, out: Path, surveys: list, minutes: float) -> str:
    """Poll until the crossflow screen is gone, or the deadline passes."""
    deadline = time.time() + minutes * 60
    last = ""
    while time.time() < deadline:
        url = page.url
        if url != last:
            last = url
            print(f"  [{int(time.time()) % 10000:>5}] {url[:130]}")
        if "crossflowverification" not in url and "devicephoneverification" not in page.content().lower():
            break
        page.wait_for_timeout(2500)
    info = survey(page, out, surveys, "post_qr")
    return analyse_gate(info)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default=".profiles/google_qr")
    ap.add_argument("--minutes", type=float, default=8,
                    help="how long to wait for the phone scan")
    ap.add_argument("--second", action="store_true", default=True,
                    help="after success, test whether a second signup needs another scan")
    args = ap.parse_args()

    out = ROOT / "debug" / "google_qr" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (ROOT / args.profile).mkdir(parents=True, exist_ok=True)

    surveys: list = []
    result: dict = {}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=False,
            args=LAUNCH_ARGS, locale="en-US",
            viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        tag = f"{secrets.randbelow(9000) + 1000}"
        gate, qr_url = one_signup(page, out, surveys, "Nadia", "Voss",
                                  f"nadia.voss{tag}")
        print(f"\ngate     {gate}")
        result["first_gate"] = gate
        result["qr_url"] = qr_url

        if gate != "PHONE_GATE":
            print("did not reach the QR gate — nothing to hand over")
            (out / "result.json").write_text(json.dumps(result, indent=2))
            ctx.close()
            return 2

        print("\nscan this with your phone (Camera app), then send the SMS it opens")
        print(f"  {qr_url or '(decode failed — look at the window)'}")
        print(f"\nwaiting up to {args.minutes:.0f} min for the scan ...")

        after = wait_for_gate_to_clear(page, out, surveys, args.minutes)
        print(f"after scan: {after}")
        result["after_scan_gate"] = after

        # did the account land?
        signed_in = False
        try:
            page.goto("https://myaccount.google.com/", wait_until="domcontentloaded",
                      timeout=60000)
            page.wait_for_timeout(2000)
            signed_in = ("myaccount.google.com" in page.url
                         and "signin" not in page.url)
            print(f"session   {'SIGNED IN' if signed_in else 'not signed in'}")
            result["signed_in_after_first"] = signed_in
            result["first_url"] = page.url
        except Exception as exc:  # noqa: BLE001
            print(f"session check failed: {type(exc).__name__}")

        # second signup, same profile: does it ask for the phone again?
        if args.second and signed_in:
            print("\nsecond signup, same browser profile ...")
            try:
                ctx.clear_cookies()
            except Exception:
                pass
            page2 = ctx.new_page()
            gate2, qr2 = one_signup(page2, out, surveys, "Owen", "Reyes",
                                    f"owen.reyes{tag}")
            print(f"second gate: {gate2}")
            result["second_gate"] = gate2
            result["second_qr"] = qr2
            if gate2 != "PHONE_GATE":
                print("  -> the scan trusted the profile; no second scan needed")
            else:
                print("  -> the gate is per-signup; one scan per account")

        (out / "survey.json").write_text(json.dumps(surveys, indent=2))
        (out / "result.json").write_text(json.dumps(result, indent=2))
        print(f"\nartifacts -> {out}")
        ctx.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
