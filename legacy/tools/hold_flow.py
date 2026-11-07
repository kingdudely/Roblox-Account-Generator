"""Create accounts through the press-and-hold gate using the retry/re-draw rule.

The gate resolves in three ways, and the third is the one that matters:

    completed   the panel flips to "Human Challenge completed, please wait"
    rejected    "Please try again" — the blue button re-arms, hold it again
    dead        still reads "Press and hold", no progress, no retry. Not winnable
                from inside. Close the modal, submit the form again, and take
                the fresh challenge — it is often an Arkose puzzle instead.

This runs the whole path: fill, submit, hold, and on the dead state dismiss and
re-draw. It reports what each submission drew and whether an account landed.

    PYTHONPATH=. .venv/bin/python tools/hold_flow.py                    # 3 accounts, direct
    PYTHONPATH=. .venv/bin/python tools/hold_flow.py --proxy-file proxies.txt --tries 5
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from roblox_creator import captcha as cap
from roblox_creator import device_profile as dp
from roblox_creator import signup_form as form
from roblox_creator.browser import BrowserSession
from roblox_creator.config import COOKIE_NAME, SIGNUP_URL, Settings, load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse

OUT = Path("debug/hold_flow")


def log(message: str) -> None:
    print(message, flush=True)


def await_gate(page, timeout: float = 25.0) -> str:
    """What the submission drew: hold | arkose | passive | none."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cap.find_press_hold(page):
            return "hold"
        for frame in page.frames:
            url = frame.url or ""
            if "arkoselabs" in url and len(url) > 60:
                return "arkose"
        if "/home" in page.url or "/discover" in page.url:
            return "passive"
        page.wait_for_timeout(500)
    return "none"


def settle(page, ctx, timeout: float = 25.0) -> dict:
    """Did the account land, or did something else take over?"""
    started = time.time()
    trail: list[dict] = []
    while time.time() - started < timeout:
        page.wait_for_timeout(900)
        cookie = any(c["name"] == COOKIE_NAME for c in ctx.cookies())
        arkose = [f.url[:60] for f in page.frames if "arkoselabs" in (f.url or "")]
        trail.append({"t": round(time.time() - started, 1), "cookie": cookie,
                      "url": page.url[:56], "arkose": len(arkose)})
        if cookie:
            return {"outcome": "created", "trail": trail}
        if arkose:
            return {"outcome": "arkose", "trail": trail}
        if "/home" in page.url or "/discover" in page.url:
            return {"outcome": "redirect", "trail": trail}
    return {"outcome": "stalled", "trail": trail}


def create_one(session: BrowserSession, raw: str | None, submits: int,
               device: bool = False) -> dict:
    profile = None
    init = None
    tz = None
    if device:
        dev = dp.sample()
        profile = dp.context(dev)
        init = dp.init_script(dev)
        tz = dp.timezone_for(raw)
    ctx = session.new_context(proxy=raw, profile=profile, timezone=tz)
    if init:
        ctx.add_init_script(init)
    page = session.new_page(ctx)
    rng = random.Random()
    identity = make_identity(rng)
    report: dict = {
        "exit": "direct" if raw is None else parse(raw).masked(),
        "device": dp.describe(dev) if device else "current",
        "identity": f"{identity.username}:{identity.password}",
        "rounds": [],
    }

    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#signup-username", state="visible", timeout=30_000)
    page.wait_for_timeout(rng.randint(800, 1600))

    variant = form.detect_variant(page)
    if variant != form.CLASSIC and form.ensure_layout(page, form.CLASSIC, tries=4):
        variant = form.CLASSIC
    report["variant"] = variant
    if not form.set_birthday(page, identity.birthday_month, identity.birthday_day,
                             identity.birthday_year, rng, variant):
        report["outcome"] = "birthday-failed"
        return report

    page.fill("#signup-username", identity.username)
    page.wait_for_timeout(rng.randint(300, 700))
    form.choose_gender(page, identity.gender, rng)
    page.wait_for_timeout(rng.randint(300, 800))
    if form.fill_credentials(page, identity.username, identity.password, rng) != "password-filled":
        report["outcome"] = "password-step-failed"
        return report

    form.click_submit(page, rng)

    for round_index in range(submits):
        gate = await_gate(page)
        log(f"    round {round_index + 1}: drew {gate}")
        entry: dict = {"gate": gate}
        report["rounds"].append(entry)

        if gate == "arkose":
            entry["result"] = "arkose-puzzle"
            report["outcome"] = "arkose-escalated"
            break
        if gate == "passive":
            report.update(settle(page, ctx))
            break
        if gate == "none":
            entry["result"] = "no gate"
            report.update(settle(page, ctx, timeout=10.0))
            break

        result = cap.solve_hold_gate(
            page, rng,
            submit=lambda: form.click_submit(page, rng),
            ctx=ctx,
            max_holds=8,
            hold_window=9.0,
            verbose=True,
        )
        entry["result"] = result
        log(f"      -> {result}")

        if result == "cleared":
            # the panel closing does not resubmit the form
            page.wait_for_timeout(rng.randint(1200, 2000))
            entry["nudged"] = form.click_submit(page, rng)
            report.update(settle(page, ctx))
            break
        if result == "resubmitted":
            continue
        report["outcome"] = result
        break

    report.setdefault("outcome", "unknown")
    try:
        report["cookie"] = any(c["name"] == COOKIE_NAME for c in ctx.cookies())
    except Exception:
        report["cookie"] = None
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(OUT / f"{time.strftime('%H%M%S')}_{variant}.png"))
    except Exception:
        pass
    return report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies.txt")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--device", action="store_true",
                    help="apply a coherent sampled Windows device profile")
    ap.add_argument("--submits", type=int, default=4,
                    help="submissions per account: each dead hold re-draws one")
    args = ap.parse_args()

    pool = [None] if args.no_proxy else load_proxies(Path(args.proxy_file))
    if not args.no_proxy:
        random.shuffle(pool)

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tally: dict[str, int] = {}

    with BrowserSession(Settings(headless=False)) as session:
        for raw in pool[: args.tries]:
            try:
                report = create_one(session, raw, args.submits, device=args.device)
            except Exception as exc:
                log(f"  failed: {type(exc).__name__}: {str(exc)[:90]}")
                continue
            outcome = report.get("outcome", "?")
            tally[outcome] = tally.get(outcome, 0) + 1
            path = OUT / f"{stamp}_{report.get('identity', 'x').split(':')[0]}.json"
            path.write_text(json.dumps(report, indent=1))
            log(f"  {report.get('identity', '?').split(':')[0]}: "
                f"device={report.get('device', 'current')} "
                f"variant={report.get('variant')} outcome={outcome} "
                f"cookie={report.get('cookie')} rounds={[r.get('result') for r in report['rounds']]}")
            log(f"  -> {path}")

    log(f"tally: {tally}")


if __name__ == "__main__":
    main()
