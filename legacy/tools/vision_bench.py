#!/usr/bin/env python3
"""Score a vision counter against the puzzle's own guarantee.

There is no ground truth for captured waves: `/fc/ca/` gives no per-wave
feedback and `solved` is one bit per five answers. But the game states its own
invariant — **exactly one tile shows the key symbol exactly N times** — so a
counter can be scored without labels:

    unique-hit   exactly one tile == N        the wave is answerable
    no-hit       no tile == N                 the counter missed the real tile
    multi-hit    two or more tiles == N       the counter cannot separate them

That is the metric `tools/symbol_match.py` was judged on (2/36), so results here
are directly comparable.

Three things this harness does that a naive one does not:

1. **Retries a tile on a fresh exit.** A `None` in the count vector poisons the
   whole wave, because the vector no longer has one entry per tile.
2. **Reads N twice.** `dicecount.read_target` is a pixel reader (13/14 on its
   labelled set) and the VLM is a semantic reader. When they disagree the wave is
   scored against both, so a disagreement is visible instead of silently wrong.
3. **Re-asks tied tiles.** A multi-hit is not a loss if the tie can be broken by
   asking the two tied tiles a harder question.

    ./tools/vlm_bench.py --provider ovh --limit 10
    ./tools/vlm_bench.py --provider mistral --limit 10     # needs MISTRAL_API_KEY
    ./tools/vlm_bench.py --provider ovh --census           # stronger prompt
    ./tools/vlm_bench.py --strip /tmp/opencode/gate/strip_00.jpg --verbose
"""

from __future__ import annotations

import argparse
import random
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np

from roblox_creator import dicecount
from roblox_creator.vlm_client import (PROMPT_CENSUS, PROMPT_COUNT,
                                       VisionClient)
from tools.wave_montage import split_strip

N_PROMPT = ("The image shows a key panel: a die symbol and a count written as "
            "'x N'. What is N? Reply with one integer only.")


def collect() -> list[Path]:
    seen: list[Path] = []
    for pat in ("debug/live/*/strip*.jpg", "debug/ca/*/wave*.jpg",
                "debug/pure/*/wave*.jpg"):
        seen += sorted(Path(".").glob(pat))
    gate = Path("/tmp/opencode/gate")
    if gate.is_dir():
        seen += sorted(gate.glob("strip_*.jpg"))
    uniq: dict[str, Path] = {}
    for p in seen:
        uniq[f"{p.name}:{p.stat().st_size}"] = p
    return list(uniq.values())


def read_n(client: VisionClient, key_img, verbose: bool,
           source: str = "cross") -> tuple[int | None, str]:
    """N from the pixel reader, with the VLM as an optional cross-check.

    `source="pixels"` must not call the model at all: otherwise the model's own
    reading decides which waves survive, and two providers would be scored on
    different subsets — which is the same trap as comparing arms on different
    proxy pools.
    """
    pix = None
    try:
        pix, conf = dicecount.read_target(
            np.array(key_img)[:, :, ::-1].copy())
        # 0 is not a valid required count, so a returned 0 is the reader failing
        # rather than a wave with nothing to find. Treat it as unreadable.
        if not pix or not (1 <= pix <= 6):
            pix = None
    except Exception:
        pix = None
    if source == "pixels":
        return pix, ("pixels" if pix else "none")

    text, _ = client._ask(N_PROMPT, [key_img], max_tokens=8)
    import re
    m = re.search(r"\d+", text or "")
    vlm = int(m.group()) if m else None
    if not vlm or not (1 <= vlm <= 6):
        vlm = None
    if source == "vlm":
        return vlm, ("vlm" if vlm else "none")
    if pix is not None and vlm is not None:
        if pix == vlm:
            return vlm, "agree"
        if verbose:
            print(f"      N disagreement: pixels={pix} vlm={vlm}")
        return vlm, f"disagree(pix={pix})"
    if vlm is not None:
        return vlm, "vlm"
    if pix is not None:
        return pix, "pixels"
    return None, "none"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", default="ovh")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--strip", type=Path, default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--census", action="store_true",
                    help="use the enumeration prompt for the counts")
    ap.add_argument("--reask", action="store_true",
                    help="re-ask tied tiles with the census prompt")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--n-source", choices=("pixels", "vlm", "cross"),
                    default="cross",
                    help="pixels: trust the reader only; vlm: ask the model only; "
                         "cross: require agreement, drop the wave otherwise")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    from roblox_creator.config import load_proxies
    from roblox_creator.proxyurl import parse as parse_proxy

    exits = [parse_proxy(p).url() for p in load_proxies(args.proxy_file)]
    random.shuffle(exits)
    client = VisionClient(provider=args.provider, exits=exits)

    strips = [args.strip] if args.strip else collect()
    if args.limit:
        strips = strips[:args.limit]
    print(f"  provider {args.provider} model {client.cfg.model} "
          f"keyless={client.cfg.keyless} exits={len(exits)}")
    print(f"  {len(strips)} waves\n")

    prompt = PROMPT_CENSUS if args.census else PROMPT_COUNT
    rows = []
    for i, strip in enumerate(strips, 1):
        try:
            key_img, tiles = split_strip(strip)
        except Exception as exc:
            print(f"  [{i:2}/{len(strips)}] {strip.name:16} split {type(exc).__name__}")
            continue

        n, how = read_n(client, key_img, args.verbose, args.n_source)
        if args.n_source == "pixels" and how not in ("agree", "pixels"):
            n = None
        elif args.n_source == "vlm" and how not in ("agree", "vlm"):
            n = None
        elif args.n_source == "cross" and how != "agree":
            n = None
        if n is None:
            print(f"  [{i:2}/{len(strips)}] {strip.name:16} skip N ({how})")
            continue

        counts, metas = client.count_wave_parallel(key_img, tiles,
                                                   workers=args.workers,
                                                   prompt=prompt)
        missing = sum(1 for c in counts if c is None)

        if args.reask:
            tied = [j for j, c in enumerate(counts) if c == n]
            if len(tied) > 1:
                for j in tied:
                    c2, _ = client.count(key_img, tiles[j], PROMPT_CENSUS)
                    if c2 is not None:
                        counts[j] = c2
                missing = sum(1 for c in counts if c is None)

        hits = [j + 1 for j, c in enumerate(counts) if c == n]
        verdict = ("unique" if len(hits) == 1 else
                   "none" if not hits else "multi")
        rows.append({"verdict": verdict, "missing": missing, "n": n, "how": how})
        print(f"  [{i:2}/{len(strips)}] {strip.name:16} n={n}({how:12}) "
              f"hits={str(hits):18} {verdict:6} miss={missing}")
        if args.verbose:
            print(f"      counts={counts}")

    ok = rows
    tally = Counter(r["verdict"] for r in ok)
    total = len(ok) or 1
    print(f"\n  unique-hit {tally['unique']}/{len(ok)} ({tally['unique']/total:.0%})"
          f"   no-hit {tally['none']}   multi-hit {tally['multi']}")
    miss = sum(r["missing"] for r in ok)
    print(f"  tile requests lost: {miss}/{10 * len(ok)}")
    print(f"  client stats: {client.stats}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
