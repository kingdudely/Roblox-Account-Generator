#!/usr/bin/env python3
"""Try presentations of one known strip until the model stops guessing.

Wave 1 of `debug/browser_solver/20261101-190344/` is labelled **3** by hand: the
panel where the character is standing on the target object. Scored through the
current grid it answers 2. So this prints, for each presentation, what the model
says and why — the "why" is the whole point, because a wrong answer with the right
reasoning means a presentation bug and a wrong answer with confused reasoning means
the pixels never made it.

    ./tools/present_probe.py --case debug/browser_solver/20261101-190344 --wave 1

Presentations:

    grid      target card then candidates, one row, plain white gaps
    bigkey    target card blown up on its own, then the candidates
    deux      two images: the target alone, then the candidate row
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image

from roblox_creator.captcha_vision import GRID_PROMPT, solve
from roblox_creator.strip_montage import _fit, build, key_width, segments

PROMPT = """You are solving a web CAPTCHA made of a target and five candidate scenes.

The image has a white border and thin white gaps between panels.

- Panel A (leftmost, on its own) is the TARGET ICON. It is not an answer.
- Panels 1-5 are candidate scenes, left to right.

Every candidate scene contains the same objects in different arrangements, and a
small character standing somewhere. One scene differs in the relation the task
cares about.

Look at where the character's feet are in each scene, and which object they are on
or beside. Then pick the candidate number that satisfies the task relative to the
target icon.

Think briefly. Then end with exactly one line:

ANSWER=<number>"""


def bigkey(strip: Image.Image, max_edge: int = 2000) -> Image.Image:
    """Target card at full size, candidates underneath, single image."""
    key, cands = segments(strip)
    kw = key_width(strip)
    key = strip.crop((0, 190, kw, strip.height))
    cells = [key] + cands
    cell = max(150, (max_edge - 6 * len(cells)) // len(cells))
    w = cell * len(cells) + 6 * (len(cells) + 1)
    h = cell + 12
    canvas = Image.new("RGB", (w, h), (255, 255, 255))
    for i, piece in enumerate(cells):
        t = _fit(piece, cell)
        canvas.paste(t, (6 + i * (cell + 6) + (cell - t.width) // 2,
                         6 + (cell - t.height) // 2))
    return canvas


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=Path, required=True)
    ap.add_argument("--wave", type=int, required=True)
    ap.add_argument("--truth", type=int, default=0)
    ap.add_argument("--provider", default="lithosai")
    args = ap.parse_args()

    path = args.case / f"strip_{args.wave:02d}.jpg"
    strip = Image.open(path).convert("RGB")

    variants = {
        "grid": build(strip, max_edge=2000),
        "bigkey": bigkey(strip, max_edge=2000),
    }
    for name, image in variants.items():
        image.save(f"/tmp/opencode/present_{name}.jpg", quality=95)
        result = solve(image, provider=args.provider, max_edge=2000,
                       prompt=PROMPT)
        print(f"\n=== {name}  {image.size}  -> {result['answer']}"
              f"{'  OK' if result['answer'] == args.truth else ''}")
        print(result["text"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
