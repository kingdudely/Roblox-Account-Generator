#!/usr/bin/env python3
"""Measure what a free exit can actually carry, in bytes and seconds.

A free proxy can answer one small GET and still be useless for a browser, because
a browser is not one request. CreateAccount pulls a couple of hundred assets —
megabytes, over dozens of connections, in parallel. An exit that returns 200 in
6 s on a 40 KB response has told you almost nothing about that workload.

So this measures the workload directly: fetch the signup page, then fetch a real
asset from rbxcdn through the same exit, and report throughput. An exit that
cannot move a megabyte in a reasonable time cannot host a browser no matter how
healthy its status code looks.

    ./tools/free_throughput.py --proxy-file /tmp/opencode/free_big.txt --sample 10
    ./tools/free_throughput.py --file free.txt --sample 20 --min-bps 60000
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# A fixed 2 MB payload on a neutral host, used purely as a throughput ruler.
# The first version of this used a real rbxcdn bundle — which turned out to be
# 3 KB, so every exit "passed" and the numbers meant nothing. Size is the point:
# the measurement has to be big enough that a throttled exit cannot hide.
ASSET = "https://speed.cloudflare.com/__down?bytes=2000000"
PAGE = "https://www.roblox.com/CreateAccount"


def probe(proxy_url: str, timeout: float) -> dict:
    from curl_cffi import requests as creq

    out = {"proxy": proxy_url, "page_s": None, "page_bytes": 0,
           "asset_s": None, "asset_bytes": 0, "bps": None, "error": None}
    session = creq.Session(impersonate="chrome", timeout=timeout)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        t0 = time.time()
        page = session.get(PAGE, headers={"accept": "text/html",
                                          "user-agent": UA})
        out["page_s"] = round(time.time() - t0, 2)
        out["page_bytes"] = len(page.content or b"")
        if page.status_code != 200:
            out["error"] = f"page {page.status_code}"
            return out

        t1 = time.time()
        asset = session.get(ASSET, headers={"accept": "*/*", "user-agent": UA},
                            allow_redirects=True)
        dt = max(time.time() - t1, 0.001)
        out["asset_s"] = round(dt, 2)
        out["asset_bytes"] = len(asset.content or b"")
        out["bps"] = int(out["asset_bytes"] / dt)
        if asset.status_code != 200:
            out["error"] = f"asset {asset.status_code}"
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:40]}"
        return out
    finally:
        try:
            session.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=Path("/tmp/opencode/free_big.txt"))
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--min-bps", type=int, default=0,
                    help="only print exits at or above this throughput")
    ap.add_argument("--out", type=Path, default=None,
                    help="write the fast survivors to this proxy file")
    args = ap.parse_args()

    pool = load_proxies(args.proxy_file)
    if not pool:
        print("empty pool")
        return 2
    picked = random.sample(pool, min(args.sample, len(pool)))
    print(f"{len(pool)} in pool, probing {len(picked)}")

    rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe, parse_proxy(x).url(), args.timeout): x
                for x in picked}
        for fut, raw in futs.items():
            r = fut.result()
            rows.append(r)
            masked = parse_proxy(raw).masked()
            if r["error"]:
                print(f"  {masked[:40]:<40} {r['error']}")
            else:
                print(f"  {masked[:40]:<40} page {r['page_s']:>6.1f}s "
                      f"asset {r['asset_s']:>6.2f}s "
                      f"{r['asset_bytes'] // 1024:>5} KB "
                      f"{r['bps'] // 1024:>5} KB/s")

    good = [r for r in rows if r["bps"]]
    if good:
        bps = [r["bps"] for r in good]
        print(f"\n  {len(good)}/{len(rows)} moved the asset")
        print(f"  median {statistics.median(bps) // 1024} KB/s   "
              f"best {max(bps) // 1024} KB/s   worst {min(bps) // 1024} KB/s")
        # A browser needs roughly 300-600 KB/s to render a heavy page before the
        # navigation timeout, so this is the line that decides usability.
        slow = sum(1 for b in bps if b < 150_000)
        print(f"  below 150 KB/s (too slow to render): {slow}/{len(good)}")

    if args.out:
        keep = [r["proxy"] for r in good if r["bps"] >= (args.min_bps or 0)]
        if keep:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text("\n".join(keep) + "\n")
            print(f"\n-> {args.out}  ({len(keep)} fast exits)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
