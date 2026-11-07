#!/usr/bin/env python3
"""AI-controlled browser: VLM sees screenshot, returns x/y coordinates.

The VLM decides everything: where to click, what to type, how long to hold.
No element extraction, no selectors — just screenshot → action → execute.
The AI learns hold duration by trying different values and seeing if the widget
disappears.

    ./tools/browser_agent.py --proxy-file proxies.txt --count 3 --timeout 300
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import os
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image
from playwright.async_api import async_playwright

from roblox_creator import config
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from roblox_creator.vlm_client import VisionClient
from tools.browser_creator import LAUNCH_ARGS, STEALTH_JS, UA, make_credentials

os.environ.setdefault("LITHOSAI_API_KEY", "lith_sk_3PcDFg0GMDF6sPVFpLiGgp76xgzvzIAyHNjpt6l2KZj2cunyE")

# Phone-sized viewport
VIEWPORT_W, VIEWPORT_H = 390, 844

SYSTEM_PROMPT = """You are an AI agent controlling a phone-sized browser (390x844 pixels) to create a Roblox account.

You see a screenshot. Decide the NEXT single action. Return ONLY valid JSON:
{{"action": "click", "x": <number>, "y": <number>, "reason": "..."}}
{{"action": "type", "text": "...", "reason": "..."}}
{{"action": "hold", "x": <number>, "y": <number>, "duration": <seconds>, "reason": "..."}}
{{"action": "wait", "seconds": <number>, "reason": "..."}}
{{"action": "scroll", "dy": <number>, "reason": "..."}}
{{"action": "done", "reason": "..."}}
{{"action": "fail", "reason": "..."}}

Coordinates are in SCREEN pixels (390x844). Click the CENTER of elements.

Task: Create a Roblox account.
The form is ALREADY FILLED (username, password, birthday, gender). 
Steps remaining:
1. Click "Sign Up" button (usually bottom of form)
2. If "Press and hold" appears, use hold action. Try 3 seconds first. If widget doesn't disappear, try 4, then 5.
3. If "Verifying browser..." appears, wait 3 seconds
4. If dice puzzle appears, count matching dice and click the right one, then click Submit
5. If page reaches /home or "Welcome", use done
6. If stuck 5+ steps, use fail

