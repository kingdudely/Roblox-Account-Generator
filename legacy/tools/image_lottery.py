#!/usr/bin/env python3
"""Is the image gate a quota on the exit, or a lottery per challenge?

`docs/RUNTIME.md` models the gate as a request budget: ~18 Arkose requests per
exit and the wall lands there, which makes a 5-wave challenge (17 requests) barely
fit and a 10-wave one (27) impossible. `docs/PROXIES.md` reads that as a supply
problem — you need virgin IPs, and free pools are spent.

A short ad-hoc run on 2026-11-01 contradicted both. On one sticky exit, six mints
in a row returned wave-0 statuses `403, 403, 403, 200, 403, 200` — roughly 50 more
requests to Arkose than the claimed wall — and a granted URL then served twice.
That is not a quota shape. A quota degrades monotonically and stays dead; this
alternates.

Two models, opposite consequences:

    quota     every account costs a virgin exit. Supply sets the ceiling.
              Retrying is wasted motion; the pool is the product.
    lottery   each mint has probability p of receiving an image grant. Retrying
              converts p into near-certainty, and one exit hosts many accounts.
              The pool is nearly free and the solver becomes the limiter.

This measures which one is true, and — because the two differ exactly in whether
cumulative requests matter — it records the running request count against the
outcome. If p is flat as requests accumulate, it is a lottery. If p collapses, it
is a quota and RUNTIME.md was right.

    ./tools/image_lottery.py --proxy-file proxies2.txt --exits 3 --mints 8
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools.budget_recovery import fetch, identity, mint

OUT = ROOT / "debug" / "lottery"

# One mint costs seven Arkose requests before its first image: gt2, five proof of
# work calls, gfct. Recorded here rather than inferred, because the whole quota
# argument rests on it.
REQUESTS_PER_MINT = 7


def log(message: str) -> None:
    print(message, flush=True)


def run_exit(raw: str, mints: int) -> dict:
    parsed = parse_proxy(raw)
    proxy_url = parsed.url()
    row = {"exit": parsed.masked(), "ip": identity(proxy_url),
           "mints": [], "granted": 0, "attempted": 0}
    log(f"\nexit {parsed.masked()}  ip={row['ip']}")

    for index in range(mints):
        cumulative = (index + 1) * REQUESTS_PER_MINT
        try:
            ctx = mint(proxy_url)
        except Exception as exc:
            row["mints"].append({"n": index, "mint": False,
                                 "error": type(exc).__name__,
                                 "cumulative_requests": cumulative})
            log(f"  mint {index}: error {type(exc).__name__}")
            continue

        if ctx is None:
            row["mints"].append({"n": index, "mint": False,
                                 "cumulative_requests": cumulative})
            log(f"  mint {index}: failed (requests ~{cumulative})")
            continue

        first = fetch(ctx, 0)
        second = fetch(ctx, 0)
        granted = first == 200
        row["attempted"] += 1
        if granted:
            row["granted"] += 1
        row["mints"].append({"n": index, "mint": True, "status": first,
                             "repeat": second, "waves": ctx["waves"],
                             "cumulative_requests": cumulative})
        log(f"  mint {index}: {first} (again {second})  waves={ctx['waves']}"
            f"  requests~{cumulative}")
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, action="append", default=None)
    ap.add_argument("--exits", type=int, default=3)
    ap.add_argument("--mints", type=int, default=8)
    ap.add_argument("--workers", type=int, default=1,
                    help="exits at once; 1 keeps the request accounting honest")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    files = args.proxy_file or [ROOT / "proxies2.txt"]
    proxies: list[str] = []
    for path in files:
        proxies.extend(load_proxies(path))
    random.shuffle(proxies)
    picked = proxies[:args.exits]
    if not picked:
        log("no proxies")
        return 1

    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(pool.map(lambda raw: run_exit(raw, args.mints), picked))

    granted = sum(row["granted"] for row in rows)
    attempted = sum(row["attempted"] for row in rows)
    rate = (granted / attempted) if attempted else 0.0

    OUT.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = args.out or (OUT / f"{stamp}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({
        "stamp": stamp, "grant_rate": round(rate, 3),
        "granted": granted, "attempted": attempted,
        "elapsed_minutes": round((time.time() - started) / 60, 1),
        "rows": rows,
    }, indent=1))

    log("\n--- grant rate ---")
    for row in rows:
        marks = " ".join(str(m.get("status", "x")) for m in row["mints"])
        log(f"  {row['granted']}/{row['attempted']}  {marks}  {row['ip']}")
    log(f"\noverall: {granted}/{attempted} = {rate:.0%} per mint"
        f"  (saved {target})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
