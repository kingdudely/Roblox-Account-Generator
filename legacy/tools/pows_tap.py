#!/usr/bin/env python3
"""Record the exact /pows/* request bodies.

The PoW stage is the last unexplained hop: setup, started, split and check all
work, but `check` answers `400 Invalid Request` to a body built from the bundle's
own `Ye()` shape. Rather than derive it again, run the real page and copy.

    ./tools/pows_tap.py --proxy-file proxies3.txt --wait 65
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

from roblox_creator import captcha as cap
from roblox_creator.browser import BrowserSession
from roblox_creator.config import Settings, load_proxies
from roblox_creator.creator import Creator
from roblox_creator.identity import make_identity
from roblox_creator.store import AccountStore

TAP = r"""
(() => {
  const H = (window.__powtap = window.__powtap || []);
  const clip = (s, n) => { try { return String(s).slice(0, n || 6000); } catch (e) { return ""; } };
  const WANT = /\/pows\/|gfct/i;

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const method = (init && init.method) || "GET";
    const entry = { via: "fetch", method, url: clip(url, 300),
                    body: init && init.body ? clip(init.body) : "", t: Date.now() };
    const p = origFetch.apply(this, arguments);
    if (WANT.test(url)) {
      p.then((r) => { entry.status = r.status;
        r.clone().text().then((t) => { entry.response = clip(t, 1500); H.push(entry); })
                  .catch(() => H.push(entry)); })
       .catch((e) => { entry.error = String(e); H.push(entry); });
    }
    return p;
  };

  const Orig = window.XMLHttpRequest;
  function XHR() {
    const x = new Orig();
    const open = x.open, send = x.send, setH = x.setRequestHeader;
    x.open = function (m, u) { this.__m = m; this.__u = u; return open.apply(this, arguments); };
    x.send = function (b) {
      const url = String(this.__u || "");
      if (WANT.test(url)) {
        const entry = { via: "xhr", method: this.__m, url: clip(url, 300),
                        body: clip(b || ""), t: Date.now() };
        this.addEventListener("load", () => {
          entry.status = this.status;
          entry.response = clip(this.responseText, 1500);
          H.push(entry);
        });
      }
      return send.apply(this, arguments);
    };
    return x;
  }
  XHR.prototype = Orig.prototype;
  window.XMLHttpRequest = XHR;
})();
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=65.0)
    args = ap.parse_args()

    proxy = args.proxy or random.choice(load_proxies(args.proxy_file))
    out = ROOT / "debug" / "pows" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    settings = Settings(headless=args.headless)
    creator = Creator(settings, AccountStore(Path("/tmp/opencode/pows_tap")))
    rng = random.Random()
    records: list[dict] = []

    with BrowserSession(settings) as sess:
        ctx = sess.new_context(proxy=proxy)
        ctx.add_init_script(TAP)
        page = sess.new_page(ctx)

        # frame-level interception: the PoW runs inside the Arkose iframe, so a
        # main-frame JS tap never sees it
        def on_request(request):
            url = request.url
            if "/pows/" not in url and "/fc/gfct" not in url:
                return
            try:
                body = request.post_data
            except Exception:
                body = None
            records.append({"kind": "request", "method": request.method,
                            "url": url[:200], "body": (body or "")[:6000]})

        def on_response(response):
            url = response.url
            if "/pows/" not in url and "/fc/gfct" not in url:
                return
            entry = {"kind": "response", "status": response.status, "url": url[:200]}
            try:
                entry["body"] = response.text()[:1500]
            except Exception:
                pass
            records.append(entry)

        page.on("request", on_request)
        page.on("response", on_response)

        creator._open(page)
        creator._fill(page, make_identity(rng))
        creator._submit(page)
        gate = cap.wait_for_gate(page, timeout=args.wait)
        print(f"  gate -> {gate}", flush=True)
        page.wait_for_timeout(18000)

    (out / "pows.json").write_text(json.dumps(records, indent=1))
    print(f"  {len(records)} record(s)")
    for entry in records:
        if entry["kind"] == "request":
            print(f"\n  REQ  {entry['method']} {entry['url']}")
            if entry.get("body"):
                print(f"       body: {entry['body'][:600]}")
        else:
            print(f"  RESP {entry['status']} {entry['url']}")
            if entry.get("body"):
                print(f"       body: {entry['body'][:300]}")
    print(f"\n  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
