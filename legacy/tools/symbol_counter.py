#!/usr/bin/env python3
"""Count dice symbols with template matching — no model, no network.

The puzzle needs one skill: how many dice faces in a candidate show the key
symbol. Both the key and the candidates come from the same sprite set, so the
answer is a correlation problem, not a reasoning problem.

Approach per image:
  1. threshold for bright, low-saturation "plates" (the decal each symbol sits on)
  2. take each plate's min-area quadrilateral, warp it to a square
  3. match squares against a bank of known symbols by rotation-swept correlation

The bank is built from the key crops themselves — every wave hands us one free,
already-labelled template.

    ./tools/symbol_counter.py --wave debug/vision/20261029-084809-unknown-1
    ./tools/symbol_counter.py --all --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]

SIZE = 40           # normalised plate edge
ROTS = range(0, 360, 15)


def plates(path: Path, min_area: int = 60, max_area: int = 3000):
    """Bright, low-saturation blobs with a quad-ish shape -> warped squares."""
    img = cv2.imread(str(path))
    if img is None:
        return []
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    # symbols sit on near-white decals: bright and low saturation
    mask = ((gray > 135) & (hsv[:, :, 1] < 110)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    out = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        a = cv2.contourArea(c)
        if not (min_area <= a <= max_area):
            continue
        rect = cv2.minAreaRect(c)
        (cx, cy), (w, h), ang = rect
        if min(w, h) < 8:
            continue
        box = cv2.boxPoints(rect).astype(np.float32)
        # order corners and warp to a canonical square
        s = SIZE
        dst = np.array([[0, 0], [s - 1, 0], [s - 1, s - 1], [0, s - 1]], np.float32)
        M = cv2.getPerspectiveTransform(_order(box), dst)
        warp = cv2.warpPerspective(img, M, (s, s))
        out.append({"rect": rect, "warp": warp, "area": a, "angle": ang})
    return out


def _order(pts: np.ndarray) -> np.ndarray:
    """TL, TR, BR, BL ordering for a perspective warp."""
    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)],
                     pts[np.argmax(s)], pts[np.argmax(d)]], np.float32)


def normalise(img: np.ndarray) -> np.ndarray:
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).astype(np.float32)
    g -= g.mean()
    g /= (g.std() + 1e-6)
    return g


def best_score(a: np.ndarray, b: np.ndarray) -> float:
    """Max normalised correlation of `a` against `b` over a rotation sweep."""
    A = normalise(a)
    best = -1.0
    h, w = b.shape[:2]
    for ang in ROTS:
        M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, 1.0)
        rotated = cv2.warpAffine(b, M, (w, h), flags=cv2.INTER_LINEAR,
                                 borderMode=cv2.BORDER_REPLICATE)
        B = normalise(rotated)
        score = float((A * B).mean())
        if score > best:
            best = score
    return best


def key_template(wave: Path):
    """The key image holds one symbol plus 'x N'; the symbol is the plate that
    sits furthest left, since the count is drawn to its right."""
    ps = plates(wave / "key.img", min_area=120)
    if not ps:
        return None
    ps.sort(key=lambda p: p["rect"][0][0])
    return ps[0]


def solve(wave: Path, verbose: bool = False) -> dict:
    tpl = key_template(wave)
    if tpl is None:
        return {"wave": wave.name, "answer": None, "error": "no key plate"}
    counts = []
    scores = []
    for i in range(1, 11):
        p = wave / f"cand{i}.img"
        if not p.exists():
            continue
        hits = 0
        best = 0.0
        for plate in plates(p):
            s = best_score(tpl["warp"], plate["warp"])
            best = max(best, s)
            if s >= 0.55:
                hits += 1
        counts.append(hits)
        scores.append(round(best, 2))
        if verbose:
            print(f"    cand{i}: count={hits} best={best:.2f}")
    return {"wave": wave.name, "counts": counts, "scores": scores}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=Path, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--base", type=Path, default=ROOT / "debug" / "vision")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    waves = [args.wave] if args.wave else (
        sorted(d for d in args.base.iterdir()
               if d.is_dir() and (d / "key.img").exists()) if args.all else [])
    for w in waves:
        r = solve(w, args.verbose)
        print(f"  {w.name:32} counts={r.get('counts')} best={r.get('scores')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
