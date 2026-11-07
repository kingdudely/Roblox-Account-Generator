#!/usr/bin/env python3
"""Capture the PoW stage completely, including the worker's own messages.

Everything else in the chain is reproduced. The last mystery is `/pows/check`
answering `400 Invalid Request` to a body built from the bundle's own `Ye()`.

Guessing at that has cost more than measuring it would. This run records, from a
real browser:

    * the full /pows/setup response (unclipped — the earlier tap cut at 1500)
    * every /pows/* and /fc/gfct request body and response body
    * every message passed between the page and its Web Worker, which is where
      the `done` payload (hashRate / time / finalTransform / targetHashData)
      actually comes from

With the worker's real `done` payload in hand, the identical setup can be replayed
through `tools/powrun.js` and diffed field by field. That isolates a sandbox
difference from a protocol difference instead of guessing which.

    ./tools/pows_tap2.py --proxy-file proxies3.txt --wait 55
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

# Wrap Worker so every message in both directions is recorded. Added as an init
# script, so it applies to the Arkose iframe as well as the top document.
WORKER_HOOK = r"""
(() => {
  const Orig = window.Worker;
  if (!Orig) return;
  const log = (window.__wk = window.__wk || []);
  const safe = (v) => { try { return JSON.parse(JSON.stringify(v)); } catch (e) { return null; } };

  function Wrapped(url, opts) {
    const worker = new Orig(url, opts);
    const tag = String(url).slice(-48);
    const send = worker.postMessage.bind(worker);
    worker.postMessage = function (msg, ...rest) {
      log.push({ dir: "out", tag, msg: safe(msg), t: Date.now() });
      return send(msg, ...rest);
    };
    worker.addEventListener("message", (event) => {
      log.push({ dir: "in", tag, msg: safe(event.data), t: Date.now() });
    });
    return worker;
  }
  Wrapped.prototype = Orig.prototype;
  window.Worker = Wrapped;
})();
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=55.0)
    args = ap.parse_args()

    pool = load_proxies(args.proxy_file)
    out = ROOT / "debug" / "pows2" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)

    settings = Settings(headless=args.headless)
    creator = Creator(settings, AccountStore(Path("/tmp/opencode/pows_tap2")))
    rng = random.Random()
    records: list[dict] = []
    worker_log: list = []

    # Roblox hands out several gates; only the puzzle one runs the PoW stage we
    # need. `press_hold` and friends are a different flavour, so retry.
    for attempt in range(4):
        proxy = args.proxy or random.choice(pool)
        records.clear()
        worker_log.clear()
        with BrowserSession(settings) as sess:
            ctx = sess.new_context(proxy=proxy)
            ctx.add_init_script(WORKER_HOOK)
            page = sess.new_page(ctx)

            def interesting(url: str) -> bool:
                return "/pows/" in url or "/fc/gfct" in url

            def on_request(request):
                if not interesting(request.url):
                    return
                try:
                    body = request.post_data
                except Exception:
                    body = None
                records.append({"kind": "request", "method": request.method,
                                "url": request.url, "body": body or ""})

            def on_response(response):
                if not interesting(response.url):
                    return
                entry = {"kind": "response", "status": response.status,
                         "url": response.url}
                try:
                    entry["body"] = response.text()
                except Exception as exc:
                    entry["error"] = f"{type(exc).__name__}"
                records.append(entry)

            page.on("request", on_request)
            page.on("response", on_response)

            creator._open(page)
            creator._fill(page, make_identity(rng))
            creator._submit(page)
            gate = cap.wait_for_gate(page, timeout=args.wait)
            print(f"  [{attempt}] gate -> {gate}", flush=True)
            page.wait_for_timeout(20000)

            for frame in page.frames:
                try:
                    raw = frame.evaluate("() => JSON.stringify(window.__wk || [])")
                    worker_log.extend(json.loads(raw or "[]"))
                except Exception:
                    continue

        if any(r["kind"] == "request" and r["url"].endswith("/pows/check")
               for r in records):
            break

    worker_log.sort(key=lambda e: e.get("t", 0))
    (out / "pows.json").write_text(json.dumps(records, indent=1))
    (out / "worker.json").write_text(json.dumps(worker_log, indent=1))

    print(f"\n  {len(records)} http record(s), {len(worker_log)} worker message(s) -> {out}")
    for rec in records:
        if rec["kind"] == "request":
            print(f"\n  REQ  {rec['method']:<4} {rec['url'][:96]}")
            print(f"       {rec['body'][:220]}")
        else:
            body = rec.get("body") or ""
            print(f"  RESP {rec['status']} {rec['url'][:96]}  ({len(body)} chars)")

    for msg in worker_log[:40]:
        payload = json.dumps(msg.get("msg"))[:220]
        print(f"\n  WK {msg['dir']:<3} {payload}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
