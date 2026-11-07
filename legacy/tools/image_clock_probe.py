#!/usr/bin/env python3
"""What arms the 30-45s death of a wave image URL?

`tools/image_ttl_probe.py` found a URL that served at +0s and +16s and answered
`403 DENIED ACCESS` at +47s, while its own `expires` parameter claimed 3599s. So
something else expires. Two candidates remain, and they imply different fixes:

  mint-armed     the challenge's image grant lapses ~30-45s after `gfct`
                 -> the solver must answer inside that window, or the whole
                    challenge must be re-minted per wave
  fetch-armed    the first successful fetch starts a short counter
                 -> fetch once, submit, and never re-fetch the same URL

Each schedule below isolates one. `--schedule` picks it; run both.

    ./tools/image_clock_probe.py --schedule late       # never fetch until the end
    ./tools/image_clock_probe.py --schedule early      # fetch at 0s, then wait
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


def mint(proxies: list[str], tries: int, payload_pool) -> dict | None:
    order = list(proxies)
    random.shuffle(order)
    for proxy in order[:tries]:
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
        if not urls:
            continue
        session_token = game.get("session_token") or ""
        referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
                   f"standard/index.html?session={session_token}")
        return {"proxy": proxy, "url": urls[0], "referer": referer, "ua": ua,
                "session": session, "t0": time.time(), "session_token": session_token,
                "game_token": game.get("challengeID") or "", "sid": game.get("sid") or "us-east-1"}
    return None


def grab(ctx: dict) -> str:
    t = time.time() - ctx["t0"]
    try:
        blob = pca.fetch_image(ctx["session"], ctx["url"], ctx["referer"], ctx["ua"])
    except Exception as exc:
        return f"  +{t:6.1f}s  EXC {type(exc).__name__}"
    return (f"  +{t:6.1f}s  IMAGE {len(blob) // 1024} KB" if blob
            else f"  +{t:6.1f}s  fail")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--tries", type=int, default=8)
    ap.add_argument("--schedule", choices=("late", "early", "submit-late"),
                    default="late")
    ap.add_argument("--wait", type=float, default=60.0)
    args = ap.parse_args()

    from roblox_creator import ca

    proxies = load_proxies(args.proxy_file)
    ctx = mint(proxies, args.tries, payloads.load_payloads())
    if ctx is None:
        print("  could not mint")
        return 1

    if args.schedule == "late":
        # No fetch at all until `--wait`. If this still works, the grant does not
        # lapse on its own, and the first fetch is what arms it.
        print(f"  minted; waiting {args.wait:.0f}s before the first fetch")
        time.sleep(args.wait)
        print(grab(ctx))
        print(grab(ctx))
        return 0

    if args.schedule == "early":
        # Fetch immediately, then wait, then fetch again. This is the live-loop
        # shape: wave 0 right away, next wave after solver latency.
        print(grab(ctx))
        print(f"  waiting {args.wait:.0f}s")
        time.sleep(args.wait)
        print(grab(ctx))
        return 0

    # submit-late: prove the URL is only needed *once* per wave, and that a late
    # submission still works. If yes, the fix is "fetch the image first, solve
    # second, never re-fetch" — no TTL problem at all.
    print(f"  waiting {args.wait:.0f}s before fetching, then submitting")
    time.sleep(args.wait)
    got = grab(ctx)
    print(got)
    if "IMAGE" not in got:
        return 0
    form = ca.build_form(ctx["session_token"], ctx["game_token"], ctx["sid"],
                         [{"index": 0}])
    resp = pca.submit(ctx["session"], form, ctx["ua"], ctx["referer"])
    print(f"  submit after {time.time() - ctx['t0']:.0f}s: status={resp.get('status')} "
          f"response={resp.get('response')!r} incorrect={resp.get('incorrect_guess')}")
    urls = resp.get("_challenge_imgs") or []
    print(f"  next wave images: {len(urls)}")
    if urls:
        ctx["url"] = urls[0]
        ctx["t0"] = time.time()
        print("  fetching wave 1 immediately:")
        print(grab(ctx))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
