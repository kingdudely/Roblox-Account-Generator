#!/usr/bin/env python3
"""Does an exit that has spent its image allowance ever get it back?

This is the number the whole product depends on, and nothing in the repo measures
it. `image_cooldown_probe.py` polls one signed URL, but the URL and the exit die
separately: `docs/RUNTIME.md` shows a signature is reusable and IP-bound, so a
URL that 403s may be dead while the exit behind it is fine — and vice versa. What
we need to know is the *exit's* recovery curve.

`pool_audit.py --stage image` tells you the fraction of exits that are fresh right
now. It cannot tell you whether a spent exit is spent forever, which is the
difference between two completely different businesses:

    resets    pool size = accounts/min x 20 requests x reset window. Feasible.
    never     every account costs a virgin exit. The supply sets the ceiling.

Method, per exit, on one sticky session so the IP cannot rotate:

    burn      mint a challenge, fetch the same signed URL repeatedly until 403,
              and record how many fetches the allowance was worth
    recover   at each checkpoint re-mint a *fresh* challenge on the same exit and
              fetch its wave 0. A mint costs ~7 Arkose requests, so a checkpoint
              is 8 requests — cheap enough to ask often.

The identity probe between checkpoints is not decoration: a sticky session that
silently rotates to a new IP would make a "recovery" mean nothing.

    ./tools/budget_recovery.py --proxy-file proxies2.txt --exits 3 \
        --schedule 1,5,15,30,60,120
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import payloads
from roblox_creator.arkose_py import Fingerprint, load_fingerprints, pick_fingerprint
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_challenge as pc

OUT = ROOT / "debug" / "budget"
DEFAULT_PK = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"

IMAGE_HEADERS = {
    "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "sec-fetch-dest": "image",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-site": "same-origin",
}


def log(message: str) -> None:
    print(message, flush=True)


def mint(proxy_url: str) -> dict | None:
    """Walk blob -> gt2 -> pows -> gfct. Returns what is needed to fetch a wave."""
    challenge = pc.request_blob(proxy_url)
    if not challenge:
        return None

    session = pc.new_session(proxy_url)
    record = payloads.pick_payload(payloads.load_payloads())
    if record is not None:
        c_blob, ua = payloads.reseal(record)
        minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                             c_blob, ua, session=session)
    else:
        fp = Fingerprint.from_raw(pick_fingerprint(load_fingerprints()))
        ua = fp.user_agent
        minted = pc.gt2(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                        fp, session=session)

    token = minted.get("token", "")
    if not token:
        return None
    if not pc.pows(proxy_url, token, ua, session=session).get("ok"):
        return None
    game = pc.gfct(proxy_url, token, ua, session=session)
    if game.get("status") != 200:
        return None

    data = game.get("game_data") or {}
    session_token = game.get("session_token", "")
    images = (data.get("customGUI") or {}).get("_challenge_imgs") or []
    if not session_token or not images:
        return None

    referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}"
               f"/standard/index.html?session={session_token}")
    return {"session": session, "ua": ua, "referer": referer,
            "url": images[0], "waves": data.get("waves"),
            "public_key": (challenge.get("metadata") or {}).get("publicKey") or DEFAULT_PK}


def fetch(ctx: dict, index: int = 0) -> int:
    """Fetch one wave image. Returns the HTTP status; 200 means the allowance is live."""
    url = re.sub(r"challenge=\d+", f"challenge={index}", ctx["url"])
    try:
        resp = ctx["session"].get(url, headers={"user-agent": ctx["ua"],
                                                "referer": ctx["referer"],
                                                **IMAGE_HEADERS},
                                  timeout_seconds=25)
        return resp.status_code
    except Exception:
        return -1


def identity(proxy_url: str) -> str | None:
    """The exit's own view of its address, so a silent rotation cannot fake recovery."""
    from curl_cffi import requests as creq

    try:
        session = creq.Session(impersonate="chrome", timeout=15)
        session.proxies = {"http": proxy_url, "https": proxy_url}
        resp = session.get("https://api.ipify.org?format=json")
        return resp.json().get("ip")
    except Exception:
        return None


def burn(proxy_url: str, limit: int = 40) -> dict:
    """Spend the allowance on one challenge and report what it was worth."""
    ctx = mint(proxy_url)
    if ctx is None:
        return {"burned": 0, "note": "mint failed"}

    served = 0
    for _ in range(limit):
        status = fetch(ctx, 0)
        if status != 200:
            return {"burned": served, "stop_status": status,
                    "waves": ctx["waves"]}
        served += 1
    return {"burned": served, "stop_status": None, "waves": ctx["waves"],
            "note": f"still serving after {limit} fetches"}


def recover(proxy_url: str) -> dict:
    """Re-mint on the same exit and ask for one image."""
    ctx = mint(proxy_url)
    if ctx is None:
        return {"mint": False, "status": None}
    return {"mint": True, "status": fetch(ctx, 0)}


def run_exit(raw: str, schedule: list[float], limit: int) -> dict:
    parsed = parse_proxy(raw)
    proxy_url = parsed.url()
    row = {"exit": parsed.masked(), "ip": identity(proxy_url),
           "burn": None, "checks": []}
    log(f"\nexit {parsed.masked()}  ip={row['ip']}")

    row["burn"] = burn(proxy_url, limit)
    log(f"  burn: served {row['burn']['burned']} image(s), "
        f"stop={row['burn'].get('stop_status')}")

    t0 = time.time()
    for checkpoint in schedule:
        wait = checkpoint * 60 - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)
        ip = identity(proxy_url)
        result = recover(proxy_url)
        row["checks"].append({
            "minutes": checkpoint,
            "ip": ip,
            "ip_same": ip == row["ip"],
            "mint": result["mint"],
            "status": result["status"],
        })
        log(f"  +{checkpoint:>5.0f}m  mint={result['mint']}  "
            f"status={result['status']}  ip_same={ip == row['ip']}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, action="append", default=None)
    ap.add_argument("--exits", type=int, default=2)
    ap.add_argument("--schedule", default="1,5,15,30,60,120",
                    help="checkpoints in minutes, comma separated")
    ap.add_argument("--limit", type=int, default=40,
                    help="fetches to spend before calling the allowance gone")
    ap.add_argument("--workers", type=int, default=None,
                    help="exits measured at once (default: all)")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    schedule = [float(part) for part in args.schedule.split(",") if part.strip()]
    files = args.proxy_file or [ROOT / "proxies2.txt"]
    proxies: list[str] = []
    for path in files:
        proxies.extend(load_proxies(path))
    random.shuffle(proxies)
    picked = proxies[:args.exits]
    if not picked:
        log("no proxies")
        return 1

    workers = args.workers or len(picked)
    started = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(lambda raw: run_exit(raw, schedule, args.limit), picked))

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = args.out or (OUT / f"{stamp}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "stamp": stamp, "schedule": schedule,
        "elapsed_minutes": round((time.time() - started) / 60, 1),
        "rows": rows,
    }, indent=1))

    log("\n--- recovery ---")
    for row in rows:
        marks = " ".join(
            f"{c['minutes']:.0f}m:{c['status']}" for c in row["checks"])
        log(f"  burn={row['burn']['burned']:<3} {marks}")
    log(f"\nsaved {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
