#!/usr/bin/env python3
"""Play the wave puzzle with no browser, and learn the `/fc/ca/` response.

Runs the whole chain in one session:

    signup 403 → dataExchangeBlob
    our `c`    → gt2 token
    /pows/*    → proof of work (Arkose's own worker under Node)
    /fc/gfct/  → game token, session token, wave image
    /fc/ca/    → answer, repeat

Answers come from `--answers` until the shape of the response is fully known;
the CV path plugs into the same slot later. A deliberately wrong answer still
teaches the protocol: it says `incorrect` and hands back a fresh challenge.

    ./tools/pure_ca.py --proxy-file proxies3.txt --answers 0,1,2,3,4,5,6,7,8,9
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

from roblox_creator import ca
from roblox_creator.arkose_py import Fingerprint, load_fingerprints, pick_fingerprint
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_challenge as pc

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/155.0.0.0 Safari/537.36")


BASE_IMAGE_HEADERS = {
    "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "sec-fetch-dest": "image",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-site": "same-origin",
}


def _try_image(session, url, headers, cookies=None) -> tuple[int, bytes]:
    # `cookies=None` is not the same as omitting the argument: tls_client takes
    # it as an instruction about the jar, and the signed image URL needs the
    # cookie `gfct` just set. Passing None here produced `403 DENIED ACCESS` on
    # an image URL that a plain `session.get(url, headers=...)` fetched fine.
    if cookies is None:
        resp = session.get(url, headers=headers, timeout_seconds=25)
    else:
        resp = session.get(url, headers=headers, cookies=cookies, timeout_seconds=25)
    magic = resp.content[:4]
    ok = resp.status_code == 200 and (magic[:3] == b"\xff\xd8\xff" or magic == b"\x89PNG")
    return (200 if ok else resp.status_code), resp.content


def fetch_image(session, url: str, referer: str, ua: str,
                attempts: int = 2) -> bytes | None:
    """Grab one wave strip.

    `/rtig/image` is signed and picky: a bare request 403s even with a fresh
    signature, and the accepted combination is not documented anywhere in the
    bundle. Rather than guess once, try the plausible shapes in order and keep
    whichever answers — the first success is reported so the winner is visible.

    `attempts` caps how many shapes are tried. It defaults to two, not four,
    because the endpoint meters requests per exit: a challenge costs seven
    requests before the first image, and sweeping four shapes on every failure
    spends that allowance on being wrong. When the first two shapes both 403 the
    exit is almost always exhausted, and the caller should start a new challenge
    rather than keep asking.
    """
    stamp = pc.esync()
    variants = [
        ("ua+referer", {"user-agent": ua, "referer": referer, **BASE_IMAGE_HEADERS}, None),
        ("+esync", {"user-agent": ua, "referer": referer,
                    "x-ark-esync-value": stamp, "ark-build-id": pc.BUILD_ID,
                    **BASE_IMAGE_HEADERS}, {"timestamp": stamp}),
        ("+origin", {"user-agent": ua, "referer": referer,
                     "origin": "https://www.roblox.com",
                     "x-ark-esync-value": stamp,
                     **BASE_IMAGE_HEADERS}, {"timestamp": stamp}),
        ("bare", {"user-agent": ua, **BASE_IMAGE_HEADERS}, None),
    ]
    last = None
    for label, headers, cookies in variants[:max(1, attempts)]:
        try:
            status, body = _try_image(session, url, headers, cookies)
        except Exception as exc:
            last = f"{type(exc).__name__}"
            continue
        if status == 200:
            if label != "ua+referer":
                print(f"       image ok via {label}")
            return body
        last = f"{status} {body[:40]!r}"
    print(f"       image failed: {last}")
    return None


def submit(session, form: dict[str, str], ua: str, referer: str) -> dict:
    resp = session.post(f"{pc.ARKOSE}/fc/ca/", data=form,
                        headers={
                            "accept": "*/*",
                            "accept-language": "en-US,en;q=0.9",
                            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                            "origin": "https://www.roblox.com",
                            "referer": referer,
                            "user-agent": ua,
                            "x-ark-esync-value": pc.esync(),
                            "ark-build-id": pc.BUILD_ID,
                        },
                        cookies={"timestamp": pc.esync()}, timeout_seconds=25)
    out: dict = {"status": resp.status_code}
    try:
        out.update(resp.json())
    except Exception:
        out["raw"] = resp.text[:300]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--answers", default="0,1,2,3,4,5,6,7,8,9,0,1,2,3,4",
                    help="comma-separated indices to submit, in order")
    ap.add_argument("--tguess", action="store_true",
                    help="also send the dapib-transformed answer")
    ap.add_argument("--tries", type=int, default=3)
    args = ap.parse_args()

    answers = [int(x) for x in args.answers.split(",") if x.strip()]
    pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)

    for attempt in range(args.tries):
        proxy = random.choice(pool)
        print(f"\n  [{attempt}] {parse_proxy(proxy).masked()}")
        if play(proxy, answers, args.tguess) == 0:
            return 0
    return 1


def play(proxy: str, answers: list[int], with_tguess: bool) -> int:
    proxy_url = parse_proxy(proxy).url()

    challenge = pc.request_blob(proxy_url)
    if not challenge:
        print("  no Arkose challenge")
        return 1

    fp = Fingerprint.from_raw(pick_fingerprint(load_fingerprints()))
    session = pc.new_session(proxy_url)

    minted = pc.gt2(proxy_url, challenge["metadata"]["dataExchangeBlob"], fp, session=session)
    token = minted.get("token", "")
    print(f"  gt2: {minted['status']} sup={'sup=1' in token}")
    if not token:
        return 1

    pow_result = pc.pows(proxy_url, token, fp.user_agent, session=session)
    if not pow_result.get("ok"):
        last = (pow_result.get("rounds") or [{}])[-1]
        print(f"  pows failed: {last}")
        return 1
    print(f"  pows: {[r.get('action') for r in pow_result['rounds']]}")

    game = pc.gfct(proxy_url, token, fp.user_agent, session=session)
    if game.get("status") != 200 or "game_data" not in game:
        print(f"  gfct failed: {str(game)[:200]}")
        return 1

    data = game["game_data"]
    print(f"  game: type={data.get('gameType')} waves={data.get('waves')} "
          f"instr={data.get('instruction_string')}")

    out = ROOT / "debug" / "ca" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "gfct.json").write_text(json.dumps(game, indent=1))

    game_token = game.get("challengeID") or ""
    session_token = game.get("session_token") or ""
    sid = game.get("sid") or "us-east-1"
    # the game frame identifies itself with the *gfct* session token, not the
    # gt2 one — the signed image URL is only honoured from that referer
    referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/standard/"
               f"index.html?session={session_token}")
    images = data.get("customGUI", {}).get("_challenge_imgs") or []

    guesses: list[dict] = []
    next_url = images[0] if images else None
    log: list[dict] = []

    for wave, answer in enumerate(answers):
        if not next_url:
            print(f"  wave {wave}: no image URL")
            break
        blob = fetch_image(session, next_url, referer, fp.user_agent)
        if blob:
            name = "wave%02d.jpg" % wave
            (out / name).write_bytes(blob)
            print(f"  wave {wave}: image {len(blob)} bytes -> {name}")
        else:
            print(f"  wave {wave}: image fetch failed")

        guesses.append({"index": answer})
        form = ca.build_form(session_token, game_token, sid, guesses)
        result = submit(session, form, fp.user_agent, referer)
        log.append({"wave": wave, "answer": answer, "result":
                    {k: v for k, v in result.items() if k != "string_table"}})
        print(f"    -> {result.get('status')} response={result.get('response')!r} "
              f"solved={result.get('solved')}")
        for key, value in result.items():
            if key in ("response", "solved", "status", "string_table"):
                continue
            print(f"       {key}: {str(value)[:100]}")

        if result.get("solved") or result.get("decryption_key"):
            print("\n  SOLVED")
            (out / "ca.json").write_text(json.dumps(log, indent=1))
            (out / "solved.json").write_text(json.dumps(result, indent=1))
            return 0

        imgs = result.get("_challenge_imgs") or []
        next_url = imgs[0] if imgs else None

    (out / "ca.json").write_text(json.dumps(log, indent=1))
    print(f"  -> {out}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
