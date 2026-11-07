#!/usr/bin/env python3
"""Can a real browser sign up through a free proxy exit?

The browserless chain dies on `/rtig/image` because that endpoint meters images
per exit and a challenge costs more than the allowance. The browser never asks
for those images at all — it renders its own tiles — so the allowance question
does not exist on this path. That makes a cheap, plentiful, disposable pool far
more interesting here than it is on the browserless path.

What this measures, per exit, in order:

    form      does CreateAccount load with the username field present
    submit    does the button fire and does auth accept the POST
    challenge does a captcha challenge mount, and of what kind
    survivable how many subsequent probes still get a 200 (burned exits show up
               as a form that loads once and then 403s)

    ./tools/free_browser.py --proxy-file /tmp/opencode/free.txt --tries 8
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import LAUNCH_ARGS, STEALTH_JS, UA


def log(msg: str) -> None:
    print(f"  {msg}", flush=True)


def username() -> str:
    from roblox_creator.identity import make_identity
    import random as _r
    return make_identity(_r.Random()).username


def precheck(raw: str, timeout: float = 12.0) -> bool:
    """One cheap GET, immediately before the browser touches the exit.

    A free exit's usable window is minutes. Verifying a whole list up front and
    then spending ten minutes working through it means most of the browser
    attempts hit exits that died while queueing — and that looks exactly like
    "free proxies do not work" when it is really "the list was stale by the time
    you used it".
    """
    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=timeout)
    session.proxies = {"http": raw, "https": raw}
    try:
        resp = session.get("https://www.roblox.com/CreateAccount",
                           headers={"accept": "text/html", "user-agent": UA})
        return resp.status_code == 200
    except Exception:
        return False
    finally:
        try:
            session.close()
        except Exception:
            pass


async def one(playwright, raw: str, timeout: float) -> dict:
    """Sign up through one exit and describe what the server did back."""
    p = parse_proxy(raw)
    row = {"proxy": p.masked(), "scheme": p.scheme, "form": False,
           "submit": None, "challenge": None, "url": None, "error": None}

    relay = None
    use_proxy = None
    # Chromium cannot carry SOCKS5 credentials; a loopback relay is the only
    # way through. Credential-free exits, and http exits, go direct.
    if p.scheme == "socks5" and (p.user or p.password):
        relay = await SocksRelay.start(f"{p.host}:{p.port}", p.user, p.password)
        use_proxy = {"server": relay.url}
    else:
        use_proxy = {"server": raw}

    browser = None
    try:
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        ctx = await browser.new_context(
            proxy=use_proxy, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        responses: list[tuple[int, str]] = []
        page.on("response", lambda r: responses.append((r.status, r.url)))

        t0 = time.time()
        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=timeout * 1000)
        await page.wait_for_selector("#signup-username", timeout=15000)
        row["form"] = True
        row["load_s"] = round(time.time() - t0, 1)

        await page.fill("#signup-username", username())
        await page.fill("#signup-password", "Passw0rd!" + str(random.randint(1000, 9999)))
        await page.select_option("#MonthDropdown", index=5)
        await page.select_option("#DayDropdown", index=10)
        await page.select_option("#YearDropdown", "1996")
        await page.click("#FemaleButton")
        await page.wait_for_timeout(1200)

        # The submit button is disabled until username validation comes back, so
        # a plain click() sits there waiting and reports "timeout" — which looks
        # like a dead proxy when it is really a slow round trip. Capture the
        # state first, scroll it into view, and only then click.
        state = await page.evaluate(
            """() => {
                const b = document.querySelector('#signup-button');
                if (!b) return 'missing';
                b.scrollIntoView({block: 'center'});
                return b.disabled ? 'disabled' : 'enabled';
            }""")
        row["button"] = state
        try:
            await page.click("#signup-button", timeout=15000)
        except Exception as exc:
            row["click_error"] = type(exc).__name__
            # Disabled or covered: fire the handler directly rather than give up,
            # so the server's verdict is still observed.
            await page.evaluate(
                "() => document.querySelector('#signup-button')?.click()")
        # A challenge takes a few seconds to mount; a session rejection is
        # immediate. Wait for either rather than a fixed sleep.
        for _ in range(20):
            await page.wait_for_timeout(700)
            challenge = await page.evaluate(
                """() => {
                    const f = [...document.querySelectorAll('iframe')]
                        .map(e => e.src || '').join(' ');
                    const txt = document.body.innerText || '';
                    if (/arkoselabs|funcaptcha|arkose/i.test(f)) return 'arkose-frame';
                    if (/Press and hold/i.test(txt)) return 'hold';
                    if (/Start Puzzle/i.test(txt)) return 'start-puzzle';
                    if (/verify you are human|not a robot/i.test(txt)) return 'human';
                    if (/Whoops|Something went wrong|already taken/i.test(txt)) return 'rejected';
                    if (f.trim()) return 'other-frame';
                    return null;
                }""")
            if challenge:
                row["challenge"] = challenge
                break

        signup = [s for s, u in responses if "auth.roblox.com/v2/signup" in u]
        row["submit"] = signup[-1] if signup else "no-post"
        meta = []
        for s, u in responses:
            if "captcha/v1/metadata" in u or "challenge" in u:
                meta.append(f"{s} {u.split('?')[0][-46:]}")
        row["challenge_requests"] = meta[-4:]
        row["url"] = page.url
        row["elapsed"] = round(time.time() - t0, 1)
        await page.screenshot(path=f"/tmp/opencode/freebrowser_{p.host.replace('.', '_')}.png")
        return row
    except Exception as exc:
        row["error"] = f"{type(exc).__name__}: {str(exc)[:60]}"
        return row
    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        if relay is not None:
            await relay.close()


async def main_async(args) -> int:
    pool = load_proxies(args.proxy_file)
    pool = [x for x in pool if not args.skip_prefix or not
            parse_proxy(x).host.startswith(args.skip_prefix)]
    random.shuffle(pool)

    rows: list[dict] = []
    skipped = 0
    async with async_playwright() as playwright:
        for raw in pool:
            if len(rows) >= args.tries:
                break
            if args.precheck and not precheck(raw):
                skipped += 1
                continue
            log(f"{parse_proxy(raw).masked()}")
            row = await one(playwright, raw, args.timeout)
            rows.append(row)
            if row["error"]:
                log(f"  {row['error']}")
            else:
                log(f"  form={row['form']} submit={row['submit']} "
                    f"challenge={row['challenge']} {row.get('elapsed')}s")
                if row["challenge_requests"]:
                    for c in row["challenge_requests"]:
                        log(f"    {c}")
            log("")

    good = [r for r in rows if r["form"]]
    challenged = [r for r in rows if r["challenge"] and r["challenge"] != "rejected"]
    print(f"  precheck skipped {skipped} dead exits")
    print(f"  form loaded   {len(good)}/{len(rows)}")
    print(f"  challenge     {len(challenged)}/{len(rows)}")
    print(f"  rejected      {len([r for r in rows if r['challenge'] == 'rejected'])}")
    for r in rows:
        print(f"    {r['proxy'][:44]:<44} {str(r['challenge'] or r['error'])[:34]}")
    return 0 if challenged else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=Path("/tmp/opencode/free.txt"))
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=40.0)
    ap.add_argument("--skip-prefix", default="45.74.31.")
    ap.add_argument("--no-precheck", dest="precheck", action="store_false",
                    help="use the exit even if it fails the immediate GET")
    ap.set_defaults(precheck=True)
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
