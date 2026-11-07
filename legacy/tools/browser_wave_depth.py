#!/usr/bin/env python3
"""Does a real browser get more wave images than the browserless chain does?

The browserless chain reliably gets two `/rtig/image` 200s and then `403 DENIED
ACCESS` on the third, while the signed URLs keep arriving from `/fc/ca/`. Two
readings, opposite fixes:

    exit-starved   Arkose soft-blocks a flagged IP by starving it mid-challenge.
                   The human sees a game that cannot be finished. Fix is pool.
    client-starved our request shape burns the grant. Fix is the transport.

A real browser distinguishes them. This drives Chromium through the same exit,
answers every wave by clicking candidate 1 and Verify (wrong answers are fine:
the chain advances on any answered wave — measured, index 0 twice still produced
wave 2's URL), and counts `/rtig/image` responses by status per wave.

    ./tools/browser_wave_depth.py --proxy-file proxies2.txt --tries 6
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from roblox_creator import config
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import LAUNCH_ARGS, STEALTH_JS, UA, fill_form, make_credentials


async def drive(playwright, raw: str, timeout: float) -> dict:
    parsed = parse_proxy(raw)
    relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                   parsed.user, parsed.password)
    browser = None
    result = {"exit": parsed.masked(), "rtig": [], "waves_clicked": 0,
              "outcome": "unknown"}
    try:
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        context = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()
        page.on("response", lambda r: result["rtig"].append(r.status)
                if "/rtig/image" in r.url else None)

        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(config.SEL_USERNAME, timeout=20000)
        user, password = make_credentials()
        await fill_form(page, user, password, human=True)

        from roblox_creator.arkose_ui import find_hold_frame, press_and_hold
        from roblox_creator.puzzle_ui import GameError, GameUI

        # Get past the ladder into a puzzle.
        ui = None
        waited = 0.0
        while waited < timeout and ui is None:
            await page.wait_for_timeout(1000)
            waited += 1
            frame, _ = await find_hold_frame(page)
            if frame is not None:
                await press_and_hold(page)
                continue
            for candidate in page.frames:
                if "game-core" in (candidate.url or "") and "enforcement" not in (candidate.url or ""):
                    for button in await candidate.query_selector_all("button"):
                        label = ((await button.inner_text()) or "").strip().lower()
                        if "start" in label:
                            try:
                                await button.click()
                            except Exception:
                                pass
                            break
            try:
                ui = await GameUI.attach(page, timeout=3, log=None)
            except GameError:
                ui = None

        if ui is None:
            result["outcome"] = "no-puzzle"
            return result

        _, total = await ui.position()
        picks = 0
        for wave in range(12):
            before = len(result["rtig"])
            if not await ui.submit():
                break
            result["waves_clicked"] = wave + 1
            # Wait for the next wave's image to arrive, or for the widget to go.
            for _ in range(40):
                await page.wait_for_timeout(500)
                if len(result["rtig"]) > before:
                    break
            if "/home" in page.url:
                result["outcome"] = "created"
                break
            await page.wait_for_timeout(1200)
            if len(result["rtig"]) == before:
                # nothing new: the challenge is finished or stuck
                result["outcome"] = "no-more-images"
                break
            picks += 1
        if result["outcome"] == "unknown":
            result["outcome"] = f"stopped after {result['waves_clicked']} submits"
        return result
    except Exception as exc:
        result["outcome"] = f"error {type(exc).__name__}: {str(exc)[:60]}"
        return result
    finally:
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        await relay.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies2.txt")
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--timeout", type=float, default=70.0)
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    async with async_playwright() as playwright:
        for raw in proxies[:args.tries]:
            row = await drive(playwright, raw, args.timeout)
            ok = sum(1 for s in row["rtig"] if s == 200)
            bad = sum(1 for s in row["rtig"] if s == 403)
            print(f"\n{row['exit'][:52]}")
            print(f"  outcome: {row['outcome']}")
            print(f"  rtig 200={ok}  403={bad}  statuses={row['rtig']}")
            print(f"  submits: {row['waves_clicked']}")
            if ok >= 4:
                print("  -> the browser IS getting more images than we do")
                return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
