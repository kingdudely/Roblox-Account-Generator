"""Record one *successful, human* signup end to end, so it can be replicated.

Two accounts were created by hand on this project; every automated run since has
been challenged. The difference is not the solve logic — it is what the browser
and the network looked like before and during the signup. That difference is
observable: record the human's traffic, then diff it against an automated
attempt and fix the tells one at a time.

The window is handed to the operator. Nothing is filled, clicked or typed by this
script — that is the whole point. It records requests, response headers, cookie
mutations and screenshots, and stops when `.ROBLOSECURITY` appears.

    PYTHONPATH=. .venv/bin/python tools/human_win.py                 # direct, warm profile
    PYTHONPATH=. .venv/bin/python tools/human_win.py --proxy-file proxies1.txt
    PYTHONPATH=. .venv/bin/python tools/human_win.py --profile debug/profiles/win1

Use the same network the two successful accounts used. If that was a home
connection, run it with `--no-proxy`.

Do not touch the mouse while a hold is in progress — a real cursor leaving the
page fires the events the widget reads as "released".
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from roblox_creator.browser import BrowserSession
from roblox_creator.config import COOKIE_NAME, SIGNUP_URL, Settings, load_proxies
from roblox_creator.proxyurl import parse

OUT = Path("debug/human_win")
WATCH = ("arkoselabs", "funcaptcha", "px-cloud", "px-cdn", "captcha",
         "/v2/signup", "/v2/captcha", "challenge", "auth.roblox")


def log(message: str) -> None:
    print(message, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies1.txt")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--profile", default="debug/profiles/human",
                    help="persistent Chrome profile; reuse it between runs")
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    exit_ = None
    if not args.no_proxy:
        pool = load_proxies(Path(args.proxy_file))
        exit_ = pool[0]

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    events: list[dict] = []
    shots: list[str] = []
    started = time.time()

    def note(kind: str, url: str, extra: dict | None = None) -> None:
        if not any(key in url for key in WATCH):
            return
        events.append({"t": round(time.time() - started, 2), "kind": kind,
                       "url": url[:150], **(extra or {})})

    settings = Settings(headless=False)
    with BrowserSession(settings) as session:
        profile_dir = Path(args.profile)
        ctx = session.new_context(profile_dir=profile_dir, proxy=exit_)
        page = session.new_page(ctx)

        def on_response(response):
            headers = {}
            try:
                for key, value in response.headers.items():
                    low = key.lower()
                    if low.startswith("rblx-") or low in ("set-cookie", "content-type"):
                        headers[key] = value[:160]
            except Exception:
                pass
            note("res", response.url, {"status": response.status, "headers": headers})

        page.on("request", lambda r: note("req", r.url, {"method": r.method}))
        page.on("requestfailed", lambda r: note("fail", r.url,
                                                {"why": str(r.failure)[:60]}))
        page.on("response", on_response)

        if not profile_dir.exists() or not any(profile_dir.iterdir()):
            # a brand-new profile with no history is itself a tell; warm it a
            # little on the same origin before asking for a signup
            log("warm profile: browsing roblox for a few seconds")
            page.goto("https://www.roblox.com/", wait_until="domcontentloaded",
                      timeout=60_000)
            page.wait_for_timeout(6000)
            page.mouse.move(420, 300, steps=18)
            page.wait_for_timeout(1200)

        page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)

        log("")
        log("  Sign up by hand in the window that just opened.")
        log("  Do it exactly as you did for the two accounts that worked.")
        log("  Leave this terminal running; it stops on its own when a cookie lands.")
        log("")

        cookie = None
        last_beat = 0.0
        while time.time() - started < args.timeout:
            page.wait_for_timeout(1000)
            try:
                cookie = next((c for c in ctx.cookies() if c["name"] == COOKIE_NAME), None)
            except Exception:
                cookie = None
            if cookie:
                break
            if "/home" in page.url or "/discover" in page.url:
                cookie = {"name": COOKIE_NAME, "value": "<redirect>"}
                break
            if time.time() - last_beat > 30:
                last_beat = time.time()
                log(f"  …{int(time.time() - started)}s  url={page.url[:60]}")
                try:
                    path = str(OUT / f"{stamp}_{int(time.time() - started)}s.png")
                    page.screenshot(path=path)
                    shots.append(path)
                except Exception:
                    pass

        outcome = "created" if cookie else "timeout"
        log(f"\n  result: {outcome}")

        cookies = []
        try:
            cookies = [{"name": c["name"], "domain": c["domain"],
                        "value_len": len(c["value"])} for c in ctx.cookies()]
        except Exception:
            pass

        report = {
            "exit": "direct" if exit_ is None else parse(exit_).masked(),
            "profile": str(profile_dir),
            "outcome": outcome,
            "elapsed": round(time.time() - started, 1),
            "final_url": page.url,
            "cookies": cookies,
            "events": events,
            "screenshots": shots,
        }
        path = OUT / f"{stamp}.json"
        path.write_text(json.dumps(report, indent=1))

        hosts: dict[str, int] = {}
        for event in events:
            if event["kind"] != "res":
                continue
            host = event["url"].split("/")[2] if "//" in event["url"] else "?"
            hosts[host] = hosts.get(host, 0) + 1
        log("  challenge-relevant hosts:")
        for host, count in sorted(hosts.items(), key=lambda kv: -kv[1])[:10]:
            log(f"    {count:>3}  {host}")
        log(f"  -> {path}")
        log("")
        log("  Send me that file. It is the recipe: which calls were made, in what")
        log("  order, with which challenge headers, and what the browser had on it.")


if __name__ == "__main__":
    main()
