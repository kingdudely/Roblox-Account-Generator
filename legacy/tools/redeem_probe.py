#!/usr/bin/env python3
"""Make the page redeem a token we minted, and record the call it makes.

Everything up to the token is now pure Python:

    /v2/signup 403  ->  rblx-challenge-metadata.dataExchangeBlob
    our `c`         ->  /fc/gt2/  ->  token with sup=1

The last hop is Roblox's own "redeem this Arkose token" call, and the path in
`config.py` (captcha.roblox.com/v1/funcaptcha/signup/web) now 404s — it moved.
Rather than guess, let the page do it: inject our token into the SDK's captured
`onCompleted` and watch what the application POSTs next.

This is a discovery run, not the production path. Run it once, read
`redeem.json`, and the endpoint + body become constants.

    ./tools/redeem_probe.py --proxy-file proxies3.txt --wait 60
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

from roblox_creator import bda as bdamod
from roblox_creator import captcha as cap
from roblox_creator.arkose_py import Fingerprint, load_fingerprints
from roblox_creator.browser import BrowserSession
from roblox_creator.config import Settings, load_proxies
from roblox_creator.creator import Creator
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.store import AccountStore

SITE_KEY = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
API = "https://arkoselabs.roblox.com"
BUILD_ID = "270438f7-2325-426f-9636-88978b387c8d"

# records every request the page makes after injection, not just the usual ones
FULL_TAP = r"""
(() => {
  const H = (window.__fulltap = window.__fulltap || []);
  const clip = (s, n) => { try { return String(s).slice(0, n || 3000); } catch (e) { return ""; } };
  const push = (o) => { try { H.push(o); } catch (e) {} };
  const WANT = /captcha|challenge|signup|arkose|funcaptcha|redeem|token/i;

  const origFetch = window.fetch;
  window.fetch = function (input, init) {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const method = (init && init.method) || (input && input.method) || "GET";
    const entry = { via: "fetch", method, url: clip(url, 500),
                    body: init && init.body ? clip(init.body) : "", t: Date.now() };
    const p = origFetch.apply(this, arguments);
    if (WANT.test(url)) {
      p.then((r) => {
        entry.status = r.status;
        r.clone().text().then((t) => { entry.response = clip(t, 1200); push(entry); })
                  .catch(() => push(entry));
      }).catch((e) => { entry.error = String(e); push(entry); });
    }
    return p;
  };

  const Orig = window.XMLHttpRequest;
  function XHR() {
    const x = new Orig();
    const open = x.open, send = x.send;
    x.open = function (m, u) { this.__m = m; this.__u = u; return open.apply(this, arguments); };
    x.send = function (b) {
      const url = String(this.__u || "");
      const entry = { via: "xhr", method: this.__m, url: clip(url, 500), body: clip(b || ""), t: Date.now() };
      if (WANT.test(url)) {
        this.addEventListener("load", () => { entry.status = this.status; entry.response = clip(this.responseText, 1200); push(entry); });
      }
      return send.apply(this, arguments);
    };
    return x;
  }
  XHR.prototype = Orig.prototype;
  window.XMLHttpRequest = XHR;

  // which Request the page posts is often a URL object, not a string
  const origOpen = Orig.prototype.open;
})();
"""


READ_BLOB = r"""
() => {
  const out = {};
  try {
    const cfg = window.__lunar && window.__lunar.config;
    if (cfg) {
      out.configKeys = Object.keys(cfg);
      if (cfg.data) {
        out.dataKeys = Object.keys(cfg.data);
        out.blob = cfg.data.blob || cfg.data.dataExchangeBlob || null;
      }
      if (cfg.dataExchangeBlob) out.blob = cfg.dataExchangeBlob;
    }
  } catch (e) { out.error = String(e); }
  return JSON.stringify(out);
}
"""


def read_blob(page) -> tuple[str | None, dict]:
    """Pull `data.blob` out of the SDK config the hook captured."""
    try:
        info = json.loads(page.evaluate(READ_BLOB))
    except Exception as exc:
        info = {"error": str(exc)}
    blob = info.get("blob")
    if not blob:
        blob = cap.extract_blob(page)
    return blob, info


def mint_token(blob: str, fp: Fingerprint, proxy: str | None) -> dict:
    import tls_client

    c = bdamod.build(fp)
    stamp = str(int(time.time() - time.time() % 21600))
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.roblox.com",
        "referer": "https://www.roblox.com/",
        "user-agent": fp.user_agent,
        "x-ark-esync-value": stamp,
        "ark-build-id": BUILD_ID,
    }
    form = {
        "c": c, "public_key": SITE_KEY, "site": "https://www.roblox.com",
        "userbrowser": fp.user_agent, "capi_version": "4.6.1",
        "capi_mode": "lightbox", "style_theme": "modal",
        "rnd": str(random.random()), "data[blob]": blob,
    }
    session = tls_client.Session(client_identifier="chrome_131")
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    resp = session.post(f"{API}/fc/gt2/public_key/{SITE_KEY}", data=form,
                        headers=headers, cookies={"timestamp": stamp}, timeout_seconds=25)
    out = {"status": resp.status_code, "c_len": len(c)}
    try:
        body = resp.json()
        out["token"] = body.get("token", "")
        out["sup"] = body.get("sup")
    except Exception:
        out["raw"] = resp.text[:200]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=60.0)
    args = ap.parse_args()

    proxy = args.proxy
    if not proxy and args.proxy_file.exists():
        proxy = random.choice(load_proxies(args.proxy_file))
    print(f"  proxy: {parse_proxy(proxy).masked() if proxy else 'direct'}")

    out = ROOT / "debug" / "redeem" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    settings = Settings(headless=args.headless)
    creator = Creator(settings, AccountStore(Path("/tmp/opencode/redeem_probe")))
    rng = random.Random()

    with BrowserSession(settings) as sess:
        ctx = sess.new_context(proxy=proxy)
        ctx.add_init_script(cap.PAGE_HOOK)
        ctx.add_init_script(FULL_TAP)
        page = sess.new_page(ctx)

        creator._open(page)
        creator._fill(page, make_identity(rng))
        creator._submit(page)
        gate = cap.wait_for_gate(page, timeout=args.wait)
        print(f"  gate -> {gate}", flush=True)

        blob, info = read_blob(page)
        print(f"  config keys: {info.get('configKeys')} data keys: {info.get('dataKeys')}")
        print(f"  blob from page: {'yes, ' + str(len(blob)) + ' chars' if blob else 'none'}")
        if not blob:
            print("  cannot mint without a blob")
            return 1

        fp = Fingerprint.from_raw(random.choice(load_fingerprints()))
        minted = mint_token(blob, fp, parse_proxy(proxy).url() if proxy else None)
        print(f"  gt2 -> {minted['status']}  sup={minted.get('sup')}  "
              f"token_len={len(minted.get('token', ''))}")
        if not minted.get("token"):
            print(f"  no token: {str(minted)[:200]}")
            return 1

        # clear the tap so only post-injection traffic is recorded
        page.evaluate("() => { if (window.__fulltap) window.__fulltap.length = 0; }")
        status = cap.inject_token(page, minted["token"])
        print(f"  inject -> {status}", flush=True)
        page.wait_for_timeout(9000)

        try:
            calls = json.loads(page.evaluate("() => JSON.stringify(window.__fulltap || [])"))
        except Exception:
            calls = []

    (out / "redeem.json").write_text(json.dumps({"minted": minted, "calls": calls}, indent=1))
    print(f"\n  {len(calls)} call(s) after injection")
    for call in calls:
        print(f"\n  [{call.get('via')}] {call.get('method')} {call.get('url', '')[:130]}")
        if call.get("body"):
            print(f"    body: {call['body'][:300]}")
        if call.get("status"):
            print(f"    status: {call['status']}")
        if call.get("response"):
            print(f"    resp: {call['response'][:300]}")
    print(f"\n  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
