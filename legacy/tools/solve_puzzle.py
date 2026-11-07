#!/usr/bin/env python3
"""Solve one live dice round in the browser, end to end.

The browser hands us a paged game instead of a strip:

    key panel (symbol + "x N")   fixed on the left
    one candidate at a time      200x200, arrows to page, Submit when matched
    "1 of 10" in the pip

So the loop is: read N once, page every candidate and ask the vision model how
many top faces carry the symbol, remember the page whose count equals N, navigate
back to it, Submit.

CORRECTION (measured 2026-11-01, tools/netprobe4-6): the browser DOES call
`/rtig/image` — the game core fetches one strip per wave from the same signed URL
the browserless path uses, then paints the tiles from the returned JPEG. The
earlier claim that the browser "already rendered its own tiles" and therefore
never touched the image allowance was wrong. Captured browser request:

    GET /rtig/image?challenge=0&expires=..&gameToken=..&sessionToken=..&signature=..
    referer: .../game-core/1.40.0/standard/index.html?session=..&r=us-east-1&...

Header/cookie shape was A/B tested (browser headers vs ours, with and without the
high-precision `timestamp` cookie) and made no difference: the same exit returns
200 or 403 regardless. The gate is per-exit reputation, so the browser path hits
exactly the same wall as the browserless path. Do not assume the browser route
avoids the image allowance.

    ./tools/solve_puzzle.py --proxy-file proxies.txt
    ./tools/solve_puzzle.py --provider kilo-dots      # slower, for comparison
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image

from roblox_creator import dicecount
from roblox_creator.keycrop import symbol_crop
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.puzzle_ui import GameError, GameUI
from roblox_creator.socksrelay import SocksRelay
from roblox_creator.vlm_client import VisionClient
from tools.browser_creator import (LAUNCH_ARGS, STEALTH_JS, UA, fill_form,
                                   make_credentials)


N_PROMPT = ("This image shows a die face with a symbol, followed by an 'x' and a "
            "number. What is that number? Answer with one integer only.")


def read_n_pixels(png: bytes) -> tuple[int | None, float]:
    """Pixel reader, run on the panel minus its caption bar and border.

    `dicecount.read_target` was tuned for the strip's key band, and the browser's
    key panel is a different shape: a bordered 134x200 card with a "Match This!"
    caption along the bottom. Feeding it the whole card confuses the layout, so
    the caption and border are trimmed first and the reader gets the interior.
    """
    im = Image.open(io.BytesIO(png)).convert("RGB")
    w, h = im.size
    boxes = [(int(w * 0.04), int(h * 0.06), int(w * 0.96), int(h * 0.86)),
             (int(w * 0.18), int(h * 0.55), int(w * 0.85), int(h * 0.80)),
             None]
    for box in boxes:
        sub = im if box is None else im.crop(box)
        try:
            n, conf = dicecount.read_target(np.array(sub)[:, :, ::-1].copy())
        except Exception:
            continue
        if n and 1 <= n <= 6:
            return n, float(conf)
    return None, 0.0


async def read_n(png: bytes, client: VisionClient, log) -> int | None:
    """The required count.

    The browser's key panel is a 134x200 card with a "Match This!" caption, not
    the strip's key band, so the pixel reader needs its interior crop. Try that
    first (instant), then the model, then the raw pixel reader.
    """
    pix, conf = read_n_pixels(png)
    if pix:
        log(f"  N={pix} (pixels, conf {conf:.2f})")
        return pix

    im = Image.open(io.BytesIO(png)).convert("RGB")
    text, meta = client._ask(N_PROMPT, [im], max_tokens=8)
    if text:
        import re
        m = re.search(r"\d+", text)
        if m:
            v = int(m.group())
            if 1 <= v <= 6:
                log(f"  N={v} (model; pixels said {pix})")
                return v
    log(f"  N unreadable (pixels={pix}, model={text!r})")
    return None


async def solve_round(page, ui, client: VisionClient, log) -> dict:
    """Play the round currently on screen."""
    key, cand = await ui.key_and_candidate()
    if key is None or cand is None:
        return {"outcome": "no_images"}

    n = await read_n(key["png"], client, log)
    if not n:
        return {"outcome": "no_n"}

    _, total = await ui.position()
    log(f"  {total} candidates")

    # Count against the *symbol alone*. Showing the counter the whole card means
    # showing it "x N", and a model that can read N will answer N — one measured
    # round returned five tiles "equal" to the printed 3.
    key_rgb = np.array(Image.open(io.BytesIO(key["png"])).convert("RGB"))
    symbol = symbol_crop(key_rgb)
    if symbol is None:
        symbol = Image.fromarray(key_rgb)
        log("  symbol crop failed; using the whole card")
    else:
        log(f"  symbol crop {symbol.size}")

    started = time.time()

    # Phase 1 — capture every candidate. Paging is local and cheap, so do it all
    # first instead of interleaving it with network calls. Waiting for a model
    # answer before turning the page is what made a ten-candidate round take
    # forty seconds.
    shots: list[dict | None] = []
    await ui.goto(1, total)
    for i in range(1, total + 1):
        shots.append(await ui.candidate())
        if i < total and not await ui.next():
            break
        await page.wait_for_timeout(140)
    captured = time.time() - started
    log(f"  captured {len(shots)} candidates in {captured:.1f}s")

    # Phase 2 — count them all at once. Use the VLM (lithosai) for counting.
    tiles = [Image.open(io.BytesIO(sh["png"])).convert("RGB") if sh else None
             for sh in shots]
    jobs = [(j, t) for j, t in enumerate(tiles) if t is not None]
    counts: list[int | None] = [None] * len(tiles)
    counted = time.time()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(10, max(1, len(jobs)))) as pool:
        futures = {pool.submit(client.count, symbol, t): j for j, t in jobs}
        for fut, j in list(futures.items()):
            try:
                c, meta = fut.result()
            except Exception:
                c, meta = None, {}
            counts[j] = c
            log(f"  [{j + 1:2}/{total}] count={c} {meta.get('elapsed', '?')}s")
    log(f"  counted {len(jobs)} tiles in {time.time() - counted:.1f}s "
        f"(wall {time.time() - started:.1f}s)")

    hits = [i + 1 for i, c in enumerate(counts) if c is not None and c == n]
    result = {"outcome": "scored", "n": n, "counts": counts, "hits": hits,
              "seconds": round(time.time() - started, 1),
              "capture_seconds": round(captured, 1)}

    # In the browserless path an answer had to be right first time, because a
    # retry burned the exit's image allowance. Here nothing is metered: the page
    # already rendered its tiles. So a guess costs one submission and buys real
    # feedback, which makes abandoning strictly worse than trying.
    if not hits:
        ranked = [(c if c is not None else -1, i + 1) for i, c in enumerate(counts)]
        ranked.sort(reverse=True)
        best, tile = ranked[0] if ranked else (-1, 1)
        if best >= 0:
            hits = [tile]
            result["guessed"] = True
            log(f"  no tile has {n}; best guess is tile {tile} (count {best})")
        else:
            result["outcome"] = "no_hit"
            return result

    # A tie means the counter cannot separate two tiles; re-ask just those with
    # the enumeration prompt, which is a harder question and usually breaks it.
    if len(hits) > 1:
        log(f"  tie on {hits} — re-asking those")
        tied = [(j - 1, tiles[j - 1]) for j in hits if tiles[j - 1] is not None]
        refined: dict[int, int | None] = {}
        with ThreadPoolExecutor(max_workers=len(tied) or 1) as pool:
            futs = {pool.submit(client.count, symbol, t): j for j, t in tied}
            for fut, j in list(futs.items()):
                try:
                    refined[j + 1] = fut.result()[0]
                except Exception:
                    refined[j + 1] = None
        survivors = [i for i in hits if refined.get(i) == n]
        result["refined"] = refined
        if len(survivors) == 1:
            hits = survivors
        else:
            result["guessed_tie"] = True
            log(f"  tie unresolved; trying tile {hits[0]} anyway")

    target = hits[0]
    log(f"  -> tile {target} has the symbol {n} times; submitting")
    if not await ui.goto(target, total):
        result["outcome"] = "nav_failed"
        return result
    await page.wait_for_timeout(random.randint(300, 700))
    await ui.submit()

    # Read what the server did with it. There is no per-wave verdict to be had
    # from the browser either, but the *shape* of the next screen is informative:
    # a fresh pip means the round advanced, /home means the account exists, and a
    # visible error means the guess was wrong.
    for _ in range(24):
        await page.wait_for_timeout(500)
        if "/home" in page.url:
            result["outcome"] = "solved"
            return result
    try:
        live = await ui.frame()
        body = (await live.inner_text("body")).lower()
        pos = await ui.position()
    except Exception:
        body, pos = "", (0, 0)
    result["next_position"] = pos
    result["next_text"] = body[:120]
    if any(w in body for w in ("incorrect", "wrong", "try again", "failed")):
        result["outcome"] = "wrong"
    elif pos[0] != target:
        result["outcome"] = "advanced"
    else:
        result["outcome"] = "unchanged"
    return result


async def one(playwright, raw: str, client: VisionClient, log, timeout: float,
              headed: bool) -> dict:
    p = parse_proxy(raw)
    relay = await SocksRelay.start(f"{p.host}:{p.port}", p.user, p.password)
    browser = await playwright.chromium.launch(headless=not headed, args=LAUNCH_ARGS)
    out: dict = {"proxy": p.masked()}
    try:
        ctx = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York", device_scale_factor=2)
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()
        user, pwd = make_credentials()
        out["username"] = user
        try:
            await fill_form(page, user, pwd, human=True)
        except Exception as exc:
            out["outcome"] = "form_error"
            out["error"] = f"{type(exc).__name__}"
            return out
        if "/home" in page.url:
            out["outcome"] = "created_passive"
            return out

        ladder_shots = Path("/tmp/opencode/ladder")
        ladder_shots.mkdir(parents=True, exist_ok=True)
        try:
            ui = await GameUI.attach(page, timeout=timeout, log=log,
                                     shots=ladder_shots)
        except GameError as exc:
            out["outcome"] = "no_game"
            out["error"] = str(exc)[:80]
            if "/home" in page.url:
                out["outcome"] = "created_passive"
            return out

        if "/home" in page.url:
            out["outcome"] = "created_passive"
            return out
        out["instruction"] = (await ui.instruction())[:110]

        # The dice challenge is several rounds, not one. Submitting a round and
        # returning was the reason an account never appeared: the game was still
        # going and the browser was closed under it.
        rounds: list[dict] = []
        out["outcome"] = "played"
        for rnd in range(1, 13):
            if "/home" in page.url or "/discover" in page.url:
                out["outcome"] = "solved"
                break
            log(f"  --- round {rnd} ---")
            try:
                r = await solve_round(page, ui, client, log)
            except GameError as exc:
                out["error"] = f"round {rnd}: {str(exc)[:70]}"
                out["outcome"] = "game_error"
                break
            rounds.append(r)
            log(f"  round {rnd}: {r['outcome']}")
            if r["outcome"] == "solved" or "/home" in page.url:
                out["outcome"] = "solved"
                break
            if r["outcome"] in ("no_n", "no_images", "no_hit", "nav_failed"):
                out["outcome"] = r["outcome"]
                break
            # a new round needs a moment to mount its own key and first tile
            await page.wait_for_timeout(1200)
            try:
                await ui.goto(1, (await ui.position())[1])
            except Exception:
                pass
        out["rounds"] = [
            {"outcome": r.get("outcome"), "n": r.get("n"),
             "hits": r.get("hits"), "counts": r.get("counts")}
            for r in rounds
        ]
        if "/home" in page.url:
            out["outcome"] = "solved"

        if "/home" in page.url or out.get("outcome") == "solved":
            from roblox_creator import config
            cookies = await ctx.cookies()
            for c in cookies:
                if c.get("name") == config.COOKIE_NAME:
                    out["cookie"] = c.get("value")
                    break
        return out
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await relay.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--provider", default="ovh")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=70.0)
    ap.add_argument("--headless", action="store_true")
    args = ap.parse_args()

    exits = [parse_proxy(x).url() for x in load_proxies(args.proxy_file)]
    random.shuffle(exits)
    client = VisionClient(provider=args.provider, exits=exits)

    from playwright.async_api import async_playwright

    results = []
    async with async_playwright() as p:
        for i in range(args.rounds):
            print(f"\n== round {i + 1}/{args.rounds} ==")
            r = await one(p, exits[i % len(exits)], client,
                          lambda m: print(m, flush=True), args.timeout,
                          not args.headless)
            results.append(r)
            print(f"  outcome: {r.get('outcome')}  {r.get('error','')}")
            if r.get("counts"):
                print(f"  n={r.get('n')} counts={r['counts']} hits={r.get('hits')}")
            if r.get("cookie"):
                print(f"  COOKIE {r['cookie'][:24]}...")
                Path("/tmp/opencode/solved_cookie.txt").write_text(r["cookie"])
            if r.get("outcome") in ("solved", "created_passive"):
                break
    print(f"\n  stats: {client.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
