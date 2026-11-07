"""Read a correctness screen back into a label — the dense labeller.

Measured 2026-11-01 (`debug/browser_solver/20261101-195306/final.png`): a wrong
answer does not fail the round, it advances it, and the challenge is graded once
at the end. The grading screen reads "That was not quite right." and shows one
candidate tile with a green check. That screen is the only dense label source in
the system. The alternatives are one bit per five answers (`solved`) or a vision
model's own answers, and the model's answers are the bias Wazza0 measured: 73%
validation, 22% production.

Three readers, cheapest first:

    1. network   the /fc/ca/ response may carry the verdict      tools/verdict_probe.py
    2. dom       the game frame's state may expose the index     scan_state()
    3. pixels    the checked tile is on screen                   match_tile(), match_card()

This module owns (2) and (3), both of which run offline against a saved capture.

Matching is NCC over a 64x64 grey resize. That is deliberately simple, and it is
enough because both sides come from the *same* source JPEG: the strip is the file
the game fetched, and the verdict crop is that same file re-rendered larger. The
case this must not be trusted for is a cross-rendering match (the glossy sticker
vs the painted die face that killed template matching on the dice family). A
shadow on a brick wall is the same rendering on both sides, so NCC applies.

Measured on the 40 human-labelled waves, each query perturbed like the verdict
panel (0.92 scale, 5 px offset, green check overlay):

    match_tile within the right strip        40/40
    match_board across the whole capture     40/40   <- the reader to use
    match_card (plain NCC, 64 px)            13/40   <- context only
    match_card (white-icon mask + shift)     34/40

So `match_board` is the labeller: a crop is matched against every tile of every
saved strip, and the strongest hit is the answer. The reference card is reported
beside it for a human reading the capture, never used as a constraint — a wrong
card would silently restrict the tile search to the wrong round.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from PIL import Image

__all__ = [
    "TILE_W", "CAND_H", "CARD_Y", "CARD_MAX_W",
    "tiles", "card", "match_tile", "match_card", "match_board",
    "board_similarity", "scan_state", "parse_index",
]

TILE_W = 200        # measured on both families
CAND_H = 190        # candidate band; the remaining 10 px is the tile shadow
CARD_Y = 200        # target card starts below the candidate band
CARD_MAX_W = 150    # real cards measured 125 and 135

_GREY = 64          # NCC resize target
_STATE_KEY = re.compile(
    r"(correct|answer|solution|truth|verdict|expected|right|result|grade)",
    re.I)


def _open(image) -> Image.Image:
    if isinstance(image, (str, Path)):
        return Image.open(image).convert("RGB")
    return image.convert("RGB")


def _vector(image: Image.Image, size: int = _GREY) -> np.ndarray:
    """Grey, resized, mean-removed, unit-norm. One float vector per image."""
    grey = _open(image).convert("L").resize((size, size), Image.LANCZOS)
    arr = np.asarray(grey, dtype=np.float32).reshape(-1)
    arr = arr - arr.mean()
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


def ncc(a, b) -> float:
    """Pearson correlation of two images at a fixed size. Range -1..1."""
    return float(np.dot(_vector(a), _vector(b)))


def tiles(strip, count: int | None = None) -> list[Image.Image]:
    """The candidate band, one 200x190 crop per tile, left to right."""
    strip = _open(strip)
    n = count or max(1, strip.width // TILE_W)
    return [strip.crop((i * TILE_W, 0, (i + 1) * TILE_W, CAND_H))
            for i in range(n)]


def card(strip) -> Image.Image:
    """The target card in the bottom-left corner.

    Width is read from the pixels, not assumed: the two families differ (1000 vs
    2000 wide) and a hard-coded crop silently takes the wrong region on the one
    it was not written for.
    """
    strip = _open(strip)
    probe_y = min(strip.height - 4, CARD_Y + (strip.height - CARD_Y) // 2)
    last = 0
    for x in range(0, min(strip.width, CARD_MAX_W + 60)):
        if max(strip.getpixel((x, probe_y))) > 40:
            last = x
    width = max(60, min(CARD_MAX_W, last + 12))
    return strip.crop((0, CARD_Y, width, strip.height))


def match_tile(query, strip, count: int | None = None) -> dict:
    """Which candidate of `strip` is `query`? Returns a 1-based index.

    The verdict screen renders the correct tile from the same source image, so a
    correct answer should score far above the runner-up. `margin` is reported
    instead of a boolean: a torn board is a thing to look at, not to silence.
    """
    candidates = tiles(strip, count)
    scores = [ncc(query, tile) for tile in candidates]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    top = order[0]
    runner = scores[order[1]] if len(order) > 1 else 0.0
    return {
        "index": top + 1,                    # 1-based, matches GameUI.goto()
        "score": round(scores[top], 4),
        "runner_up": round(runner, 4),
        "margin": round(scores[top] - runner, 4),
        "scores": [round(s, 3) for s in scores],
    }


def match_card(query, strips: list) -> dict:
    """Which strip's target card is `query`? Returns a 1-based wave position.

    This is how a verdict screen is tied back to the round it graded. The cards
    carry different props on every round, so the match is normally unambiguous;
    when it is not, the margin says so.
    """
    cards = [card(s) for s in strips]
    scores = [ncc(query, c) for c in cards]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    top = order[0]
    runner = scores[order[1]] if len(order) > 1 else 0.0
    return {
        "wave": top + 1,
        "score": round(scores[top], 4),
        "runner_up": round(runner, 4),
        "margin": round(scores[top] - runner, 4),
        "scores": [round(s, 3) for s in scores],
    }


def match_board(query, strips: list) -> dict:
    """Best card match *and* best tile match across every saved strip.

    A verdict crop can be either panel: the reference card on the left or the
    checked candidate on the right. Scoring both across the whole capture is what
    tells them apart — a card crop correlates with a card, a tile crop with a
    tile — and it ties a tile back to the round it belongs to without needing the
    card in the same screenshot.
    """
    card_scores: list[tuple[float, int]] = []
    tile_scores: list[tuple[float, int, int]] = []
    for wave, strip in enumerate(strips, 1):
        card_scores.append((ncc(query, card(strip)), wave))
        for index, tile in enumerate(tiles(strip), 1):
            tile_scores.append((ncc(query, tile), wave, index))
    card_scores.sort(reverse=True)
    tile_scores.sort(reverse=True)
    top_card = card_scores[0] if card_scores else (0.0, 0)
    next_card = card_scores[1][0] if len(card_scores) > 1 else 0.0
    top_tile = tile_scores[0] if tile_scores else (0.0, 0, 0)
    next_tile = tile_scores[1][0] if len(tile_scores) > 1 else 0.0
    card_hit = {
        "wave": top_card[1], "score": round(top_card[0], 4),
        "runner_up": round(next_card, 4),
        "margin": round(top_card[0] - next_card, 4),
    }
    tile_hit = {
        "wave": top_tile[1], "index": top_tile[2],
        "score": round(top_tile[0], 4),
        "runner_up": round(next_tile, 4),
        "margin": round(top_tile[0] - next_tile, 4),
    }
    return {
        "card": card_hit,
        "tile": tile_hit,
        "kind": "card" if card_hit["score"] >= tile_hit["score"] else "tile",
    }


def board_similarity(a, b) -> float:
    """How alike two strips are, 0..1-ish. Used to test whether a retry repeats.

    A re-served board is the same JPEG re-encoded at most once, so a true repeat
    scores near 1.0; a fresh board with different props scores far below it.
    """
    return ncc(a, b)


def scan_state(obj, path: str = "", depth: int = 0):
    """Yield `(path, value)` for every correctness-looking key in a JS state dump.

    A recursive walk, bounded so a deep React tree cannot run away. Only scalars
    and small lists of scalars are reported: a key called `correct` holding an
    object is noise, a key called `correct` holding `4` is the label.
    """
    if depth > 6:
        return
    if isinstance(obj, dict):
        for key, value in obj.items():
            here = f"{path}.{key}" if path else str(key)
            if _STATE_KEY.search(str(key)):
                if isinstance(value, (int, float, str, bool)) or (
                        isinstance(value, list)
                        and 0 < len(value) <= 20
                        and all(isinstance(v, (int, float, str, bool))
                                for v in value)):
                    yield here, value
            if isinstance(value, (dict, list)):
                yield from scan_state(value, here, depth + 1)
    elif isinstance(obj, list):
        for i, value in enumerate(obj[:40]):
            if isinstance(value, (dict, list)):
                yield from scan_state(value, f"{path}[{i}]", depth + 1)


def parse_index(reply: str) -> int | None:
    """Pull a candidate index out of a verdict sentence, if one is written.

    Deliberately narrow: only an explicit "candidate/tile/option N", an
    "answer: N", or a leading integer. A mid-sentence count is never taken —
    that inversion cost this repo a week on the dice family.
    """
    if not reply:
        return None
    text = reply.strip()
    m = re.search(r"(?:candidate|tile|option)\s*#?\s*(\d{1,2})", text, re.I)
    if m:
        return int(m.group(1))
    m = re.search(r"answer\s*[:=]\s*#?\s*(\d{1,2})", text, re.I)
    if m:
        return int(m.group(1))
    m = re.match(r"\s*(\d{1,2})\b", text)
    if m:
        return int(m.group(1))
    return None
