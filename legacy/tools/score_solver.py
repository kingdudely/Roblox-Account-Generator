#!/usr/bin/env python3
"""Score the vision solver against hand-labelled strips.

Every other number in this repo measures the *machine*: images fetched, waves
submitted, accounts created. None of them measures whether the answer was right,
because Arkose gives no per-wave feedback — a wrong answer and a right one both
come back `not answered`. A whole challenge can look perfectly healthy and still
fail on every single wave.

This closes that gap. A human reads the montages and writes down the answers once;
this replays those strips through whichever presentation is being tested and
reports accuracy per wave.

    ./tools/score_solver.py --case debug/browser_solver/20261101-190344 \
                            --labels debug/labels/20261101-190344.json --grid

Labels are 1-based candidate numbers, left to right, as read off the montage:

    {"1": 3, "2": 2, "3": 2, ...}
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from roblox_creator.captcha_vision import GRID_PROMPT, solve
from roblox_creator.strip_montage import build


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=Path, required=True)
    ap.add_argument("--labels", type=Path, required=True)
    ap.add_argument("--grid", action="store_true", help="test the rebuilt grid")
    ap.add_argument("--save", action="store_true", help="write the grids out")
    ap.add_argument("--provider", default="lithosai")
    ap.add_argument("--max-edge", type=int, default=2000)
    args = ap.parse_args()

    raw = json.loads(args.labels.read_text())
    labels = {str(k): int(v) for k, v in raw.items()
              if not str(k).startswith("_") and str(v).isdigit()}
    strips = sorted(args.case.glob("strip_*.jpg"))
    if not strips:
        print(f"no strips in {args.case}")
        return 1

    right = 0
    scored = 0
    out_dir = args.case / "grid"
    if args.save:
        out_dir.mkdir(exist_ok=True)

    for path in strips:
        number = str(int(path.stem.split("_")[1]))
        truth = labels.get(number)
        if truth is None:
            continue
        image = Image.open(path)
        if args.grid:
            image = build(image, max_edge=args.max_edge)
            if args.save:
                image.save(out_dir / f"grid_{int(number):02d}.jpg", quality=94)
        result = solve(image, provider=args.provider, max_edge=args.max_edge,
                       prompt=GRID_PROMPT if args.grid else "")
        got = result["answer"]
        scored += 1
        hit = got == truth
        right += 1 if hit else 0
        print(f"wave {number:>2}: got={got} truth={truth} "
              f"{'ok' if hit else 'MISS'}  ({result['seconds']}s)")

    if scored:
        print(f"\naccuracy {right}/{scored} = {right / scored:.0%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
