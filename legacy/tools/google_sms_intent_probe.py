#!/usr/bin/env python3
"""Read the outbound-SMS step of Google's cross-device gate.

The `crossflowverification` screen hands a QR to a phone; the phone then *sends*
an SMS to Google. That is the difference between an OTP-receive service and a
solution: receiving a code is easy, sending the right message from a number
Google accepts is not.

This drives a signup to the gate, decodes the QR, opens the phone half in a
mobile browser context, and reports exactly what the "Send SMS" control would do:

    sms:<destination>?body=<payload>      -> automatable via an SMS-sending API
    a shortcode / carrier-dependent intent -> not automatable that way

    python3 tools/google_sms_intent_probe.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.google_qr_handover import one_signup
from tools.google_signup_probe import LAUNCH_ARGS, capture_qr, survey

INSPECT = r"""
() => {
  const html = document.documentElement.innerHTML;
  const sms = [...new Set((html.match(/sms:[^"'<> ]+/gi) || []))].slice(0, 8);
  const links = [...document.querySelectorAll('a')]
      .map(a => a.getAttribute('href') || '')
      .filter(h => /^(sms|tel):/i.test(h));
  const buttons = [...document.querySelectorAll('button,[role=button],a')]
      .map(e => ({
        tag: e.tagName,
        text: ((e.innerText || e.getAttribute('aria-label') || '') + '').trim().slice(0, 60),
        href: e.getAttribute('href') || null,
        onclick: (e.getAttribute('onclick') || '').slice(0, 120) || null,
      }))
      .filter(b => b.text || b.href)
      .slice(0, 25);
  const text = (document.body.innerText || '').replace(/\s+/g, ' ').trim();
  const numbers = [...new Set((text.match(/\b(\+?\d[\d\s-]{4,16}\d)\b/g) || []))]
      .slice(0, 10);
  return {url: location.href, text: text.slice(0, 900), sms, links, buttons, numbers};
}
"""


def main() -> int:
    out = ROOT / "debug" / "google_sms_intent" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    profile = ROOT / ".profiles" / "google_sms_intent"
    profile.mkdir(parents=True, exist_ok=True)

    surveys: list = []
    result: dict = {}

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False, args=LAUNCH_ARGS,
            locale="en-US", viewport={"width": 1366, "height": 900})
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        tag = str(int(time.time()))[-4:]
        gate, qr_url = one_signup(page, out, surveys, "Mira", "Falk",
                                  f"mira.falk{tag}")
        print(f"gate: {gate}")
        result["gate"] = gate
        result["qr_url"] = qr_url
        if not qr_url:
            (out / "result.json").write_text(json.dumps(result, indent=2))
            ctx.close()
            return 2

        # open the phone half in a mobile context
        phone = pw.chromium.launch(headless=False, args=LAUNCH_ARGS).new_context(
            **pw.devices["Pixel 7"], locale="en-US")
        mp = phone.new_page()
        captures: list[str] = []
        console: list[str] = []
        rpc_bodies: list[dict] = []

        mp.on("request", lambda r: captures.append(r.url[:300])
              if any(k in r.url.lower() for k in ("sms", "devicephone", "verif"))
              else None)
        mp.on("framenavigated",
              lambda f: captures.append(f"NAV {f.url[:300]}")
              if f == mp.main_frame else None)
        mp.on("console", lambda m: console.append(f"{m.type}: {m.text}"[:400]))

        def on_response(res):
            url = res.url
            if "devicephoneverification" not in url:
                return
            try:
                body = res.text()
            except Exception:
                return
            rpc_bodies.append({"url": url[:220], "status": res.status,
                               "body": body[:3000]})

        mp.on("response", on_response)

        # Catch the outbound intent however the page fires it: window.open,
        # an anchor click, or an assignment to location.href.
        mp.add_init_script("""
            window.__captured = [];
            const rawOpen = window.open;
            window.open = function(u, ...rest) {
              try { window.__captured.push('open:' + String(u)); } catch (e) {}
              return rawOpen ? rawOpen.call(window, u, ...rest) : null;
            };
            const rawClick = HTMLAnchorElement.prototype.click;
            HTMLAnchorElement.prototype.click = function() {
              try {
                if (this.href) window.__captured.push('anchor:' + this.href);
              } catch (e) {}
              return rawClick.call(this);
            };
            new MutationObserver(muts => {
              for (const m of muts) for (const n of m.addedNodes) {
                try { if (n.href) window.__captured.push('node:' + n.href); } catch (e) {}
              }
            }).observe(document.documentElement, {childList: true, subtree: true});
        """)

        print("opening QR target ...")
        try:
            mp.goto(qr_url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:  # noqa: BLE001
            print(f"  nav failed: {type(exc).__name__}")
        mp.wait_for_timeout(5000)

        before = mp.evaluate(INSPECT)
        print("\n--- before click ---")
        print("url   ", before.get("url", "")[:140])
        print("text  ", (before.get("text") or "")[:400])
        print("sms   ", before.get("sms"))
        print("links ", before.get("links"))
        print("nums  ", before.get("numbers"))
        result["before"] = before

        clicked = False
        for label in ("Send SMS", "Send sms", "Send", "Continue"):
            try:
                btn = mp.get_by_role("button", name=label, exact=False)
                if btn.count() and btn.first.is_visible():
                    btn.first.click()
                    clicked = True
                    print(f"\nclicked: {label}")
                    break
            except Exception:
                continue

        mp.wait_for_timeout(4000)
        after = mp.evaluate(INSPECT)
        try:
            hijacked = mp.evaluate("() => window.__captured || []")
        except Exception:
            hijacked = []
        print("\n--- after click ---")
        print("url   ", after.get("url", "")[:140])
        print("text  ", (after.get("text") or "")[:400])
        print("sms   ", after.get("sms"))
        print("links ", after.get("links"))
        print("hijack", hijacked[:8])
        result["after"] = after
        result["hijacked"] = hijacked
        result["clicked"] = clicked
        result["captures"] = captures[:40]
        result["console"] = console[-20:]
        result["rpc"] = rpc_bodies[-10:]

        mp.screenshot(path=str(out / "phone_half.png"), full_page=True)

        print("\n--- network ---")
        for c in captures[:20]:
            print("  ", c[:160])

        print("\n--- console ---")
        for c in console[-10:]:
            print("  ", c[:220])

        print("\n--- devicephone RPC bodies ---")
        for r in rpc_bodies[-4:]:
            print("  ", r["url"][:120], "->", r["status"])
            print("     ", r["body"][:400].replace("\n", " "))

        # verdict: look everywhere an sms: URI could have surfaced
        candidates = []
        candidates += [x for x in hijacked if "sms:" in x.lower()]
        candidates += [c for c in console if "sms:" in c.lower()]
        candidates += [x for x in (after.get("sms") or []) + (after.get("links") or [])
                       if "sms:" in str(x).lower()]
        candidates += [r["body"] for r in rpc_bodies if "sms:" in r["body"].lower()]
        dest = candidates[0] if candidates else None
        if dest:
            idx = dest.lower().find("sms:")
            print(f"\nVERDICT: sms URI found -> {dest[idx:idx+220]}")
            print("  if the destination is a normal number and the body is stable,")
            print("  a programmable sender can reproduce it.")
            result["sms_intent"] = dest[idx:idx+220]
        else:
            print("\nVERDICT: no sms: URI surfaced to the page layer.")
            print("  the intent is built natively (Android Intent) after the server")
            print("  supplies it, so a receive-only OTP number cannot complete it.")

        (out / "result.json").write_text(json.dumps(result, indent=2))
        (out / "survey.json").write_text(json.dumps(surveys, indent=2))
        print(f"\nartifacts -> {out}")

        ctx.close()
        phone.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
