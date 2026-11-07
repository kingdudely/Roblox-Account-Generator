#!/usr/bin/env python3
"""Collect dice strips and score the matcher on them, without needing labels.

`tools/solve_live.py` reports a wave as solved or not, which is one bit per
challenge and costs a mint to learn. That is the wrong feedback loop for fixing
a matcher — it makes every iteration cost network round trips.

But the puzzle carries its own ground truth. For `MatchDiceIconCount` the key
shows a symbol and a count `N`, and exactly one of the ten candidates is supposed
to contain that symbol `N` times. So a correct matcher produces **exactly one**
hit almost every time. Zero hits means the plate detector or the threshold missed;
several hits means it is over-matching. Neither needs a human to label anything.

That makes this a fast loop: harvest strips once (a few minutes), then iterate on
`roblox_creator/dicecount.py` in seconds against the saved files.

    ./tools/strip_harvest.py --proxy-file proxies2.txt --grants 12
    ./tools/strip_harvest.py --evaluate debug/strips/20261101-150000
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

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools.budget_recovery import fetch, mint

OUT = ROOT / "debug" / "strips"


def log(message: str) -> None:
    print(message, flush=True)


def grab(ctx: dict) -> bytes | None:
    """Fetch wave 0 and return the JPEG bytes, or None when the grant was refused."""
    url = ctx["url"]
    try:
        resp = ctx["session"].get(url, headers={
            "user-agent": ctx["ua"],
            "referer": ctx["referer"],
            "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "sec-fetch-dest": "image",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-site": "same-origin",
        }, timeout_seconds=25)
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    data = resp.content
    if data[:3] != b"\xff\xd8\xff":
        return None
    return data


def harvest(proxy_file: Path, grants: int, max_mints: int) -> Path:
    proxies = load_proxies(proxy_file)
    random.shuffle(proxies)
    if not proxies:
        raise SystemExit("no proxies")

    target = OUT / time.strftime("%Y%m%d-%H%M%S")
    target.mkdir(parents=True, exist_ok=True)

    exit_index = 0
    parsd = parse_proxy(proxies[exit_index])
    proxy_url = parsd.url()
    log(f"exit {parsd.masked()}")

    got = 0
    for attempt in range(1, max_mints + 1):
        if got >= grants:
            break
        try:
            ctx = mint(proxy_url)
        except Exception as exc:
            log(f"  mint {attempt}: error {type(exc).__name__}")
            ctx = None
        if ctx is None:
            # A run of mint failures is what image_lottery.py saw at the end of an
            # exit's useful life. Move to the next exit rather than burning the loop.
            log(f"  mint {attempt}: failed, rotating exit")
            exit_index += 1
            if exit_index >= len(proxies):
                break
            parsd = parse_proxy(proxies[exit_index])
            proxy_url = parsd.url()
            log(f"exit {parsd.masked()}")
            continue

        blob = grab(ctx)
        if blob is None:
            log(f"  mint {attempt}: no grant")
            continue

        got += 1
        path = target / f"wave_{got:03}.jpg"
        path.write_bytes(blob)
        log(f"  mint {attempt}: saved {path.name} ({len(blob)} bytes)")

    log(f"\n{got} strips in {target}")
    return target


def evaluate(directory: Path) -> int:
    import numpy as np

    from roblox_creator.dicecount import solve
    from tools.wave_montage import split_strip

    strips = sorted(directory.glob("*.jpg"))
    if not strips:
        log(f"no strips in {directory}")
        return 1

    rows = []
    unique = zero = multi = unreadable = 0
    for strip in strips:
        try:
            key, tiles = split_strip(strip)
        except Exception as exc:
            log(f"  {strip.name}: unreadable ({exc})")
            unreadable += 1
            continue
        key_arr = np.array(key.convert("RGB"))[:, :, ::-1].copy()
        tile_arrs = [np.array(t.convert("RGB"))[:, :, ::-1].copy() for t in tiles]
        result = solve(key_arr, tile_arrs)
        hits = result.get("candidates") or []
        if result.get("n") is None:
            verdict = "no-n"
            unreadable += 1
        elif len(hits) == 1:
            verdict = "unique"
            unique += 1
        elif not hits:
            verdict = "none"
            zero += 1
        else:
            verdict = "multi"
            multi += 1
        rows.append({"strip": strip.name, "n": result.get("n"),
                     "n_conf": result.get("n_conf"), "counts": result.get("counts"),
                     "verdict": verdict, "answer": result.get("answer")})
        log(f"  {strip.name}  n={result.get('n')} ({result.get('n_conf')})"
            f"  counts={result.get('counts')}  -> {verdict}")

    total = len(strips)
    log("\n--- matcher verdict ---")
    log(f"  exactly one hit : {unique}/{total}")
    log(f"  no hit          : {zero}/{total}")
    log(f"  several hits    : {multi}/{total}")
    log(f"  unreadable      : {unreadable}/{total}")
    (directory / "eval.json").write_text(json.dumps(rows, indent=1))
    log(f"\nsaved {directory / 'eval.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies2.txt")
    ap.add_argument("--grants", type=int, default=10,
                    help="stop after this many strips")
    ap.add_argument("--max-mints", type=int, default=40,
                    help="mints to spend trying (grant rate is ~50%%)")
    ap.add_argument("--evaluate", type=Path, default=None)
    args = ap.parse_args()

    if args.evaluate is not None:
        return evaluate(args.evaluate)
    directory = harvest(args.proxy_file, args.grants, args.max_mints)
    return evaluate(directory)


if __name__ == "__main__":
    raise SystemExit(main())
