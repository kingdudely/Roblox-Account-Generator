"""Two questions, one harness.

A. Split test — does spoofing `navigator.userAgentData` break the signup submit?
   The coherent device profile made the browser claim Windows, and the run then
   produced "no gate": no challenge, no account, no error. Client hints are the
   newest and most invasive override, and Roblox's signup code consumes them, so
   it is the prime suspect. Same device sample and same exit, one arm with the
   override and one without; the metric is the single observable that matters —
   did `/v2/signup` leave the browser, and what came back.

B. Radix hang — what is pending when step two sticks?
   On the radix layout, Continue mounts `#signup-v2-password` under a spinner that
   never clears and never issues `/v2/signup`. Recording every relevant request
   around the click and listing the ones that never received a response names the
   call the form is waiting on.

Each sample gets its own browser and is closed immediately; a long run of shared
contexts is what killed the previous attempt.

    PYTHONPATH=. .venv/bin/python tools/split_test.py --split 3
    PYTHONPATH=. .venv/bin/python tools/split_test.py --radix
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from roblox_creator import device_profile as dp
from roblox_creator import signup_form as form
from roblox_creator.config import load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.socks_bridge import SocksBridge
from tools.browser_creator import LAUNCH_ARGS

OUT = Path("debug/split_test")
WATCH = ("/v2/signup", "/v2/captcha", "arkoselabs", "px-cloud", "challenge",
         "usernames/validate", "passwords/validate", "user-agreements",
         "signup", "captcha")


def log(msg: str) -> None:
    print(msg, flush=True)


class Sample:
    """One browser, one context, closed no matter what."""

    def __init__(self, raw: str, device: dp.Device, uadata: bool):
        self.raw = raw
        self.device = device
        self.uadata = uadata
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        self.pw = sync_playwright().start()
        proxy = None
        if self.raw:
            proxy = {"server": SocksBridge(self.raw).start()}
        self.browser = self.pw.chromium.launch(headless=False, args=LAUNCH_ARGS)
        self.ctx = self.browser.new_context(proxy=proxy, **dp.context(self.device))
        self.ctx.add_init_script(dp.init_script(self.device, self.uadata))
        self.page = self.ctx.new_page()
        return self

    def load(self, timeout: int = 45000) -> bool:
        try:
            self.page.goto("https://www.roblox.com/CreateAccount", wait_until="domcontentloaded", timeout=timeout)
            self.page.wait_for_selector("#signup-username", state="visible", timeout=25000)
            self.page.wait_for_timeout(1800)
            return True
        except Exception:
            return False

    def reload(self) -> bool:
        try:
            self.page.reload(wait_until="domcontentloaded", timeout=45000)
            self.page.wait_for_selector("#signup-username", state="visible", timeout=20000)
            self.page.wait_for_timeout(1200)
            return True
        except Exception:
            return False

    def __exit__(self, *exc):
        for closer in (lambda: self.browser.close(), lambda: self.pw.stop()):
            try:
                closer()
            except Exception:
                pass
        return False


def watch_network(page) -> dict:
    """Requests that got no response by the end of the sample."""
    pending: dict[int, dict] = {}

    def on_request(r):
        if any(k in r.url for k in WATCH):
            pending[id(r)] = {"url": r.url[:120], "method": r.method}

    def on_response(r):
        try:
            pending.pop(id(r.request), None)
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("requestfailed", lambda r: pending.pop(id(r), None))
    return pending


def fill_classic(page, ident, rng) -> None:
    form.set_birthday(page, ident.birthday_month, ident.birthday_day,
                      ident.birthday_year, rng, form.CLASSIC)
    page.fill("#signup-username", ident.username)
    page.wait_for_timeout(350)
    form.choose_gender(page, ident.gender, rng)
    page.wait_for_timeout(350)
    selector = form.password_selector(page)
    if selector:
        page.fill(selector, ident.password)
        page.wait_for_timeout(300)


def split_arm(raw: str, uadata: bool) -> dict:
    device = dp.sample()
    arm = "uadata" if uadata else "plain"
    result = {"arm": arm, "device": dp.describe(device)}
    with Sample(raw, device, uadata) as smp:
        if not smp.load():
            result["outcome"] = "load-failed"
            return result
        page = smp.page
        layout = form.detect_variant(page)
        if layout != form.CLASSIC:
            form.ensure_layout(page, form.CLASSIC, tries=6)
            layout = form.detect_variant(page)
        result["layout"] = layout
        if layout != form.CLASSIC:
            result["outcome"] = "radix-only"
            return result

        events = []
        page.on("response", lambda r: events.append((r.status, r.url[:90]))
              if "/v2/signup" in r.url or "/v2/captcha" in r.url else None)
        pending = watch_network(page)

        rng = random.Random()
        ident = make_identity(rng)
        result["identity"] = ident.username
        fill_classic(page, ident, rng)

        started = time.time()
        result["clicked"] = form.click_submit(page, rng)
        for _ in range(40):
            page.wait_for_timeout(600)
            if events:
                break
        result["elapsed"] = round(time.time() - started, 1)
        result["signup_calls"] = events[:4]
        result["outcome"] = "signup-fired" if events else "no-call"
        result["pending"] = list(pending.values())[:6]
        return result


def radix_capture(raw: str) -> dict:
    device = dp.sample()
    with Sample(raw, device, True) as smp:
        if not smp.load():
            return {"outcome": "load-failed"}
        page = smp.page
        for _ in range(6):
            if form.detect_variant(page) == form.RADIX:
                break
            if not smp.reload():
                break
        if form.detect_variant(page) != form.RADIX:
            return {"outcome": "no-radix"}

        rng = random.Random()
        ident = make_identity(rng)
        form.set_birthday(page, ident.birthday_month, ident.birthday_day,
                          ident.birthday_year, rng, form.RADIX)
        page.fill("#signup-username", ident.username)
        page.wait_for_timeout(300)
        form.choose_gender(page, ident.gender, rng)
        page.wait_for_timeout(500)

        pending = watch_network(page)
        clicked = form.click_submit(page, rng)
        states = []
        for step in range(9):
            page.wait_for_timeout(1500)
            state = page.evaluate(
                """() => ({ busy: !!document.querySelector('[aria-busy="true"]'),
                            pw: !!document.querySelector('#signup-v2-password'),
                            hidden: (() => { const p = document.querySelector('#signup-v2-password');
                              if (!p) return null;
                              const r = p.getBoundingClientRect();
                              const hit = document.elementFromPoint(r.x + r.width/2, r.y + r.height/2);
                              return hit ? (hit.tagName + '#' + (hit.id||'')) : 'none'; })(),
                            buttons: [...document.querySelectorAll('button')]
                              .filter(b => b.getBoundingClientRect().width > 40)
                              .map(b => (b.getAttribute('aria-label') || b.innerText || '').trim().slice(0, 18))
                              .filter(Boolean).slice(0, 6) })""")
            states.append({"t": (step + 1) * 1.5, **state})
        return {"outcome": "captured", "clicked": clicked, "identity": ident.username,
                "states": states, "pending": list(pending.values())[:10],
                "layout": page.evaluate("() => document.querySelector('#MonthDropdown').tagName")}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies1.txt")
    ap.add_argument("--split", type=int, default=0, help="samples per arm")
    ap.add_argument("--radix", action="store_true")
    ap.add_argument("--tries", type=int, default=8)
    ap.add_argument("--no-proxy", action="store_true",
                    help="direct egress; enough for the UA-data split and the radix hang")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    pool = [None] if args.no_proxy else load_proxies(Path(args.proxy_file))
    if not args.no_proxy:
        random.shuffle(pool)
    cursor = 0
    report: dict = {"split": [], "radix": None}

    def next_exit() -> str:
        nonlocal cursor
        cursor += 1
        return pool[cursor % len(pool)]

    if args.split:
        for uadata in (True, False):
            done = 0
            for _ in range(args.tries):
                if done >= args.split:
                    break
                try:
                    row = split_arm(next_exit(), uadata)
                except Exception as exc:
                    log(f"  {'uadata' if uadata else 'plain'}: "
                        f"{type(exc).__name__}: {str(exc)[:60]}")
                    continue
                if row.get("outcome") in ("load-failed", "radix-only"):
                    log(f"  {'uadata' if uadata else 'plain'}: {row['outcome']}")
                    continue
                report["split"].append(row)
                done += 1
                log(f"  {row['arm']:<6} layout={row.get('layout')} "
                    f"clicked={row.get('clicked')} -> {row.get('outcome')} "
                    f"calls={row.get('signup_calls')}")

    if args.radix:
        for _ in range(args.tries):
            try:
                row = radix_capture(next_exit())
            except Exception as exc:
                log(f"  radix: {type(exc).__name__}: {str(exc)[:60]}")
                continue
            if row.get("outcome") != "captured":
                log(f"  radix: {row.get('outcome')}")
                continue
            report["radix"] = row
            log(f"  radix clicked={row['clicked']} layout={row['layout']}")
            for state in row["states"][-3:]:
                log(f"    t{state['t']:>4}s busy={state['busy']} pw={state['pw']} "
                    f"hit={state['hidden']} btns={state['buttons']}")
            log(f"    pending ({len(row['pending'])}):")
            for item in row["pending"]:
                log(f"      {item['method']:<4} {item['url']}")
            break

    path = OUT / f"{stamp}.json"
    path.write_text(json.dumps(report, indent=1))
    log(f"\n  -> {path}")

    arms: dict[str, dict] = {}
    for row in report["split"]:
        bucket = arms.setdefault(row["arm"], {"fired": 0, "none": 0})
        bucket["fired" if row.get("outcome") == "signup-fired" else "none"] += 1
    for key, bucket in arms.items():
        log(f"  arm {key:<6} signup-fired={bucket['fired']} no-call={bucket['none']}")


if __name__ == "__main__":
    main()
