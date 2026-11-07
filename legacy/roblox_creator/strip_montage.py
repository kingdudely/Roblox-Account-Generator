"""Split an Arkose strip into the two images a vision model can actually read.

## What the strip is

Measured on saved strips from real challenges (2026-11-01), the `/rtig/image` body
is always 400 px tall. The top band holds the candidates; the bottom-left corner
holds the target, and the rest of the bottom is black filler.

    ten-tile family                          five-tile family
    +----+----+----+----+----+----+----+     +----+----+----+----+----+
    |  1 |  2 |  3 |  ...        | 10 |     |  1 |  2 |  3 |  4 |  5 |
    +----+----+----+----+----+----+----+     +----+----+----+----+----+
    |target|                                 |target|
    +------+                                 +------+

    2000x400, tiles 200x190                  1000x400, tiles 200x190
    target card x 0-125, y 200-400            target card x 0-135, y 200-400

The target card carries the object silhouettes, white on the brick (two of them in
the ten-tile family: the tile has to hold both). Candidates are dark silhouettes of
tools on the same brick wall.

## Why two images and not one

Every vision API silently resizes what it is given — Claude caps at a 1568 px long
edge, GPT-4o tiles, Qwen uses a token budget — so a single 2000x400 montage arrives
with roughly a quarter of its pixels. Sending one image also means the card and the
tiles *share* one resolution budget: the more tiles, the smaller the card that
carries the answer.

So the two halves are sent separately, each sized to sit just inside the usual
1.15 MP / 1568 px ceiling. Each gets a full budget of its own, and neither is
downscaled in transit. Two, not eleven: DeepSeek's vision stack degrades sharply
from the third image onward, and a per-tile image would also destroy the
side-by-side comparison that the answer depends on.

No numbers, no borders, no title text. Measured on 2026-11-01: a montage with red
number plates made the model read the plates instead of the tiles.

Everything is upscaled with LANCZOS and then sharpened, because the source glyphs
are ~40 px tall and the model needs edges, not smooth gradients.
"""

from __future__ import annotations

from PIL import Image, ImageFilter

__all__ = [
    "TILE_W", "CAND_H", "CARD_Y", "layout", "candidate_count",
    "target_image", "candidates_image", "single_image", "build",
]

TILE_W = 200        # measured on both families
CAND_H = 190        # candidate band; the remaining 10 px is the tile shadow
CARD_Y = 200        # target card starts below the candidate band
CARD_MAX_W = 150    # generous upper bound; the real cards measured 125 and 135

MAX_LONG_EDGE = 1568   # Claude's hard cap, the tightest of the common providers

# Target card upscale. The card is ~125x200 with a ~45x55 icon; at 7x the icon
# arrives as ~315x385, which is the first size where the outline was legible.
CARD_SCALE = 7
# Candidate cell. 300 is 1.5x the native 200 and keeps ten tiles under 1.15 MP.
CELL = 300
COLS = 5
GAP = 4


def _sharpen(image: Image.Image, percent: int = 130) -> Image.Image:
    return image.filter(ImageFilter.UnsharpMask(radius=2, percent=percent,
                                                threshold=2))


def _is_black(pixel) -> bool:
    return max(pixel) <= 40


