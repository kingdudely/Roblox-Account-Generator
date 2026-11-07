#!/usr/bin/env python3
"""Find out where `data[blob]` comes from.

`gt2` will not issue a token without it: the same `c` with no `data[blob]`
returns 400, and with a blob returns 401 (blob present but rejected). So the
blob is a required input, and it is not produced by Arkose's crypto — the
crypto hook saw only two AES-GCM outputs, neither of them 363 bytes.

`captcha.py` documents the origin: the page POSTs `/v1/arkose/bot-activity-ticket`
and hands the ticket to `ArkoseEnforcement.setConfig({ data: { blob } })`. That
makes it a plain Roblox API call, which is the good outcome — no sensor VM.

This probe records the exact request (method, url, body, headers) and response
so the call can be reproduced cold.

    ./tools/blob_probe.py --proxy-file proxies3.txt --wait 60
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import captcha as cap          # noqa: E402
from roblox_creator.browser import BrowserSession  # noqa: E402
from roblox_creator.config import Settings, load_proxies  # noqa: E402
from roblox_creator.creator import Creator         # noqa: E402
from roblox_creator.identity import make_identity  # noqa: E402
from roblox_creator.store import AccountStore      # noqa: E402

TAP = r"""
(() => {
  const H = (window.__tap = window.__tap || []);
  const WANT = /bot-activity-ticket|arkose|funcaptcha|captcha\/v1|challenge/i;
  const clip = (s, n) => { try { return String(s).slice(0, n || 4000); } catch (e) { return ""; } };
  const push = (o) => { try { H.push(o); } catch (e) {} };

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const method = (init && init.method) || (input && input.method) || "GET";
    const body = init && init.body ? clip(init.body) : "";
    const entry = { via: "fetch", method, url: clip(url, 400), body, t: Date.now() };
    const p = origFetch.apply(this, arguments);
    if (WANT.test(url)) {
      p.then((r) => {
        entry.status = r.status;
        r.clone().text().then((t) => { entry.response = clip(t); push(entry); }).catch(() => push(entry));
      }).catch((e) => { entry.error = String(e); push(entry); });
    }
    return p;
  };

  const OrigXHR = window.XMLHttpRequest;
  function XHR() {
    const x = new OrigXHR();
    const open = x.open, send = x.send;
    x.open = function (m, u) { this.__m = m; this.__u = u; return open.apply(this, arguments); };
    x.send = function (b) {
      const entry = { via: "xhr", method: this.__m, url: clip(this.__u, 400), body: clip(b || ""), t: Date.now() };
      this.addEventListener("load", () => {
        entry.status = this.status;
        entry.response = clip(this.responseText);
        if (WANT.test(entry.url)) push(entry);
      });
      return send.apply(this, arguments);
    };
    return x;
  }
  XHR.prototype = OrigXHR.prototype;
  window.XMLHttpRequest = XHR;

  // the SDK instance carries the config, including data.blob
  const grabConfig = () => {
    try {
      if (window.ArkoseEnforcement) {
        const inst = window.ArkoseEnforcement;
        push({ via: "config", has: true, keys: Object.keys(inst || {}) });
      }
    } catch (e) {}
  };
  setTimeout(grabConfig, 8000);
  setTimeout(grabConfig, 20000);
})();
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=60.0)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    proxy = args.proxy
    if not proxy and args.proxy_file.exists():
        proxy = random.choice(load_proxies(args.proxy_file))

    out = ROOT / "debug" / "blob" / (time.strftime("%Y%m%d-%H%M%S") + (f"-{args.tag}" if args.tag else ""))
    out.mkdir(parents=True, exist_ok=True)

    settings = Settings(headless=args.headless)
    creator = Creator(settings, AccountStore(Path("/tmp/opencode/blob_probe")))
    rng = random.Random()

    with BrowserSession(settings) as sess:
        ctx = sess.new_context(proxy=proxy)
        ctx.add_init_script(TAP)
        page = sess.new_page(ctx)

        creator._open(page)
        creator._fill(page, make_identity(rng))
        creator._submit(page)
        gate = cap.wait_for_gate(page, timeout=args.wait)
        print(f"  gate -> {gate}", flush=True)
        page.wait_for_timeout(4000)

        try:
            entries = json.loads(page.evaluate("() => JSON.stringify(window.__tap || [])"))
        except Exception as exc:
            entries = []
            print(f"  tap read failed: {exc}")
        blob = cap.extract_blob(page)
        try:
            config_json = page.evaluate(
                "() => { try { return JSON.stringify(window.__lunar && window.__lunar.config); }"
                " catch (e) { return null; } }")
        except Exception:
            config_json = None

    (out / "tap.json").write_text(json.dumps(entries, indent=1))
    if blob:
        (out / "blob.txt").write_text(blob)
    if config_json:
        (out / "config.json").write_text(config_json)

    print(f"  entries: {len(entries)}   blob: {'yes' if blob else 'no'} "
          f"({len(blob) if blob else 0} chars)")
    for entry in entries:
        print(f"\n  [{entry.get('via')}] {entry.get('method')} {entry.get('url', '')[:110]}")
        if entry.get("body"):
            print(f"    body: {entry['body'][:200]}")
        if entry.get("status"):
            print(f"    status: {entry['status']}")
        if entry.get("response"):
            print(f"    resp: {entry['response'][:260]}")
    print(f"\n  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
