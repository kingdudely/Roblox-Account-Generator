"""What does the hold actually do on the wire?

If the gesture is scored in the page and exchanged for a token, the token is the
thing Roblox's server trusts — and there may be a way to ask for it directly.
This records every request, response header and cookie mutation across a full
gate cycle (press, retry, press, dead) and tags each event with the panel state,
so the answer is read off the traffic instead of assumed.

    PYTHONPATH=. .venv/bin/python tools/hold_wire.py --no-proxy
    PYTHONPATH=. .venv/bin/python tools/hold_wire.py --proxy-file proxies.txt
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from roblox_creator import captcha as cap
from roblox_creator import signup_form as form
from roblox_creator.browser import BrowserSession
from roblox_creator.config import COOKIE_NAME, SIGNUP_URL, Settings, load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse

OUT = Path("debug/hold_wire")
INTERESTING = ("px-cloud", "px-cdn", "captcha", "arkose", "funcaptcha",
               "/v2/signup", "auth.roblox", "human")


def log(message: str) -> None:
    print(message, flush=True)


def wire_up(page, ctx, events: list[dict]) -> None:
    t0 = time.time()

    def note(kind, url, extra=None):
        if not any(key in url for key in INTERESTING):
            return
        events.append({"t": round(time.time() - t0, 2), "kind": kind,
                       "url": url[:150],
                       "panel": cap.gate_state(page) if not page.is_closed() else "?",
                       **(extra or {})})

    def body_of(request):
        # gzip posts are binary; decoding them raises inside the listener
        try:
            return (request.post_data or "")[:180]
        except Exception:
            return "<binary>"

    page.on("request", lambda r: note("req", r.url,
                                      {"method": r.method, "body": body_of(r)}))
    page.on("response", lambda r: note("res", r.url, {"status": r.status}))
    page.on("requestfailed", lambda r: note("fail", r.url,
                                            {"why": str(r.failure)[:60]}))


def hold_once(page, rng, seconds: float = 9.0) -> dict:
    hit = cap.find_press_hold(page)
    if not hit:
        return {"ok": False, "why": "no target"}
    frame = hit["frame"]
    x, y = hit["x"], hit["y"]
    page.mouse.move(x - 4, y - 3, steps=5)
    page.wait_for_timeout(220)
    page.mouse.move(x, y, steps=3)
    page.wait_for_timeout(320)
    page.mouse.down()
    states = []
    started = time.time()
    while time.time() - started < seconds:
        page.mouse.move(x + rng.uniform(-2.4, 2.4), y + rng.uniform(-2.0, 2.0),
                        steps=3)
        page.wait_for_timeout(rng.randint(40, 70))
        state = cap.gate_state(page)
        states.append((round(time.time() - started, 1), state))
        if state in ("completed", "gone"):
            break
    page.mouse.up()
    return {"ok": True, "states": states[-6:],
            "held": round(time.time() - started, 1),
            "final": states[-1][1] if states else "?"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies.txt")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--tries", type=int, default=3)
    args = ap.parse_args()

    pool = [None] if args.no_proxy else load_proxies(Path(args.proxy_file))
    if not args.no_proxy:
        random.shuffle(pool)

    OUT.mkdir(parents=True, exist_ok=True)
    with BrowserSession(Settings(headless=False)) as session:
        for raw in pool[: args.tries]:
            ctx = session.new_context(proxy=raw)
            page = session.new_page(ctx)
            events: list[dict] = []
            wire_up(page, ctx, events)
            rng = random.Random()
            identity = make_identity(rng)
            try:
                page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_selector("#signup-username", state="visible", timeout=30_000)
                page.wait_for_timeout(rng.randint(900, 1500))
                variant = form.detect_variant(page)
                form.set_birthday(page, identity.birthday_month, identity.birthday_day,
                                  identity.birthday_year, rng, variant)
                page.fill("#signup-username", identity.username)
                page.wait_for_timeout(400)
                form.choose_gender(page, identity.gender, rng)
                page.wait_for_timeout(400)
                if form.fill_credentials(page, identity.username, identity.password, rng) \
                        != "password-filled":
                    log("  no password step")
                    continue

                before_cookies = {c["name"]: c["value"][:16] for c in ctx.cookies()}
                form.click_submit(page, rng)

                deadline = time.time() + 25
                while time.time() < deadline and cap.gate_state(page) not in ("pressable",):
                    page.wait_for_timeout(500)
                if cap.gate_state(page) != "pressable":
                    log(f"  drew {cap.gate_state(page)} — not a hold")
                    continue

                log("  gate up — pressing twice")
                cycle = []
                for index in range(2):
                    result = hold_once(page, rng)
                    cycle.append(result)
                    log(f"    hold {index + 1}: held {result.get('held')}s -> "
                        f"{result.get('final')}")
                    page.wait_for_timeout(rng.randint(900, 1500))

                after_cookies = {c["name"]: c["value"][:16] for c in ctx.cookies()}
                changed = {k: v for k, v in after_cookies.items()
                           if before_cookies.get(k) != v}
                report = {
                    "exit": "direct" if raw is None else parse(raw).masked(),
                    "variant": variant,
                    "identity": f"{identity.username}:{identity.password}",
                    "cycle": cycle,
                    "panel_final": cap.gate_state(page),
                    "cookies_changed": list(changed),
                    "cookie_names": sorted(after_cookies),
                    "roblosecurity": any(c["name"] == COOKIE_NAME for c in ctx.cookies()),
                    "events": events,
                }
                path = OUT / f"{int(time.time())}_wire.json"
                path.write_text(json.dumps(report, indent=1))
                log(f"  cookies changed: {list(changed)}")
                log(f"  panel final: {report['panel_final']}")
                interesting = [e for e in events
                               if e["kind"] in ("req", "fail") and "px-cloud" not in e["url"]]
                log(f"  ({len(events)} events, {len(interesting)} non-beacon)")
                for e in interesting[-8:]:
                    log(f"    {e['t']:>6}s {e['kind']:<4} {e['url'][:88]}")
                log(f"  -> {path}")
                return
            except Exception as exc:
                log(f"  failed: {type(exc).__name__}: {str(exc)[:80]}")
                continue


if __name__ == "__main__":
    main()
