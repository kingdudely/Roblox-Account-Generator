#!/usr/bin/env python3
"""Compare presentations of one strip against the same question.

The rule text is no longer a guess: a live board on 2026-11-01 read

    Use the Arrows to match the shadow of the icon shown on the left.

so the reference card holds a lit icon (or two) and every candidate holds a
*shadow* of some object. The answer is the shadow that matches. What is not known
is which presentation lets the model actually see that: the source pixels are only
200 px per tile, and every vision API resizes what it is handed.

This runs several presentations of the same strip through the same prompt and
prints what each one answered and how it reasoned. Agreement is not proof of
correctness, but a run where every presentation names the same objects and picks
the same tile is a run where the pixels arrived. A run where the answers scatter
is a run where the model is guessing, and no prompt will fix that.

    ./tools/present_ab.py --strip debug/browser_solver/20261101-195547/strip_01.jpg \
        --rule "Use the Arrows to match the shadow of the icon shown on the left."
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageFilter, ImageOps

from roblox_creator.captcha_vision import solve
from roblox_creator.strip_montage import (CAND_H, TILE_W, candidates_image,
                                          candidate_count, target_image)

OUT = Path("/tmp/opencode/present")


def _sharpen(image: Image.Image, percent: int = 120) -> Image.Image:
    return image.filter(ImageFilter.UnsharpMask(radius=2, percent=percent,
                                                threshold=2))


def card_crop(strip: Image.Image) -> Image.Image:
    """The reference card, trimmed to the glyphs, upscaled until they are legible."""
    card = strip.crop((0, CAND_H + 10, 150, strip.height)).convert("RGB")
    # Trim the black filler on the right and the brick margin, keep the glyphs.
    card = ImageOps.crop(card, border=0)
    return _sharpen(card.resize((card.width * 7, card.height * 7),
                                Image.LANCZOS), 150)


def grid_crop(strip: Image.Image, cell: int, cols: int) -> Image.Image:
    return candidates_image(strip, cols=cols, cell=cell)


def side_by_side(strip: Image.Image, cell: int = 300, cols: int = 5,
                 gap: int = 6) -> Image.Image:
    """Card on the left, candidate grid to its right, one canvas.

    The card is scaled so its height matches the grid's, which keeps the two at a
    comparable feature size instead of one dominating the frame.
    """
    grid = grid_crop(strip, cell, cols)
    card = card_crop(strip)
    target_h = grid.height
    ratio = target_h / card.height
    card = card.resize((max(1, int(card.width * ratio)), target_h), Image.LANCZOS)

    canvas = Image.new("RGB", (gap + card.width + gap + grid.width + gap,
                               target_h + 2 * gap), (255, 255, 255))
    canvas.paste(card, (gap, gap))
    canvas.paste(grid, (gap + card.width + gap, gap))
    return canvas


def big_tiles(strip: Image.Image, cell: int = 500, cols: int = 3) -> Image.Image:
    """Few columns, large cells: four rows of three, ~1500 px wide."""
    return candidates_image(strip, cols=cols, cell=cell)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strip", type=Path, required=True)
    ap.add_argument("--rule", default="Use the Arrows to match the shadow of the "
                                     "icon shown on the left.")
    ap.add_argument("--provider", default="lithosai")
    ap.add_argument("--max-edge", type=int, default=1568)
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    strip = Image.open(args.strip).convert("RGB")
    total = candidate_count(strip)
    print(f"{args.strip.name} {strip.size} tiles={total}")
    print(f"rule: {args.rule}\n")

    variants: dict[str, list[Image.Image]] = {
        "split": [target_image(strip), grid_crop(strip, 300, 5)],
        "side": [side_by_side(strip)],
        "big": [big_tiles(strip)],
        "split-big": [card_crop(strip), big_tiles(strip)],
    }

    for name, pictures in variants.items():
        if args.only and args.only != name:
            continue
        for index, picture in enumerate(pictures):
            picture.save(OUT / f"{name}_{index}.jpg", quality=95)
        sizes = " + ".join(f"{p.width}x{p.height}" for p in pictures)
        started = time.time()
        result = solve(images=pictures, provider=args.provider,
                       max_edge=args.max_edge, instruction=args.rule,
                       count=total)
        print(f"=== {name}  [{sizes}]  -> {result['answer']}"
              f"  ({result['status']}, {time.time() - started:.1f}s)")
        print(result["text"].strip() or "(empty)")
        print()

    print(f"images in {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
