#!/usr/bin/env python3
"""Does Roblox's Apple button skip the captcha the way Google's does?

The whole SSO fleet rests on one fact: choosing Google at CreateAccount produces
an account with no Arkose challenge. Apple is the only other identity provider
Roblox offers, and if it behaves the same way it is a second supply arm — with a
much better identity ratio, because Apple lets one trusted number back several
accounts and a trusted device mints the 2FA codes.

This probe needs **no Apple ID**. It answers the first question only: what does
Roblox do when the Apple button is pressed? Three outcomes matter:

    arkose before apple   -> the captcha is tied to account creation, not to the
                             provider, and Apple gains nothing
    straight to appleid   -> the captcha lives on the provider side; Apple is a
                             real arm, and the next question is Apple's signup
    blocked/offer only    -> Apple is not available on this surface

    ./tools/apple_sso_probe.py
    ./tools/apple_sso_probe.py --attempts 5 --minutes 1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.browser_creator import LAUNCH_ARGS

SIGNUP = "https://www.roblox.com/CreateAccount"
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36")

INTERESTING = ("roblox.com", "rbxcdn.com", "arkoselabs", "funcaptcha", "apple.com",
               "appleid", "idmsa", "captcha", "challenge", "signup")
SKIP_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".ico", ".woff",
            ".woff2", ".ttf", ".css", ".mp4", ".avif")


def log(msg: str) -> None:
    print(msg, flush=True)


def capture(page, out: Path) -> None:
    """Record the request surface while the operator (or a click) drives."""
    path = out / "requests.jsonl"
    started = time.time()

    def write(row: dict) -> None:
        row["t"] = round(time.time() - started, 2)
        with path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def on_request(req):
        url = req.url
        if any(url.endswith(ext) for ext in SKIP_EXT):
            return
        if not any(k in url for k in INTERESTING):
            return
        if req.resource_type not in ("document", "xhr", "fetch", "script"):
            return
        try:
            body = (req.post_data or "")[:1500]
        except Exception:  # noqa: BLE001 - gzipped bodies are not decodable
            body = "<binary>"
        write({"dir": "req", "method": req.method, "url": url[:300],
               "type": req.resource_type, "body": body})

    def on_response(res):
        if any(res.url.endswith(ext) for ext in SKIP_EXT):
            return
        if not any(k in res.url for k in INTERESTING):
            return
        if res.request.resource_type not in ("document", "xhr", "fetch"):
            return
        body = ""
        try:
            if res.request.resource_type in ("xhr", "fetch"):
                body = res.text()[:800]
        except Exception:  # noqa: BLE001
            body = "<unreadable>"
        write({"dir": "res", "status": res.status, "url": res.url[:300], "body": body})

    page.context.on("request", on_request)
    page.context.on("response", on_response)


def apple_state(page) -> dict:
    return page.evaluate(
        """() => {
          const names = [...document.querySelectorAll('button,a,[role=button]')]
            .map(e => ((e.getAttribute('aria-label')||'') + ' ' + (e.innerText||'')).trim());
          const frameSrc = [...document.querySelectorAll('iframe')]
            .map(f => f.src || '').filter(Boolean);
          return {
            url: location.href,
            apple: names.filter(n => /apple/i.test(n)).slice(0, 4),
            arkose: frameSrc.filter(s => /arkose|funcaptcha/i.test(s)).length,
            frames: frameSrc.map(s => s.slice(0, 90)).slice(0, 6),
          };
        }""")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attempts", type=int, default=4)
    ap.add_argument("--minutes", type=float, default=1.0)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--profile", default=".profiles/apple")
    args = ap.parse_args()

    out = ROOT / "debug" / "apple_sso" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        launch = {"headless": args.headless}
        if args.headless:
            launch["channel"] = "chromium"
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), **launch,
            args=LAUNCH_ARGS, user_agent=MAC_UA,
            viewport={"width": 1366, "height": 900},
            locale="en-US", color_scheme="light")

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        capture(page, out)

        state = None
        for attempt in range(1, args.attempts + 1):
            page.goto(SIGNUP, wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("#signup-username", state="visible", timeout=20000)
            except Exception:  # noqa: BLE001
                log(f"  {attempt}: no signup form, retrying")
                page.wait_for_timeout(2000)
                continue
            page.wait_for_timeout(1800)
            state = apple_state(page)
            tag = "new UI" if page.query_selector("#MonthDropdown") else "classic"
            log(f"  {attempt}: {tag} apple={state['apple']} arkose_frames={state['arkose']}")
            if state["apple"]:
                break
        else:
            log("no Apple button found — the button is not offered on this surface")
            (out / "state.json").write_text(json.dumps(state, indent=2))
            ctx.close()
            return 3

        before_arkose = state["arkose"]
        log(f"\nclicking Apple (arkose before: {before_arkose})")

        target = page.get_by_role("button", name="Apple")
        if not target.count():
            target = page.get_by_text("Apple", exact=False)
        before_pages = len(ctx.pages)
        try:
            target.first.click()
        except Exception as exc:  # noqa: BLE001
            log(f"click failed: {type(exc).__name__}: {str(exc)[:120]}")

        deadline = time.time() + args.minutes * 60
        seen_urls: list[str] = []
        while time.time() < deadline:
            for p in ctx.pages:
                if p.url and p.url not in seen_urls:
                    seen_urls.append(p.url)
            page.wait_for_timeout(1000)

        after = apple_state(page)
        (out / "state.json").write_text(json.dumps(
            {"before": state, "after": after, "urls": seen_urls}, indent=2))
        for p in ctx.pages:
            try:
                p.screenshot(path=str(out / f"page_{abs(hash(p.url)) % 10000}.png"),
                             full_page=True)
            except Exception:  # noqa: BLE001
                pass

        log("\nurl trail:")
        for u in seen_urls:
            log(f"   {u[:150]}")
        log(f"\narkose frames now: {after['arkose']}")
        log(f"pages opened: {len(ctx.pages) - before_pages}")

        rows = [json.loads(l) for l in (out / "requests.jsonl").read_text().splitlines() if l.strip()]
        arkose_hits = [r for r in rows if "arkose" in r.get("url", "") or "funcaptcha" in r.get("url", "")]
        log(f"arkose requests during click: {len(arkose_hits)}")
        for r in arkose_hits[:5]:
            log(f"   {r['url'][:130]}")

        verdict = "apple-hosted" if any("apple" in u for u in seen_urls) else "no apple redirect"
        if arkose_hits:
            verdict = "arkose fired before apple"
        log(f"\nverdict: {verdict}")
        (out / "verdict.txt").write_text(verdict)
        log(f"\nartifacts -> {out}")

        if not args.headless:
            log("window stays open for 30s for a visual check")
            page.wait_for_timeout(30000)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
