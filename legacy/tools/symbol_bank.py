#!/usr/bin/env python3
"""Build a bank of dice symbols from captured waves.

Every wave hands us 10 candidates of ~7-9 visible faces, each face carrying one
symbol on a light decal. The sprite set is small and reused across puzzles, so a
bank built once turns counting into a nearest-neighbour lookup.

Stage 1 (this file): extract every decal, normalise it, and dump contact sheets
so the vocabulary can be inspected before anything is clustered.

    ./tools/symbol_bank.py --extract --sheet 120
"""

from __future__ import annotations

import argparse
import pickle
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
CACHE = Path("/tmp/opencode/bank")
S = 64


def decals(path: Path, gmin: int = 195, smax: int = 70,
           amin: int = 35, amax: int = 1200) -> list[dict]:
    """Bright, low-saturation blobs -> perspective-warped squares.

    The decal is a flat sticker on a die face, so a min-area-rect warp undoes
    most of the 3D rotation and leaves the glyph at a canonical scale. Residual
    in-plane rotation is handled later by a rotation sweep.
    """
    img = cv2.imread(str(path))
    if img is None:
        return []
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = ((gray > gmin) & (hsv[:, :, 1] < smax)).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((2, 2), np.uint8))

    out = []
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for c in contours:
        area = cv2.contourArea(c)
        if not (amin <= area <= amax):
            continue
        rect = cv2.minAreaRect(c)
        (cx, cy), (w, h), _ang = rect
        if min(w, h) < 7:
            continue
        # a decal is a compact quad; dice clusters and background shards are
        # spiky, so solidity separates them cleanly
        hull = cv2.convexHull(c)
        hull_area = cv2.contourArea(hull)
        if hull_area <= 0 or area / hull_area < 0.78:
            continue
        if not (10 <= min(w, h) <= 48) or not (10 <= max(w, h) <= 52):
            continue
        box = cv2.boxPoints(rect).astype(np.float32)
        s = box.sum(axis=1)
        d = np.diff(box, axis=1).ravel()
        src = np.array([box[np.argmin(s)], box[np.argmin(d)],
                        box[np.argmax(s)], box[np.argmax(d)]], np.float32)
        dst = np.array([[0, 0], [S - 1, 0], [S - 1, S - 1], [0, S - 1]], np.float32)
        warp = cv2.warpPerspective(img, cv2.getPerspectiveTransform(src, dst), (S, S))
        out.append({"warp": warp, "centre": (cx, cy), "area": area,
                    "size": (w, h), "angle": _ang})
    return _dedupe(out)


def _dedupe(items: list[dict], r: float = 6.0) -> list[dict]:
    keep: list[dict] = []
    for p in items:
        if all((p["centre"][0] - q["centre"][0]) ** 2 +
               (p["centre"][1] - q["centre"][1]) ** 2 > r * r for q in keep):
            keep.append(p)
    return keep


def glyph(warp: np.ndarray, inset: int = 7) -> dict | None:
    """Separate the glyph from its decal.

    Symbols come in two polarities: dark marks on a white disc, and white marks
    on a dark disc. Normalise both to "dark marks on light field" so one
    descriptor covers everything.
    """
    g = cv2.cvtColor(warp, cv2.COLOR_BGR2GRAY).astype(np.float32)
    inner = g[inset:S - inset, inset:S - inset]
    if inner.size == 0:
        return None
    mean, std = float(inner.mean()), float(inner.std())
    if std < 8:
        return None

    dark = (inner < mean - 0.55 * std)
    light = (inner > mean + 0.55 * std)
    inverted = light.mean() > dark.mean()

    mask = (light if inverted else dark).astype(np.float32)
    if mask.sum() < 6:
        return None

    ys, xs = np.nonzero(mask)
    crop = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    if crop.shape[0] < 3 or crop.shape[1] < 3:
        return None

    fill = float(mask.sum()) / float(crop.size)
    aspect = crop.shape[1] / max(crop.shape[0], 1)
    norm = cv2.resize(crop, (24, 24), interpolation=cv2.INTER_AREA)

    return {"mask": norm, "inverted": inverted, "fill": fill,
            "aspect": aspect, "std": std, "mean": mean}


def extract(base: Path) -> list[dict]:
    rows: list[dict] = []
    for wave in sorted(d for d in base.iterdir()
                       if d.is_dir() and (d / "key.img").exists()):
        for i in range(1, 11):
            p = wave / f"cand{i}.img"
            if not p.exists():
                continue
            for j, d in enumerate(decals(p)):
                gl = glyph(d["warp"])
                if gl is None:
                    continue
                rows.append({"wave": wave.name, "cand": i, "idx": j,
                             "warp": d["warp"], **gl})
    return rows


