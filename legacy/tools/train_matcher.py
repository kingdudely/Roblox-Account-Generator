#!/usr/bin/env python3
"""Train the tile matcher: a small siamese encoder, distilled from hand labels.

## Why a matcher and not a classifier

The boards differ in family, tile count, card width and art direction, and they
change when Arkose ships new art. A classifier over "one of ten answers" learns
nothing transferable — it learns ten output slots. A *matcher* learns one job:
embed the reference, embed each candidate, pick the candidate closest to the
reference. Tile count stops existing, families share the same weights, and the
same encoder answers a five-tile board it has never seen.

That structure also survives the art changing. New objects, new lighting, new
brick — the encoder still has to answer "which of these looks like that", which
is the only question the puzzle ever asks.

## Where the labels come from

`debug/labels/labels.jsonl`, written by `tools/label_ui.py`. Rows:

    {"strip": "debug/browser_solver/<run>/strip_04.jpg", "tile": 7, ...}

`tile` is 1-based and counts left to right across the candidate band, which is
exactly how the carousel is numbered, so no translation is needed anywhere.

The vision model is not a usable teacher here. Measured 2026-11-01: four naming
passes over one reference card returned "jet", "compass", "Chinese character 水"
and "watering can". Distilling from a teacher that disagrees with itself teaches
the student to disagree with itself, faster. The human is the teacher; this script
just consumes what the human produced.

## The second teacher

`debug/labels/verdict.jsonl` is written by `tools/harvest_labels.py` from the
Arkose *grading screen*, which reveals the correct tile on a failed challenge
(see `docs/RUNTIME.md`). That file is read by default alongside the human labels
— a row is a row once it has cleared the harvest gate, and the trainer should not
care which teacher produced it. `--labels a.jsonl,b.jsonl` overrides both.

## Splitting honestly

Data is split by *run*, never by strip. Strips inside one run come from one
session of one family at one resolution, so a random strip-level split puts near
duplicates on both sides and reports a number that means nothing. Leave-one-run-out
is the only split here that answers "does this work on a board it has never seen".

    ./tools/train_matcher.py --cv                 # accuracy per held-out run
    ./tools/train_matcher.py --export models/matcher.onnx
    ./tools/train_matcher.py --export models/matcher.onnx --all   # final fit
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
from PIL import Image

from roblox_creator.strip_montage import CAND_H, TILE_W, layout

LABELS = ROOT / "debug" / "labels" / "labels.jsonl"
VERDICT_LABELS = ROOT / "debug" / "labels" / "verdict.jsonl"
MODELS = ROOT / "models"

SIZE = 160          # encoder input; the source tiles are 200x190, cards ~136x200
EMBED = 256


# ---------------------------------------------------------------- data

def load_rows(paths=LABELS) -> list[dict]:
    """Read one label file or several. Human and verdict labels share a schema.

    `tools/harvest_labels.py` writes the second file with `who: "verdict"`; both
    are consumed here because a label is a label once it has passed the harvest
    gate, and the trainer has no business caring which teacher produced it.
    """
    if isinstance(paths, (str, Path)):
        paths = [paths]
    rows = []
    for path in paths:
        path = Path(path)
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            strip = ROOT / row["strip"]
            if not strip.exists() or not isinstance(row.get("tile"), int):
                continue
            row["path"] = strip
            rows.append(row)
    return rows


def _square(image: Image.Image, size: int = SIZE) -> np.ndarray:
    """Resize to a square, preserving the whole panel rather than cropping it.

    Cropping would cut into the silhouette the answer depends on, and the two
    source shapes (200x190, 136x200) differ by less than a sixth, so padding to a
    square costs less than any crop.
    """
    image = image.convert("RGB")
    scale = size / max(image.width, image.height)
    inner = image.resize((max(1, int(image.width * scale)),
                          max(1, int(image.height * scale))), Image.LANCZOS)
    canvas = Image.new("RGB", (size, size), (0, 0, 0))
    canvas.paste(inner, ((size - inner.width) // 2, (size - inner.height) // 2))
    return np.asarray(canvas, dtype=np.uint8)


def build_samples(rows: list[dict]) -> list[dict]:
    """One entry per strip: the reference panel and every candidate panel."""
    by_strip: dict[str, dict] = {}
    for row in rows:
        key = str(row["path"])
        by_strip.setdefault(key, {"path": row["path"], "tiles": [],
                                  "run": row["path"].parent.name})
    for row in rows:
        entry = by_strip[str(row["path"])]
        entry["tiles"].append(row["tile"])

    samples = []
    for key, entry in by_strip.items():
        try:
            strip = Image.open(entry["path"]).convert("RGB")
        except Exception:
            continue
        geometry = layout(strip)
        count = geometry["count"]
        if any(t < 1 or t > count for t in entry["tiles"]):
            continue

        card = _square(strip.crop(geometry["card"]))
        tiles = [_square(strip.crop((i * TILE_W, 0, (i + 1) * TILE_W, CAND_H)))
                 for i in range(count)]

        # A strip labelled more than once (two sessions, or an undo) takes the
        # first label. Disagreement inside one strip is a labelling bug, and
        # silently averaging it would hide that.
        answer = entry["tiles"][0] - 1
        samples.append({"card": card, "tiles": np.stack(tiles), "answer": answer,
                        "run": entry["run"], "strip": key})
    return samples


# ---------------------------------------------------------------- augment

def augment(sample: dict, rng: random.Random) -> dict:
    """Relation-preserving augmentation.

    A horizontal flip is applied to the card *and every tile together*. That is
    the one geometric transform that keeps the matching relation true — the
    answer is still the answer after both sides are mirrored. Flipping only the
    card, or tiles independently, invents pairs that were never correct and
    teaches the encoder that orientation does not matter, which is the one thing
    this puzzle is about.
    """
    card = sample["card"].copy()
    tiles = sample["tiles"].copy()

    if rng.random() < 0.5:
        card = card[:, ::-1]
        tiles = tiles[:, :, ::-1]

    # Photometric jitter, applied to each side independently. Light and brick
    # tone drift between runs; the shadows drift with them, and the relation
    # survives because both sides see the same world.
    for target in (card, tiles):
        gain = 1.0 + rng.uniform(-0.22, 0.22)
        bias = rng.uniform(-18, 18)
        tint = np.array([rng.uniform(0.94, 1.06) for _ in range(3)])
        target[...] = np.clip(target.astype(np.float32) * gain * tint + bias,
                              0, 255).astype(np.uint8)

    if rng.random() < 0.35:
        noise = np.random.normal(0, rng.uniform(2.0, 7.0), tiles.shape)
        tiles[...] = np.clip(tiles + noise, 0, 255).astype(np.uint8)

    return {"card": card, "tiles": tiles, "answer": sample["answer"]}


def to_tensor(array: np.ndarray):
    """HWC or NHWC uint8 in, normalised CHW or NCHW float out."""
    import torch
    if array.ndim == 3:
        array = array[None]
    tensor = torch.from_numpy(np.ascontiguousarray(array.transpose(0, 3, 1, 2)))
    return tensor.float().div_(255.0).sub_(0.45).div_(0.25)


# ---------------------------------------------------------------- model

def build_encoder():
    """The shared encoder. GroupNorm, not BatchNorm — measured the hard way.

    The first version used BatchNorm and reported a train/eval contradiction:
    loss fell to 0.088 while training accuracy stayed at 2/40. The cause is the
    siamese forward: the reference is a batch of **1** and the candidates a batch
    of **10**, so BN's running statistics are trained on a mixture of two
    distributions that matches neither at eval time. The model fits the training
    pass (per-batch statistics) and collapses when `encoder.eval()` switches to
    the running statistics — which is exactly the mode `evaluate()` uses.

    GroupNorm has no running statistics and normalises per sample, so train and
    eval are the same function. That is the correct choice for metric learning
    with tiny batches, not a preference.

    Measured the hard way, twice: the first fix replaced only the GroupNorm inside
    `block()`'s first conv and left the second conv on BatchNorm, so the collapse
    survived and cost another 40-epoch run to notice. `check_eval_invariance()`
    now runs before training starts, because reading the loss curve is far too
    slow a way to detect this.
    """
    import torch.nn as nn

    def block(cin: int, cout: int) -> nn.Sequential:
        return nn.Sequential(
            nn.Conv2d(cin, cout, 3, stride=2, padding=1, bias=False),
            nn.GroupNorm(8, cout),
            nn.SiLU(inplace=True),
            nn.Conv2d(cout, cout, 3, stride=1, padding=1, bias=False),
            nn.GroupNorm(8, cout),
            nn.SiLU(inplace=True),
        )

    return nn.Sequential(
        block(3, 32),      # 80
        block(32, 64),     # 40
        block(64, 128),    # 20
        block(128, 192),   # 10
        block(192, EMBED), # 5
        nn.AdaptiveAvgPool2d(1),
        nn.Flatten(),
    )


def similarity(card, tiles):
    """Cosine of the reference against every candidate.

    `card` is (B, D), `tiles` is (B, N, D). Candidates are L2-normalised along the
    last axis and the card likewise, so the dot product is a cosine in [-1, 1] —
    a bounded score that behaves the same for five tiles and for ten.
    """
    import torch.nn.functional as F
    card = F.normalize(card, dim=-1)
    tiles = F.normalize(tiles, dim=-1)
    return (tiles * card.unsqueeze(1)).sum(-1) * 12.0


def check_eval_invariance(encoder, sample: dict, tol: float = 1e-4) -> str | None:
    """Assert that train() and eval() produce the same embedding.

    This is the invariant that broke twice and cost two full training runs. Any
    module carrying running statistics (BatchNorm is the only common one) makes
    train and eval different functions, and the siamese forward feeds the encoder
    a batch of 1 and a batch of 10 — so those statistics are trained on a mixture
    of two distributions that matches neither. The symptom is unmistakable once
    you know it: the training loss falls to near zero while `evaluate()` reports
    chance. The check is one forward pass, so there is no reason not to run it.

    Returns None when invariant, else a description of the offending modules.
    """
    import torch

    card = to_tensor(sample["card"])
    encoder.train()
    with torch.no_grad():
        trained = encoder(card).clone()
    encoder.eval()
    with torch.no_grad():
        evaluated = encoder(card).clone()
    encoder.train()

    delta = float((trained - evaluated).abs().max())
    if delta <= tol:
        return None

    offenders = [name for name, module in encoder.named_modules()
                 if getattr(module, "running_mean", None) is not None
                 or getattr(module, "track_running_stats", False)]
    detail = ", ".join(offenders) if offenders else "no running-stat modules found"
    return (f"train/eval mismatch: max |delta| = {delta:.4f} "
            f"(tolerance {tol}) — offending modules: {detail}")


# ---------------------------------------------------------------- train

def train(samples: list[dict], epochs: int = 60, seed: int = 0, quiet=False,
          lr: float = 2e-3):
    import torch
    import torch.nn.functional as F

    torch.manual_seed(seed)
    rng = random.Random(seed)
    encoder = build_encoder()

    # Fail loudly before spending 10 minutes proving it the slow way.
    problem = check_eval_invariance(encoder, samples[0])
    if problem:
        raise RuntimeError(problem)

    optimiser = torch.optim.AdamW(encoder.parameters(), lr=lr, weight_decay=1e-4)
    schedule = torch.optim.lr_scheduler.OneCycleLR(
        optimiser, max_lr=lr, total_steps=max(1, epochs * len(samples)))

    for epoch in range(epochs):
        encoder.train()
        order = list(range(len(samples)))
        rng.shuffle(order)
        running = 0.0
        for index in order:
            sample = augment(samples[index], rng)
            card = to_tensor(sample["card"])
            tiles = to_tensor(sample["tiles"])
            logits = similarity(encoder(card), encoder(tiles))[0]
            loss = F.cross_entropy(logits[None],
                                   torch.tensor([sample["answer"]]))
            optimiser.zero_grad(set_to_none=True)
            loss.backward()
            optimiser.step()
            schedule.step()
            running += float(loss)
        if not quiet and (epoch % 10 == 0 or epoch == epochs - 1):
            print(f"    epoch {epoch:3d}  loss {running / len(samples):.4f}")
    return encoder


def evaluate(encoder, samples: list[dict]) -> tuple[int, int]:
    import torch
    encoder.eval()
    hits = 0
    with torch.no_grad():
        for sample in samples:
            card = to_tensor(sample["card"])
            tiles = to_tensor(sample["tiles"])
            logits = similarity(encoder(card), encoder(tiles))[0]
            hits += int(int(logits.argmax()) == sample["answer"])
    return hits, len(samples)


def cross_validate(samples: list[dict], epochs: int, seeds: int = 2,
                   lr: float = 2e-3) -> None:
    runs = sorted({s["run"] for s in samples})
    print(f"\nleave-one-run-out over {len(runs)} runs, {len(samples)} strips")
    total_hits = total_seen = 0
    for held in runs:
        train_set = [s for s in samples if s["run"] != held]
        test_set = [s for s in samples if s["run"] == held]
        if not train_set or not test_set:
            continue
        hits = seen = 0
        for seed in range(seeds):
            encoder = train(train_set, epochs=epochs, seed=seed, quiet=True,
                            lr=lr)
            h, n = evaluate(encoder, test_set)
            hits += h
            seen += n
        total_hits += hits
        total_seen += seen
        print(f"  hold out {held}: {hits}/{seen}"
              f"  ({100.0 * hits / max(1, seen):.0f}%)")
    if total_seen:
        print(f"  overall: {total_hits}/{total_seen}"
              f"  ({100.0 * total_hits / total_seen:.1f}%)")


def export(encoder, path: Path) -> None:
    import torch
    path.parent.mkdir(parents=True, exist_ok=True)
    encoder.eval()
    card = torch.zeros(1, 3, SIZE, SIZE)
    tiles = torch.zeros(1, 20, 3, SIZE, SIZE)
    torch.onnx.export(
        encoder, (card,), str(path.with_name("matcher_card.onnx")),
        input_names=["card"], output_names=["card_embedding"],
        dynamic_axes={"card": {0: "batch"}}, opset_version=17)
    torch.onnx.export(
        encoder, (tiles.view(-1, 3, SIZE, SIZE),), str(path.with_name("matcher_tile.onnx")),
        input_names=["tile"], output_names=["tile_embedding"],
        dynamic_axes={"tile": {0: "batch"}}, opset_version=17)
    print(f"  wrote {path.with_name('matcher_card.onnx')}")
    print(f"  wrote {path.with_name('matcher_tile.onnx')}")
    print("  both share one encoder; scoring is a cosine between their outputs")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=f"{LABELS},{VERDICT_LABELS}",
                    help="one file, or several comma-separated")
    ap.add_argument("--cv", action="store_true", help="leave-one-run-out accuracy")
    ap.add_argument("--export", type=Path, default=None)
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--seeds", type=int, default=2,
                    help="training runs per held-out fold; 1 halves the CV cost")
    ap.add_argument("--lr", type=float, default=2e-3,
                    help="OneCycle peak LR. 2e-3 does not fit 40 boards — measured; "
                         "the loss sits at ln(10) until the LR anneals.")
    ap.add_argument("--all", action="store_true",
                    help="fit on every run when exporting (no holdout)")
    args = ap.parse_args()

    rows = load_rows([Path(part) for part in str(args.labels).split(",")
                      if part.strip()])
    if not rows:
        print(f"no labels in {args.labels}")
        print("run ./tools/label_ui.py first — nothing can be trained on guesses")
        return 1

    samples = build_samples(rows)
    print(f"{len(rows)} labels -> {len(samples)} strips across "
          f"{len({s['run'] for s in samples})} runs")

    counts: dict[int, int] = {}
    for sample in samples:
        counts[len(sample["tiles"])] = counts.get(len(sample["tiles"]), 0) + 1
    print("  boards by tile count: "
          + ", ".join(f"{k}x{v}" for k, v in sorted(counts.items())))

    started = time.time()
    if args.cv:
        cross_validate(samples, args.epochs, args.seeds, args.lr)
    if args.export:
        encoder = train(samples, epochs=args.epochs, lr=args.lr)
        hits, seen = evaluate(encoder, samples)
        print(f"\n  fit-set accuracy {hits}/{seen} (not a generalisation number)")
        export(encoder, args.export)
    if not args.cv and not args.export:
        encoder = train(samples, epochs=args.epochs, lr=args.lr)
        hits, seen = evaluate(encoder, samples)
        print(f"  training accuracy {hits}/{seen}")
    print(f"\n{time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
