#!/usr/bin/env python3
"""How many waves can one exit get through if it asks for each image exactly once?

Everything points at a request budget on `/rtig/image`: a burst of requests from one
exit gets the exit 403'd, and `fetch_image`'s four-shape sweep is itself a burst. The
product only needs one fetch per wave, so this plays a whole challenge with a fixed
answer, fetching each wave exactly once and pacing the fetches, then reports the wave
where the images stop.

If it completes, the fix is request frugality — not more retries, fewer. If it dies
at wave 1 anyway, the budget is per-challenge and the architecture has to change
(which would be worth knowing before writing more loop code).

    ./tools/wave_pace_probe.py --proxy-file proxies1.txt --gap 10 --index 0
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


def grab_once(session, url: str, referer: str, ua: str) -> tuple[bool, str]:
    """One request, shape one only. No sweep."""
    headers = {"user-agent": ua, "referer": referer, **pca.BASE_IMAGE_HEADERS}
    try:
        status, body = pca._try_image(session, url, headers)
    except Exception as exc:
        return False, f"{type(exc).__name__}"
    if status == 200:
        return True, f"{len(body) // 1024} KB"
    return False, f"{status} {body[:24]!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--tries", type=int, default=8, help="exits to try while minting")
    ap.add_argument("--gap", type=float, default=10.0, help="seconds between fetches")
    ap.add_argument("--index", type=int, default=0, help="fixed answer index")
    ap.add_argument("--retry-wait", type=float, default=0.0,
                    help="on a 403, sleep this long and retry the same URL once")
    ap.add_argument("--max-waves", type=int, default=12)
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)
    pool = payloads.load_payloads()

    for n, proxy in enumerate(proxies[:args.tries], 1):
        proxy_url = parse_proxy(proxy).url()
        label = parse_proxy(proxy).masked()
        challenge = pc.request_blob(proxy_url)
        if not challenge:
            continue
        record = payloads.pick_payload(pool)
        if record is None:
            return 1
        session = pc.new_session(proxy_url)
        c_blob, ua = payloads.reseal(record)
        minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                             c_blob, ua, session=session)
        token = minted.get("token", "")
        if not token or not pc.pows(proxy_url, token, ua, session=session).get("ok"):
            continue
        game = pc.gfct(proxy_url, token, ua, session=session)
        if game.get("status") != 200:
            continue

        sid = game.get("sid") or "us-east-1"
        session_token = game.get("session_token") or ""
        game_token = game.get("challengeID") or ""
        data = game.get("game_data", {})
        waves = (data.get("gameType") or "")
        referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
                   f"standard/index.html?session={session_token}")
        urls = data.get("customGUI", {}).get("_challenge_imgs") or []
        if not urls:
            continue

        total = data.get("customGUI", {}).get("challenge", [{}])
        print(f"  exit {label}")

        wave = 0
        url = urls[0]
        completed = True
        while wave < args.max_waves:
            if wave:
                time.sleep(args.gap)
            ok, note = grab_once(session, url, referer, ua)
            print(f"    wave {wave}: image {'ok' if ok else 'FAIL'} {note}")
            if not ok and args.retry_wait:
                print(f"    wave {wave}: waiting {args.retry_wait:.0f}s and retrying")
                time.sleep(args.retry_wait)
                ok, note = grab_once(session, url, referer, ua)
                print(f"    wave {wave}: retry {'ok' if ok else 'FAIL'} {note}")
            if not ok:
                completed = False
                break
            form = ca.build_form(session_token, game_token, sid, [{"index": args.index}])
            resp = pca.submit(session, form, ua, referer)
            solved = resp.get("solved")
            nxt = resp.get("_challenge_imgs") or []
            print(f"    wave {wave}: submit {resp.get('status')} "
                  f"response={resp.get('response')!r} solved={solved} next={len(nxt)}")
            if solved:
                print("    -> SOLVED")
                return 0
            if not nxt:
                print("    -> no next image (wrong answer ended the challenge)")
                completed = False
                break
            url = nxt[0]
            wave += 1

        if completed:
            print(f"  reached wave {wave} on one exit")
            return 0
        print("  exit exhausted, moving on")
        time.sleep(1)

    print("  no exit completed the probe")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
