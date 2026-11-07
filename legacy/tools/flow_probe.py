"""Walk the live signup flow to the challenge gate, using the production stack.

`tools/form_probe.py` and `tools/submit_probe.py` mapped the two layouts with a
bare Chromium context. This drives the same flow through `BrowserSession`, which
carries the fingerprint profile, locale, pointer warm-up and the SOCKS bridge
the real creator uses — so what it reports is what the creator will see.

It stops at the gate and reports which one appeared, because that is the only
open question left:

    arkose   a puzzle frame mounted — the vision problem
    hold     a press-and-hold — a gesture problem, no perception needed
    passive  no gate at all; the account exists
    none     the form never advanced

    PYTHONPATH=. .venv/bin/python tools/flow_probe.py --tries 6
    PYTHONPATH=. .venv/bin/python tools/flow_probe.py --variant radix --hold
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
from roblox_creator.config import SIGNUP_URL, Settings, load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse

OUT = Path("debug/flow_probe")


def log(message: str) -> None:
    print(message, flush=True)


def watch(page, seconds: float, ctx) -> dict:
    """Sample the gate for `seconds`; return the first thing that resolves."""
    observed = {"gate": "none", "t": 0.0}
    started = time.time()
    while time.time() - started < seconds:
        if any(c["name"] == ".ROBLOSECURITY" for c in ctx.cookies()):
            observed.update(gate="passive", t=round(time.time() - started, 1))
            return observed
        url = page.url
        if "/home" in url or "/discover" in url:
            observed.update(gate="passive", t=round(time.time() - started, 1), url=url[:70])
            return observed
        if cap.find_press_hold(page):
            observed.update(gate="hold", t=round(time.time() - started, 1))
            return observed
        arkose = [f.url for f in page.frames
                  if "arkoselabs" in (f.url or "") and len((f.url or "")) > 60]
        if arkose:
            observed.update(gate="arkose", t=round(time.time() - started, 1),
                            frame=arkose[0][:80])
            return observed
        if page.query_selector("#px-captcha"):
            observed["px_modal"] = True
        page.wait_for_timeout(600)
    observed["frames"] = [f.url[:70] for f in page.frames][:5]
    return observed


def run_once(session: BrowserSession, raw: str, want: str, do_hold: bool) -> dict:
    settings = session.settings
    ctx = session.new_context(proxy=raw)
    page = session.new_page(ctx)
    report: dict = {"exit": "direct" if raw is None else parse(raw).masked(), "want": want}
    rng = random.Random()
    identity = make_identity(rng)

    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#signup-username", state="visible", timeout=30_000)
    page.wait_for_timeout(rng.randint(800, 1600))

    variant = form.detect_variant(page)
    report["variant"] = variant
    if want != "any" and want != variant:
        report["skipped"] = True
        return report

    log(f"  variant={variant} user={identity.username}")
    ok = form.set_birthday(page, identity.birthday_month, identity.birthday_day,
                           identity.birthday_year, rng, variant)
    report["birthday"] = ok
    if not ok:
        return report

    page.fill("#signup-username", identity.username)
    page.wait_for_timeout(rng.randint(300, 700))
    form.choose_gender(page, identity.gender, rng)
    page.wait_for_timeout(rng.randint(400, 900))

    step = form.fill_credentials(page, identity.username, identity.password, rng)
    report["step"] = step
    log(f"  step={step} password={form.password_selector(page)}")

    if form.password_selector(page) is None:
        report["goto"] = form.state(page)
        return report

    form.click_submit(page, rng)
    outcome = watch(page, 30.0, ctx)
    report.update(outcome)
    log(f"  gate={outcome['gate']} after {outcome.get('t')}s")

    if do_hold and outcome["gate"] == "hold":
        hit = cap.find_press_hold(page)
        took = cap.press_and_hold(page, rng, hit=hit) if hit else None
        report["held"] = took
        log(f"  held {took}s")
        after = watch(page, 25.0, ctx)
        report["after_hold"] = after
        log(f"  after hold: {after['gate']}")

    report["credentials"] = f"{identity.username}:{identity.password}"
    report["cookie"] = any(c["name"] == ".ROBLOSECURITY" for c in ctx.cookies())
    OUT.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(OUT / f"{time.strftime('%H%M%S')}_{variant}.png"))
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies.txt")
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--variant", default="any", choices=("any", "classic", "radix"))
    ap.add_argument("--hold", action="store_true", help="drive the press-and-hold if it appears")
    ap.add_argument("--no-proxy", action="store_true", help="direct egress; for form validation only")
    args = ap.parse_args()

    pool = [None] if args.no_proxy else load_proxies(Path(args.proxy_file))
    if not args.no_proxy:
        random.shuffle(pool)
    Settings.headless = False
    settings = Settings(headless=False)
    tally: dict[str, int] = {}

    with BrowserSession(settings) as session:
        for raw in pool[: args.tries]:
            try:
                report = run_once(session, raw, args.variant, args.hold)
            except Exception as exc:
                log(f"  exit failed: {type(exc).__name__}: {str(exc)[:90]}")
                continue
            if report.get("skipped"):
                log(f"  served {report.get('variant')}, wanted {args.variant} — skipped")
                continue
            gate = report.get("after_hold", report).get("gate", "?")
            tally[gate] = tally.get(gate, 0) + 1
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}_{report.get('variant')}.json"
            path.write_text(json.dumps(report, indent=1))
            log(f"  -> {path}")
            log(f"  summary: {json.dumps({k: report.get(k) for k in ('variant','step','gate','held','cookie')})}")

    log(f"tally: {tally}")


if __name__ == "__main__":
    main()
