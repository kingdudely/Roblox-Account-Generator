#!/usr/bin/env python3
"""Compare the plain strip against the rebuilt montage, on saved strips.

No browser and no labels: this runs both presentations of the same strip through
the vision solver and prints the answers side by side, plus the model's own
reasoning. When the two disagree on a strip, reading the reason is usually enough
to see which one was reached honestly — "I could not make out the symbol" is a
guess; "panel 3 shows the same glyph twice" is a solve.

    ./tools/montage_check.py --dir debug/browser_solver/20261101-190344
    ./tools/montage_check.py --dir debug/strips/20261101-154312 --save
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from roblox_creator.captcha_vision import solve
from roblox_creator.strip_montage import build


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, required=True)
    ap.add_argument("--save", action="store_true", help="write montages next to the strips")
    ap.add_argument("--provider", default="lithosai")
    args = ap.parse_args()

    strips = sorted(p for p in args.dir.glob("strip_*.jpg")) or \
        sorted(p for p in args.dir.glob("wave_*.jpg"))
    if not strips:
        print(f"no strips in {args.dir}")
        return 1

    agree = 0
    for path in strips:
        strip = Image.open(path)
        montage = build(strip)
        if args.save:
            montage.save(path.with_suffix(".montage.jpg"), quality=92)
        plain = solve(strip, provider=args.provider, max_edge=2000)
        rebuilt = solve(montage, provider=args.provider, max_edge=2000)
        same = plain["answer"] == rebuilt["answer"]
        agree += 1 if same else 0
        print(f"\n{path.name}  strip={strip.size}  montage={montage.size}")
        print(f"  plain   -> {plain['answer']}  ({plain['seconds']}s)")
        print(f"  montage -> {rebuilt['answer']}  ({rebuilt['seconds']}s)  "
              f"{'same' if same else 'DIFFERENT'}")
        print(f"    plain said:   {plain['text'][:200]!r}")
        print(f"    montage said: {rebuilt['text'][:200]!r}")
    print(f"\nagreement {agree}/{len(strips)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
