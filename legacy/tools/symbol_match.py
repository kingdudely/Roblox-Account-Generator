#!/usr/bin/env python3
"""Count the key symbol with multi-scale template matching, not decal detection.

`tools/symbol_counter.py` finds bright "plates", warps each to a square and
correlates. It fails on real captures because adjacent decals merge into elongated
blobs, so the warped template stops matching and counts collapse to 0-1 when the
truth is 4.

This drops the plate assumption entirely. The key symbol is a sticker on a cube
face, drawn in essentially one style; the same symbol in a candidate tile appears at
a similar size and a small set of angles. So correlate the key crop directly over
the tile at several scales and rotations, keep peaks above a threshold, and suppress
overlapping ones. No segmentation, no warping, nothing that can merge.

The metric to trust is **self-consistency**: Arkose guarantees exactly one tile shows
exactly N. So for each captured wave, count every tile and ask whether exactly one
tile equals N. It needs no ground truth and it is exactly what the game requires.

    ./tools/symbol_match.py --self-check
    ./tools/symbol_match.py --wave /tmp/opencode/gate
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import dicecount  # noqa: E402
from tools.wave_montage import split_strip  # noqa: E402

SCALES = (0.75, 0.9, 1.0, 1.1, 1.25)
ANGLES = (-20, -10, 0, 10, 20)
THRESHOLD = 0.62
NMS_PX = 9


def key_template(key_img: np.ndarray) -> np.ndarray | None:
    """The symbol itself: the plate furthest left, at native resolution.

    The count text sits to its right, so the leftmost bright blob is the symbol.
    Keeping it at native size matters — rescaling first blurs the sticker edges
    that make the match sharp.
    """
    # The sticker is bright and *almost grey*; the desert background it sits on is
    # bright but strongly orange. The earlier mask (value 135, saturation 110)
    # admitted the ground, so "leftmost bright blob" returned sand and every match
    # scored under threshold. Tightening to saturation < 60 / value > 170 leaves
    # exactly three components in the band: the sticker, the "x", and the digit.
    hsv = cv2.cvtColor(key_img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(key_img, cv2.COLOR_BGR2GRAY)
    mask = ((gray > 170) & (hsv[:, :, 1] < 60)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    best = None
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < 200 or not (20 <= w <= 50) or not (15 <= h <= 45):
            continue
        if best is None or area > best[0]:
            best = (area, x, y, w, h)
    if best is None:
        return None
    _, x, y, w, h = best
    return key_img[y:y + h, x:x + w]


def peaks(tile: np.ndarray, template: np.ndarray,
          threshold: float = THRESHOLD) -> list[tuple[float, int, int]]:
    """All template matches above threshold, across scale and rotation."""
    found: list[tuple[float, int, int]] = []
    th, tw = template.shape[:2]
    for angle in ANGLES:
        rot = template
        if angle:
            m = cv2.getRotationMatrix2D((tw / 2, th / 2), angle, 1.0)
            rot = cv2.warpAffine(template, m, (tw, th), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE)
        for scale in SCALES:
            rw, rh = int(tw * scale), int(th * scale)
            if rw >= tile.shape[1] or rh >= tile.shape[0] or rw < 8 or rh < 8:
                continue
            t = cv2.resize(rot, (rw, rh), interpolation=cv2.INTER_AREA)
            res = cv2.matchTemplate(tile, t, cv2.TM_CCOEFF_NORMED)
            loc = np.where(res >= threshold)
            for y, x in zip(*loc):
                found.append((float(res[y, x]), int(x), int(y)))
    return found


def suppress(found: list[tuple[float, int, int]], radius: int = NMS_PX) -> list:
    """Greedy non-maximum suppression in image space, across scales."""
    kept: list[tuple[float, int, int]] = []
    for score, x, y in sorted(found, reverse=True):
        if all(abs(x - kx) > radius or abs(y - ky) > radius
               for _, kx, ky in kept):
            kept.append((score, x, y))
    return kept


def count_tile(tile: np.ndarray, template: np.ndarray,
               threshold: float = THRESHOLD) -> tuple[int, float]:
    kept = suppress(peaks(tile, template, threshold))
    return len(kept), (kept[0][0] if kept else 0.0)


def count_all(strip: Path, threshold: float = THRESHOLD,
              verbose: bool = False) -> dict:
    key_img, tiles = split_strip(strip)

    def bgr(img):
        return np.array(img)[:, :, ::-1].copy()

    key = bgr(key_img)
    tpl = key_template(key)
    if tpl is None:
        return {"strip": strip.name, "error": "no template"}
    n, n_conf = dicecount.read_target(key)
    counts, scores = [], []
    for i, tile in enumerate(tiles, 1):
        c, s = count_tile(bgr(tile), tpl, threshold)
        counts.append(c)
        scores.append(round(s, 2))
        if verbose:
            print(f"    tile {i:2}: count={c} best={s:.2f}")
    hits = [i + 1 for i, c in enumerate(counts) if c == n]
    return {"strip": strip.name, "n": n, "n_conf": round(n_conf, 2),
            "counts": counts, "scores": scores, "hits": hits,
            "confident": len(hits) == 1}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--wave", type=Path, default=None)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.wave:
        print(count_all(args.wave / "strip_00.jpg", args.threshold, args.verbose))
        return 0

    strips = (sorted(Path("debug/live").glob("*/strip*.jpg"))
              + sorted(Path("debug/ca").glob("*/wave*.jpg"))
              + sorted(Path("debug/pure").glob("*/wave0.jpg"))
              + sorted(Path("/tmp/opencode/gate").glob("strip_*.jpg")))
    uniq = total = 0
    for strip in strips:
        r = count_all(strip, args.threshold)
        if r.get("error"):
            continue
        total += 1
        uniq += bool(r["confident"])
        print(f"  {str(strip.parents[1].name if 'gate' in str(strip) else strip.parent.name)[:18]:18}"
              f" {strip.name:12} n={r['n']} counts={r['counts']} "
              f"{'OK' if r['confident'] else 'not-unique'}")
    print(f"\n  {uniq}/{total} waves had exactly one tile matching N "
          f"(threshold {args.threshold})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
