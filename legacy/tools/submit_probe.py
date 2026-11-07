"""Why does the signup form spin without ever calling /v2/signup?

`form_probe.py` showed both layouts accepting input and the submit button
switching to a spinner, but zero network traffic to auth.roblox.com. That means
the submit handler is blocked client-side before it issues a request. This dumps
the whole request log, console, and the DOM at the moment of the hang.

    PYTHONPATH=. .venv/bin/python tools/submit_probe.py --variant radix
    PYTHONPATH=. .venv/bin/python tools/submit_probe.py --variant classic
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

from roblox_creator import config
from roblox_creator.config import load_proxies
from roblox_creator.socks_bridge import SocksBridge
from tools.browser_creator import LAUNCH_ARGS, UA, make_credentials

OUT = Path("debug/submit_probe")

DUMP_JS = """() => {
  const btn = document.querySelector('#signup-button')
    || [...document.querySelectorAll('button')].find(e => /continue|sign up/i.test(e.innerText||''));
  const forms = [...document.querySelectorAll('form')].map(f => ({
    action: f.action, method: f.method, valid: f.checkValidity ? f.checkValidity() : null }));
  const invalids = [...document.querySelectorAll('input,select')].map(e => ({
    id: e.id || null, name: e.name || null, type: e.type,
    value: (e.type === 'password' ? '<pw>' : e.value),
    valid: e.checkValidity ? e.checkValidity() : null,
    msg: e.validationMessage || null }));
  return {
    button: btn ? { text: (btn.innerText||'').trim(), disabled: btn.disabled,
                    type: btn.type, cls: (btn.className||'').toString().slice(0, 90),
                    ariaBusy: btn.getAttribute('aria-busy'),
                    html: btn.outerHTML.slice(0, 260) } : null,
    forms, invalids,
    allInputs: [...document.querySelectorAll('input')].map(e => ({
      id: e.id || null, type: e.type, vis: e.offsetParent !== null })),
    allButtons: [...document.querySelectorAll('button')].map(e => ({
      id: e.id || null, type: e.type, vis: e.offsetParent !== null,
      text: (e.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 22) })),
    px: !!document.querySelector('#px-captcha'),
    pxScripts: [...document.querySelectorAll('script[src]')].map(s => s.src)
                 .filter(s => /px-|perimeter|human|captcha/i.test(s)),
    text: (document.body.innerText || '').replace(/\\s+/g, ' ').slice(0, 260),
  };
}"""


def run_once(playwright, raw: str, want_variant: str) -> dict:
    bridge = SocksBridge(raw)
    browser = playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
    report: dict = {"variant": None, "requests": [], "console": [], "t0": time.time()}
    try:
        ctx = browser.new_context(proxy={"server": bridge.start()}, user_agent=UA,
                                  viewport={"width": 1366, "height": 900})
        page = ctx.new_page()
        def note_request(r):
            report["requests"].append(
                {"t": round(time.time() - report["t0"], 2), "method": r.method,
                 "url": r.url[:120]})

        def note_failed(r):
            report["requests"].append(
                {"t": round(time.time() - report["t0"], 2), "FAILED": r.failure,
                 "url": r.url[:120]})

        page.on("request", note_request)
        page.on("requestfailed", note_failed)
        page.on("console", lambda m: report["console"].append(
            f"{m.type}: {m.text[:160]}"))
        page.on("pageerror", lambda e: report["console"].append(f"pageerror: {str(e)[:160]}"))

        page.goto("https://www.roblox.com/CreateAccount",
                  wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_selector(config.SEL_USERNAME, timeout=15_000)
        page.wait_for_timeout(3000)

        variant = page.evaluate(
            "() => { const m = document.querySelector('#MonthDropdown');"
            " return m ? m.tagName : ''; }")
        report["variant"] = "radix" if variant == "DIV" else "classic"
        if report["variant"] != want_variant:
            report["skipped"] = True
            return report

        user, pwd = make_credentials()
        report["credentials"] = f"{user}:{pwd}"

        # --- fill, layout-aware -----------------------------------------
        if report["variant"] == "radix":
            for widget, value in (("MonthDropdown", "Mar"), ("DayDropdown", "14"),
                                  ("YearDropdown", "1998")):
                page.evaluate(
                    """([id, v]) => { const s = document.querySelector('#'+id+' select');
                        const o = [...s.options].find(o => o.value === v);
                        s.value = o.value;
                        s.dispatchEvent(new Event('input', {bubbles: true}));
                        s.dispatchEvent(new Event('change', {bubbles: true})); }""",
                    [widget, value])
                page.wait_for_timeout(300)
        else:
            for widget, label in (("MonthDropdown", "March"), ("DayDropdown", "07"),
                                  ("YearDropdown", "1998")):
                page.select_option(f"select#{widget}, #{widget} select", label=label)
                page.wait_for_timeout(260)

        page.click(config.SEL_USERNAME)
        page.keyboard.type(user, delay=60)
        page.wait_for_timeout(400)
        if page.query_selector(config.SEL_PASSWORD):
            page.click(config.SEL_PASSWORD)
            page.keyboard.type(pwd, delay=60)
            page.wait_for_timeout(350)
        page.get_by_role("button", name="Female").first.click()
        page.wait_for_timeout(700)

        report["before"] = page.evaluate(DUMP_JS)

        # --- submit with a real pointer ----------------------------------
        report["t0"] = time.time()
        page.evaluate(
            """() => { const b = document.querySelector('#signup-button')
                 || [...document.querySelectorAll('button')]
                      .find(e => /^(continue|sign up|create account)$/i
                                 .test((e.innerText||'').trim()));
                 b.id = b.id || '__submit'; }""")
        page.click("#__submit" if not page.query_selector("#signup-button")
                  else "#signup-button")

        for step in range(1, 9):
            page.wait_for_timeout(1500)
            report[f"after_{step}"] = page.evaluate(DUMP_JS)
            if any("/v2/signup" in r["url"] for r in report["requests"]):
                break

        report["requests"] = report["requests"][-40:]
        report["frames"] = [f.url[:80] for f in page.frames][:6]
        report["cookies"] = [c["name"] for c in ctx.cookies()]
        OUT.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(OUT / f"{int(time.time())}_{report['variant']}.png"))
        return report
    finally:
        try:
            browser.close()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", default="radix", choices=("radix", "classic"))
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--proxy-file", default="proxies.txt")
    args = ap.parse_args()

    pool = load_proxies(Path(args.proxy_file))
    random.shuffle(pool)
    with sync_playwright() as playwright:
        for raw in pool[: args.tries]:
            try:
                report = run_once(playwright, raw, args.variant)
            except Exception as exc:
                print(f"  exit failed: {type(exc).__name__}: {str(exc)[:70]}", flush=True)
                continue
            if report.get("skipped"):
                print("  exit served the other variant", flush=True)
                continue
            OUT.mkdir(parents=True, exist_ok=True)
            path = OUT / f"{time.strftime('%Y%m%d-%H%M%S')}_{args.variant}.json"
            path.write_text(json.dumps(report, indent=1))
            print(f"\n=== {args.variant} -> {path}", flush=True)
            print("requests:", json.dumps(report["requests"][-14:], indent=1)[:1400])
            print("console:", report["console"][-6:])
            print("button after:", report.get("after_1", {}).get("button"))
            print("invalids:", [i for i in report.get("after_1", {}).get("invalids", [])
                                if i["valid"] is False])
            print("frames:", report["frames"])
            print("cookies:", report["cookies"])
            return
    print("no exit served the requested variant")


if __name__ == "__main__":
    main()
