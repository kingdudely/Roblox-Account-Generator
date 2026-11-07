#!/usr/bin/env python3
"""How long does an exit stay image-blocked after one successful fetch?

Ten rapid requests to one signed URL: the first returned the image, the following
nine answered `403 DENIED ACCESS`. That is not a signature TTL — the same URL had
served twice, 1.7 s apart, elsewhere — it is a burst limit that turns into a
cooldown. This measures the cooldown so the loop can wait it out instead of
burning challenges against it.

Method: mint, take one image, then poll every `--step` seconds until the URL
answers again, and report every transition. If it recovers in tens of seconds the
fix is a sleep between waves; if it never recovers, the exit is finished and the
fix is a fresh exit per wave (which the transport cannot do, so it would have to
be a fresh challenge per wave).

    ./tools/image_cooldown_probe.py --proxy-file proxies1.txt
"""

from __future__ import annotations

import argparse
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
from tools.image_clock_probe import mint


def one(ctx: dict) -> int:
    headers = {"user-agent": ctx["ua"], "referer": ctx["referer"],
               **pca.BASE_IMAGE_HEADERS}
    try:
        status, _ = pca._try_image(ctx["session"], ctx["url"], headers)
    except Exception:
        return 0
    return status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies1.txt")
    ap.add_argument("--tries", type=int, default=8)
    ap.add_argument("--step", type=float, default=5.0)
    ap.add_argument("--horizon", type=float, default=150.0)
    args = ap.parse_args()

    ctx = mint(load_proxies(args.proxy_file), args.tries, payloads.load_payloads())
    if ctx is None:
        print("  could not mint")
        return 1
    print(f"  minted on {parse_proxy(ctx['proxy']).masked()}")

    t0 = time.time()
    status = one(ctx)
    print(f"  +{time.time() - t0:6.1f}s  first fetch {status}")
    if status != 200:
        print("  exit was already blocked at mint — nothing to measure")
        return 0

    blocked_at = None
    while time.time() - t0 < args.horizon:
        time.sleep(args.step)
        elapsed = time.time() - t0
        status = one(ctx)
        if status == 200 and blocked_at is not None:
            print(f"  +{elapsed:6.1f}s  200  RECOVERED after "
                  f"{elapsed - blocked_at:.0f}s blocked")
            return 0
        if status == 200:
            print(f"  +{elapsed:6.1f}s  200  still serving")
        else:
            if blocked_at is None:
                blocked_at = elapsed
            print(f"  +{elapsed:6.1f}s  {status}  blocked")
    print(f"  still blocked at +{args.horizon:.0f}s"
          + (f" ({args.horizon - blocked_at:.0f}s blocked)" if blocked_at else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
