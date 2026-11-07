#!/usr/bin/env python3
"""Is the signed wave image bound to the exit that minted the challenge?

Exit health is the dominant failure: a wave-0 image fetch succeeds on only half
to three quarters of residential exits, and a challenge needs one healthy exit for
its whole life. That constraint only exists if `/rtig/image` refuses to serve a
valid signature from a different IP.

If it does not, the fix is trivial and total: mint the challenge on whatever exit
works, then fetch images through the healthiest exit in the pool, reusing the
cookie jar. No challenge would ever die of a bad exit again.

Method — one challenge, one signature, four fetches:

    A      the minting exit, its own session          (control)
    B      a different exit, cookies copied from A
    B0     a different exit, no cookies at all        (isolates the cookie)
    A2     the minting exit, fresh session, cookies from A (isolates the jar)

    ./tools/image_exit_probe.py --proxy-file proxies1.txt
    ./tools/image_exit_probe.py --proxy-file proxies1.txt --rounds 3
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


def cookie_dict(session) -> dict[str, str]:
    jar = getattr(session, "cookies", None)
    if jar is None:
        return {}
    if hasattr(jar, "get_dict"):
        try:
            return dict(jar.get_dict())
        except Exception:
            pass
    out = {}
    try:
        for c in jar:
            out[c.name] = c.value
    except Exception:
        pass
    return out


def fetch(session, url: str, referer: str, ua: str) -> tuple[bool, str]:
    try:
        blob = pca.fetch_image(session, url, referer, ua)
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:60]}"
    if not blob:
        return False, "no image"
    return True, f"{len(blob) // 1024} KB"


def mint(proxies: list[str], tries: int, payload_pool) -> dict | None:
    """Run the chain until one exit yields a wave-0 URL. Returns the context."""
    order = list(proxies)
    random.shuffle(order)
    for n, proxy in enumerate(order[:tries], 1):
        proxy_url = parse_proxy(proxy).url()
        label = parse_proxy(proxy).masked()
        print(f"  mint attempt {n}: {label}")
        challenge = pc.request_blob(proxy_url)
        if not challenge:
            print("    no challenge")
            continue
        record = payloads.pick_payload(payload_pool)
        if record is None:
            print("    empty payload corpus")
            return None
        session = pc.new_session(proxy_url)
        c_blob, ua = payloads.reseal(record)
        minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                             c_blob, ua, session=session)
        token = minted.get("token", "")
        if not token:
            print(f"    gt2 {minted.get('status')}")
            continue
        if not pc.pows(proxy_url, token, ua, session=session).get("ok"):
            print("    pow blocked")
            continue
        game = pc.gfct(proxy_url, token, ua, session=session)
        if game.get("status") != 200:
            print(f"    gfct {game.get('status')}")
            continue
        urls = game.get("game_data", {}).get("customGUI", {}).get("_challenge_imgs") or []
        if not urls:
            print("    no wave-0 image in gfct")
            continue
        session_token = game.get("session_token") or ""
        referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
                   f"standard/index.html?session={session_token}")
        print(f"    minted on {label}")
        return {"proxy": proxy, "url": urls[0], "referer": referer, "ua": ua,
                "session": session, "label": label}
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--tries", type=int, default=8, help="exits to try while minting")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--order", default="A,A2,B,B0",
                    help="sequence of fetches. A=minting session, A2=fresh session "
                         "on the minting exit with copied cookies, B=other exit with "
                         "cookies, B0=other exit without. Repeat a tag to test reuse.")
    args = ap.parse_args()

    proxies = load_proxies(args.proxy_file)
    if len(proxies) < 2:
        print("  need at least two exits")
        return 1
    payload_pool = payloads.load_payloads()

    for run in range(args.rounds):
        print(f"\n== round {run + 1}/{args.rounds} ==")
        ctx = mint(proxies, args.tries, payload_pool)
        if ctx is None:
            print("  could not mint a challenge")
            continue

        cookies = cookie_dict(ctx["session"])
        print(f"  cookies carried: {list(cookies)}")

        others = [p for p in proxies if p != ctx["proxy"]]
        random.shuffle(others)
        mint_url = parse_proxy(ctx["proxy"]).url()

        def copy_jar(target):
            for name, value in cookies.items():
                try:
                    target.cookies.set(name, value)
                except Exception:
                    pass
            return target

        for step, tag in enumerate([t.strip() for t in args.order.split(",") if t.strip()]):
            if tag == "A":
                session, where = ctx["session"], "minting exit, same session"
            elif tag == "A2":
                session = copy_jar(pc.new_session(mint_url))
                where = "minting exit, fresh session"
            else:
                proxy = others.pop() if others else None
                if proxy is None:
                    print(f"    {tag:<3} no spare exit left")
                    continue
                session = pc.new_session(parse_proxy(proxy).url())
                if tag == "B":
                    copy_jar(session)
                where = f"other exit ({parse_proxy(proxy).masked()[:16]})"
            ok, note = fetch(session, ctx["url"], ctx["referer"], ctx["ua"])
            print(f"    {step + 1}. {tag:<3} {where:<32} "
                  f"{'IMAGE' if ok else 'fail'}  {note}")
            time.sleep(0.4)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
