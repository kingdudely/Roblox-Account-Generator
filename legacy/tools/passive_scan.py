#!/usr/bin/env python3
"""What fraction of exits create an account without asking anything of us?

This is the number the whole business rests on, and it has never been measured.
Everything built so far assumes a solve is mandatory — a vision model per wave,
ten waves, per account. But on 2026-11-01 an exit was watched creating an account
with **no challenge at all**: form submitted, `/home`, done. If that is common, the
solver is a fallback for a minority and the throughput ceiling is set by proxy
supply rather than by CAPTCHA accuracy.

Arkose is a risk engine. It does not decide "human or robot" — it decides whether
this session is *worth* a challenge, from IP reputation, fingerprint, form timings
and everything else it can see. A clean residential exit that fills the form like a
person is often simply waved through. This tool measures how often.

Per exit, on its own fresh context (no shared cookies), the outcome is one of:

    passive    no challenge frame ever appeared; the account exists
    hold       a press-and-hold appeared before anything else
    puzzle     a puzzle frame appeared — the expensive class
    blocked    an error or a refusal, no account
    dead       the exit would not connect

The rates are what matter, not any single exit. `passive` is free throughput.
`hold` is nearly free — two gestures and a retrigger, no pixels to read. `puzzle`
is the class the solver is for.

    ./tools/passive_scan.py --proxy-file proxies.txt --tries 40 --workers 4

Writes `debug/passive_scan/<stamp>/results.json` and prints a running tally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.async_api import async_playwright

from roblox_creator import config
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay

OUT = ROOT / "debug" / "passive_scan"

# Chrome flags that keep a headed browser from standing out. Same set the creator
# uses, so a measurement here predicts a run there.
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36")

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
window.chrome = window.chrome || { runtime: {} };
const orig = navigator.permissions.query;
navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : orig(p)
);
"""


def log(message: str) -> None:
    print(message, flush=True)


async def classify(page, deadline: float) -> tuple[str, dict]:
    """Watch one submitted signup until it resolves into a class."""
    from roblox_creator.arkose_ui import find_hold_frame
    from roblox_creator.puzzle_ui import GameError, GameUI

    seen_arkose = False
    detail: dict = {}
    while time.time() < deadline:
        await page.wait_for_timeout(900)
        url = page.url
        if "/home" in url or "/discover" in url:
            detail["url"] = url[:60]
            return ("passive" if not seen_arkose else "passive-late"), detail

        frames = [f.url for f in page.frames]
        if any("arkoselabs" in f for f in frames):
            seen_arkose = True
            detail["frames"] = [f[:70] for f in frames if "arkoselabs" in f]

        if seen_arkose:
            frame, box = await find_hold_frame(page)
            if frame is not None and box is not None:
                detail["hold"] = f"{box['width']:.0f}x{box['height']:.0f}"
                return "hold", detail
            try:
                await GameUI.attach(page, timeout=2, log=None)
            except GameError:
                pass
            else:
                return "puzzle", detail

        # Roblox surfaces its own refusals on the form, outside the challenge.
        try:
            text = (await page.inner_text("body"))[:2000].lower()
        except Exception:
            text = ""
        for needle in ("too many attempts", "something went wrong",
                       "please try again later", "not available"):
            if needle in text:
                detail["note"] = needle
                return "blocked", detail

    return "timeout", detail


async def one_exit(browser, raw: str, stamp_dir: Path, limit: float) -> dict:
    parsed = parse_proxy(raw)
    result = {"exit": parsed.masked(), "class": "unknown"}
    relay = None
    try:
        relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                       parsed.user, parsed.password)
        context = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()
        try:
            await page.goto("https://www.roblox.com/CreateAccount",
                            wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_selector(config.SEL_USERNAME, timeout=20000)
        except Exception as exc:
            result["class"] = "dead"
            result["note"] = f"{type(exc).__name__}: {str(exc)[:60]}"
            return result

        from tools.browser_creator import fill_form, make_credentials
        user, password = make_credentials()
        result["username"] = user
        try:
            await fill_form(page, user, password, human=True)
        except Exception as exc:
            result["class"] = "form-error"
            result["note"] = f"{type(exc).__name__}: {str(exc)[:60]}"
            return result

        # fill_form navigates itself, so the form is already submitted by now.
        klass, detail = await classify(page, time.time() + limit)
        result["class"] = klass
        result.update(detail)
        try:
            await page.screenshot(path=str(stamp_dir / f"{user}.png"))
        except Exception:
            pass
        return result
    except Exception as exc:
        result["class"] = "error"
        result["note"] = f"{type(exc).__name__}: {str(exc)[:70]}"
        return result
    finally:
        try:
            if relay is not None:
                await relay.close()
        except Exception:
            pass


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--tries", type=int, default=40)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--limit", type=float, default=90.0,
                    help="seconds to watch one signup before calling it a timeout")
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)
    picks = proxies[:args.tries]

    stamp = time.strftime("%Y%m%d-%H%M%S")
    case = OUT / stamp
    case.mkdir(parents=True, exist_ok=True)

    tally: Counter[str] = Counter()
    results: list[dict] = []
    queue = asyncio.Queue()
    for raw in picks:
        queue.put_nowait(raw)

    started = time.time()

    async def worker(index: int) -> None:
        while True:
            try:
                raw = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(
                    headless=False, args=LAUNCH_ARGS)
                try:
                    result = await one_exit(browser, raw, case, args.limit)
                finally:
                    try:
                        await browser.close()
                    except Exception:
                        pass
            results.append(result)
            tally[result["class"]] += 1
            done = sum(tally.values())
            rate = done / max(1e-6, (time.time() - started) / 60)
            log(f"[w{index}] {result['class']:<12} {done:>3}/{len(picks)} "
                f"{rate:5.1f}/min  {result['exit'][:46]}")
            (case / "results.json").write_text(json.dumps(
                {"tally": dict(tally), "results": results}, indent=1))

    await asyncio.gather(*(worker(i) for i in range(args.workers)))

    json.dump({"tally": dict(tally), "results": results},
              open(case / "results.json", "w"), indent=1)

    total = max(1, sum(tally.values()))
    log("")
    log(f"--- {total} exits in {(time.time() - started) / 60:.1f} min "
        f"({case})")
    for klass, count in tally.most_common():
        log(f"    {klass:<12} {count:>4}  {count / total:6.1%}")
    log("")
    log("  free throughput lives in `passive`; `hold` costs gestures, not vision")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
