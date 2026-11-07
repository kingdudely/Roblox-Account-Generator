#!/usr/bin/env python3
"""Build the digit template bank for `roblox_creator/dicecount.py`.

The key sprite reads `<symbol> x <N>`, and the digit is the rightmost small glyph
in the key band. The bank is a handful of labelled crops — digits are upright and
drawn in one face, so nearest-neighbour on a 16x24 normalised crop is enough.

Paths are explicit rather than globbed on purpose. Labelling by "sorted files in
this directory" breaks the moment another capture lands, and a silently shifted
label set is worse than no bank at all.

`--check` is **leave-one-out**: each sample is classified against the bank built
from the others. In-sample scoring would just re-find the crop it was built from
and report 100% for a useless bank.

    ./tools/digit_bank.py --build
    ./tools/digit_bank.py --check
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

from tools.wave_montage import KEY_BAND  # noqa: E402

BANK = ROOT / "data" / "digits.npz"
GLYPH_W, GLYPH_H = 16, 24

# Read off the key-band contact sheet; every one of these was checked by eye.
SAMPLES = [
    ("debug/live/20261030-154254/strip00.jpg", 2),
    ("debug/live/20261030-154401/strip00.jpg", 1),
    ("debug/live/20261030-163924/strip00.jpg", 0),
    ("debug/live/20261030-163924/strip01.jpg", 0),
    ("debug/ca/20261030-132458/wave00.jpg", 3),
    ("debug/ca/20261030-132458/wave01.jpg", 2),
    ("debug/ca/20261030-132630/wave00.jpg", 1),
    ("debug/ca/20261030-132630/wave01.jpg", 4),
    ("debug/ca/20261030-132630/wave02.jpg", 1),
    ("debug/ca/20261030-133805/wave00.jpg", 1),
    ("debug/ca/20261030-133805/wave01.jpg", 2),
    ("debug/ca/20261030-134013/wave00.jpg", 2),
    ("debug/pure/20261030-132006/wave0.jpg", 4),
    ("debug/pure/20261030-142309/wave0.jpg", 2),
]


def _band(path: Path) -> np.ndarray:
    from PIL import Image

    with Image.open(path) as im:
        rgb = np.array(im.convert("RGB").crop(KEY_BAND))
    return rgb[:, :, ::-1].copy()


def _digit_crop(band: np.ndarray) -> np.ndarray | None:
    """Rightmost small glyph in the key band — the digit of `xN`."""
    hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(band, cv2.COLOR_BGR2GRAY)
    mask = ((gray > 150) & (hsv[:, :, 1] < 90)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    glyphs = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if 6 <= w <= 26 and 8 <= h <= 30 and area >= 30:
            glyphs.append((int(x), int(y), int(w), int(h)))
    if not glyphs:
        return None
    x, y, w, h = max(glyphs, key=lambda g: g[0] + g[2])
    return cv2.resize(band[y:y + h, x:x + w], (GLYPH_W, GLYPH_H))


def _vector(crop: np.ndarray) -> np.ndarray:
    v = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.float32).ravel()
    v -= v.mean()
    return v / (v.std() + 1e-6)


def load_samples() -> list[tuple[str, int, np.ndarray]]:
    rows = []
    for rel, label in SAMPLES:
        path = ROOT / rel
        if not path.exists():
            print(f"  missing: {rel}")
            continue
        crop = _digit_crop(_band(path))
        if crop is None:
            print(f"  {path.name}: no digit found")
            continue
        rows.append((rel, label, _vector(crop)))
    return rows


def build(rows: list[tuple[str, int, np.ndarray]]) -> int:
    if not rows:
        return 1
    vectors = np.stack([v for _, _, v in rows])
    labels = np.array([label for _, label, _ in rows], dtype=np.int32)
    BANK.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(BANK, vectors=vectors, labels=labels)
    print(f"  {len(rows)} samples -> {BANK.relative_to(ROOT)}")
    for c in sorted(set(labels.tolist())):
        print(f"    digit {c}: {(labels == c).sum()}")
    return 0


def check(rows: list[tuple[str, int, np.ndarray]]) -> int:
    """Leave-one-out: classify each sample against the rest."""
    ok = total = 0
    for i, (rel, label, v) in enumerate(rows):
        rest = np.stack([x for j, (_, _, x) in enumerate(rows) if j != i])
        rest_labels = np.array([l for j, (_, l, _) in enumerate(rows) if j != i])
        scores = (rest @ v) / len(v)
        best = int(np.argmax(scores))
        got, score = int(rest_labels[best]), float(scores[best])
        total += 1
        hit = got == label
        ok += hit
        print(f"  {Path(rel).name:22} want={label} got={got} score={score:.2f} "
              f"{'ok' if hit else 'MISS'}")
    print(f"\n  {ok}/{total} correct (leave-one-out)")
    return 0 if ok == total else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="leave-one-out accuracy")
    args = ap.parse_args()
    rows = load_samples()
    return check(rows) if args.check else build(rows)


if __name__ == "__main__":
    raise SystemExit(main())
