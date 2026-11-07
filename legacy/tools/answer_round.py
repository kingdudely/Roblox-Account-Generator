#!/usr/bin/env python3
"""Capture one live dice round and block until a human (or a script) answers it.

Everything measured so far has an ambiguity that no amount of probing resolves:
nobody knows what `advanced` means. Submitting a guess and seeing the pip move
could be the game accepting the round, or it could be the game resetting after a
wrong answer — both look like "the position changed".

The only way to settle it is to submit an answer that is *known correct*. That
needs a human looking at the tiles, which needs a live session, which needs a
process that stays alive while the answer is decided. So this captures the key and
all ten candidates, writes a numbered contact sheet, and waits on a file.

When the answer file appears it submits that tile and reports what the game did.

    ./tools/answer_round.py --proxy-file proxies.txt
    # look at /tmp/opencode/round/sheet.jpg, then:
    echo 7 > /tmp/opencode/round/answer.txt
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
    S = 380
    cols = 5
    rows = (len(tiles) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * (S + 8) + 8, (S + 46) * (rows + 1) + 12),
                      (16, 16, 18))
    dr = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 30)
    except Exception:
        font = ImageFont.load_default()

    kw = S
    kh = int(kw * key.size[1] / key.size[0])
    sheet.paste(key.resize((kw, kh), Image.LANCZOS), (8, 40))
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
        for raw in proxies[:15]:
            pr = parse_proxy(raw)
            relay = await SocksRelay.start(f"{pr.host}:{pr.port}", pr.user, pr.password)
            browser = await p.chromium.launch(headless=False, args=LAUNCH_ARGS)
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
                    print(f"  attach: {exc}")
                    continue
                if "/home" in page.url:
                    print("  created on the passive route")
                    return 0

                key, _ = await ui.key_and_candidate()
                if key is None:
                    print("  no key card")
                    continue
                await ui.goto(1, 10)
                tiles: list[bytes] = []
                for i in range(1, 11):
                    shot = await ui.candidate()
                    if shot is None:
                        break
                    tiles.append(shot["png"])
                    if i < 10 and not await ui.next():
                        break
                    await page.wait_for_timeout(200)

                (args.out / "key.png").write_bytes(key["png"])
                for i, png in enumerate(tiles, 1):
                    (args.out / f"tile{i:02d}.png").write_bytes(png)
                sheet = contact_sheet(key["png"], tiles, args.out / "sheet.jpg")
                print(f"  captured {len(tiles)} tiles -> {sheet}")

                deadline = time.time() + args.timeout
                tile = None
                while time.time() < deadline:
                    if answer_file.exists():
                        raw_txt = answer_file.read_text().strip()
                        if raw_txt:
                            try:
                                tile = int(raw_txt.split()[0])
                                break
                            except ValueError:
                                answer_file.unlink(missing_ok=True)
                    await page.wait_for_timeout(1500)
                if tile is None:
                    print("  no answer supplied")
                    return 1

                answer_file.unlink(missing_ok=True)
                before = await ui.position()
                await ui.goto(tile, before[1])
                await page.wait_for_timeout(random.randint(300, 700))
                ok = await ui.submit()
                print(f"  submitted tile {tile} (submit clicked: {ok})")
                await page.wait_for_timeout(2500)

                after = await ui.position()
                body = ""
                try:
                    body = (await (await ui.frame()).inner_text("body"))[:160]
                except Exception:
                    pass
                state = {"submitted": tile, "before": before, "after": after,
                         "url": page.url, "body": body}
                (args.out / "result.json").write_text(json.dumps(state, indent=1))
                print(f"  before={before} after={after} url={page.url}")
                print(f"  body: {body[:120]!r}")
                if "/home" in page.url:
                    print("  SOLVED")
                elif after[0] != before[0]:
                    print("  position moved -> the round advanced")
                else:
                    print("  position unchanged -> the guess was not accepted")
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
