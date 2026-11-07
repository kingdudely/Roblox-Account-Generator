#!/usr/bin/env python3
"""Why does the *second* wave image 403 when the first one works?

Wave 0's signed URL is honoured from the game-frame referer. Wave 1's URL — minted
by the `/fc/ca/` response, signed for the same session token — answers
`403 DENIED ACCESS` on every header shape `fetch_image` tries, and retrying does
not help. So something about the second request differs.

This runs one challenge, answers wave 0 with a fixed index, then throws a matrix of
plausible shapes at the wave-1 URL and reports which, if any, returns an image. If
none do, the URL itself is not the issue and the answer is that the challenge was
rejected — which the response body should already say.

    ./tools/wave2_probe.py --proxy-file proxies.txt --index 0
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

IMG_ACCEPT = {
    "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "sec-fetch-dest": "image",
    "sec-fetch-mode": "no-cors",
}


def probe(session, url: str, ua: str, frame: str, origin: str, stamp: str) -> list[dict]:
    """Try a matrix of header shapes against one URL."""
    shapes = {
        "frame-referer": {"referer": frame, **IMG_ACCEPT},
        "frame+esync": {"referer": frame, "x-ark-esync-value": stamp,
                        "ark-build-id": pc.BUILD_ID, **IMG_ACCEPT},
        "frame+origin-arkose": {"referer": frame, "origin": origin, **IMG_ACCEPT},
        "frame+origin-roblox": {"referer": frame,
                                "origin": "https://www.roblox.com", **IMG_ACCEPT},
        "roblox-referer": {"referer": "https://www.roblox.com/", **IMG_ACCEPT},
        "no-referer": dict(IMG_ACCEPT),
        "fetch-site-none": {"referer": frame, "accept": IMG_ACCEPT["accept"],
                            "sec-fetch-dest": "image", "sec-fetch-mode": "no-cors",
                            "sec-fetch-site": "none"},
        "cross-site": {"referer": frame, "accept": IMG_ACCEPT["accept"],
                       "sec-fetch-dest": "image", "sec-fetch-mode": "no-cors",
                       "sec-fetch-site": "cross-site"},
        # the shape `pure_ca.fetch_image` actually uses, for an A/B on one URL
        "same-origin": {"referer": frame, "accept": IMG_ACCEPT["accept"],
                        "sec-fetch-dest": "image", "sec-fetch-mode": "no-cors",
                        "sec-fetch-site": "same-origin"},
        "same-origin+noref": {"accept": IMG_ACCEPT["accept"],
                              "sec-fetch-dest": "image",
                              "sec-fetch-mode": "no-cors",
                              "sec-fetch-site": "same-origin"},
    }
    out = []
    for name, headers in shapes.items():
        try:
            resp = session.get(url, headers={"user-agent": ua, **headers},
                               timeout_seconds=25)
            magic = resp.content[:4]
            ok = resp.status_code == 200 and (
                magic[:3] == b"\xff\xd8\xff" or magic == b"\x89PNG")
            out.append({"shape": name, "status": resp.status_code,
                        "image": ok, "bytes": len(resp.content),
                        "body": resp.content[:40] if not ok else b"<image>"})
        except Exception as exc:
            out.append({"shape": name, "status": "EXC",
                        "body": f"{type(exc).__name__}: {exc}"[:60]})
        time.sleep(0.4)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--index", type=int, default=0)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="wait this long after answering before fetching the next "
                         "wave — mimics the VLM latency in solve_live")
    args = ap.parse_args()

    pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)
    proxy = random.choice(pool)
    proxy_url = parse_proxy(proxy).url()
    print(f"  exit {parse_proxy(proxy).masked()}")

    challenge = pc.request_blob(proxy_url)
    if not challenge:
        print("  no challenge")
        return 1

    session = pc.new_session(proxy_url)
    record = payloads.pick_payload(payloads.load_payloads())
    if record is None:
        print("  empty payload corpus")
        return 1
    c_blob, ua = payloads.reseal(record)
    minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                         c_blob, ua, session=session)
    token = minted.get("token", "")
    if not token:
        print(f"  gt2 failed: {minted.get('status')}")
        return 1
    pow_result = pc.pows(proxy_url, token, ua, session=session)
    if not pow_result.get("ok"):
        print("  pow blocked")
        return 1
    game = pc.gfct(proxy_url, token, ua, session=session)
    if game.get("status") != 200:
        print(f"  gfct failed: {game.get('status')}")
        return 1

    data = game["game_data"]
    session_token = game.get("session_token") or ""
    game_token = game.get("challengeID") or ""
    sid = game.get("sid") or "us-east-1"
    frame = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/standard/"
             f"index.html?session={session_token}")
    url0 = data["customGUI"]["_challenge_imgs"][0]
    print(f"  wave0 URL ok? ", end="")

    blob0 = pca.fetch_image(session, url0, frame, ua)
    print("yes" if blob0 else "NO")
    if not blob0:
        return 1

    form = ca.build_form(session_token, game_token, sid, [{"index": args.index}])
    resp = pca.submit(session, form, ua, frame)
    print(f"  answered index {args.index}: status={resp.get('status')} "
          f"response={resp.get('response')!r} incorrect={resp.get('incorrect_guess')} "
          f"solved={resp.get('solved')}")
    extra = {k: v for k, v in resp.items()
             if k not in ("response", "solved", "status", "string_table",
                          "incorrect_guess", "decryption_key", "time_end",
                          "time_end_seconds")}
    if extra:
        print(f"  other fields: {str(extra)[:200]}")

    if args.delay:
        print(f"  sleeping {args.delay:.0f}s before fetching (simulating VLM latency)")
        time.sleep(args.delay)

    urls = resp.get("_challenge_imgs") or []
    print(f"  next images: {len(urls)}")
    if not urls:
        print("  <- no next image, so the challenge ended here")
        return 0

    print(f"\n  wave1 URL: {urls[0][:110]}…")
    print(f"  cookies on session: {len(session.cookies)}")
    results = probe(session, urls[0], ua, frame, pc.ARKOSE, pc.esync())
    for row in results:
        print(f"    {row['shape']:<22} {row['status']} "
              f"{'IMAGE' if row.get('image') else row.get('body')}")
    good = [r for r in results if r.get("image")]
    print(f"\n  {len(good)}/{len(results)} shapes worked"
          f"{' -> ' + good[0]['shape'] if good else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
