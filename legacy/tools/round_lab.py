#!/usr/bin/env python3
"""Interactive dice-round lab: capture a round, submit arbitrary tiles, screenshot.

Three things have been unmeasurable so far and this makes them measurable.

1. What a submission does. `position()` returns the tile index inside the round,
   and every round starts at "1 of 10" — so "before=(1,10) after=(1,10)" cannot
   distinguish a new round from an unchanged one. The only honest signals are the
   *key card* (symbol and count change every round) and a screenshot.

2. Whether a wrong answer is retryable. If the round survives a bad submission the
   whole counting problem collapses: ten tiles, ten submits, guaranteed correct.
   So this loops on the answer file instead of exiting after one submit.

3. What the page actually says. The instruction text is on screen and it was never
   read: "the number of symbols on the top sides of the dice" — top faces, not
   every face. Every count so far was against the wrong region.

    ./tools/round_lab.py
    echo 7 > /tmp/opencode/round/answer.txt     # submit tile 7
    echo 3 > /tmp/opencode/round/answer.txt     # submit again, same round
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.puzzle_ui import GameError, GameUI
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import (LAUNCH_ARGS, STEALTH_JS, UA, fill_form,
                                   make_credentials)


def contact_sheet(key_png: bytes, tiles: list[bytes], out: Path) -> Path:
    key = Image.open(io.BytesIO(key_png)).convert("RGB")
    S, cols = 380, 5
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (S + 8) + 8, (S + 46) * (rows + 1) + 12),
                      (16, 16, 18))
    dr = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
    except Exception:
        font = ImageFont.load_default()

    kh = int(S * key.size[1] / key.size[0])
    sheet.paste(key.resize((S, kh), Image.LANCZOS), (8, 40))
    dr.text((12, 4), "KEY", fill=(255, 255, 0), font=font)

    for i, png in enumerate(tiles):
        im = Image.open(io.BytesIO(png)).convert("RGB").resize((S, S), Image.LANCZOS)
        col, row = i % cols, i // cols
        x = 8 + col * (S + 8)
        y = 40 + kh + 18 + row * (S + 46)
        sheet.paste(im, (x, y + 34))
        dr.text((x + 4, y), f"TILE {i + 1}", fill=(255, 255, 0), font=font)
    sheet.save(out, quality=94)
    return out


async def capture_round(ui: GameUI, out: Path, tag: str) -> dict:
    """Key card plus every candidate, written to disk with a contact sheet."""
    key, _ = await ui.key_and_candidate()
    if key is None:
        return {"error": "no key card"}

    _, total = await ui.position()
    await ui.goto(1, total)
    tiles: list[bytes] = []
    for i in range(1, total + 1):
        shot = await ui.candidate()
        if shot is None:
            break
        tiles.append(shot["png"])
        if i < total and not await ui.next():
            break
        await ui.frame()  # keep the frame handle fresh across the page turn
        await ui.page.wait_for_timeout(180)

    (out / f"{tag}_key.png").write_bytes(key["png"])
    for i, png in enumerate(tiles, 1):
        (out / f"{tag}_tile{i:02d}.png").write_bytes(png)
    sheet = contact_sheet(key["png"], tiles, out / f"{tag}_sheet.jpg")
    return {"key": key["png"], "tiles": tiles, "sheet": sheet, "total": total}


def key_fingerprint(png: bytes) -> str:
    """Cheap identity for a key card: size plus a coarse pixel hash."""
    im = Image.open(io.BytesIO(png)).convert("L").resize((16, 24))
    px = list(im.getdata())
    return f"{im.size}:{sum(px) % 100003}:{max(px)}"


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/round"))
    ap.add_argument("--timeout", type=float, default=1800.0)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    answer_file = args.out / "answer.txt"
    answer_file.unlink(missing_ok=True)

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        for attempt, raw in enumerate(proxies[:15], 1):
            pr = parse_proxy(raw)
            relay = await SocksRelay.start(f"{pr.host}:{pr.port}", pr.user, pr.password)
            browser = await p.chromium.launch(headless=False, args=LAUNCH_ARGS)
            print(f"== exit {attempt} ==", flush=True)
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
                try:
                    ui = await GameUI.attach(page, timeout=80,
                                             log=lambda m: print(m, flush=True))
                except GameError as exc:
                    print(f"  attach: {exc}", flush=True)
                    continue
                if "/home" in page.url:
                    print("  created on the passive route", flush=True)
                    return 0

                round_no = 0
                subs = 0
                deadline = time.time() + args.timeout
                while time.time() < deadline:
                    if "/home" in page.url:
                        print("  SOLVED — on /home", flush=True)
                        return 0
                    round_no += 1
                    got = await capture_round(ui, args.out, f"r{round_no}")
                    if "error" in got:
                        print(f"  round {round_no}: {got['error']}", flush=True)
                        return 1
                    instr = ""
                    try:
                        instr = (await ui.instruction())[:200]
                    except Exception:
                        pass
                    fp0 = key_fingerprint(got["key"])
                    print(f"  round {round_no}: {len(got['tiles'])} tiles, "
                          f"key={fp0}", flush=True)
                    print(f"  instruction: {instr!r}", flush=True)
                    print(f"  sheet -> {got['sheet']}", flush=True)

                    tile = None
                    while time.time() < deadline and tile is None:
                        if answer_file.exists():
                            txt = answer_file.read_text().strip()
                            answer_file.unlink(missing_ok=True)
                            try:
                                tile = int(txt.split()[0])
                            except (ValueError, IndexError):
                                tile = None
                        if tile is None:
                            await page.wait_for_timeout(1200)
                    if tile is None:
                        print("  no answer supplied", flush=True)
                        return 1

                    before = await ui.key_and_candidate()
                    before_fp = key_fingerprint(before[0]["png"]) if before[0] else "?"
                    await ui.goto(tile, got["total"])
                    await page.wait_for_timeout(random.randint(250, 600))
                    await ui.submit()
                    subs += 1
                    print(f"  submitted tile {tile} (key was {before_fp})", flush=True)

                    # Screenshot the immediate aftermath and again after the game
                    # has had time to render whatever it is going to render.
                    for wait in (300, 2500):
                        await page.wait_for_timeout(wait)
                        await page.screenshot(
                            path=str(args.out / f"r{round_no}_after{tile}_{wait}.png"))
                    if "/home" in page.url:
                        print("  SOLVED — on /home", flush=True)
                        return 0
                    nxt = await ui.key_and_candidate()
                    after_fp = key_fingerprint(nxt[0]["png"]) if nxt[0] else "?"
                    same = after_fp == before_fp
                    print(f"  after submit: key={after_fp} "
                          f"{'SAME round' if same else 'NEW round'}", flush=True)
                    print(f"  -> art in {args.out}/r{round_no}_after{tile}_*.png",
                          flush=True)
                    (args.out / "log.jsonl").open("a").write(json.dumps({
                        "round": round_no, "submits": subs, "tile": tile,
                        "key_before": before_fp, "key_after": after_fp,
                        "same_round": same, "url": page.url,
                        "instruction": instr}) + "\n")
                    if same:
                        # keep playing this round — proves retryability
                        print("  round survived the submission; "
                              "waiting for another answer", flush=True)
                        continue
                    await page.wait_for_timeout(1200)
                    break
                print("  giving up on this exit", flush=True)
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
