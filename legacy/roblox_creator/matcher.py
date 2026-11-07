"""On-device tile matcher: the ONNX student, used in place of the vision API.

## What this is

Two exported graphs (`matcher_card.onnx`, `matcher_tile.onnx`) that share one
small encoder. Scoring a board is:

    card_embedding  = card_graph(card)              # one 256-d vector
    tile_embeddings = tile_graph(each_tile)         # N 256-d vectors
    answer          = argmax(cosine(card, tiles))

No prompt, no API call, no network. Measured on this CPU: **53 ms** for a ten-tile
board at 4 threads, against 2.8 s for the vision API — 50x, and it costs nothing
when the API is rate-limited. The number is dominated by the 160 px first
convolution; a 96 px input is ~2.8x cheaper again, and int8 quantisation roughly
halves it after that. Neither is worth doing until the accuracy is there, which it
is not yet.

## What it is not

It is not free accuracy. It is a distillation of whatever labels trained it, so its
ceiling is the labeler's consistency. `tools/train_matcher.py --cv` is the only
number worth trusting about it; the training-set accuracy that script prints is a
memorisation check, not a score.

`solve_strip()` therefore returns the scores alongside the answer. A matcher whose
top score barely beats the runner-up is a matcher guessing, and the caller can
route that board to the vision model instead. That split — cheap model for the
easy majority, API for the uncertain tail — is the whole reason to have both.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from .strip_montage import CAND_H, TILE_W, layout

SIZE = 160
MODEL_DIR = None   # resolved lazily so this module imports without a model on disk


def _square(image: Image.Image, size: int = SIZE) -> np.ndarray:
    image = image.convert("RGB")
    scale = size / max(image.width, image.height)
    inner = image.resize((max(1, int(image.width * scale)),
                          max(1, int(image.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), (0, 0, 0))
    canvas.paste(inner, ((size - inner.width) // 2, (size - inner.height) // 2))
    array = np.asarray(canvas, dtype=np.float32).transpose(2, 0, 1)
    return (array / 255.0 - 0.45) / 0.25


class Matcher:
    """Lazily-loaded onnxruntime session pair."""

    def __init__(self, directory=None):
        from pathlib import Path
        here = Path(__file__).resolve().parents[1] / "models"
        self.directory = Path(directory) if directory else here
        self._sessions = None

    def available(self) -> bool:
        return ((self.directory / "matcher_card.onnx").exists()
                and (self.directory / "matcher_tile.onnx").exists())

    def _load(self):
        if self._sessions is None:
            import onnxruntime as ort
            options = ort.SessionOptions()
            # Measured 2026-11-01 on a 10-tile board: 1 thread 108 ms, 2 -> 59 ms,
            # 4 -> 53 ms, 8 -> 127 ms. Four is the knee; more threads thrash.
            options.intra_op_num_threads = 4
            options.log_severity_level = 3
            options.graph_optimization_level = (
                ort.GraphOptimizationLevel.ORT_ENABLE_ALL)
            self._sessions = (
                ort.InferenceSession(str(self.directory / "matcher_card.onnx"),
                                     options, providers=["CPUExecutionProvider"]),
                ort.InferenceSession(str(self.directory / "matcher_tile.onnx"),
                                     options, providers=["CPUExecutionProvider"]),
            )
        return self._sessions

    def solve(self, strip: Image.Image) -> dict:
        """Scores for every candidate on one strip, best first."""
        geometry = layout(strip)
        count = geometry["count"]
        card_session, tile_session = self._load()

        card = _square(strip.crop(geometry["card"]))[None]
        tiles = np.stack([_square(strip.crop((i * TILE_W, 0,
                                              (i + 1) * TILE_W, CAND_H)))
                          for i in range(count)])

        card_vector = card_session.run(None, {"card": card})[0][0]
        tile_vectors = tile_session.run(None, {"tile": tiles})[0]

        card_vector = card_vector / (np.linalg.norm(card_vector) + 1e-8)
        norms = np.linalg.norm(tile_vectors, axis=-1, keepdims=True) + 1e-8
        scores = (tile_vectors / norms) @ card_vector

        order = np.argsort(-scores)
        best = int(order[0])
        margin = float(scores[order[0]] - scores[order[1]]) if count > 1 else 1.0
        return {"answer": best + 1, "scores": [round(float(s), 4) for s in scores],
                "margin": round(margin, 4),
                "confidence": round(float(scores[order[0]]), 4), "count": count}


def solve_strip(strip: Image.Image, directory=None) -> dict | None:
    """Convenience wrapper that returns None when no model has been trained yet."""
    matcher = Matcher(directory)
    if not matcher.available():
        return None
    return matcher.solve(strip)
