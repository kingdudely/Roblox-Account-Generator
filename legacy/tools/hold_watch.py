#!/usr/bin/env python3
"""Watch a Press-and-hold rung second by second, with screenshots.

The hold never "takes" under automation but takes twice under a human hand. The
open question is mechanical, not statistical: does the widget vanish *while the
button is still down*, or only after release? Guessing wrong means either
releasing too early (rejected) or holding forever (the ring fills, nothing
happens, the run burns 25 s and retries).

This opens a real challenge, waits for the hold, and then walks one press through
every phase — before, mid-hold, at the moment the widget goes, after release —
screenshotting and dumping the widget's DOM at each. It does not solve anything
and it does not care about the answer; it only reports what the hold looks like.

    ./tools/hold_watch.py --proxy-file proxies2.txt --cycles 2
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

OUT = ROOT / "debug" / "hold_watch"

# Everything about the widget that could tell us when it is satisfied: the
# aria-live region Arkose uses for status, any progress-ish attribute, and the
# computed transform of the ring fill.
WIDGET_JS = """() => {
  const out = { live: [], nodes: [], found: false };
  for (const sel of ['[aria-live]', '[role=progressbar]', '[class*=ring]',
                     '[class*=progress]', '[class*=fill]']) {
    for (const el of document.querySelectorAll(sel)) {
      out.live.push({
        sel,
        text: (el.textContent || '').trim().slice(0, 80),
        aria: el.getAttribute('aria-valuenow'),
        cls: (el.className || '').toString().slice(0, 60),
        style: (el.getAttribute('style') || '').slice(0, 120),
      });
    }
  }
  const hold = document.querySelector('[aria-label*="hold" i]');
  if (hold) {
    out.found = true;
    let n = hold;
    for (let i = 0; i < 4 && n; i++) {
      out.nodes.push({
        level: i,
        tag: n.tagName,
        cls: (n.className || '').toString().slice(0, 80),
        style: (n.getAttribute('style') || '').slice(0, 140),
        children: n.children.length,
      });
      n = n.parentElement;
    }
    const r = hold.getBoundingClientRect();
    out.rect = { x: r.x, y: r.y, w: r.width, h: r.height };
  }
  return out;
}
"""


def log(message: str) -> None:
    print(message, flush=True)


async def snap(page, case: Path, name: str) -> None:
    try:
        await page.screenshot(path=str(case / f"{name}.png"))
    except Exception:
        pass


async def dump(ui_frame, page) -> dict:
    """Widget state from whichever frame currently holds it."""
    for frame in page.frames:
        try:
            data = await frame.evaluate(WIDGET_JS)
        except Exception:
            continue
        if data and data.get("found"):
            return data
    return {"found": False}


async def watch(playwright, raw: str, cycles: int, limit: float) -> dict:
    from roblox_creator.arkose_ui import find_hold_frame

    parsed = parse_proxy(raw)
    relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                   parsed.user, parsed.password)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    case = OUT / stamp
    case.mkdir(parents=True, exist_ok=True)

    browser = None
    report = {"exit": parsed.masked(), "case": str(case), "phases": [],
              "outcome": "no-hold"}
    try:
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        context = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        log(f"exit {parsed.masked()}")
        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(config.SEL_USERNAME, timeout=25000)
        user, password = make_credentials()
        await fill_form(page, user, password, human=True)
        log(f"submitted {user}, waiting for the hold")

        started = time.time()
        clicks = 0
        while time.time() - started < limit:
            frame, box = await find_hold_frame(page)
            if frame is not None and box is not None:
                break
            if clicks < 3:
                from roblox_creator.arkose_ui import start_puzzle
                if await start_puzzle(page, log=log):
                    clicks += 1
                    await snap(page, case, f"started_puzzle_{clicks}")
                    continue
            await page.wait_for_timeout(800)
        else:
            await snap(page, case, "never_got_hold")
            report["outcome"] = "no-hold"
            return report

        report["outcome"] = "held"
        for cycle in range(1, cycles + 1):
            frame, box = await find_hold_frame(page)
            if frame is None or box is None:
                log(f"  cycle {cycle}: no widget, stopping")
                break
            await snap(page, case, f"c{cycle}_a_before")
            state = await dump(frame, page)
            (case / f"c{cycle}_a_before.json").write_text(json.dumps(state, indent=1))
            log(f"  cycle {cycle}: box {box['width']:.0f}x{box['height']:.0f} "
                f"at ({box['x']:.0f},{box['y']:.0f})")

            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            await page.mouse.move(x, y)
            await page.wait_for_timeout(120)
            await page.mouse.down()
            log(f"  cycle {cycle}: down at ({x:.0f},{y:.0f})")

            # Sample every 400 ms for 8 s: does the widget leave while held?
            gone_at = None
            for tick in range(20):
                await page.wait_for_timeout(400)
                elapsed = (tick + 1) * 0.4
                current, _ = await find_hold_frame(page)
                if current is None:
                    gone_at = elapsed
                    log(f"    widget GONE while held at {elapsed:.1f}s")
                    break
                if tick in (2, 5, 9):
                    state = await dump(current, page)
                    (case / f"c{cycle}_hold_{elapsed:.1f}s.json").write_text(
                        json.dumps(state, indent=1))
                    await snap(page, case, f"c{cycle}_b_hold_{elapsed:.1f}s")
                    live = [e["text"] for e in state.get("live", []) if e.get("text")]
                    log(f"    t={elapsed:.1f}s live={live[:3]}")

            await page.mouse.up()
            await page.wait_for_timeout(1200)
            await snap(page, case, f"c{cycle}_c_after_release")
            after = await dump(None, page)
            (case / f"c{cycle}_c_after_release.json").write_text(
                json.dumps(after, indent=1))
            still, sbox = await find_hold_frame(page)
            log(f"  cycle {cycle}: gone_while_held={gone_at} "
                f"widget_after_release={'yes' if still is not None else 'no'}")
            report["phases"].append({
                "cycle": cycle,
                "gone_while_held": gone_at,
                "after_release": still is not None,
                "box": sbox,
            })

            if still is None:
                await page.wait_for_timeout(2000)
                await snap(page, case, f"c{cycle}_d_after_2s")
                again, _ = await find_hold_frame(page)
                log(f"  cycle {cycle}: 2 s later widget={'yes' if again else 'no'}")
                if again is None:
                    break
        return report
    except Exception as exc:
        report["outcome"] = f"error {type(exc).__name__}: {str(exc)[:90]}"
        return report
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
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--cycles", type=int, default=2)
    ap.add_argument("--limit", type=float, default=150.0)
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    async with async_playwright() as playwright:
        for raw in proxies[:args.tries]:
            report = await watch(playwright, raw, args.cycles, args.limit)
            log(f"\n--- {report['outcome']} ---")
            log(f"  case: {report.get('case')}")
            for phase in report.get("phases", []):
                log(f"  cycle {phase['cycle']}: gone_while_held="
                    f"{phase['gone_while_held']} after_release={phase['after_release']}")
            if report.get("phases"):
                return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
