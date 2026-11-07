#!/usr/bin/env python3
"""Play one wave with an answer supplied by a human looking at the montage.

Every failure in the loop lands in the same place: the next wave's image answers
`403 DENIED ACCESS`, and there is no per-wave feedback to say why. Either the exit
is out of budget, or a wrong answer voided the challenge and the 403 is how it
tells you — those two hypotheses predict completely different fixes, and the only
way to separate them is to submit an answer that is known to be *correct*.

So this mints a challenge, writes the montage out, and blocks on a file. When the
answer file appears it submits that index and reports what the next image does.
Because the image URL is bound to the minting exit, the process has to stay alive
while the answer is decided — that keeps the same proxy session and cookie jar.

    ./tools/answer_gate.py --proxy-file proxies1.txt --ask 3
    # then look at /tmp/opencode/gate/montage_00.jpg and:
    echo 4 > /tmp/opencode/gate/answer.txt     # 1-based tile label
"""

from __future__ import annotations

import argparse
import base64
import json
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
from tools.wave_montage import build_from_strip


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/gate"))
    ap.add_argument("--ask", type=int, default=3, help="how many waves to ask about")
    ap.add_argument("--offset", type=int, default=-1,
                    help="wire index = label + offset")
    ap.add_argument("--timeout", type=float, default=900.0)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    answer_file = args.out / "answer.txt"
    answer_file.unlink(missing_ok=True)
    state = args.out / "state.json"

    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)
    pool = payloads.load_payloads()

    ctx = None
    for proxy in proxies[:10]:
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
        urls = game.get("game_data", {}).get("customGUI", {}).get("_challenge_imgs") or []
        if not urls:
            continue
        ctx = {
            "session": session, "ua": ua,
            "session_token": game.get("session_token") or "",
            "game_token": game.get("challengeID") or "",
            "sid": game.get("sid") or "us-east-1",
            "url": urls[0],
            "waves": game.get("game_data", {}).get("gameType") or "",
        }
        print(f"  minted on {parse_proxy(proxy).masked()}")
        break

    if ctx is None:
        print("  could not mint")
        return 1

    referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
               f"standard/index.html?session={ctx['session_token']}")

    for wave in range(args.ask):
        blob = pca.fetch_image(ctx["session"], ctx["url"], referer, ctx["ua"])
        if not blob:
            print(f"  wave {wave}: image FAIL — stopping")
            return 1
        strip = args.out / f"strip_{wave:02d}.jpg"
        strip.write_bytes(blob)
        montage = build_from_strip(strip, args.out / f"montage_{wave:02d}.jpg")
        state.write_text(json.dumps({
            "wave": wave, "montage": str(montage),
            "strip": base64.b64encode(blob[:80]).decode(),
            "bytes": len(blob),
        }, indent=2))
        print(f"  wave {wave}: {montage}  ({len(blob) // 1024} KB)")
        print(f"  waiting for {answer_file} (tile label, 1-based)")

        deadline = time.time() + args.timeout
        label = None
        while time.time() < deadline:
            if answer_file.exists():
                raw = answer_file.read_text().strip()
                if raw:
                    try:
                        label = int(raw.split()[0])
                        break
                    except ValueError:
                        answer_file.unlink(missing_ok=True)
            time.sleep(2)
        if label is None:
            print("  no answer — giving up")
            return 1
        answer_file.unlink(missing_ok=True)

        index = label + args.offset
        print(f"  submitting label {label} -> wire index {index}")
        form = ca.build_form(ctx["session_token"], ctx["game_token"], ctx["sid"],
                             [{"index": index}])
        resp = pca.submit(ctx["session"], form, ctx["ua"], referer)
        print(f"  wave {wave}: status={resp.get('status')} "
              f"response={resp.get('response')!r} "
              f"incorrect={resp.get('incorrect_guess')} solved={resp.get('solved')}")
        nxt = resp.get("_challenge_imgs") or []
        if resp.get("solved"):
            print(f"  -> SOLVED, decryption_key={resp.get('decryption_key')}")
            return 0
        if not nxt:
            print("  -> challenge ended")
            return 1
        ctx["url"] = nxt[0]
        # fetch immediately so a budget hypothesis is not confounded by latency
        probe = pca.fetch_image(ctx["session"], ctx["url"], referer, ctx["ua"])
        print(f"  next image after a {'CORRECT' if 'not answered' in str(resp) else '?'} "
              f"answer: {'ok' if probe else '403'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
