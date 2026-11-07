#!/usr/bin/env python3
"""Create Roblox accounts with a real browser. This is the product loop.

Why this exists
---------------
The browserless path provokes a full Arkose visual challenge every time: the signup
POST returns 403 with `dataExchangeBlob`, and the only way through is seven requests
of protocol, a proof of work, a signed image per wave, and five correct puzzle
answers. Measured cost of that: roughly one challenge in six is even completable,
and per-wave accuracy is capped by a ~30px symbol.

A real browser gets a different verdict. Observed on the same proxy and the same
public key: Arkose serves `settings` -> `gt2` -> `fc/a/` and nothing else. No
`gfct`, no `/rtig/image`, no proof of work, no puzzle. The passive challenge passes
and Roblox redirects to `/home` as the new user.

So the account creator is a browser that fills a form. The hard part becomes
parallelism and survivorship, not captcha.

    ./tools/browser_creator.py --count 8 --concurrency 4 --out accounts/new
    ./tools/browser_creator.py --count 40 --concurrency 8 --proxy-file proxies.txt
    ./tools/browser_creator.py --count 2 --headless          # to check whether
                                                            # headless still passes

Each account gets its own sticky proxy session, its own relay, and its own browser
context. The `.ROBLOSECURITY` cookie is captured, verified against
`users/v1/users/authenticated`, and written to JSONL as soon as it is proven valid —
so a crash costs one account, not the batch.
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
from roblox_creator.arkose_ui import handle_challenge
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-dev-shm-usage",
    "--window-size=1366,900",
]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
window.chrome = window.chrome || {runtime: {}};
const origQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (p) => (
  p.name === 'notifications'
    ? Promise.resolve({state: Notification.permission})
    : origQuery(p)
);
"""

FIRST = ["Quiet", "Harbor", "Amber", "Vellum", "Cinder", "Larkspur", "Basalt",
         "Tidal", "Onyx", "Marigold", "Sable", "Ivory", "Juniper", "Cobalt",
         "Willow", "Flint", "Sorrel", "Indigo", "Bramble", "Halcyon"]
SECOND = ["Ridge", "Hollow", "Vale", "Reed", "Fen", "Marsh", "Brook", "Glen",
          "Thorn", "Moss", "Slate", "Crest", "Haven", "Drift", "Shore"]

# Arkose frames that mean "a puzzle is being shown", which is the failure case we
# must not spend time on: the passive path never mounts the game frame.
PUZZLE_MARKERS = ("MatchDiceIconCount", "game-core", "ec-game-core")


async def human_move(page, x: float, y: float, steps: int = 14) -> None:
    """Walk the pointer to a point along a slightly curved path.

    Playwright's `click` teleports the cursor. Arkose samples pointer telemetry
    during the passive challenge, and a straight teleport with a single mousedown
    is one of the cheapest bot signals there is — so every interaction here is
    preceded by a real path with jitter and a pause before the press.
    """
    start = await page.evaluate(
        "() => ({x: window.__lx || 300, y: window.__ly || 300})")
    x0, y0 = float(start["x"]), float(start["y"])
    for i in range(1, steps + 1):
        t = i / steps
        ease = t * t * (3 - 2 * t)
        cx = x0 + (x - x0) * ease + random.uniform(-3.0, 3.0) * (1 - t)
        cy = y0 + (y - y0) * ease + random.uniform(-2.5, 2.5) * (1 - t)
        await page.mouse.move(cx, cy)
        await page.wait_for_timeout(random.randint(8, 26))
    await page.evaluate(f"() => {{ window.__lx = {x}; window.__ly = {y}; }}")
    await page.wait_for_timeout(random.randint(90, 260))


async def human_click(page, selector: str) -> None:
    el = await page.query_selector(selector)
    if el is None:
        raise RuntimeError(f"no element for {selector}")
    # The submit button (and sometimes the year dropdown) sit below the fold.
    # page.mouse uses viewport coordinates, so clicking an off-screen box's
    # coordinates lands on empty space and the form never submits — which is why
    # every run sat on the signup page. Scroll first, then re-read the box.
    try:
        await el.scroll_into_view_if_needed()
    except Exception:
        pass
    await page.wait_for_timeout(random.randint(60, 140))
    box = await el.bounding_box()
    if box is None:
        raise RuntimeError(f"no box for {selector}")
    x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
    y = box["y"] + box["height"] * random.uniform(0.4, 0.6)
    await human_move(page, x, y)
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(45, 120))
    await page.mouse.up()
    await page.wait_for_timeout(random.randint(120, 320))


