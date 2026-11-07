#!/usr/bin/env python3
"""Passive network recorder: find out where the game gets its tiles.

The browserless path died on `/rtig/image` — a per-exit allowance made it 403
after a handful of images, capping a challenge near one completable attempt in
six, with a montage-downsampling bug on top. The browser never hits that wall: it
paints tiles from revoked `blob:` URLs. So the game core is being handed its
images some other way, and if that way is a plain authenticated GET, browserless
can call it with the same session — no Chromium, no 250 MB per worker, and no
press-and-hold rung at all.

This records every request the page makes, tagged by phase, so the answer is
observed instead of guessed. It deliberately does not depend on the puzzle UI: it
records first and pages second, so a round that never reaches the dice still
yields its traffic.

    ./tools/net_probe.py --record 45
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.arkose_ui import find_hold_frame, press_and_hold
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.puzzle_ui import GameUI
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import (LAUNCH_ARGS, STEALTH_JS, UA, fill_form,
                                   make_credentials)

INTERESTING = ("rtig", "tile", ".png", ".jpg", ".webp", "fc/", "game",
               "captcha", "arkose", "challenge")


def short(url: str) -> str:
    p = urlsplit(url)
    q = ("?" + p.query[:70]) if p.query else ""
    return f"{p.netloc}{p.path[:86]}{q}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/net"))
    ap.add_argument("--exits", type=int, default=12)
    ap.add_argument("--record", type=float, default=45.0)
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
            log: list[dict] = []
            phase = {"name": "form"}
            try:
                ctx = await browser.new_context(
                    proxy={"server": relay.url}, user_agent=UA,
                    viewport={"width": 1366, "height": 900}, locale="en-US",
                    timezone_id="America/New_York", device_scale_factor=2)
                await ctx.add_init_script(STEALTH_JS)
                page = await ctx.new_page()

                def on_request(req):
                    log.append({"phase": phase["name"], "method": req.method,
                                "url": req.url, "type": req.resource_type,
                                "frame": (req.frame.url or "")[:70]
                                if req.frame else ""})

                def on_response(resp):
                    for row in reversed(log[-6:]):
                        if row["url"] == resp.url and "status" not in row:
                            row["status"] = resp.status
                            row["ctype"] = (resp.headers or {}).get(
                                "content-type", "")[:40]
                            break

                page.on("request", on_request)
                page.on("response", on_response)

                user, pwd = make_credentials()
                try:
                    await fill_form(page, user, pwd, human=True)
                except Exception:
                    print("  form did not submit", flush=True)
                    continue

                phase["name"] = "ladder"
                ticks = int(args.record)
                held = False
                for t in range(1, ticks + 1):
                    await page.wait_for_timeout(1000)
                    if "/home" in page.url:
                        print(f"  t={t}s CREATED on the passive route", flush=True)
                        break
                    if t % 5 == 0 or t == 1:
                        print(f"  t={t:02d}s " + " | ".join(
                            (f.url or "")[8:46] for f in page.frames), flush=True)
                    if not held:
                        hf, hb = await find_hold_frame(page)
                        if hf is not None and hb is not None:
                            print(f"  t={t}s HOLD widget {hb}", flush=True)
                            held = await press_and_hold(
                                page, log=lambda m: print(m, flush=True))
                            print(f"  hold resolved: {held}", flush=True)

                if "/home" in page.url:
                    return 0

                # Best effort: if a game frame mounted, page through it so the
                # tile requests land in the log.
                try:
                    ui = GameUI(page)
                    f = await ui.frame()
                    if f is not None:
                        phase["name"] = "paging"
                        key, _ = await ui.key_and_candidate()
                        _, total = await ui.position()
                        print(f"  game: key={'yes' if key else 'no'} total={total}",
                              flush=True)
                        await ui.goto(1, total)
                        for i in range(1, min(total, 10) + 1):
                            await ui.candidate()
                            if i < min(total, 10):
                                await ui.next()
                                await page.wait_for_timeout(250)
                        print("  paged through the candidates", flush=True)
                except Exception as exc:
                    print(f"  no paging: {type(exc).__name__}: {str(exc)[:60]}",
                          flush=True)

                await page.screenshot(path=str(args.out / f"exit{n}.png"))
                paging = [r for r in log if r["phase"] == "paging"]
                print(f"  requests during paging: {len(paging)}", flush=True)
                for r in paging[:25]:
                    print(f"    {r['method']} {r.get('status','?')} "
                          f"{r['type']:9} {short(r['url'])}", flush=True)

                agg = Counter()
                for r in log:
                    if any(k in r["url"].lower() for k in INTERESTING):
                        agg[(short(r["url"]), r.get("status", "?"))] += 1
                print("  interesting URLs this session:", flush=True)
                for (u, st), c in agg.most_common(26):
                    print(f"    x{c:<3} {st} {u}", flush=True)

                out = args.out / f"exit{n}.txt"
                with out.open("w") as fh:
                    for r in log:
                        fh.write(f"{r['phase']:6} {r['method']:4} "
                                 f"{r.get('status','?'):>4} {r['type']:9} "
                                 f"{r['url']}\n")
                print(f"  full log -> {out} ({len(log)} requests)", flush=True)
                return 0
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass
                await relay.close()
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
