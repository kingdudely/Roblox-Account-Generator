#!/usr/bin/env python3
"""What actually fills while the hold pill is pressed?

Reported from watching the window: the press fills the pill about halfway and
then it falls back. So the hold is a progress gesture with a real completion
point, and releasing at 3 s releases before it finishes. The question is what in
the DOM carries that progress, so the release can be triggered by *completion*
instead of by a stopwatch.

This presses once, holds for twelve seconds, and every 200 ms snapshots the
widget's whole subtree — outerHTML, computed background, and every child's inline
style — so the frame can be diffed against the one before it. Nothing is
interpreted here; it just records the timeline.

    ./tools/hold_fill_probe.py --proxy-file proxies2.txt
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

OUT = ROOT / "debug" / "hold_fill"

SNAPSHOT_JS = """() => {
  const hold = document.querySelector('[aria-label*="hold" i]');
  if (!hold) return null;
  // Climb to the smallest ancestor that also holds the pill's fill element.
  let box = hold;
  for (let i = 0; i < 4 && box.parentElement; i++) box = box.parentElement;
  const pick = (el) => {
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      tag: el.tagName,
      cls: (el.className || '').toString().slice(0, 90),
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      style: (el.getAttribute('style') || '').slice(0, 200),
      bg: cs.background.slice(0, 120),
      bgImage: cs.backgroundImage.slice(0, 160),
      transform: cs.transform,
      width: cs.width,
      opacity: cs.opacity,
      text: (el.textContent || '').trim().slice(0, 40),
      children: Array.from(el.children).map(pick),
    };
  };
  return pick(box);
}
"""


def log(message: str) -> None:
    print(message, flush=True)


def walk(node, path="root", rows=None):
    """Flatten a snapshot into comparable rows."""
    if rows is None:
        rows = []
    if node is None:
        return rows
    rows.append((path, node["cls"][:40], node["width"], node["transform"],
                 node["bgImage"][:40], node["text"]))
    for index, child in enumerate(node.get("children") or []):
        walk(child, f"{path}/{index}", rows)
    return rows


async def run(playwright, raw: str, limit: float) -> dict:
    from roblox_creator.arkose_ui import find_hold_frame, start_puzzle

    parsed = parse_proxy(raw)
    relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                   parsed.user, parsed.password)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    case = OUT / stamp
    case.mkdir(parents=True, exist_ok=True)

    browser = None
    report = {"exit": parsed.masked(), "case": str(case), "outcome": "no-hold"}
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

        started = time.time()
        clicks = 0
        waits = 0
        while time.time() - started < limit:
            frame, box = await find_hold_frame(page)
            if frame is not None and box is not None:
                break
            if clicks < 3 and await start_puzzle(page, log=log):
                clicks += 1
                continue
            waits += 1
            if waits % 6 == 0:
                try:
                    await page.screenshot(path=str(case / f"wait_{waits:03d}.png"))
                except Exception:
                    pass
            await page.wait_for_timeout(700)
        else:
            try:
                await page.screenshot(path=str(case / "no_hold_final.png"))
            except Exception:
                pass
            log(f"  frames at exit: {[f.url[:50] for f in page.frames]}")
            return report

        log(f"  widget {box['width']:.0f}x{box['height']:.0f} at "
            f"({box['x']:.0f},{box['y']:.0f})")
        x = box["x"] + box["width"] / 2
        y = box["y"] + box["height"] / 2
        await page.mouse.move(x, y)
        await page.wait_for_timeout(150)
        await page.mouse.down()
        log("  down — sampling for 12 s, no pointer movement")

        timeline = []
        previous = None
        for tick in range(60):
            await page.wait_for_timeout(200)
            elapsed = round((tick + 1) * 0.2, 1)
            # Re-resolve the frame every tick: the widget's document is replaced
            # under the pointer while the button is held, so a handle captured
            # before mousedown goes stale within a few hundred milliseconds and
            # reads as "widget gone".
            live_frame, _ = await find_hold_frame(page)
            if live_frame is None:
                timeline.append({"t": elapsed, "gone": True})
                log(f"    t={elapsed:.1f}s widget gone")
                break
            try:
                snap = await live_frame.evaluate(SNAPSHOT_JS)
            except Exception:
                snap = None
            if snap is None:
                timeline.append({"t": elapsed, "doc_swapped": True})
                log(f"    t={elapsed:.1f}s document swapped")
                previous = None
                continue
            # A screenshot every second is the only honest record of a fill that
            # is drawn rather than styled; the DOM diff below only narrows it.
            if tick % 5 == 0:
                try:
                    await page.screenshot(path=str(case / f"hold_{int(elapsed * 10):03d}.png"))
                except Exception:
                    pass
            rows = walk(snap)
            if previous is not None:
                changed = [(p, c, w, tr, bg, tx) for p, c, w, tr, bg, tx in rows
                           if (p, c, w, tr, bg, tx) not in previous]
                if changed:
                    log(f"    t={elapsed:.1f}s {len(changed)} change(s): "
                        f"{[c[0] + ' w=' + c[2] + ' bg=' + c[4] for c in changed][:4]}")
            previous = rows
            timeline.append({"t": elapsed, "tree": snap})

        await page.mouse.up()
        log("  up")
        await page.wait_for_timeout(2500)
        try:
            await page.screenshot(path=str(case / "after_release.png"))
        except Exception:
            pass
        (case / "timeline.json").write_text(json.dumps(timeline, indent=1))
        report["outcome"] = "sampled"
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
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--limit", type=float, default=150.0)
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    async with async_playwright() as playwright:
        for raw in proxies[:args.tries]:
            report = await run(playwright, raw, args.limit)
            log(f"\n--- {report['outcome']} ---  {report.get('case')}")
            if report["outcome"] == "sampled":
                return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
