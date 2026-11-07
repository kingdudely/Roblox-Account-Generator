"""Cut the symbol out of an Arkose key card.

The key card shows the target as a sticker on a die face, then `x N` beside it,
then a caption. Handing that whole card to a vision model invites the worst
possible failure: the model reads `x 3` and answers **3** for every tile.

That is not hypothetical. One measured round had N=3 and returned
`[0,3,3,0,3,3,0,0,3,0]` — five tiles "counted" exactly the number printed on the
reference. The counter has to be shown the *symbol only*, with the number out of
frame, so the answer cannot be anchored.

The sticker is the largest bright, almost-grey blob in the card: the same rule that
separates it from the sand in `tools/symbol_match.py`, where `value > 170` and
`saturation < 60` left exactly the sticker plus the tiny glyphs.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np
from PIL import Image

__all__ = ["symbol_crop", "describe"]


def symbol_crop(rgb: np.ndarray, pad: float = 0.18) -> Image.Image | None:
    """The sticker alone, at native resolution, or None if it cannot be found."""
    if rgb.ndim != 3 or rgb.shape[2] != 3:
        return None
    h, w = rgb.shape[:2]
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)

    mask = ((gray > 150) & (hsv[:, :, 1] < 80)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    best: tuple[int, int, int, int, int] | None = None
    for i in range(1, n):
        x, y, bw, bh, area = (int(v) for v in stats[i])
        if area < max(150, int(0.004 * w * h)):
            continue
        if not (0.10 * w <= bw <= 0.75 * w):
            continue
        if not (0.10 * h <= bh <= 0.60 * h):
            continue
        aspect = bw / max(1, bh)
        if not (0.55 <= aspect <= 1.8):
            continue
        if best is None or area > best[4]:
            best = (x, y, bw, bh, area)

    if best is None:
        # Fall back to the geometric hint the game's own layout gives: the sticker
        # sits in the lower-middle of the card, left of the count.
        x0, y0 = int(w * 0.12), int(h * 0.55)
        x1, y1 = int(w * 0.55), int(h * 0.82)
        if x1 - x0 < 12 or y1 - y0 < 12:
            return None
        return Image.fromarray(rgb[y0:y1, x0:x1])

    x, y, bw, bh, _ = best
    px, py = int(bw * pad), int(bh * pad)
    x0, y0 = max(0, x - px), max(0, y - py)
    x1, y1 = min(w, x + bw + px), min(h, y + bh + py)
    return Image.fromarray(rgb[y0:y1, x0:x1])


def describe(rgb: np.ndarray) -> dict[str, Any]:
    """Diagnostics for one key card, so a bad crop is visible rather than silent."""
    crop = symbol_crop(rgb)
    return {"card": (int(rgb.shape[1]), int(rgb.shape[0])),
            "crop": None if crop is None else crop.size}
