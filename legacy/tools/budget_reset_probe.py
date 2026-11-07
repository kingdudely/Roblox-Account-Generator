#!/usr/bin/env python3
"""Is the image budget per exit, or per cookie session?

The arithmetic is suspiciously clean: a challenge costs seven Arkose requests before
the first image (gt2, five proof-of-work calls, gfct), then one fetch plus one submit
per wave, and the wall lands at roughly the fifth image — request eighteen. A
ten-wave challenge needs twenty-seven, so if the allowance really is eighteen, no
amount of pacing fixes it and the only question left is what the allowance is
counted against.

The image URL is already known to be IP-bound, not cookie-bound, because a fresh
session on the same exit (no cookies) served it. So if the *counter* sits on the
cookie session, rotating the jar after exhaustion buys a whole new allowance and the
challenge becomes solvable.

Sequence: exhaust the exit with sequential answers, then ask the same URL with

    stale    the session that just got 403        (control — must stay 403)
    fresh    new tls session, same exit, no cookies
    copy     new tls session, same exit, cookies copied from the original

    ./tools/budget_reset_probe.py --proxy-file proxies.txt
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


def jar(session) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for c in session.cookies:
            out[c.name] = c.value
    except Exception:
        pass
    return out


def attempt(session, url: str, referer: str, ua: str) -> str:
    headers = {"user-agent": ua, "referer": referer, **pca.BASE_IMAGE_HEADERS}
    try:
        status, body = pca._try_image(session, url, headers)
    except Exception as exc:
        return f"EXC {type(exc).__name__}"
    return f"{status} {'IMAGE' if status == 200 else body[:22]!r}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--tries", type=int, default=8)
    ap.add_argument("--max-waves", type=int, default=8)
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)
    pool = payloads.load_payloads()

    for proxy in proxies[:args.tries]:
        proxy_url = parse_proxy(proxy).url()
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
        data = game.get("game_data", {})
        urls = data.get("customGUI", {}).get("_challenge_imgs") or []
        if not urls:
            continue

        session_token = game.get("session_token") or ""
        referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
                   f"standard/index.html?session={session_token}")
        print(f"  {parse_proxy(proxy).masked()}  waves={data.get('waves')} "
              f"difficulty={data.get('game_difficulty')}")

        url = urls[0]
        deep = 0
        for wave in range(args.max_waves):
            blob = pca.fetch_image(session, url, referer, ua)
            if not blob:
                print(f"    wave {wave}: image 403 — exhausted after {deep} images")
                break
            deep = wave + 1
            form = ca.build_form(session_token, game.get("challengeID") or "",
                                 game.get("sid") or "us-east-1",
                                 [{"index": wave % 10}])
            resp = pca.submit(session, form, ua, referer)
            nxt = resp.get("_challenge_imgs") or []
            if resp.get("solved"):
                print("    -> SOLVED")
                return 0
            if not nxt:
                break
            url = nxt[0]
            time.sleep(1)

        if deep == 0:
            continue

        print("  now probing the exhausted URL three ways:")
        print(f"    stale  {attempt(session, url, referer, ua)}")

        fresh = pc.new_session(proxy_url)
        print(f"    fresh  {attempt(fresh, url, referer, ua)}")

        copied = pc.new_session(proxy_url)
        for name, value in jar(session).items():
            try:
                copied.cookies.set(name, value)
            except Exception:
                pass
        print(f"    copy   {attempt(copied, url, referer, ua)}")

        # and if fresh worked, does it keep working? that is the whole question
        ok_again = attempt(fresh, url, referer, ua)
        print(f"    fresh2 {ok_again}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
