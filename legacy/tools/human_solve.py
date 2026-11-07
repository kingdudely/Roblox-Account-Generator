#!/usr/bin/env python3
"""Open a real challenge and record a human solving it.

Everything expensive in this repo has been unmeasurable because Arkose gives no
per-wave feedback: a wrong answer and a right one both come back `not answered`.
That makes accuracy impossible to calibrate. One human solve fixes it, and leaves
three artefacts that cannot be obtained any other way:

1. **The index convention.** The page encrypts `JSON.stringify(guesses)` before
   posting. This wraps `JSON.stringify` inside every frame and records any array
   of objects carrying an `index` key, so the *plaintext* guess is captured right
   before it is encrypted. Whether the first candidate is `0` or `1` stops being
   a guess.
2. **Real browser image depth.** Every `/rtig/image` status is logged. If a human
   gets five 200s where the browserless chain gets two, the grant problem is our
   transport. If the human also dies at two, the exit is starved and the fix is
   the pool.
3. **A labelled puzzle.** Screenshots per wave, next to the guess that solved it.

Run it, solve the captcha in the window that opens, and leave it alone.

    ./tools/human_solve.py --proxy-file proxies2.txt
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
from tools.browser_creator import LAUNCH_ARGS, STEALTH_JS, UA, fill_form, make_credentials

OUT = ROOT / "debug" / "human"

# Captures the plaintext of every guess list, before encryptECData turns it into
# ciphertext. Installed in every frame, since the game core is an iframe.
GUESS_HOOK = """
(() => {
  if (window.__guessHook) return;
  window.__guessHook = true;
  window.__guesses = [];
  const original = JSON.stringify;
  JSON.stringify = function (value, ...rest) {
    try {
      if (Array.isArray(value) && value.length &&
          value[0] && typeof value[0] === 'object' && 'index' in value[0]) {
        window.__guesses.push(JSON.parse(original.call(this, value)));
      }
    } catch (e) {}
    return original.call(this, value, ...rest);
  };
})();
"""


def log(message: str) -> None:
    print(message, flush=True)


async def run(playwright, raw: str, limit: float) -> dict:
    parsed = parse_proxy(raw)
    relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                   parsed.user, parsed.password)
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    case = OUT / stamp
    case.mkdir(parents=True, exist_ok=True)

    result = {"exit": parsed.masked(), "rtig": [], "guesses": [], "waves": 0,
              "outcome": "unknown", "case": str(case)}
    browser = None
    try:
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        context = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await context.add_init_script(STEALTH_JS)
        await context.add_init_script(GUESS_HOOK)
        page = await context.new_page()

        def on_response(response):
            if "/rtig/image" in response.url:
                result["rtig"].append(response.status)
                log(f"    rtig {response.status}")

        page.on("response", on_response)

        log(f"exit {parsed.masked()}")
        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_selector(config.SEL_USERNAME, timeout=20000)
        user, password = make_credentials()
        await fill_form(page, user, password, human=True)
        log(f"submitted {user} — SOLVE THE CAPTCHA IN THE WINDOW")
        log("do not touch the terminal; this exits on its own")

        started = time.time()
        seen = 0
        while time.time() - started < limit:
            await page.wait_for_timeout(1000)
            elapsed = int(time.time() - started)

            if "/home" in page.url:
                result["outcome"] = "created"
                break

            captured = []
            for frame in page.frames:
                try:
                    bank = await frame.evaluate("() => window.__guesses || []")
                except Exception:
                    continue
                for entry in bank or []:
                    if entry and entry not in captured:
                        captured.append(entry)
            if len(captured) > seen:
                for entry in captured[seen:]:
                    log(f"    guess: {entry}")
                    result["guesses"].append(entry)
                seen = len(captured)
                # one screenshot per new guess: that is the wave it answered
                try:
                    await page.screenshot(path=str(case / f"wave_{seen:02d}.png"))
                except Exception:
                    pass

            if elapsed % 15 == 0 and elapsed:
                images = sum(1 for s in result["rtig"] if s == 200)
                log(f"  [{elapsed}s] images_ok={images} guesses={seen} "
                    f"frames={len(page.frames)}")

        result["rtig_ok"] = sum(1 for s in result["rtig"] if s == 200)
        result["waves"] = len(result["guesses"])

        # The plaintext guesses are the whole point; keep the final page too.
        try:
            await page.screenshot(path=str(case / "final.png"))
        except Exception:
            pass
        (case / "capture.json").write_text(json.dumps(result, indent=1))
        return result
    except Exception as exc:
        result["outcome"] = f"error {type(exc).__name__}: {str(exc)[:80]}"
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
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--limit", type=float, default=420.0,
                    help="seconds to wait for the human before giving up")
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    async with async_playwright() as playwright:
        for raw in proxies[:args.tries]:
            row = await run(playwright, raw, args.limit)
            ok = row.get("rtig_ok", 0)
            log(f"\n--- {row['outcome']} ---")
            log(f"  rtig 200={ok}  403={sum(1 for s in row['rtig'] if s == 403)}")
            log(f"  guesses captured: {len(row.get('guesses') or [])}")
            log(f"  saved {row.get('case')}")
            if row.get("outcome") in ("created",) or ok >= 3:
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
