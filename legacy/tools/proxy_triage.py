#!/usr/bin/env python3
"""Separate the three reasons a proxy pool can look dead.

A pool that answers one request at a time but fails in parallel has three
possible causes, and they need different fixes:

  * dead exits            -> the upstream IP is gone
  * per-session limit     -> one session id cannot carry two connections
  * per-account limit     -> the order has a hard concurrency cap
  * allocation rate limit -> new sessions are throttled per second

This runs the four experiments that tell them apart, through the same client
the creator uses, so the result is about the pool and not about curl.

    ./tools/proxy_triage.py --file proxies.txt
    ./tools/proxy_triage.py --file proxies1.txt --n 8
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.config import load_proxies      # noqa: E402
from roblox_creator.proxyurl import parse           # noqa: E402

TRACE = "https://www.cloudflare.com/cdn-cgi/trace"


def probe(raw: str, timeout: int = 18) -> str:
    """One request through one proxy.

    Returns the exit IP on success, or a string beginning with ``!`` on
    failure. The sentinel has to be a prefix, not "contains a colon": IPv6
    exits contain colons and were being counted as failures.
    """
    from curl_cffi import requests as creq

    p = parse(raw)
    s = creq.Session(impersonate="chrome", timeout=timeout)
    try:
        s.proxies = {"http": p.requests_url(), "https": p.requests_url()}
        r = s.get(TRACE, timeout=timeout)
        for line in r.text.splitlines():
            if line.startswith("ip="):
                return line[3:]
        return "!NOIP"
    except Exception as exc:
        name = type(exc).__name__
        # curl_cffi wraps SOCKS failures; the useful signal is the message
        msg = str(exc).lower()
        if "proxy" in msg or "socks" in msg:
            return f"!proxy:{name}"
        if "timeout" in msg:
            return f"!timeout:{name}"
        return f"!error:{name}"
    finally:
        s.close()


def run_parallel(items: list[str], stagger: float = 0.0) -> list[str]:
    def task(arg):
        idx, raw = arg
        if stagger:
            time.sleep(idx * stagger)
        return probe(raw)

    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        return list(pool.map(task, list(enumerate(items))))


def summarise(label: str, results: list[str]) -> str:
    ok = [r for r in results if not r.startswith("!")]
    fails = [r for r in results if r.startswith("!")]
    ip_note = f" ips={len(set(ok))}" if ok else ""
    detail = ""
    if fails:
        counts: dict[str, int] = {}
        for f in fails:
            counts[f] = counts.get(f, 0) + 1
        detail = "  " + " ".join(f"{k}x{v}" for k, v in sorted(counts.items(), key=lambda x: -x[1])[:3])
    print(f"  {label:34} {len(ok):>2}/{len(results):<2} alive{ip_note}{detail}")
    return f"{len(ok)}/{len(results)}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, required=True)
    ap.add_argument("--n", type=int, default=8)
    args = ap.parse_args()

    pool = load_proxies(args.file)
    if not pool:
        print(f"  no proxies parsed from {args.file}")
        return 2
    print(f"\n{args.file.name}: {len(pool)} sessions parsed")
    sample = random.sample(pool, min(args.n, len(pool)))

    # 1. baseline: one at a time, no delay
    print("\n  1. sequential, no delay")
    t0 = time.time()
    seq = [probe(p) for p in sample]
    summarise(f"sequential n={len(seq)}", seq)
    print(f"     ({time.time() - t0:.1f}s total)")

    # 2. same session twice at once: is the limit per session id?
    print("\n  2. one session, 2 concurrent")
    one = sample[0]
    same = run_parallel([one, one])
    summarise("same session x2", same)

    # 3. different sessions, staggered: allocation rate or hard cap?
    print("\n  3. different sessions, 6s stagger")
    staggered = run_parallel(sample[1:5], stagger=6.0)
    summarise("staggered x4", staggered)

    # 4. different sessions, all at once: the real ceiling
    print("\n  4. different sessions, concurrent")
    burst = run_parallel(sample[4:8])
    summarise("burst x4", burst)

    print("\n  reading:")
    print("    seq ok + burst fails        -> concurrency cap on the order")
    print("    same-session ok + burst fail-> cap is per account, not per session")
    print("    staggered ok + burst fails  -> allocation rate limit, not a hard cap")
    print("    seq fails                   -> the exits themselves are gone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