async def human_type(page, selector: str, text: str) -> None:
    await human_click(page, selector)
    for ch in text:
        await page.keyboard.type(ch)
        await page.wait_for_timeout(random.randint(35, 130))


def make_credentials() -> tuple[str, str]:
    user = f"{random.choice(FIRST)}{random.choice(SECOND)}{random.randint(100, 9999)}"
    pwd = f"{random.choice(FIRST)}{random.randint(1000, 9999)}!{random.choice(SECOND)}"
    return user, pwd


async def fill_form(page, username: str, password: str, human: bool = True) -> bool:
    await page.goto("https://www.roblox.com/CreateAccount",
                    wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_selector(config.SEL_USERNAME, timeout=45000)

    if not human:
        await page.wait_for_timeout(random.randint(300, 900))
        await page.fill(config.SEL_USERNAME, username)
        await page.wait_for_timeout(random.randint(120, 380))
        await page.fill(config.SEL_PASSWORD, password)
        await page.wait_for_timeout(random.randint(120, 380))
        await page.select_option(config.SEL_MONTH, index=random.randint(1, 12))
        await page.select_option(config.SEL_DAY, index=random.randint(1, 27))
        await page.select_option(config.SEL_YEAR, str(random.randint(1994, 2002)))
        await page.wait_for_timeout(random.randint(200, 500))
        await page.click(config.SEL_FEMALE if random.random() < 0.5 else config.SEL_MALE)
        await page.wait_for_timeout(random.randint(400, 1100))
        await page.click(config.SEL_SUBMIT, timeout=15000)
        return True

    await page.wait_for_timeout(random.randint(700, 1600))

    # drift around the page first, so the pointer has history before any control
    for _ in range(random.randint(2, 4)):
        await human_move(page, random.randint(120, 1240), random.randint(120, 700),
                         steps=random.randint(9, 18))
    await page.mouse.wheel(0, random.randint(60, 220))
    await page.wait_for_timeout(random.randint(400, 900))

    await human_type(page, config.SEL_USERNAME, username)
    await page.wait_for_timeout(random.randint(300, 750))
    await human_type(page, config.SEL_PASSWORD, password)
    await page.wait_for_timeout(random.randint(400, 900))

    for sel, idx in ((config.SEL_MONTH, random.randint(1, 12)),
                     (config.SEL_DAY, random.randint(1, 27))):
        await human_click(page, sel)
        await page.wait_for_timeout(random.randint(180, 420))
        await page.select_option(sel, index=idx)
        await page.wait_for_timeout(random.randint(200, 480))
    await human_click(page, config.SEL_YEAR)
    await page.wait_for_timeout(random.randint(180, 420))
    await page.select_option(config.SEL_YEAR, str(random.randint(1994, 2002)))
    await page.wait_for_timeout(random.randint(300, 700))

    await human_click(page, config.SEL_FEMALE if random.random() < 0.5
                      else config.SEL_MALE)
    await page.wait_for_timeout(random.randint(600, 1400))
    await human_click(page, config.SEL_SUBMIT)
    return True


async def wait_for_account(page, timeout: float, log=None) -> dict:
    """Drive the Arkose ladder until the account lands or a puzzle blocks it.

    The browser is handed one of four challenges (see `roblox_creator/arkose_ui`).
    Three of them need no answers at all, so this delegates rather than watching:
    only the dice puzzle is a real loss, and it is reported as such.
    """
    out = await handle_challenge(page, timeout=timeout, log=log)
    out["url"] = page.url
    if out["outcome"] != "created":
        try:
            out["body"] = (await page.inner_text("body"))[:240].replace("\n", " | ")
        except Exception:
            pass
    return out


def find_cookie(cookies: list[dict], name: str) -> str | None:
    for c in cookies:
        if c.get("name") == name:
            return c.get("value")
    return None


async def verify(context, cookie: str) -> dict | None:
    """Prove the cookie works before it is written down."""
    try:
        resp = await context.request.get(
            config.AUTHENTICATED_USER,
            headers={"accept": "application/json"},
            timeout=20000)
        if resp.status != 200:
            return None
        return await resp.json()
    except Exception:
        return None


async def make_one(playwright, browser, raw_proxy: str, index: int,
                   headless: bool, timeout: float, human: bool = True) -> dict:
    p = parse_proxy(raw_proxy)
    relay = None
    context = None
    started = time.time()
    result: dict = {"index": index, "proxy": p.masked(), "when": time.strftime(
        "%Y-%m-%dT%H:%M:%S")}
    try:
        relay = await SocksRelay.start(f"{p.host}:{p.port}", p.user, p.password)
        context = await browser.new_context(
            proxy={"server": relay.url},
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            device_scale_factor=1,
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        username, password = make_credentials()
        result["username"] = username
        result["password"] = password

        try:
            await fill_form(page, username, password, human=human)
        except Exception as exc:
            result.update(outcome="form_error",
                          error=f"{type(exc).__name__}: {str(exc)[:140]}")
            return result

        state = await wait_for_account(
            page, timeout, log=lambda m: print(f"    {m.strip()}", flush=True))
        result.update(state)

        if state["outcome"] != "created":
            # a puzzle means Arkose escalated; a timeout may mean a username
            # collision, which shows as an inline error on the same form
            try:
                result["body"] = (await page.inner_text("body"))[:240].replace(
                    "\n", " | ")
            except Exception:
                pass
            return result

        cookies = await context.cookies()
        cookie = find_cookie(cookies, config.COOKIE_NAME)
        if not cookie:
            result.update(outcome="no_cookie",
                          cookie_names=[c["name"] for c in cookies])
            return result

        ident = await verify(context, cookie)
        if not ident:
            result.update(outcome="cookie_unverified")
            return result

        result.update(
            outcome="created",
            user_id=ident.get("id"),
            display_name=ident.get("displayName"),
            name=ident.get("name"),
            cookie=cookie,
            applied=bool(ident.get("isBanned")) is False,
        )
        return result
    except Exception as exc:
        result.update(outcome="error", error=f"{type(exc).__name__}: {str(exc)[:140]}")
        return result
    finally:
        result["seconds"] = round(time.time() - started, 1)
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if relay is not None:
            await relay.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=8)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--out", type=Path, default=ROOT / "accounts" / "new")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--human", dest="human", action="store_true", default=True,
                    help="realistic pointer/typing telemetry (default)")
    ap.add_argument("--no-human", dest="human", action="store_false",
                    help="instant fill, for A/B against telemetry")
    ap.add_argument("--interleave", action="store_true",
                    help="alternate human/instant per exit, so pool drift cannot be "
                         "mistaken for an effect")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    jsonl = args.out / "accounts.jsonl"
    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)
    if len(proxies) < args.count:
        print(f"  only {len(proxies)} exits for {args.count} accounts")
        return 1
    batch = proxies[:args.count]

    from playwright.async_api import async_playwright

    sem = asyncio.Semaphore(args.concurrency)
    done = 0
    made = 0
    results_all: list[dict] = []
    lock = asyncio.Lock()
    started = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=args.headless, args=LAUNCH_ARGS)

        async def worker(idx: int, raw: str) -> None:
            nonlocal done, made
            async with sem:
                arm = (idx % 2 == 0) if args.interleave else args.human
                r = await make_one(p, browser, raw, idx, args.headless,
                                   args.timeout, human=arm)
                r["arm"] = "human" if arm else "instant"
                async with lock:
                    done += 1
                    results_all.append(r)
                    if r.get("outcome") == "created" and r.get("cookie"):
                        made += 1
                        with jsonl.open("a") as fh:
                            fh.write(json.dumps(r) + "\n")
                        print(f"  [{done}/{args.count}] {r.get('arm',''):8} "
                              f"CREATED {r.get('name')} ({r.get('user_id')}) "
                              f"in {r.get('seconds')}s")
                    else:
                        extra = r.get("body") or r.get("error") or r.get("frame") or ""
                        print(f"  [{done}/{args.count}] {r.get('arm',''):8} "
                              f"{r.get('outcome'):14} {r.get('seconds')}s  "
                              f"{str(extra)[:70]}")

        await asyncio.gather(*(worker(i, raw) for i, raw in enumerate(batch)))
        await browser.close()

    elapsed = time.time() - started
    rate = made / elapsed * 60 if elapsed else 0
    print(f"\n  {made}/{args.count} accounts in {elapsed:.0f}s "
          f"({rate:.1f}/min at concurrency {args.concurrency})")
    if args.interleave:
        tally: dict[str, dict[str, int]] = {}
        for r in results_all:
            a = r.get("arm", "?")
            tally.setdefault(a, {"n": 0, "ok": 0})
            tally[a]["n"] += 1
            tally[a]["ok"] += int(r.get("outcome") == "created")
        for a, t in tally.items():
            print(f"  arm {a:8}: {t['ok']}/{t['n']} created")
    print(f"  -> {jsonl}")
    return 0 if made else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
