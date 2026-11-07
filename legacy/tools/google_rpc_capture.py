#!/usr/bin/env python3
"""Capture the exact batchexecute RPC sequence behind a Google signup.

Why this exists
---------------
The rendered wizard is a thin client over
    accounts.google.com/lifecycle/_/AccountLifecyclePlatformSignupUi/data/batchexecute
If the phone gate is a *server decision* taken at one of those RPCs, then a pure
RPC client hits the same decision without a browser, without BotGuard JS, and
without the UI's little traps. To build that client you first need the wire
truth: every request, every f.req payload, every response.

This tool drives the wizard once in Chromium with full request/response
recording, walks to the crossflow screen, and writes:

    debug/google_rpc/<stamp>/requests.jsonl   all requests + bodies
    debug/google_rpc/<stamp>/rpc.jsonl        only batchexecute calls
    debug/google_rpc/<stamp>/cookies.json     cookie jar at each step
    debug/google_rpc/<stamp>/timeline.txt     human-readable step order

    ./tools/google_rpc_capture.py --proxy socks5://host:port
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.google_signup_probe import (
    LAUNCH_ARGS, SIGNUP_URL, analyse_gate, click_next, fill, pick, survey,
)

RPC_MARKERS = ("batchexecute", "data/batchexecute")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def attach_recorder(ctx, out: Path):
    """Record every request/response into files, return live counters."""
    req_f = (out / "requests.jsonl").open("a", encoding="utf-8")
    rpc_f = (out / "rpc.jsonl").open("a", encoding="utf-8")
    seen = {"n": 0, "rpc": 0}

    def on_request(req):
        seen["n"] += 1
        try:
            body = req.post_data
        except Exception:
            body = None
        rec = {
            "t": now(),
            "n": seen["n"],
            "method": req.method,
            "url": req.url,
            "resource": req.resource_type,
            "headers": dict(req.headers),
            "body": body,
        }
        req_f.write(json.dumps(rec) + "\n")
        req_f.flush()
        if any(m in req.url for m in RPC_MARKERS) or req.resource_type == "xhr":
            seen["rpc"] += 1
            rpc_f.write(json.dumps(rec) + "\n")
            rpc_f.flush()

    def on_response(resp):
        url = resp.url
        if not (any(m in url for m in RPC_MARKERS) or resp.request.resource_type == "xhr"):
            return
        try:
            body = resp.text()
        except Exception:
            body = None
        rec = {
            "t": now(),
            "url": url,
            "status": resp.status,
            "headers": dict(resp.headers),
            "body": (body or "")[:20000],
        }
        rpc_f.write(json.dumps({"response": rec}) + "\n")
        rpc_f.flush()

    ctx.on("request", on_request)
    ctx.on("response", on_response)
    return req_f, rpc_f, seen


def dump_cookies(ctx, out: Path, step: str):
    jar = []
    for c in ctx.cookies():
        jar.append({k: c.get(k) for k in
                    ("name", "value", "domain", "path", "expires", "httpOnly",
                     "secure", "sameSite")})
    (out / f"cookies_{step}.json").write_text(json.dumps(jar, indent=2))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None, help="e.g. socks5://host:port")
    ap.add_argument("--profile", default=".profiles/google_rpc")
    ap.add_argument("--first", default="Tomas")
    ap.add_argument("--last", default="Reid")
    ap.add_argument("--timeout", type=int, default=120000)
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "debug" / "google_rpc" / stamp
    out.mkdir(parents=True, exist_ok=True)
    timeline = (out / "timeline.txt").open("a", encoding="utf-8")

    def note(msg: str):
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        timeline.write(line + "\n")
        timeline.flush()

    surveys: list = []
    with sync_playwright() as pw:
        proxy = {"server": args.proxy} if args.proxy else None
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str((ROOT / args.profile).resolve()),
            headless=False,
            args=LAUNCH_ARGS,
            proxy=proxy,
            locale="en-US",
            viewport={"width": 1366, "height": 900},
        )
        attach_recorder(ctx, out)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        note(f"goto {SIGNUP_URL}")
        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=args.timeout)
        page.wait_for_timeout(2500)
        info = survey(page, out, surveys, "landing")
        gate = analyse_gate(info)
        note(f"gate={gate} url={page.url[:120]}")
        dump_cookies(ctx, out, "landing")

        if gate == "NAME":
            fill(page, 'input[name="firstName"]', args.first)
            fill(page, 'input[name="lastName"]', args.last)
            click_next(page)
            page.wait_for_timeout(2800)
            info = survey(page, out, surveys, "after_name")
            gate = analyse_gate(info)
            note(f"gate={gate} url={page.url[:120]}")
            dump_cookies(ctx, out, "after_name")

        if gate == "BIRTHDAY":
            for attempt in range(4):
                fill(page, 'input[name="day"]', "12")
                fill(page, 'input[name="year"]', "1992")
                pick(page, "month", "July")
                pick(page, "gender", "Male")
                click_next(page)
                page.wait_for_timeout(3000)
                if "birthdaygender" not in page.url:
                    break
                note(f"birthday retry {attempt + 1}")
            info = survey(page, out, surveys, "after_birthday")
            gate = analyse_gate(info)
            note(f"gate={gate} url={page.url[:120]}")
            dump_cookies(ctx, out, "after_birthday")

        # new-gmail branch: collectemailphone -> "don't have" -> username
        if page.locator('input[name="emailPhone"]').count():
            note("collectemailphone: choosing 'don't have an email'")
            for txt in ("Don't have an email address or phone number?",
                        "Don’t have an email address or phone number?",
                        "Don't have an email"):
                try:
                    b = page.get_by_role("button", name=txt, exact=False)
                    if b.count() and b.first.is_visible():
                        b.first.click()
                        break
                except Exception:
                    pass
            page.wait_for_timeout(3000)
            info = survey(page, out, surveys, "after_no_email")
            gate = analyse_gate(info)
            note(f"gate={gate} url={page.url[:120]}")
            dump_cookies(ctx, out, "after_no_email")

        radios = page.locator('input[name="usernameRadio"]')
        if radios.count():
            note(f"username radios={radios.count()}; selecting first")
            try:
                radios.first.check(force=True)
            except Exception:
                try:
                    radios.first.click(force=True)
                except Exception as exc:
                    note(f"radio select failed: {exc}")
        elif page.locator('input[name="Username"]').count():
            uname = f"tomas.reid{random.randint(1000, 9999)}"
            note(f"username={uname}")
            fill(page, 'input[name="Username"]', uname)
        if page.locator('input[name="usernameRadio"]').count() or page.locator('input[name="Username"]').count():
            click_next(page)
            page.wait_for_timeout(3000)
            info = survey(page, out, surveys, "after_username")
            gate = analyse_gate(info)
            note(f"gate={gate} url={page.url[:120]}")
            dump_cookies(ctx, out, "after_username")

        if page.locator('input[name="Passwd"]').count():
            pw_ = "Th3-Quiet-Depth-94"
            fill(page, 'input[name="Passwd"]', pw_)
            if page.locator('input[name="PasswdAgain"]').count():
                fill(page, 'input[name="PasswdAgain"]', pw_)
            click_next(page)
            page.wait_for_timeout(4000)
            info = survey(page, out, surveys, "after_password")
            gate = analyse_gate(info)
            note(f"gate={gate} url={page.url[:120]}")
            dump_cookies(ctx, out, "after_password")

        note(f"FINAL url={page.url[:160]}")
        body = (page.evaluate("document.body.innerText") or "")[:400]
        note("TEXT " + body.replace("\n", " | "))
        page.screenshot(path=str(out / "final.png"), full_page=True)
        page.wait_for_timeout(2000)
        ctx.close()

    (out / "surveys.json").write_text(json.dumps(surveys, indent=2))
    note(f"artifacts -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