def sheet(rows: list[dict], out: Path, cols: int = 14, tile: int = 64,
          labels: bool = True, order: list[int] | None = None) -> Path:
    idx = order if order is not None else list(range(len(rows)))
    n = len(idx)
    rows_n = (n + cols - 1) // cols
    lh = 14 if labels else 0
    canvas = np.zeros((rows_n * (tile + lh), cols * tile, 3), np.uint8)
    for k, i in enumerate(idx):
        r, c = divmod(k, cols)
        img = cv2.resize(rows[i]["warp"], (tile, tile), interpolation=cv2.INTER_NEAREST)
        canvas[r * (tile + lh):r * (tile + lh) + tile, c * tile:(c + 1) * tile] = img
        if labels:
            cv2.putText(canvas, str(i), (c * tile + 2, r * (tile + lh) + tile + 11),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, (200, 200, 200), 1)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), canvas)
    return out


def match(a: np.ndarray, b: np.ndarray, rots: int = 12) -> float:
    """Best IoU of two glyph masks over an in-plane rotation sweep."""
    best = 0.0
    h, w = a.shape[:2]
    A = a > 0.5
    na = A.sum()
    if na == 0:
        return 0.0
    for k in range(rots):
        ang = 360.0 * k / rots
        M = cv2.getRotationMatrix2D((w / 2 - 0.5, h / 2 - 0.5), ang, 1.0)
        rb = cv2.warpAffine(b, M, (w, h), flags=cv2.INTER_NEAREST, borderValue=0) > 0.5
        nb = rb.sum()
        if nb == 0:
            continue
        inter = np.logical_and(A, rb).sum()
        union = na + nb - inter
        if union:
            score = inter / union
            if score > best:
                best = score
    return float(best)


def cluster(rows: list[dict], threshold: float = 0.62) -> tuple[list[int], list[list[int]]]:
    """Greedy: seed a cluster, absorb everything above `threshold`, repeat.

    Returns (label per row, member lists). Order-dependent by nature, so the
    first seeds matter — they are sorted by mask density, which favours the
    cleanest, least-clipped glyphs.
    """
    n = len(rows)
    order = sorted(range(n), key=lambda i: -float(rows[i]["mask"].sum()))
    labels = [-1] * n
    groups: list[list[int]] = []

    for i in order:
        if labels[i] >= 0:
            continue
        gid = len(groups)
        members = [i]
        labels[i] = gid
        for j in order:
            if labels[j] >= 0:
                continue
            if match(rows[i]["mask"], rows[j]["mask"]) >= threshold:
                labels[j] = gid
                members.append(j)
        groups.append(members)
    return labels, groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--extract", action="store_true")
    ap.add_argument("--base", type=Path, default=ROOT / "debug" / "vision")
    ap.add_argument("--sheet", type=int, default=0, help="how many decals to render")
    ap.add_argument("--cluster", action="store_true")
    ap.add_argument("--threshold", type=float, default=0.62)
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/bank/sheet.png"))
    args = ap.parse_args()

    if args.extract:
        CACHE.mkdir(parents=True, exist_ok=True)
        rows = extract(args.base)
        (CACHE / "rows.pkl").write_bytes(pickle.dumps(rows))
        print(f"  extracted {len(rows)} decals from {len({r['wave'] for r in rows})} waves")
        per = {}
        for r in rows:
            per[r["wave"]] = per.get(r["wave"], 0) + 1
        for w, n in per.items():
            print(f"    {w:34} {n}")

    if args.sheet:
        rows = pickle.loads((CACHE / "rows.pkl").read_bytes())
        pick = list(range(min(args.sheet, len(rows))))
        p = sheet(rows, args.out, order=pick)
        print(f"  sheet -> {p}")

    if args.cluster:
        rows = pickle.loads((CACHE / "rows.pkl").read_bytes())
        labels, groups = cluster(rows, args.threshold)
        sizes = sorted((len(g) for g in groups), reverse=True)
        print(f"  {len(rows)} decals -> {len(groups)} clusters at IoU>={args.threshold}")
        print(f"  sizes: {sizes[:24]}{' ...' if len(sizes) > 24 else ''}")
        print(f"  singletons: {sum(1 for s in sizes if s == 1)}")
        # render grouped: each cluster on its own row block
        order = [i for g in groups for i in g]
        p = sheet(rows, args.out.with_name("clusters.png"), cols=12, tile=56, order=order)
        (CACHE / "groups.pkl").write_bytes(pickle.dumps(groups))
        print(f"  clusters -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
