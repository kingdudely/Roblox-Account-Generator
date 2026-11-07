#!/usr/bin/env python3
"""Record everything that happens to the network while a hand presses the pill.

Automation presses the hold and Arkose answers "Please try again" or resets the
fill at 0%. A hand presses the same pixel and it passes. Something in the gesture
is different, and the cheapest way to find what is to let a human do it and watch
the wire: if the press triggers a request of its own (a gesture score, a
behavioural beacon, a proof-of-work), it will appear in the timeline and the
automated press can then be made to produce the same thing.

The page installs a `pointerdown`/`pointerup` listener on every frame, so the
human press is timestamped exactly, and every request/response is recorded with
its offset from that press. Screenshots bracket the gesture.

    ./tools/hold_network.py --proxy-file proxies.txt --limit 420

Solve the challenge by hand in the window that opens. The capture is written to
`debug/hold_net/<stamp>/`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from roblox_creator import config
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import (LAUNCH_ARGS, STEALTH_JS, UA, fill_form,
                                   make_credentials)

OUT = ROOT / "debug" / "hold_net"

# Marks the human's gesture. Installed in every frame, because the pill lives in
# an Arkose iframe, not in the host document.
PRESS_HOOK = """
(() => {
  if (window.__pressLog) return;
  window.__pressLog = [];
  const stamp = (kind, ev) => {
    window.__pressLog.push({
      kind,
      t: performance.now(),
      x: Math.round(ev.clientX || 0),
      y: Math.round(ev.clientY || 0),
      target: (ev.target && (ev.target.className || ev.target.tagName) || '').toString().slice(0, 60),
    });
  };
  for (const type of ['pointerdown', 'pointerup', 'mousedown', 'mouseup', 'touchstart', 'touchend']) {
    window.addEventListener(type, (ev) => stamp(type, ev), true);
  }
})();
"""


def log(message: str) -> None:
    print(message, flush=True)


async def run(playwright, raw: str, limit: float) -> dict:
    from roblox_creator.arkose_ui import find_hold_frame, start_puzzle

    parsed = parse_proxy(raw)
    relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                   parsed.user, parsed.password)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    case = OUT / stamp
    case.mkdir(parents=True, exist_ok=True)

    events: list[dict] = []
    t0 = time.time()

    def mark(entry: dict) -> None:
        entry["t"] = round(time.time() - t0, 3)
        events.append(entry)

    browser = None
    report = {"exit": parsed.masked(), "case": str(case), "outcome": "no-hold"}
    try:
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        context = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await context.add_init_script(STEALTH_JS)
        await context.add_init_script(PRESS_HOOK)

        context.on("request", lambda r: mark({
            "dir": "req", "method": r.method, "url": r.url[:180],
            "type": r.resource_type}))
        context.on("response", lambda r: mark({
            "dir": "res", "status": r.status, "url": r.url[:180]}))
        context.on("requestfailed", lambda r: mark({
            "dir": "fail", "url": r.url[:180], "err": (r.failure or "")[:80]}))

        page = await context.new_page()

        log(f"exit {parsed.masked()}")
        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(config.SEL_USERNAME, timeout=25000)
        user, password = make_credentials()
        await fill_form(page, user, password, human=True)
        log(f"submitted {user}")
        log("")
        log("  >>> SOLVE THE CHALLENGE BY HAND IN THE WINDOW <<<")
        log("  press and hold exactly as you normally would")
        log("")

        waited = 0.0
        while waited < limit:
            await page.wait_for_timeout(700)
            waited += 0.7
            if "/home" in page.url or "/discover" in page.url:
                # Some exits pass without any challenge at all — reported live on
                # 2026-11-01: the account was created and this loop kept waiting
                # for a hold that was never coming.
                report["outcome"] = "created-without-challenge"
                try:
                    await page.screenshot(path=str(case / "created.png"))
                except Exception:
                    pass
                return report
            frame, box = await find_hold_frame(page)
            if frame is not None and box is not None:
                break
            if await start_puzzle(page, log=log):
                continue

        if frame is None or box is None:
            report["outcome"] = "no-hold"
            return report

        report["outcome"] = "capped"
        log(f"  pill at ({box['x']:.0f},{box['y']:.0f}) {box['width']:.0f}x{box['height']:.0f}")

        # Sit here until the human finishes; screenshots every 2 s bracket the
        # gesture so the visual state can be matched to the network timeline.
        started = time.time()
        seen_press = 0
        shot = 0
        while time.time() - started < limit:
            await page.wait_for_timeout(500)
            presses = []
            for candidate in page.frames:
                try:
                    presses += await candidate.evaluate("() => window.__pressLog || []")
                except Exception:
                    continue
            if len(presses) > seen_press:
                for press in presses[seen_press:]:
                    mark({"dir": "press", **press})
                    log(f"    {press['kind']} at ({press['x']},{press['y']})")
                seen_press = len(presses)
            if int(time.time() - started) % 2 == 0:
                try:
                    await page.screenshot(path=str(case / f"shot_{shot:03d}.png"))
                    shot += 1
                except Exception:
                    pass
            if "/home" in page.url:
                report["outcome"] = "created"
                break
            current, _ = await find_hold_frame(page)
            if current is None and seen_press:
                report["outcome"] = "hold-done"

        (case / "events.json").write_text(json.dumps(events, indent=1))
        report["events"] = len(events)
        report["presses"] = seen_press
        return report
    except Exception as exc:
        report["outcome"] = f"error {type(exc).__name__}: {str(exc)[:90]}"
        return report
    finally:
        try:
            (case / "events.json").write_text(json.dumps(events, indent=1))
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        await relay.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--limit", type=float, default=420.0)
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    async with async_playwright() as playwright:
        for raw in proxies[:args.tries]:
            report = await run(playwright, raw, args.limit)
            log(f"\n--- {report['outcome']} ---  {report.get('case')}")
            log(f"  events={report.get('events')} presses={report.get('presses')}")
            if report.get("presses"):
                return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
