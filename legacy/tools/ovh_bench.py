#!/usr/bin/env python3
"""Score a keyless VLM counter against the puzzle's own guarantee.

There is no ground truth for captured waves: `/fc/ca/` gives no per-wave
feedback, and `solved` is one bit per five answers. But the game states its own
invariant — **exactly one tile shows the key symbol exactly N times** — so a
counter can be scored without labels:

    unique-hit   exactly one tile == N        the wave is answerable
    no-hit       no tile == N                 the counter missed the real tile
    multi-hit    two or more tiles == N       the counter cannot separate them

That is the same metric `tools/symbol_match.py` was judged on, so the two are
directly comparable: template matcher 2/36, this should be far better or the
model is not worth wiring in.

One image per request, so each tile is seen at its own resolution instead of
being crushed into a montage. Requests rotate across proxy exits because the
endpoint's limit is 2 RPM *per IP*, which is the whole reason it is usable: the
pool is the quota.

    ./tools/ovh_bench.py --limit 12 --proxy-file proxies.txt
    ./tools/ovh_bench.py --strip /tmp/opencode/gate/strip_00.jpg --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import dicecount
from tools.ovh_count import ask
from tools.wave_montage import split_strip

import random


def collect() -> list[Path]:
    seen: list[Path] = []
    patterns = ("debug/live/*/strip*.jpg", "debug/ca/*/wave*.jpg",
                "debug/pure/*/wave*.jpg")
    for pat in patterns:
        seen += sorted(Path(".").glob(pat))
    # /tmp/opencode/gate is outside the repo root, so glob it absolutely
    gate = Path("/tmp/opencode/gate")
    if gate.is_dir():
        seen += sorted(gate.glob("strip_*.jpg"))
    # a wave saved twice under different names is still one wave
    uniq = {}
    for p in seen:
        uniq[p.name + str(p.stat().st_size)] = p
    return list(uniq.values())


async def score(strip: Path, proxies: list[str], verbose: bool) -> dict:
    try:
        key_img, tiles = split_strip(strip)
    except Exception as exc:
        return {"strip": str(strip), "error": f"split: {type(exc).__name__}"}

    n, conf = dicecount.read_target(
        __import__("numpy").array(key_img)[:, :, ::-1].copy())
    if not n:
        return {"strip": strip.name, "error": "no N"}

    loop = asyncio.get_running_loop()
    jobs = []
    for i, tile in enumerate(tiles):
        proxy = proxies[i % len(proxies)] if proxies else None
        jobs.append(loop.run_in_executor(None, ask, key_img, tile, proxy))
    results = await asyncio.gather(*jobs, return_exceptions=True)

    counts = []
    for r in results:
        if isinstance(r, Exception):
            counts.append(None)
        else:
            counts.append(r[0])

    known = [c for c in counts if c is not None]
    hits = [i + 1 for i, c in enumerate(counts) if c == n]
    verdict = ("unique" if len(hits) == 1 else
               "none" if not hits else "multi")
    out = {"strip": strip.name, "n": n, "conf": round(conf, 2),
           "counts": counts, "hits": hits, "verdict": verdict,
           "answered": len(known), "dist": dict(sorted(Counter(known).items()))}
    if verbose:
        print(f"    counts={counts} n={n} hits={hits} -> {verdict}")
    return out


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--strip", type=Path, default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from roblox_creator.config import load_proxies
    from roblox_creator.proxyurl import parse as parse_proxy

    proxies = [parse_proxy(p).url() for p in load_proxies(args.proxy_file)]
    random.shuffle(proxies)

    strips = [args.strip] if args.strip else collect()
    if args.limit:
        strips = strips[:args.limit]
    print(f"  {len(strips)} waves, {len(proxies)} exits")

    rows = []
    for i, s in enumerate(strips, 1):
        r = await score(s, proxies, args.verbose)
        rows.append(r)
        if r.get("error"):
            print(f"  [{i:2}/{len(strips)}] {s.name:16} {r['error']}")
        else:
            print(f"  [{i:2}/{len(strips)}] {s.name:16} n={r['n']} "
                  f"hits={r['hits']} {r['verdict']:6} "
                  f"answered={r['answered']}/10")

    ok = [r for r in rows if not r.get("error")]
    tally = Counter(r["verdict"] for r in ok)
    total = len(ok) or 1
    print(f"\n  unique-hit {tally['unique']}/{len(ok)} ({tally['unique']/total:.0%})"
          f"   no-hit {tally['none']}   multi-hit {tally['multi']}")
    print(f"  answered {sum(r['answered'] for r in ok)}/"
          f"{10 * len(ok)} tile requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
