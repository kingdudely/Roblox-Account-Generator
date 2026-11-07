"""Deterministic `MatchDiceIconCount` solver — no model, no network, no uploads.

The puzzle is one sentence long: *the key shows a symbol and a count N; pick the
tile where that symbol appears exactly N times.* `tools/symbol_counter.py`
already counts symbol occurrences by correlating the key's plate against every
plate in a candidate. It was missing the other half — reading N.

Reading N
---------
The key sprite is a symbol plus `xN` text. Thresholding bright, low-saturation
pixels and taking **the rightmost small glyph** lands on the digit every time,
because the `x` always sits to its left. Verified by eye on twelve captured keys
that read `2,1,0,0,3,2,1,4,1,1,2,2`. Note `x0` is legal: the answer is then the
tile with *no* occurrences.

Classification is nearest-neighbour over normalised glyph crops in
`data/digits.npz` (`tools/digit_bank.py --build`). Digits are upright and always
the same face, so no rotation sweep is needed.

Why this instead of a VLM
-------------------------
A vision call costs one upload, one conversation and 17-35 s per wave. At 50k
accounts/day that is ~292k solves/day — an anonymous service cannot carry that,
regardless of accuracy. Counting is not a reasoning problem; it is segmentation
plus correlation. This runs in milliseconds and has no per-wave cost.

Interface
---------
    read_target(key_image)            -> (n, confidence) | (None, conf)
    count_tiles(key_image, tiles)     -> list[int]
    solve(key_image, tiles)           -> {answer, n, counts, ...}

`answer` is the **1-based tile label** (what the montage shows) or None. The
wire index convention is the caller's problem — see docs/SOLVER.md.
"""

from __future__ import annotations

import base64
import io
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

import cv2
import numpy as np

__all__ = ["read_target", "count_tiles", "solve", "plates_from_image", "GLYPH_W", "GLYPH_H"]

BANK = Path(__file__).resolve().parents[1] / "data" / "digits.npz"

GLYPH_W, GLYPH_H = 16, 24

# A plate is a near-white decal each symbol sits on. Same thresholds as
# `symbol_counter.plates`, which is the calibrated pair.
PLATE_MIN_AREA, PLATE_MAX_AREA = 60, 3000
MATCH_THRESHOLD = 0.55


# ------------------------------------------------------------------- glyphs


def _glyph_mask(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = ((gray > 150) & (hsv[:, :, 1] < 90)).astype(np.uint8) * 255
    return cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))


def _small_glyphs(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Text-sized components: thin, tall, and far too small to be a symbol plate."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(_glyph_mask(img), 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if 6 <= w <= 26 and 8 <= h <= 30 and area >= 30:
            out.append((int(x), int(y), int(w), int(h)))
    return out


def digit_crop(key_img: np.ndarray) -> np.ndarray | None:
    """The `N` of `xN` — the rightmost small glyph in the key band."""
    glyphs = _small_glyphs(key_img)
    if not glyphs:
        return None
    x, y, w, h = max(glyphs, key=lambda g: g[0] + g[2])
    crop = key_img[y:y + h, x:x + w]
    if crop.size == 0:
        return None
    return cv2.resize(crop, (GLYPH_W, GLYPH_H), interpolation=cv2.INTER_LINEAR)


def _normalise(gray: np.ndarray) -> np.ndarray:
    g = gray.astype(np.float32)
    g -= g.mean()
    return g / (g.std() + 1e-6)


def _glyph_vector(crop: np.ndarray) -> np.ndarray:
    return _normalise(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)).ravel()


@lru_cache(maxsize=1)
def _bank() -> tuple[np.ndarray, np.ndarray] | None:
    if not BANK.exists():
        return None
    data = np.load(BANK, allow_pickle=False)
    return data["vectors"], data["labels"]


def read_target(key_img: np.ndarray) -> tuple[int | None, float]:
    """Read N from the key sprite. Returns (None, score) when unsure."""
    crop = digit_crop(key_img)
    if crop is None:
        return None, 0.0
    bank = _bank()
    if bank is None:
        return None, 0.0
    vectors, labels = bank
    v = _glyph_vector(crop)
    # cosine-style score; vectors are already mean-centred and unit-scaled
    scores = (vectors @ v) / len(v)
    best = int(np.argmax(scores))
    score = float(scores[best])
    if math.isnan(score) or score < 0.55:
        return None, score
    return int(labels[best]), score


# ------------------------------------------------------------------- plates


def plates_from_image(img: np.ndarray) -> list[dict[str, Any]]:
    """Bright low-saturation blobs, each warped to a canonical square."""
    from tools.symbol_counter import _order, SIZE

    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = ((gray > 135) & (hsv[:, :, 1] < 110)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    out = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        area = cv2.contourArea(c)
        if not (PLATE_MIN_AREA <= area <= PLATE_MAX_AREA):
            continue
        rect = cv2.minAreaRect(c)
        (_, _), (w, h), _ = rect
        if min(w, h) < 8:
            continue
        box = cv2.boxPoints(rect).astype(np.float32)
        dst = np.array([[0, 0], [SIZE - 1, 0], [SIZE - 1, SIZE - 1], [0, SIZE - 1]], np.float32)
        M = cv2.getPerspectiveTransform(_order(box), dst)
        out.append({"rect": rect, "area": area,
                    "warp": cv2.warpPerspective(img, M, (SIZE, SIZE))})
    return out


def _key_plate(key_img: np.ndarray) -> dict | None:
    """The symbol is the plate furthest left; the count is drawn to its right."""
    ps = [p for p in plates_from_image(key_img)
          if p["area"] >= 120 or max(p["rect"][1]) >= 30]
    if not ps:
        return None
    ps.sort(key=lambda p: p["rect"][0][0])
    return ps[0]


def count_tiles(key_img: np.ndarray, tiles: list[np.ndarray],
                threshold: float = MATCH_THRESHOLD) -> tuple[list[int], list[float]]:
    from tools.symbol_counter import best_score

    tpl = _key_plate(key_img)
    if tpl is None:
        return [], []
    counts, scores = [], []
    for tile in tiles:
        hits, best = 0, 0.0
        for plate in plates_from_image(tile):
            s = best_score(tpl["warp"], plate["warp"])
            best = max(best, s)
            if s >= threshold:
                hits += 1
        counts.append(hits)
        scores.append(round(best, 2))
    return counts, scores


def solve(key_img: np.ndarray, tiles: list[np.ndarray],
          threshold: float = MATCH_THRESHOLD) -> dict:
    """Return the matching tile as a 1-based label, with everything that decided it."""
    n, n_conf = read_target(key_img)
    counts, scores = count_tiles(key_img, tiles, threshold)
    out: dict[str, Any] = {"n": n, "n_conf": round(n_conf, 2),
                           "counts": counts, "scores": scores, "answer": None}
    if n is None or not counts:
        out["error"] = "no target" if n is None else "no key plate"
        return out
    hits = [i + 1 for i, c in enumerate(counts) if c == n]
    out["candidates"] = hits
    if len(hits) == 1:
        out["answer"] = hits[0]
        out["confident"] = True
    elif hits:
        # several tiles share the count. For N>0 pick the one whose best plate
        # matched hardest (most confident). For N==0 pick the one whose best
        # plate matched weakest (least likely to contain the symbol).
        if n == 0:
            best = min(hits, key=lambda i: scores[i - 1])
        else:
            best = max(hits, key=lambda i: scores[i - 1])
        out["answer"] = best
        out["confident"] = False
        out["ambiguous"] = hits
    else:
        out["error"] = "no tile matches n"
    return out
