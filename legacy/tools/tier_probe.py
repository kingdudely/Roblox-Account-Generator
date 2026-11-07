#!/usr/bin/env python3
"""Does `analytics_tier` set the number of waves?

`gfct` returns `game_data.waves`, and one of the fields we send is
`analytics_tier: "40"`. If that number is difficulty rather than telemetry, a lower
tier means a shorter challenge — which is worth more than any transport tuning,
because the images an exit will serve are the binding constraint (roughly one or
two per exit).

Cheap to test: it needs `gfct` only, no image fetches. One tier per exit so a
cached game config cannot leak between arms.

    ./tools/tier_probe.py --proxy-file proxies3.txt --tiers 0,5,10,20,40
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import payloads
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_challenge as pc


def gfct_tier(session, token: str, ua: str, tier: str) -> dict:
    sid = ""
    for part in token.split("|"):
        if part.startswith("r="):
            sid = part[2:]
    referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}/"
               f"standard/index.html?session={token.split('|')[0]}")
    resp = session.post(
        f"{pc.ARKOSE}/fc/gfct/",
        data={"token": token.split("|")[0], "sid": sid, "render_type": "canvas",
              "lang": "", "isAudioGame": "false",
              "is_compatibility_mode": "false", "apiBreakerVersion": "green",
              "analytics_tier": tier},
        headers=pc.arkose_headers(ua, referer),
        cookies={"timestamp": pc.esync()}, timeout_seconds=25)
    out = {"status": resp.status_code}
    try:
        out.update(resp.json())
    except Exception:
        out["raw"] = resp.text[:200]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--tiers", default="0,5,10,20,40")
    ap.add_argument("--per-tier", type=int, default=2)
    args = ap.parse_args()

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)
    pool = payloads.load_payloads()
    seen: dict[str, list[int]] = defaultdict(list)

    need = len(tiers) * args.per_tier
    hit = 0
    for proxy in proxies:
        if hit >= need:
            break
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

        tier = tiers[hit % len(tiers)]
        game = gfct_tier(session, token, ua, tier)
        if game.get("status") != 200:
            print(f"  tier {tier:>3}: gfct {game.get('status')}")
            continue
        data = game.get("game_data", {})
        waves = data.get("waves")
        kind = game.get("game_type") or data.get("gameType")
        instr = data.get("instruction_string")
        seen[tier].append(int(waves or 0))
        print(f"  tier {tier:>3}: waves={waves} gameType={kind} {instr!r}")
        hit += 1

    print()
    for tier in tiers:
        vals = seen.get(tier) or []
        print(f"  tier {tier:>3}: {vals or 'no data'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