Credentials: username='{user}', password='{pwd}'.
"""


def log(msg):
    print(f"  {msg}", flush=True)


async def human_move(page, x, y, steps=8):
    start = await page.evaluate("() => ({x: window.__lx||195, y: window.__ly||422})")
    x0, y0 = float(start["x"]), float(start["y"])
    for i in range(1, steps + 1):
        t = i / steps
        ease = t * t * (3 - 2 * t)
        cx = x0 + (x - x0) * ease + random.uniform(-1.5, 1.5) * (1 - t)
        cy = y0 + (y - y0) * ease + random.uniform(-1.5, 1.5) * (1 - t)
        await page.mouse.move(cx, cy)
        await page.wait_for_timeout(random.randint(5, 12))
    await page.evaluate(f"() => {{ window.__lx = {x}; window.__ly = {y}; }}")
    await page.wait_for_timeout(random.randint(30, 80))


async def execute_action(page, action):
    act = action.get("action", "wait")
    if act == "click":
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        await human_move(page, x, y)
        await page.mouse.down()
        await page.wait_for_timeout(random.randint(40, 100))
        await page.mouse.up()
        await page.wait_for_timeout(random.randint(200, 400))
    elif act == "type":
        text = action.get("text", "")
        for ch in text:
            await page.keyboard.type(ch)
            await page.wait_for_timeout(random.randint(30, 80))
    elif act == "hold":
        dur = action.get("duration", 4)
        from roblox_creator.arkose_ui import find_hold_frame
        hf, hb = await find_hold_frame(page)
        if hf is not None and hb is not None:
            x = hb["x"] + hb["width"] / 2
            y = hb["y"] + hb["height"] / 2
            log(f"  holding at ({x:.0f},{y:.0f}) size {hb['width']:.0f}x{hb['height']:.0f} for {dur}s")
        else:
            x, y = int(action.get("x", 0)), int(action.get("y", 0))
            log(f"  holding at VLM coords ({x},{y}) for {dur}s")
        await human_move(page, x, y)
        await page.wait_for_timeout(200)
        await page.mouse.down()
        end = time.time() + dur
        gone = False
        while time.time() < end:
            await page.mouse.move(x + random.uniform(-0.3, 0.3), y + random.uniform(-0.3, 0.3))
            await page.wait_for_timeout(300)
            still, _ = await find_hold_frame(page)
            if still is None:
                gone = True
                break
        await page.mouse.up()
        await page.wait_for_timeout(500)
        log(f"  hold done, widget gone: {gone}")
    elif act == "wait":
        await page.wait_for_timeout(int(action.get("seconds", 2) * 1000))
    elif act == "scroll":
        await page.mouse.wheel(0, action.get("dy", 200))
        await page.wait_for_timeout(300)
    elif act in ("done", "fail"):
        return act
    return None


def parse_action(text):
    if not text:
        return {"action": "wait", "seconds": 2, "reason": "no response"}
    m = re.search(r'\{[^{}]*\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    return {"action": "wait", "seconds": 2, "reason": f"unparsed: {text[:80]}"}


async def run_account(pw, raw_proxy, client, username, password, timeout, max_steps):
    p = parse_proxy(raw_proxy)
    relay = await SocksRelay.start(f"{p.host}:{p.port}", p.user, p.password)
    try:
        browser = await pw.chromium.launch(headless=False, args=LAUNCH_ARGS)
        ctx = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
            locale="en-US", timezone_id="America/New_York")
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        prompt = SYSTEM_PROMPT.replace("{user}", username).replace("{pwd}", password)

        await page.goto("https://www.roblox.com/CreateAccount",
                        wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_timeout(2000)

        # Pre-fill form
        try:
            await page.fill("#signup-username", username)
            await page.press("#signup-username", "Tab")
            await page.wait_for_timeout(300)
            await page.fill("#signup-password", password)
            await page.press("#signup-password", "Tab")
            await page.wait_for_timeout(300)
            await page.select_option("#MonthDropdown", value="May")
            await page.select_option("#DayDropdown", value="15")
            await page.select_option("#YearDropdown", value="1996")
            await page.click("#FemaleButton")
            await page.wait_for_timeout(1500)
            log("form pre-filled")
        except Exception as e:
            log(f"pre-fill error: {e}")

        t0 = time.time()
        for step in range(max_steps):
            if time.time() - t0 > timeout:
                log("timeout")
                break
            if "/home" in page.url or "/discover" in page.url:
                log("reached home — done")
                cookies = await ctx.cookies()
                for c in cookies:
                    if c["name"] == config.COOKIE_NAME:
                        return {"outcome": "created", "cookie": c["value"],
                                "username": username, "password": password}
                return {"outcome": "created_no_cookie", "username": username}

            # Screenshot
            try:
                cdp = await ctx.new_cdp_session(page)
                r = await cdp.send("Page.captureScreenshot", {"format": "png"})
                png = base64.b64decode(r["data"])
                await cdp.detach()
            except Exception:
                png = await page.screenshot(type="png", timeout=5000)
            img = Image.open(io.BytesIO(png))

            text, meta = client._ask(prompt, [img], max_tokens=150)
            action = parse_action(text)
            log(f"step {step+1}: {action.get('action')} {str(action.get('reason',''))[:60]}")

            result = await execute_action(page, action)
            if result == "done":
                log("agent says done")
                cookies = await ctx.cookies()
                for c in cookies:
                    if c["name"] == config.COOKIE_NAME:
                        return {"outcome": "created", "cookie": c["value"],
                                "username": username, "password": password}
                return {"outcome": "done_no_cookie", "username": username}
            if result == "fail":
                log(f"agent says fail: {action.get('reason','')}")
                return {"outcome": "agent_fail", "reason": action.get("reason", ""),
                        "username": username}

            await page.wait_for_timeout(random.randint(300, 600))

        return {"outcome": "max_steps", "username": username}
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await relay.close()


async def main_async(args):
    proxies = [l.strip() for l in open(args.proxy_file) if l.strip()]
    random.shuffle(proxies)
    client = VisionClient(provider=args.provider, exits=[],
                          api_key=args.api_key or os.environ.get("LITHOSAI_API_KEY", ""))
    results = []
    async with async_playwright() as pw:
        for i in range(args.count):
            raw = proxies[i % len(proxies)]
            username, password = make_credentials()
            log(f"\n=== account {i+1}/{args.count}: {username} ===")
            r = await run_account(pw, raw, client, username, password,
                                  args.timeout, args.max_steps)
            results.append(r)
            log(f"  -> {r.get('outcome')}")
            if r.get("outcome") == "created" and r.get("cookie"):
                args.out.mkdir(parents=True, exist_ok=True)
                jsonl = args.out / "accounts.jsonl"
                with jsonl.open("a") as fh:
                    import json
                    fh.write(json.dumps(r) + "\n")
                log(f"  SAVED cookie for {r.get('username')}")
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--proxy-file", type=Path, default=Path("proxies.txt"))
    ap.add_argument("--out", type=Path, default=Path("accounts/agent"))
    ap.add_argument("--provider", default="lithosai")
    ap.add_argument("--api-key", default=None)
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("--max-steps", type=int, default=60)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    asyncio.run(main_async(args))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
PYEOF