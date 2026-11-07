#!/usr/bin/env python3
"""Does a signed `/rtig/image` URL die on a clock, or with the exit?

The live loop's failure shape is specific: wave 0 fetches immediately and works,
then the solver spends 30-70 s, then the next wave's URL answers `403 DENIED
ACCESS`. `tools/image_exit_probe.py` showed the URL is reusable and IP-bound but
not single-use, so the remaining suspects are:

  (a) a short wall-clock TTL on the signature, or
  (b) the exit going bad after N requests to Arkose.

This distinguishes them. One challenge, then fetch the *same* URL on a schedule and
report status per attempt with elapsed seconds. If it flips at roughly a fixed
elapsed time, it is a TTL. If it flips after a number of requests regardless of
spacing, it is a request budget. If it never flips, the loop's wave-1 failures were
unhealthy exits and nothing else.

    ./tools/image_ttl_probe.py --proxy-file proxies1.txt --gaps 0,10,20,30,45,60,90
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import payloads
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_ca as pca
from tools import pure_challenge as pc


def expiry_of(url: str) -> int | None:
    from urllib.parse import parse_qs, urlparse

    q = parse_qs(urlparse(url).query)
    raw = q.get("expires", [None])[0]
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def mint(proxies: list[str], tries: int, payload_pool) -> dict | None:
    order = list(proxies)
    random.shuffle(order)
    for n, proxy in enumerate(order[:tries], 1):
        proxy_url = parse_proxy(proxy).url()
        challenge = pc.request_blob(proxy_url)
        if not challenge:
            continue
        record = payloads.pick_payload(payload_pool)
        if record is None:
            return None
        session = pc.new_session(proxy_url)
        c_blob, ua = payloads.reseal(record)
        minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                             c_blob, ua, session=session)
        token = minted.get("token", "")
        if not token:
            continue
        if not pc.pows(proxy_url, token, ua, session=session).get("ok"):
            continue
        game = pc.gfct(proxy_url, token, ua, session=session)
        if game.get("status") != 200:
            continue
        urls = game.get("game_data", {}).get("customGUI", {}).get("_challenge_imgs") or []
        session_token = game.get("session_token") or ""
        if not urls:
            continue
        referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
                   f"standard/index.html?session={session_token}")
        return {"proxy": proxy, "url": urls[0], "referer": referer, "ua": ua,
                "session": session, "minted_at": time.time(),
                "expires": expiry_of(urls[0])}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--tries", type=int, default=8)
    ap.add_argument("--gaps", default="0,10,20,30,45,60,90",
                    help="comma-separated waits before each fetch, in seconds")
    args = ap.parse_args()

    gaps = [float(g) for g in args.gaps.split(",") if g.strip()]
    proxies = load_proxies(args.proxy_file)
    payload_pool = payloads.load_payloads()

    ctx = mint(proxies, args.tries, payload_pool)
    if ctx is None:
        print("  could not mint a challenge")
        return 1
    print(f"  minted on {parse_proxy(ctx['proxy']).masked()}")
    if ctx["expires"]:
        ttl = ctx["expires"] - ctx["minted_at"]
        print(f"  signature expires param: {ctx['expires']} "
              f"({ttl:+.0f}s from mint, server clock)")
    else:
        print("  no expires param")

    first_ok = None
    for gap in gaps:
        time.sleep(gap)
        elapsed = time.time() - ctx["minted_at"]
        ok, note = (False, "")
        try:
            blob = pca.fetch_image(ctx["session"], ctx["url"], ctx["referer"], ctx["ua"])
            if blob:
                ok, note = True, f"{len(blob) // 1024} KB"
        except Exception as exc:
            note = f"{type(exc).__name__}"
        if ok and first_ok is None:
            first_ok = elapsed
        print(f"  +{elapsed:6.1f}s  {'IMAGE' if ok else 'fail '}  {note}")
        if not ok and first_ok is not None:
            print(f"  -> flipped {elapsed - first_ok:.0f}s after first success")
            break
    else:
        print("  -> never flipped inside the window tested")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
