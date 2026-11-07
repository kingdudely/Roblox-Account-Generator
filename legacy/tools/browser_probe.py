#!/usr/bin/env python3
"""Does a real browser get challenged on Roblox signup at all?

Everything built so far assumes the account creator must *solve* an Arkose
challenge, because a bare HTTP client always provokes one: the signup POST
answers 403 with `rblx-challenge-type: captcha` and a `dataExchangeBlob`, every
time. But that 403 is a risk verdict, not a fixed toll. A real browser from a
residential exit with a real renderer, real TLS, real canvas and real timing is a
completely different signal — and Roblox may either skip the challenge entirely,
issue one that auto-passes, or issue a real one.

That distinction decides the whole architecture, so it is worth five minutes
before writing another line of solver:

    no challenge   -> accounts are a browser-fill problem, and today's work is done
    auto-pass      -> same, with one wait
    real challenge -> the solver matters, and the browser still helps because it
                      fetches and renders the puzzle natively

This does one signup per proxy and reports what happened, with screenshots.

    ./tools/browser_probe.py --proxy-file proxies.txt --count 3
    ./tools/browser_probe.py --proxy-file proxies.txt --headed
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

from roblox_creator import config
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1366,900",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(p)
);
"""

WORDS = ["Quiet", "Harbor", "Amber", "Vellum", "Cinder", "Larkspur", "Basalt",
         "Tidal", "Onyx", "Marigold", "Sable", "Ivory", "Juniper", "Cobalt"]


def make_credentials() -> tuple[str, str]:
    user = f"{random.choice(WORDS)}{random.choice(WORDS)}{random.randint(100, 9999)}"
    pwd = f"{random.choice(WORDS)}{random.randint(1000, 9999)}!{random.choice(WORDS)}"
    return user, pwd


async def fill_and_submit(page, username: str, password: str) -> None:
    await page.goto("https://www.roblox.com/CreateAccount",
                    wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_selector(config.SEL_USERNAME, timeout=45000)

    await page.fill(config.SEL_USERNAME, username)
    await page.fill(config.SEL_PASSWORD, password)

    await page.select_option(config.SEL_MONTH, index=random.randint(1, 12))
    await page.select_option(config.SEL_DAY, index=random.randint(1, 27))
    await page.select_option(config.SEL_YEAR, str(random.randint(1994, 2002)))

    await page.click(config.SEL_FEMALE if random.random() < 0.5 else config.SEL_MALE)
    await page.wait_for_timeout(random.randint(400, 1100))
    await page.click(config.SEL_SUBMIT)


async def observe(page, seconds: float = 35.0) -> dict:
    """Watch for an Arkose frame, a login redirect, or an error toast."""
    started = time.time()
    frame_seen = False
    while time.time() - started < seconds:
        for f in page.frames:
            if "arkoselabs" in (f.url or "") or "funcaptcha" in (f.url or ""):
                frame_seen = True
                break
        if frame_seen:
            break
        if "home" in page.url or "/discover" in page.url:
            break
        await page.wait_for_timeout(500)

    state: dict = {
        "url": page.url,
        "arkose_frame": frame_seen,
        "elapsed": round(time.time() - started, 1),
    }
    try:
        state["cookies"] = [c["name"] for c in await page.context.cookies()]
    except Exception:
        state["cookies"] = []
    try:
        state["body"] = (await page.inner_text("body"))[:300].replace("\n", " | ")
    except Exception:
        state["body"] = ""
    return state


async def one(playwright, raw_proxy: str, out: Path, index: int,
              headed: bool) -> dict:
    p = parse_proxy(raw_proxy)
    relay = await SocksRelay.start(f"{p.host}:{p.port}", p.user, p.password)
    username, password = make_credentials()
    result: dict = {"proxy": p.masked(), "username": username,
                    "password": password, "relay": relay.port}

    browser = await playwright.chromium.launch(headless=not headed, args=LAUNCH_ARGS)
    try:
        context = await browser.new_context(
            proxy={"server": relay.url},
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            device_scale_factor=1,
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        t0 = time.time()
        try:
            await fill_and_submit(page, username, password)
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {str(exc)[:150]}"
            await page.screenshot(path=str(out / f"{index:02d}_error.png"))
            await browser.close()
            return result

        state = await observe(page)
        result.update(state)
        result["total_seconds"] = round(time.time() - t0, 1)
        await page.screenshot(path=str(out / f"{index:02d}_after.png"),
                              full_page=False)
        if state["arkose_frame"]:
            try:
                for f in page.frames:
                    if "arkoselabs" in (f.url or ""):
                        await f.page.screenshot(path=str(out / f"{index:02d}_frame.png"))
                        break
            except Exception:
                pass
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await relay.close()
    return result


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--headed", action="store_true", default=True)
    ap.add_argument("--headless", dest="headed", action="store_false")
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/bprobe"))
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        for i, raw in enumerate(proxies[:args.count]):
            print(f"\n== signup {i + 1}/{args.count} ==")
            r = await one(p, raw, args.out, i, args.headed)
            results.append(r)
            if r.get("error"):
                print(f"  error: {r['error']}")
                continue
            print(f"  proxy   {r['proxy']}")
            print(f"  user    {r['username']}")
            print(f"  arkose  {r['arkose_frame']}")
            print(f"  url     {r['url'][:90]}")
            print(f"  body    {r['body'][:140]}")
            print(f"  {r['total_seconds']}s")

    challenged = sum(1 for r in results if r.get("arkose_frame"))
    print(f"\n  challenged {challenged}/{len(results)}")
    print(f"  screenshots -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
