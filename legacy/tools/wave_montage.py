#!/usr/bin/env python3
"""Lay a whole Arkose dice wave into one labelled image.

One wave = 11 images (key + 10 candidates). Uploading them separately costs 11
of the 18 uploads a fresh ChatGPT identity gets, so a montage turns that into
one — and it also removes the "which attachment is which" ambiguity for the
model.

Layout: key in its own row, candidates in a 5x2 grid beneath it, every tile
numbered. Tiles are upscaled 2x with LANCZOS because the symbols are ~15 px in
the source captures.

    ./tools/wave_montage.py --wave debug/vision/20261029-084809-unknown-1
    ./tools/wave_montage.py --all --out /tmp/opencode/montage
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TILE = 200          # source tile edge
SCALE = 2           # upscale before composing
PAD = 10            # gutter
LABEL_H = 26        # strip above each tile for its index
COLS = 5            # candidates per row

# A downloaded wave strip is one 2000x400 JPEG: ten answer tiles of 200 px across
# the top, and the key sprite in the bottom-left cell. The sprite carries the
# symbol and the required count ("<symbol> x N") and drifts between y=266 and
# y=375 across waves, so the band is fixed and generous rather than detected —
# terrain highlights defeat every blob heuristic tried (see KEY_BAND history).
STRIP_TILES = 10
KEY_BAND = (0, 255, 150, 400)


def _load(path: Path, scale: int):
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        return im.resize((im.width * scale, im.height * scale), Image.LANCZOS)


def build_candidates(wave: Path, out: Path, scale: int = SCALE) -> Path:
    """Candidates only, numbered in yellow inside each tile's top-left corner.

    Sent as the second image, after the key. Two uploads per wave instead of
    eleven, and the numbers are unambiguous because they are drawn on the tile.
    """
    from PIL import Image, ImageDraw, ImageFont

    cands = sorted(
        [p for p in wave.glob("cand*.img")],
        key=lambda p: int("".join(c for c in p.stem if c.isdigit()) or 0),
    )
    if not cands:
        raise RuntimeError(f"{wave}: no candidates")

    tile = TILE * scale
    label_h = 28
    cols = 5
    rows = (len(cands) + cols - 1) // cols
    width = cols * tile + (cols + 1) * PAD
    height = rows * (tile + label_h) + (rows + 1) * PAD

    canvas = Image.new("RGB", (width, height), (20, 20, 24))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 22)
    except Exception:
        font = ImageFont.load_default()

    for i, path in enumerate(cands):
        row, col = divmod(i, cols)
        x = PAD + col * (tile + PAD)
        y = PAD + row * (tile + label_h + PAD)
        draw.text((x + 4, y + 2), str(i + 1), fill=(255, 255, 0), font=font)
        with Image.open(path) as im:
            canvas.paste(
                im.convert("RGB").resize((tile, tile), Image.LANCZOS),
                (x, y + label_h),
            )

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=95, subsampling=0)
    return out


def split_strip(strip: Path, n: int = STRIP_TILES):
    """Cut a downloaded strip into (key image, [candidate images]).

    Removes the need to save eleven files per wave: the strip is what the server
    hands back, so everything downstream can work from it directly.
    """
    from PIL import Image

    with Image.open(strip) as im:
        im = im.convert("RGB")
        if im.size[1] < 400 or im.size[0] < n * TILE:
            raise RuntimeError(f"{strip}: not a wave strip ({im.size})")
        tiles = [im.crop((i * TILE, 0, (i + 1) * TILE, TILE)) for i in range(n)]
        key = im.crop(KEY_BAND)
    return key, tiles


def build_parts(key_img, cands, out: Path, scale: int = SCALE) -> Path:
    """Compose key + candidates into one labelled image.

    Takes PIL images rather than paths so a freshly downloaded strip can be
    composed without writing eleven files first.
    """
    from PIL import Image, ImageDraw, ImageFont

    def up(img):
        return img.convert("RGB").resize(
            (img.width * scale, img.height * scale), Image.LANCZOS)

    key = up(key_img)
    tiles = [up(c) for c in cands]
    tile_w = TILE * scale
    tile_h = TILE * scale + LABEL_H
    rows = (len(tiles) + COLS - 1) // COLS

    width = max(COLS * tile_w, key.width) + (COLS + 1) * PAD
    height = PAD + (key.height + LABEL_H) + PAD + rows * (tile_h + PAD) + PAD
    canvas = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(canvas)

    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except Exception:
        font = ImageFont.load_default()

    def place(img, x, y, label):
        draw.rectangle([x, y, x + img.width, y + LABEL_H], fill=(24, 24, 28))
        draw.text((x + 6, y + 2), label, fill=(255, 255, 0), font=font)
        canvas.paste(img, (x, y + LABEL_H))

    # the key is the whole question: symbol plus a required count
    place(key, PAD, PAD, "KEY: this symbol, this many times")

    for i, tile in enumerate(tiles):
        row, col = divmod(i, COLS)
        place(tile, PAD + col * (tile_w + PAD),
              PAD + key.height + LABEL_H + PAD + row * (tile_h + PAD),
              f"{i + 1}")

    out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out, quality=95, subsampling=0)
    return out


def build(wave: Path, out: Path, scale: int = SCALE) -> Path:
    """Compose from a pre-cut wave directory (`key.img` + `cand1..10.img`)."""
    key = wave / "key.img"
    cands = sorted(
        [p for p in wave.glob("cand*.img")],
        key=lambda p: int("".join(c for c in p.stem if c.isdigit()) or 0),
    )
    if not key.exists() or not cands:
        raise RuntimeError(f"{wave}: missing key or candidates")
    from PIL import Image
    with Image.open(key) as k:
        key_img = k.copy()
    tiles = []
    for c in cands:
        with Image.open(c) as im:
            tiles.append(im.copy())
    return build_parts(key_img, tiles, out, scale)


def build_from_strip(strip: Path, out: Path, scale: int = SCALE) -> Path:
    """Compose straight from a downloaded `/rtig/image` strip."""
    key_img, tiles = split_strip(strip)
    return build_parts(key_img, tiles, out, scale)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", type=Path, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--base", type=Path, default=ROOT / "debug" / "vision")
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/montage"))
    ap.add_argument("--scale", type=int, default=SCALE)
    args = ap.parse_args()

    waves = [args.wave] if args.wave else (
        sorted(d for d in args.base.iterdir()
               if d.is_dir() and (d / "key.img").exists()) if args.all else [])
    if not waves:
        print("pass --wave DIR or --all")
        return 2
    for w in waves:
        p = build(w, args.out / f"{w.name}.jpg", args.scale)
        from PIL import Image as I
        with I.open(p) as im:
            print(f"  {p}  {im.size[0]}x{im.size[1]}  {p.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