def layout(strip: Image.Image) -> dict:
    """Measure the strip: tile count, tile box, and where the target card is.

    Read from the pixels rather than assumed, because the two families differ in
    width (1000 vs 2000) and card width (135 vs 125) and a hard-coded guess
    silently crops the wrong region on the family it was not written for.
    """
    width, height = strip.size
    count = max(1, width // TILE_W)

    # The card ends where the black filler begins. Walk the card's mid-row.
    probe_y = min(height - 4, CARD_Y + (height - CARD_Y) // 2)
    card_w = 0
    for x in range(0, min(width, CARD_MAX_W + 60)):
        if not _is_black(strip.getpixel((x, probe_y))):
            card_w = x
    card_w = max(60, min(CARD_MAX_W, card_w + 12))

    return {
        "count": count,
        "width": width,
        "height": height,
        "band_h": CAND_H,
        "tile_w": TILE_W,
        "card": (0, CARD_Y, card_w, height),
    }


def candidate_count(strip: Image.Image) -> int:
    return layout(strip)["count"]


def target_image(strip: Image.Image) -> Image.Image:
    """The target card alone, upscaled until its outlines are legible."""
    geometry = layout(strip)
    card = strip.crop(geometry["card"]).convert("RGB")
    card = card.resize((card.width * CARD_SCALE, card.height * CARD_SCALE),
                       Image.LANCZOS)
    return _sharpen(card, percent=150)


def candidates_image(strip: Image.Image, cols: int = COLS,
                     cell: int = CELL) -> Image.Image:
    """The candidate tiles as a reflowed grid, each cell at >= native scale.

    Ten tiles do not fit in one row inside the resolution ceiling — 2000 px of
    tiles downscaled to 1568 makes every tile *smaller* than the source. Wrapping
    into rows of five keeps every tile above its native 200 px instead.
    """
    geometry = layout(strip)
    count = geometry["count"]
    tiles = [strip.crop((i * TILE_W, 0, (i + 1) * TILE_W, CAND_H)).convert("RGB")
             for i in range(count)]

    cols = max(1, min(cols, count))
    rows = (count + cols - 1) // cols
    cell_h = int(cell * CAND_H / TILE_W)

    canvas = Image.new("RGB", (cols * cell + (cols + 1) * GAP,
                               rows * cell_h + (rows + 1) * GAP),
                       (255, 255, 255))
    for index, tile in enumerate(tiles):
        row, col = divmod(index, cols)
        big = tile.resize((cell, cell_h), Image.LANCZOS)
        big = _sharpen(big, percent=120)
        canvas.paste(big, (GAP + col * (cell + GAP),
                           GAP + row * (cell_h + GAP)))

    # Keep the long edge under the tightest provider cap without touching the
    # tiles again — scaling the finished canvas once is cheaper and softer than
    # letting the API do it after JPEG encoding.
    if canvas.width > MAX_LONG_EDGE:
        ratio = MAX_LONG_EDGE / canvas.width
        canvas = canvas.resize((MAX_LONG_EDGE, max(1, int(canvas.height * ratio))),
                               Image.LANCZOS)
    return canvas


CARD_PANEL_W = 400   # on-screen width of the reference column in `single_image`
LABEL_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def _label(image: Image.Image, text: str, at=(6, 4)) -> None:
    """One small digit per tile, in place.

    Numbering is not decoration: the model has to answer with an index, and on
    unnumbered boards it has been measured answering 14, 15 and 20 on a
    ten-tile grid by counting silhouettes instead of tiles. A plate *behind* the
    number is what broke an earlier attempt — the model read the plate and
    ignored the tile — so this is a bare glyph with a dark halo and nothing else.
    """
    from PIL import ImageDraw, ImageFont
    try:
        font = ImageFont.truetype(LABEL_FONT, 20)
    except Exception:
        font = ImageFont.load_default()
    draw = ImageDraw.Draw(image)
    x, y = at
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            if dx or dy:
                draw.text((x + dx, y + dy), text, font=font, fill=(0, 0, 0))
    draw.text((x, y), text, font=font, fill=(255, 255, 255))


def single_image(strip: Image.Image, cols: int = COLS, gap: int = 6,
                 max_width: int = MAX_LONG_EDGE,
                 card_panel_w: int = CARD_PANEL_W) -> Image.Image:
    """Card and candidates on one canvas, card first, tiles left to right.

    The tiles are pasted at *native* 200x190 and never resampled: the source
    pixels are all the information that exists, so upscaling them adds nothing a
    model can use while costing the card the room it needs. The card is the one
    thing worth enlarging — it is 125-150 px wide and carries the question — so it
    gets a column of its own on the left, scaled until its glyphs read, with the
    candidate grid beside it.

    Laid out this way the canvas lands at ~1440x650, inside the 1568 px ceiling
    that most vision APIs apply, which means neither half is downscaled in
    transit. One image also survives providers that quietly degrade from the
    third image onward; two was already the safe limit.
    """
    geometry = layout(strip)
    count = geometry["count"]
    card_box = geometry["card"]

    card = strip.crop(card_box).convert("RGB")
    card_scale = card_panel_w / card.width
    card = card.resize((card_panel_w, max(1, int(card.height * card_scale))),
                       Image.LANCZOS)
    card = _sharpen(card, percent=140)

    cols = max(1, min(cols, count))
    rows = (count + cols - 1) // cols

    # Native tile size unless the ceiling forces smaller.
    room = max_width - 3 * gap - card_panel_w
    cell = min(TILE_W, (room - (cols - 1) * gap) // cols)
    cell_h = int(cell * CAND_H / TILE_W)

    width = gap + card_panel_w + gap + cols * cell + (cols - 1) * gap + gap
    height = 2 * gap + max(card.height, rows * cell_h + (rows - 1) * gap)

    canvas = Image.new("RGB", (width, height), (250, 250, 250))
    canvas.paste(card, (gap, gap))

    left = gap + card_panel_w + gap
    for index in range(count):
        row, col = divmod(index, cols)
        tile = strip.crop((index * TILE_W, 0, (index + 1) * TILE_W,
                           CAND_H)).convert("RGB")
        if cell != TILE_W:
            tile = _sharpen(tile.resize((cell, cell_h), Image.LANCZOS),
                            percent=120)
        _label(tile, str(index + 1))
        canvas.paste(tile, (left + col * (cell + gap),
                            gap + row * (cell_h + gap)))

    return canvas


def build(strip: Image.Image, **kwargs) -> Image.Image:
    """Backwards-compatible single-image view: the candidate grid."""
    return candidates_image(strip, **kwargs)
