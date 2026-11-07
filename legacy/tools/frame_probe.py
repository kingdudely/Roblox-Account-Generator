#!/usr/bin/env python3
"""What is the Arkose frame on Roblox signup actually doing?

`tools/browser_probe.py` established that a real browser *is* challenged — the
signup form disables its fields and an `arkoselabs.roblox.com` frame appears. The
next question is what kind of challenge it is, because the answer decides whether
the browser path needs a solver at all:

    invisible / passive   the frame collects behaviour and passes silently, often
                          after a few seconds. Nothing to solve.
    callback / press-and-hold   one interaction, no puzzle.
    visual puzzle          the MatchDiceIconCount game, which needs answers.

So this drives the same signup, then reports the frame's size, visibility, body
text, and the Arkose network calls it made. Screenshots of the frame in both the
page and as an element.

    ./tools/frame_probe.py --proxy-file proxies.txt
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

from roblox_creator import config
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_probe import (LAUNCH_ARGS, STEALTH_JS, UA,
                                 fill_and_submit, make_credentials)


async def describe(page, out: Path) -> dict:
    snap: dict = {"frames": [], "arkose_calls": [], "url": page.url}
    for f in page.frames:
        info = {"url": (f.url or "")[:120]}
        try:
            el = await f.frame_element()
            box = await el.bounding_box()
            info["box"] = {k: round(v) for k, v in (box or {}).items()}
            info["visible"] = await el.is_visible()
        except Exception as exc:
            info["box_error"] = type(exc).__name__
        try:
            info["text"] = (await f.inner_text("body"))[:260].replace("\n", " | ")
        except Exception:
            info["text"] = ""
        try:
            html = await f.content()
            info["html_len"] = len(html)
            # the game mounts on a canvas; report which element type is present
            info["has_canvas"] = "<canvas" in html
            info["has_iframe"] = "<iframe" in html
            info["imgs"] = html.count("<img")
        except Exception:
            pass
        snap["frames"].append(info)
    return snap


async def one(playwright, raw: str, out: Path, index: int) -> dict:
    p = parse_proxy(raw)
    relay = await SocksRelay.start(f"{p.host}:{p.port}", p.user, p.password)
    username, password = make_credentials()
    result: dict = {"proxy": p.masked(), "username": username}

    browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
    calls: list[str] = []
    try:
        ctx = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await ctx.add_init_script(STEALTH_JS)

        def on_req(req):
            u = req.url
            if "arkoselabs" in u or "funcaptcha" in u:
                calls.append(f"{req.method} {u.split('?')[0]}")

        ctx.on("request", on_req)
        page = await ctx.new_page()

        await fill_and_submit(page, username, password)

        # watch for 40s, sampling for an Arkose frame every 2s
        for _ in range(20):
            await page.wait_for_timeout(2000)
            ark = [f for f in page.frames
                   if "arkoselabs" in (f.url or "") or "funcaptcha" in (f.url or "")]
            if ark:
                break

        await page.screenshot(path=str(out / f"{index:02d}_page.png"))
        snap = await describe(page, out)
        result.update(snap)
        result["arkose_calls"] = calls[:24]
        result["n_calls"] = len(calls)

        # try to screenshot each arkose frame element directly
        n = 0
        for f in page.frames:
            if "arkoselabs" in (f.url or ""):
                try:
                    el = await f.frame_element()
                    await el.screenshot(path=str(out / f"{index:02d}_ark{n}.png"))
                    n += 1
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
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/fprobe"))
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        for i, raw in enumerate(proxies[:args.count]):
            print(f"\n== frame probe {i + 1}/{args.count} ==")
            r = await one(p, raw, args.out, i)
            for f in r.get("frames", []):
                tag = "ARKOSE" if ("arkoselabs" in f["url"] or "funcaptcha" in f["url"]) else "page  "
                print(f"  [{tag}] {f.get('visible')} {f.get('box')} {f['url'][:70]}")
                if f.get("text"):
                    print(f"          text: {f['text'][:150]}")
                if f.get("has_canvas"):
                    print(f"          canvas={f['has_canvas']} imgs={f.get('imgs')} "
                          f"html={f.get('html_len')}")
            print(f"  arkose calls: {r.get('n_calls')}")
            for c in r.get("arkose_calls", [])[:12]:
                print(f"    {c[:100]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
