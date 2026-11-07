#!/usr/bin/env python3
"""Catch a "Press and hold" screen and press it properly, with evidence.

`tools/hold_probe.py` settled the mystery: the hold widget is served from an
**`about:blank` frame**, whose URL contains neither "arkoselabs" nor "funcaptcha".
`_arkose_frames()` filters on exactly those substrings, so the hold was invisible
to the ladder and never pressed — every round that showed it was left to time out.

The widget itself is a 310x42 pill: HTML/BODY/DIV/P wrapping one div with
`aria-label="Press and hold"`. The previous press used the frame element's centre
after a jittery mouse path, held ~3 s, and the modal was still up afterwards.

So this presses the element by its own live box, with no pointer movement, and for
a duration chosen on the command line. After the release it screenshots and says
whether the hold text survived.

    ./tools/hold_try.py --hold 6
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

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import (LAUNCH_ARGS, STEALTH_JS, UA, fill_form,
                                   make_credentials)

HOLD_WORDS = ("press and hold", "hold the button", "press & hold", "hold to")


async def find_hold(page):
    """The frame that owns the hold widget, plus the widget's own box.

    The top document must never be a candidate. `innerText` on the host page
    includes the rendered text of its child iframes, so the CreateAccount page
    "contains" the words *Press and hold* even though the widget is not inside it —
    and pressing the host page's centre clicks the backdrop, which dismisses the
    challenge instead of answering it. Only child frames are considered.
    """
    best = None
    for f in page.frames:
        if f == page.main_frame:
            continue
        try:
            text = ((await f.inner_text("body")) or "").lower()
        except Exception:
            continue
        if not any(w in text for w in HOLD_WORDS):
            continue
        weight = len(text)
        if best is None or weight < best[0]:
            best = (weight, f)

    # fall back to the deepest child frame that is a small widget, even if its
    # text has not painted yet — the frame exists before its content does
    if best is None:
        for f in reversed(page.frames):
            if f == page.main_frame:
                continue
            try:
                box = await (await f.frame_element()).bounding_box()
            except Exception:
                continue
            if box and 100 <= box["width"] <= 600 and 20 <= box["height"] <= 240:
                best = (0, f)
                break
    if best is None:
        return None, None, None

    frame = best[1]
    box = None
    for sel in ('[aria-label*="hold" i]', '[class] p', "p", "div[class]",
                "button", "[role=button]"):
        try:
            for el in await frame.query_selector_all(sel):
                b = await el.bounding_box()
                if b and b["width"] >= 120 and b["height"] >= 24:
                    box = b
                    break
        except Exception:
            continue
        if box:
            break
    if box is None:
        try:
            box = await (await frame.frame_element()).bounding_box()
        except Exception:
            box = None
    return frame, box, best[0]


async def hold_text_present(page) -> bool:
    for f in page.frames:
        try:
            text = ((await f.inner_text("body")) or "").lower()
        except Exception:
            continue
        if any(w in text for w in HOLD_WORDS):
            return True
    return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/hold"))
    ap.add_argument("--hold", type=float, default=6.0)
    ap.add_argument("--exits", type=int, default=25)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        for n, raw in enumerate(proxies[:args.exits], 1):
            pr = parse_proxy(raw)
            relay = await SocksRelay.start(f"{pr.host}:{pr.port}", pr.user, pr.password)
            browser = await p.chromium.launch(headless=False, args=LAUNCH_ARGS)
            print(f"\n=== exit {n} ===", flush=True)
            try:
                ctx = await browser.new_context(
                    proxy={"server": relay.url}, user_agent=UA,
                    viewport={"width": 1366, "height": 900}, locale="en-US",
                    timezone_id="America/New_York", device_scale_factor=2)
                await ctx.add_init_script(STEALTH_JS)
                page = await ctx.new_page()
                user, pwd = make_credentials()
                try:
                    await fill_form(page, user, pwd, human=True)
                except Exception:
                    continue

                frame = box = None
                weight = 0
                for _ in range(14):
                    await page.wait_for_timeout(1000)
                    if "/home" in page.url:
                        print("  created (passive)", flush=True)
                        return 0
                    frame, box, weight = await find_hold(page)
                    if frame is not None and box is not None:
                        break
                    if _ in (2, 6, 10):
                        print(f"  t={_ + 1}s frames: "
                              + " | ".join(f"{(f.url or '')[:38]!r}"
                                           for f in page.frames), flush=True)
                    frame = None
                if frame is None or box is None:
                    print("  no hold", flush=True)
                    continue

                print(f"  hold frame: {frame.url[:70]!r} body={weight} chars",
                      flush=True)
                print(f"  widget box: {box}", flush=True)
                await page.screenshot(path=str(args.out / f"e{n}_before.png"))
                x = box["x"] + box["width"] / 2
                y = box["y"] + box["height"] / 2
                print(f"  pressing ({x:.0f},{y:.0f}) for {args.hold}s, no movement",
                      flush=True)

                await page.mouse.move(x, y)
                await page.wait_for_timeout(160)
                await page.mouse.down()
                await page.wait_for_timeout(int(args.hold * 1000))
                await page.mouse.up()
                await page.wait_for_timeout(2000)
                await page.screenshot(path=str(args.out / f"e{n}_after.png"))

                still = await hold_text_present(page)
                print(f"  after hold: hold text {'STILL THERE' if still else 'GONE'}"
                      f"  url={page.url}", flush=True)
                for f in page.frames:
                    try:
                        t = ((await f.inner_text("body")) or "").strip()
                    except Exception:
                        continue
                    if t:
                        print(f"    f {f.url[:60]!r} :: {t[:90]!r}", flush=True)
                if "/home" in page.url:
                    return 0
                if not still:
                    return 0
                return 1
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
                await relay.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
