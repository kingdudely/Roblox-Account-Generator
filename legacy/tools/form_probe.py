"""Answer the open questions about Roblox's two live signup variants.

Roblox A/B serves two different CreateAccount pages from the same URL:

    classic   birthday is three native <select> elements, password is on the
              same screen, submit is #signup-button
    radix     birthday is three Radix comboboxes (a <div> wrapping a hidden
              native <select>), there is no password field, submit says
              Continue and moves the flow to a second step

`Creator._fill` assumed the classic layout, so it dies on the radix variant with
"no element for #signup-password". This probe drives both layouts and reports
what each step actually does, so the fix is written against measurements.

    .venv/bin/python tools/form_probe.py                    # 4 exits, both variants
    .venv/bin/python tools/form_probe.py --tries 8
    .venv/bin/python tools/form_probe.py --save-shots
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
from roblox_creator.proxyurl import parse
from roblox_creator.socks_bridge import SocksBridge
from tools.browser_creator import LAUNCH_ARGS, UA, make_credentials

OUT = Path("debug/form_probe")

STATE_JS = """() => {
  const q = (s) => document.querySelector(s);
  const vis = (s) => { const e = q(s); if (!e) return null;
    const r = e.getBoundingClientRect();
    return { tag: e.tagName, text: (e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 40),
             w: Math.round(r.width), h: Math.round(r.height) }; };
  const sel = [...document.querySelectorAll('select')].map(s => ({
    id: s.id, vis: s.offsetParent !== null, value: s.value,
    label: s.selectedOptions[0] ? s.selectedOptions[0].text : null, opts: s.options.length }));
  const btns = [...document.querySelectorAll('button')]
    .filter(e => e.offsetParent !== null)
    .map(e => ({ id: e.id || null, role: e.getAttribute('role'),
                 text: (e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 22) }))
    .filter(b => b.text || b.role === 'combobox');
  return {
    url: location.href,
    month: vis('#MonthDropdown'), day: vis('#DayDropdown'), year: vis('#YearDropdown'),
    username: !!q('#signup-username'), password: !!q('#signup-password'),
    selects: sel, buttons: btns,
    lists: [...document.querySelectorAll('[role=listbox],[role=option]')].length,
    alert: (document.querySelector('[role=alert],.text-danger') || {}).innerText || null,
  };
}"""

OPTIONS_JS = """(id) => {
  const root = document.getElementById(id);
  if (!root) return [];
  return [...root.querySelectorAll('[role=option]')].map(e => ({
    text: (e.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 18),
    vis: e.offsetParent !== null,
    testid: e.getAttribute('data-testid') }));
}"""


def log(message: str) -> None:
    print(message, flush=True)


def one_exit(playwright, raw: str, shots: bool) -> dict:
    exit_ = parse(raw)
    bridge = SocksBridge(raw)
    proxy_url = {"server": bridge.start()}
    browser = playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
    report: dict = {"exit": exit_.masked()}
    try:
        ctx = browser.new_context(
            proxy=proxy_url,
            user_agent=UA,
            viewport={"width": 1366, "height": 900},
        )
        page = ctx.new_page()
        page.goto("https://www.roblox.com/CreateAccount",
                  wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_selector(config.SEL_USERNAME, timeout=15_000)
        page.wait_for_timeout(2500)

        start = page.evaluate(STATE_JS)
        report["start"] = start
        radix = start["month"] and start["month"]["tag"] == "DIV"
        report["variant"] = "radix" if radix else "classic"
        log(f"  variant={report['variant']} password_now={start['password']}")

        username, password = make_credentials()

        # --- birthday ---------------------------------------------------
        if radix:
            for widget, want in (("MonthDropdown", "Mar"),
                                 ("DayDropdown", "14"),
                                 ("YearDropdown", "1998")):
                box = page.evaluate(
                    """(id) => { const b = document.querySelector('#'+id+' [role=combobox]')
                        || document.querySelector('#'+id+' button');
                        if (!b) return null; const r = b.getBoundingClientRect();
                        return {x: r.x + r.width/2, y: r.y + r.height/2}; }""", widget)
                if not box:
                    report["birthday"] = f"{widget}: no combobox"
                    break
                page.mouse.click(box["x"], box["y"])
                page.wait_for_timeout(700)
                opts = page.evaluate(OPTIONS_JS, widget)
                report.setdefault("option_dumps", {})[widget] = opts[:6]
                hit = page.locator(f"#{widget} [role=option]", has_text=want)
                n = hit.count()
                if n:
                    hit.first.click()
                    page.wait_for_timeout(450)
                    report.setdefault("birthday_picks", {})[widget] = f"option({n})"
                else:
                    picked = page.evaluate(
                        """([id, want]) => { const s = document.querySelector('#'+id+' select');
                            if (!s) return 'no select';
                            const o = [...s.options].find(o => o.text.trim() === want
                                        || o.value === want);
                            if (!o) return 'no option ' + want;
                            s.value = o.value;
                            s.dispatchEvent(new Event('input', {bubbles: true}));
                            s.dispatchEvent(new Event('change', {bubbles: true}));
                            return 'dispatched ' + o.value; }""", [widget, want])
                    report.setdefault("birthday_picks", {})[widget] = f"dispatch: {picked}"
                    if "dispatched" not in picked:
                        break
        else:
            for widget, label in (("MonthDropdown", "March"),
                                  ("DayDropdown", "07"),
                                  ("YearDropdown", "1998")):
                try:
                    page.select_option(f"select#{widget}, #{widget} select", label=label)
                    report.setdefault("birthday_picks", {})[widget] = f"label={label}"
                except Exception as exc:
                    report.setdefault("birthday_picks", {})[widget] = \
                        f"{type(exc).__name__}: {str(exc)[:60]}"

        page.wait_for_timeout(500)
        report["after_birthday"] = page.evaluate(STATE_JS)

        # --- username + gender ------------------------------------------
        page.fill(config.SEL_USERNAME, username)
        page.wait_for_timeout(500)
        gender = "Female"
        if page.query_selector(config.SEL_FEMALE):
            page.click(config.SEL_FEMALE)
            report["gender"] = "id:Fem"
        else:
            page.get_by_role("button", name=gender).first.click()
            report["gender"] = "text:Fem"
        page.wait_for_timeout(600)
        report["after_identity"] = page.evaluate(STATE_JS)

        if page.query_selector(config.SEL_PASSWORD):
            page.fill(config.SEL_PASSWORD, password)
            report["password_filled"] = "same step"
        else:
            report["password_filled"] = "not on step 1"
        page.wait_for_timeout(400)

        # --- advance ----------------------------------------------------
        events: list[str] = []
        page.on("response", lambda r: events.append(f"{r.status} {r.url[:90]}")
              if "/v2/signup" in r.url or "captcha" in r.url else None)

        clicked = page.evaluate(
            """() => {
              const b = document.querySelector('#signup-button')
                     || [...document.querySelectorAll('button')]
                          .find(e => /^(continue|sign up|create account)$/i
                                     .test((e.innerText || '').trim()));
              if (!b) return null;
              b.click();
              return (b.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 20);
            }""")
        report["clicked"] = clicked
        log(f"  clicked {clicked!r} as {username}")

        for step in range(1, 11):
            page.wait_for_timeout(2000)
            state = page.evaluate(STATE_JS)
            ark = [f.url[:60] for f in page.frames if "arkoselabs" in (f.url or "")]
            px = [f.url[:60] for f in page.frames if "px-cdn" in (f.url or "")
                  or "human" in (f.url or "")]
            report[f"t{step * 2}s"] = {
                "url": state["url"][:64], "password": state["password"],
                "alert": state["alert"], "frames": len(page.frames),
                "arkose": len(ark), "px": len(px),
                "buttons": [b["text"] for b in state["buttons"]][:5],
            }
            if state["url"] != start["url"] or state["password"] or ark or px:
                break

        report["events"] = events[:10]
        report["final"] = page.evaluate(STATE_JS)
        report["frames"] = [f.url[:70] for f in page.frames][:6]
        report["credentials"] = f"{username}:{password}"

        if shots:
            OUT.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(OUT / f"{int(time.time())}_{report['variant']}.png"))
        try:
            report["cookie"] = any(c["name"] == config.COOKIE_NAME
                                   for c in ctx.cookies())
        except Exception:
            report["cookie"] = None
        return report
    finally:
        try:
            browser.close()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies.txt")
    ap.add_argument("--tries", type=int, default=4)
    ap.add_argument("--save-shots", action="store_true")
    args = ap.parse_args()

    pool = load_proxies(Path(args.proxy_file))
    random.shuffle(pool)
    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    seen: dict[str, int] = {}

    with sync_playwright() as playwright:
        for raw in pool:
            if sum(seen.values()) >= args.tries:
                break
            # two exits per variant is enough to trust a layout
            try:
                report = one_exit(playwright, raw, args.save_shots)
            except Exception as exc:
                log(f"  exit failed: {type(exc).__name__}: {str(exc)[:70]}")
                continue
            variant = report.get("variant", "unknown")
            if seen.get(variant, 0) >= 2 and len(seen) > 1:
                continue
            seen[variant] = seen.get(variant, 0) + 1
            path = OUT / f"{stamp}_{variant}_{seen[variant]}.json"
            path.write_text(json.dumps(report, indent=1))
            log(f"  -> {path}")

    log(f"variants seen: {seen}")


if __name__ == "__main__":
    main()
