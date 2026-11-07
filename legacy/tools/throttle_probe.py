#!/usr/bin/env python3
"""Does pacing the Arkose requests buy a challenge more waves?

A challenge costs about seven requests to `arkoselabs.roblox.com` before the first
image is even fetched — `gt2`, five proof-of-work calls, `gfct` — and all of them
land inside a couple of seconds. After that an exit serves roughly one or two wave
images and then answers `403 DENIED ACCESS` for good, including after a 40 s wait.

If that wall is a per-IP request rate, spacing the requests should push the wall
back. This throttles every request on the session to one per `--interval` seconds,
plays a challenge with a fixed answer, and reports the deepest wave reached. Run it
at two intervals and compare; if the depth does not move, pacing is not the lever and
the budget is a hard per-exit allowance.

    ./tools/throttle_probe.py --proxy-file proxies1.txt --interval 0    # baseline
    ./tools/throttle_probe.py --proxy-file proxies1.txt --interval 2.5
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

from roblox_creator import ca, payloads
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_ca as pca
from tools import pure_challenge as pc

REQUESTS: list[tuple[str, int | str]] = []


def throttled_session(proxy_url: str | None, interval: float):
    """A normal session whose every request waits for the interval to elapse."""
    import tls_client

    session = pc.new_session(proxy_url)
    if interval <= 0:
        return session

    last = [0.0]
    orig_get, orig_post = session.get, session.post

    def gate():
        wait = interval - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.time()

    def get(url, *a, **kw):
        gate()
        try:
            r = orig_get(url, *a, **kw)
        except Exception as exc:
            REQUESTS.append((url.split("?")[0], "EXC"))
            raise exc
        REQUESTS.append((url.split("?")[0], r.status_code))
        return r

    def post(url, *a, **kw):
        gate()
        try:
            r = orig_post(url, *a, **kw)
        except Exception as exc:
            REQUESTS.append((url.split("?")[0], "EXC"))
            raise exc
        REQUESTS.append((url.split("?")[0], r.status_code))
        return r

    session.get, session.post = get, post
    return session


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--tries", type=int, default=6)
    ap.add_argument("--interval", type=float, default=0.0)
    ap.add_argument("--thinks", default=None,
                    help="comma list of solver latencies to interleave (e.g. 0,20) — "
                         "seconds spent between fetching a wave image and submitting "
                         "the answer, which is what a VLM costs")
    ap.add_argument("--intervals", default=None,
                    help="comma list (e.g. 0,2.5) to interleave arms on one pool, so "
                         "pool decay cannot masquerade as an effect")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--gap", type=float, default=3.0, help="between waves")
    ap.add_argument("--max-waves", type=int, default=10)
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)
    pool = payloads.load_payloads()
    deepest = -1
    arms = ([float(x) for x in args.intervals.split(",") if x.strip()]
            if args.intervals else [args.interval])
    thinks = ([float(x) for x in args.thinks.split(",") if x.strip()]
              if args.thinks else [0.0])
    results: dict[tuple[float, float], list[int]] = {(a, t): [] for a in arms for t in thinks}

    for n, proxy in enumerate(proxies[:args.tries]):
        proxy_url = parse_proxy(proxy).url()
        interval = arms[n % len(arms)]
        think = thinks[n % len(thinks)]
        REQUESTS.clear()
        session = throttled_session(proxy_url, interval)
        t0 = time.time()

        challenge = pc.request_blob(proxy_url)
        if not challenge:
            continue
        record = payloads.pick_payload(pool)
        if record is None:
            return 1
        c_blob, ua = payloads.reseal(record)
        minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                             c_blob, ua, session=session)
        token = minted.get("token", "")
        if not token or not pc.pows(proxy_url, token, ua, session=session).get("ok"):
            continue
        game = pc.gfct(proxy_url, token, ua, session=session)
        if game.get("status") != 200:
            continue
        data = game.get("game_data", {})
        urls = data.get("customGUI", {}).get("_challenge_imgs") or []
        if not urls:
            continue

        session_token = game.get("session_token") or ""
        referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
                   f"standard/index.html?session={session_token}")
        print(f"  [{interval}s think {think:.0f}s] {parse_proxy(proxy).masked()}  mint "
              f"{time.time() - t0:.1f}s ({len(REQUESTS)} arkose calls)")

        wave = 0
        url = urls[0]
        while wave < args.max_waves:
            if wave:
                time.sleep(args.gap)
            blob = pca.fetch_image(session, url, referer, ua)
            if not blob:
                print(f"    wave {wave}: image 403 — dead at wave {wave}")
                results[(interval, think)].append(wave - 1)
                break
            if think:
                time.sleep(think)
            form = ca.build_form(session_token, game.get("challengeID") or "",
                                 game.get("sid") or "us-east-1",
                                 [{"index": args.index}])
            resp = pca.submit(session, form, ua, referer)
            deepest = max(deepest, wave)
            nxt = resp.get("_challenge_imgs") or []
            print(f"    wave {wave}: image ok, submit {resp.get('status')} "
                  f"next={len(nxt)} elapsed {time.time() - t0:.0f}s")
            if resp.get("solved"):
                print("    -> SOLVED")
                results[(interval, think)].append(wave)
                return 0
            if not nxt:
                results[(interval, think)].append(wave)
                break
            url = nxt[0]
            wave += 1

    print(f"  deepest wave reached: {deepest}")
    for (iv, th), depths in results.items():
        if depths:
            print(f"  interval {iv}s think {th:.0f}s: {len(depths)} challenges, "
                  f"depth {depths}, mean {sum(depths) / len(depths):.1f}")
        else:
            print(f"  interval {iv}s think {th:.0f}s: no challenge completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
