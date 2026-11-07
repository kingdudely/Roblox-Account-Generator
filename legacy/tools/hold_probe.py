"""Measure the press-and-hold gate instead of guessing its timing.

Roblox's HUMAN/PX gate is a gesture, not a puzzle: a button labelled "Hold the
button to confirm you're human" with a ring that fills while the pointer is
down. Releasing early gets "Please try again"; holding past completion stalls
it. The correct release point is a measurement, so this probes the widget's own
state at 150 ms resolution and reports what it says at every step.

    PYTHONPATH=. .venv/bin/python tools/hold_probe.py --no-proxy
    PYTHONPATH=. .venv/bin/python tools/hold_probe.py --proxy-file proxies.txt

Writes debug/hold/<stamp>.json with the per-attempt timeline.
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

OUT = Path("debug/hold")

# Everything the widget exposes, probed through shadow roots because PX renders
# inside a closed-ish tree: aria labels, any progressbar, and the visible text.
WIDGET_JS = """() => {
  const walk = (root, out) => {
    for (const e of root.querySelectorAll('*')) {
      out.push(e);
      if (e.shadowRoot) walk(e.shadowRoot, out);
    }
    return out;
  };
  const els = walk(document, []);
  const labels = els.filter(e => e.getAttribute && e.getAttribute('aria-label'))
    .map(e => e.getAttribute('aria-label')).slice(0, 8);
  const bar = els.find(e => e.getAttribute && e.getAttribute('role') === 'progressbar');
  const text = (document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 120);
  const btn = els.find(e => e.getAttribute && /hold/i.test(e.getAttribute('aria-label') || ''));
  let rect = null;
  if (btn) { const r = btn.getBoundingClientRect();
             rect = {x: Math.round(r.x), y: Math.round(r.y),
                     w: Math.round(r.width), h: Math.round(r.height)}; }
  const ring = els.filter(e => e.tagName === 'circle' && e.style)
    .map(e => ({ off: e.style.strokeDashoffset, len: e.style.strokeDasharray }))
    .filter(r => r.off || r.len).slice(0, 3);
  const valued = els.filter(e => e.getAttribute && e.getAttribute('aria-valuenow'))
    .map(e => ({ role: e.getAttribute('role'),
                 now: e.getAttribute('aria-valuenow'),
                 max: e.getAttribute('aria-valuemax') })).slice(0, 3);
  return {
    ring, valued,
    focus: (document.activeElement && (document.activeElement.tagName + ':'
            + (document.activeElement.getAttribute('aria-label') || ''))).slice(0, 50),
    labels,
    progress: bar ? { now: parseFloat(bar.getAttribute('aria-valuenow') || '0'),
                      max: parseFloat(bar.getAttribute('aria-valuemax') || '0'),
                      text: (bar.getAttribute('aria-valuetext') || '').slice(0, 40) } : null,
    text,
    rect,
    gone: !btn,
    retry: /try again|please try|failed/i.test(text),
    done: /challenge completed|completed, please wait/i.test(text),
  };
}"""


def log(message: str) -> None:
    print(message, flush=True)


def reach_gate(session: BrowserSession, raw: str | None) -> tuple[object, object, dict]:
    ctx = session.new_context(proxy=raw)
    page = session.new_page(ctx)
    rng = random.Random()
    identity = make_identity(rng)

    page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector("#signup-username", state="visible", timeout=30_000)
    page.wait_for_timeout(rng.randint(800, 1500))

    variant = form.detect_variant(page)
    form.set_birthday(page, identity.birthday_month, identity.birthday_day,
                      identity.birthday_year, rng, variant)
    page.fill("#signup-username", identity.username)
    page.wait_for_timeout(rng.randint(300, 600))
    form.choose_gender(page, identity.gender, rng)
    page.wait_for_timeout(rng.randint(300, 700))
    form.fill_credentials(page, identity.username, identity.password, rng)
    form.click_submit(page, rng)

    deadline = time.time() + 30
    while time.time() < deadline:
        if cap.find_press_hold(page):
            break
        page.wait_for_timeout(500)
    return page, ctx, {"variant": variant, "username": identity.username,
                       "password": identity.password}


def follow_through(page, ctx, seconds: float = 40.0) -> dict:
    """After the widget reports completion: does signup finish, or escalate?"""
    started = time.time()
    samples: list[dict] = []
    while time.time() - started < seconds:
        page.wait_for_timeout(1000)
        cookie = any(c["name"] == ".ROBLOSECURITY" for c in ctx.cookies())
        url = page.url
        arkose = [f.url[:70] for f in page.frames if "arkoselabs" in (f.url or "")]
        sample = {"t": round(time.time() - started, 1), "cookie": cookie,
                  "url": url[:60], "arkose": arkose[:2],
                  "hold": bool(cap.find_press_hold(page))}
        samples.append(sample)
        if cookie or "/home" in url or "/discover" in url:
            return {"outcome": "created", "samples": samples}
        if arkose:
            return {"outcome": "arkose", "samples": samples}
    return {"outcome": "stalled", "samples": samples}


def attempt(page, duration: float, rng: random.Random,
            jitter: float = 1.2, dwell: float = 0.12) -> dict:
    hit = cap.find_press_hold(page)
    if not hit:
        return {"error": "no hold target"}
    frame = hit["frame"]
    x, y = hit["x"], hit["y"]
    timeline: list[dict] = []

    page.mouse.move(x - rng.uniform(2, 6), y - rng.uniform(1, 5), steps=rng.randint(4, 8))
    page.wait_for_timeout(rng.randint(120, 260))
    page.mouse.move(x, y, steps=rng.randint(2, 4))
    page.wait_for_timeout(int(dwell * 1000 + rng.randint(-40, 60)))
    page.mouse.down()

    started = time.time()
    released_at = None
    while time.time() - started < duration:
        page.mouse.move(x + rng.uniform(-jitter, jitter),
                        y + rng.uniform(-jitter * 0.8, jitter * 0.8),
                        steps=rng.randint(2, 5))
        page.wait_for_timeout(rng.randint(30, 60))
        try:
            state = frame.evaluate(WIDGET_JS)
        except Exception:
            state = {"gone": True, "text": "<frame gone>"}
        elapsed = round(time.time() - started, 2)
        timeline.append({"t": elapsed, **state})
        if state.get("retry"):
            released_at = elapsed
            break
        if state.get("progress") and state["progress"]["max"] > 0 \
                and state["progress"]["now"] >= state["progress"]["max"] - 0.01:
            released_at = elapsed
            break
    page.wait_for_timeout(rng.randint(30, 90))
    page.mouse.up()
    held = round(time.time() - started, 2)

    after: list[dict] = []
    for step in range(12):
        page.wait_for_timeout(600)
        try:
            state = frame.evaluate(WIDGET_JS)
        except Exception:
            state = {"gone": True, "text": "<frame gone>"}
        after.append({"t": round(step * 0.6 + 0.6, 1), **state})
        if state.get("gone") or state.get("retry") or state.get("done"):
            break
    return {"held": held, "released_at": released_at,
            "timeline": timeline, "after": after}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies.txt")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--durations", default="3.0,5.0,7.0,9.0")
    ap.add_argument("--jitter", default="1.2,3.0")
    ap.add_argument("--dwell", default="0.12,0.5")
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--tries", type=int, default=3)
    args = ap.parse_args()

    durations = [float(d) for d in args.durations.split(",")]
    jitters = [float(v) for v in args.jitter.split(",")]
    dwells = [float(v) for v in args.dwell.split(",")]
    pool = [None] if args.no_proxy else load_proxies(Path(args.proxy_file))
    if not args.no_proxy:
        random.shuffle(pool)

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    with BrowserSession(Settings(headless=False)) as session:
        for raw in pool[: args.tries]:
            try:
                page, ctx, meta = reach_gate(session, raw)
            except Exception as exc:
                log(f"  no gate: {type(exc).__name__}: {str(exc)[:80]}")
                continue
            if not cap.find_press_hold(page):
                log("  gate never appeared")
                continue
            log(f"  gate up ({meta['variant']}, {meta['username']})")
            report = {"meta": meta,
                      "exit": "direct" if raw is None else parse(raw).masked(),
                      "attempts": []}
            rng = random.Random()
            for index in range(args.attempts):
                duration = durations[min(index, len(durations) - 1)]
                jitter = jitters[min(index, len(jitters) - 1)]
                dwell = dwells[min(index, len(dwells) - 1)]
                result = attempt(page, duration, rng, jitter=jitter, dwell=dwell)
                result["planned"] = duration
                result["jitter"] = jitter
                result["dwell"] = dwell
                report["attempts"].append(result)
                first = (result.get("timeline") or [{}])[0]
                log(f"    held {result.get('held')}s "
                    f"progress={first.get('progress')} "
                    f"after={[(a.get('text') or '')[:34] for a in result.get('after', [])][:2]}")
                text = " ".join((a.get("text") or "") for a in result.get("after", []))
                if "completed" in text:
                    log("    challenge reported completed — following through")
                    follow = follow_through(page, ctx)
                    report["follow"] = follow
                    log(f"    outcome: {follow['outcome']}")
                    path = OUT / f"{stamp}_{meta['variant']}.json"
                    path.write_text(json.dumps(report, indent=1))
                    return
                if not cap.find_press_hold(page):
                    log("    widget gone after release")
                    break
                page.wait_for_timeout(rng.randint(700, 1400))
            path = OUT / f"{stamp}_{meta['variant']}.json"
            path.write_text(json.dumps(report, indent=1))
            log(f"  -> {path}")
            try:
                page.screenshot(path=str(OUT / f"{stamp}_{meta['variant']}_end.png"))
            except Exception:
                pass
            return


if __name__ == "__main__":
    main()
