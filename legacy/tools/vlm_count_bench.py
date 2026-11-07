#!/usr/bin/env python3
"""Measure whether a local VLM can actually count icons — with ground truth.

The Arkose `MatchDiceIconCount` puzzle needs one specific skill: given a scene
containing several kinds of icon, report how many of ONE kind are present. No
public round has a verified answer, so accuracy cannot be measured on real
captures. This generates scenes where the count is known, then asks the model.

    ./tools/vlm_count_bench.py --n 24
    ./tools/vlm_count_bench.py --n 40 --server http://127.0.0.1:8080
    ./tools/vlm_count_bench.py --n 24 --save debug/vlm_bench

Reports per-shape accuracy, exact-match rate, and latency. A model that only
gets "is it present" right but misses counts shows up as a large off-by-one
column, which is the failure mode that matters: one wrong count in ten waves
drops account success to ~60%.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SHAPES = ("circle", "triangle", "square", "star")
COLORS = {
    "circle": (200, 40, 40),
    "triangle": (30, 90, 200),
    "square": (30, 150, 60),
    "star": (210, 150, 20),
}

PROMPT = ("Count how many {shape}s are in this image. "
          "Answer with a single digit and nothing else.")


def draw_scene(path: Path, counts: dict[str, int], size: int = 256,
               seed: int = 0) -> None:
    """Scatter shapes on a white field. Positions are jittered but non-overlapping."""
    from PIL import Image, ImageDraw

    rng = random.Random(seed)
    img = Image.new("RGB", (size, size), (250, 250, 250))
    d = ImageDraw.Draw(img)

    placed: list[tuple[int, int, int]] = []
    for shape, n in counts.items():
        for _ in range(n):
            r = rng.randint(14, 22)
            for _attempt in range(200):
                x = rng.randint(r + 6, size - r - 6)
                y = rng.randint(r + 6, size - r - 6)
                if all((x - px) ** 2 + (y - py) ** 2 > (r + pr + 12) ** 2
                       for px, py, pr in placed):
                    break
            else:
                continue
            placed.append((x, y, r))
            col = COLORS[shape]
            if shape == "circle":
                d.ellipse([x - r, y - r, x + r, y + r], fill=col)
            elif shape == "square":
                d.rectangle([x - r, y - r, x + r, y + r], fill=col)
            elif shape == "triangle":
                d.polygon([(x, y - r), (x - r, y + r), (x + r, y + r)], fill=col)
            else:  # star
                pts = []
                for k in range(10):
                    ang = -1.5708 + k * 3.14159 / 5
                    rr = r if k % 2 == 0 else r * 0.45
                    pts.append((x + rr * __import__("math").cos(ang),
                                y + rr * __import__("math").sin(ang)))
                d.polygon(pts, fill=col)
    img.save(path)


def ask(server: str, image: Path, prompt: str, timeout: float = 300.0) -> tuple[str, float]:
    from curl_cffi import requests as creq

    b64 = base64.b64encode(image.read_bytes()).decode()
    t0 = time.time()
    r = creq.post(f"{server}/v1/chat/completions", timeout=timeout, json={
        "model": "local",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]}],
        "max_tokens": 1024,
        "temperature": 0.0,
    })
    dt = time.time() - t0
    data = r.json()
    msg = data["choices"][0]["message"]
    # MiniCPM-V 4.6 thinks first: content may be empty while reasoning_content
    # holds the whole trace. Fall back to the trace's tail so a truncated
    # response still yields a number instead of silently scoring as a miss.
    # content only: the reasoning trace contains list numbers ("1.", "2.") that
    # parse as bogus counts. An empty content means the trace was truncated.
    return (msg.get("content") or "").strip(), dt


def parse_int(text: str) -> int | None:
    m = re.search(r"-?\d+", text)
    return int(m.group()) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    ap.add_argument("--server", default="http://127.0.0.1:8080")
    ap.add_argument("--save", type=Path, default=None)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    rng = random.Random(args.seed)
    rows = []
    lat: list[float] = []

    for i in range(args.n):
        target = SHAPES[i % len(SHAPES)]
        counts = {s: rng.randint(0, 5) for s in SHAPES}
        counts[target] = rng.randint(1, 5)  # always something to count
        p = (args.save / f"scene{i:03d}.png") if args.save else Path(f"/tmp/opencode/scene{i:03d}.png")
        draw_scene(p, counts, seed=rng.randint(0, 10**6))
        ans, dt = ask(args.server, p, PROMPT.format(shape=target))
        got = parse_int(ans)
        lat.append(dt)
        rows.append({
            "i": i, "target": target, "truth": counts[target],
            "raw": ans[:40], "got": got, "ok": got == counts[target],
            "dt": round(dt, 2), "counts": counts,
        })
        flag = "OK " if got == counts[target] else "XX "
        print(f"  {flag}{i:>2} {target:8} truth={counts[target]} got={got} "
              f"({ans[:18]!r}) {dt:.1f}s", flush=True)

    n = len(rows)
    exact = sum(1 for r in rows if r["ok"])
    off1 = sum(1 for r in rows if r["got"] is not None and abs((r["got"] or 0) - r["truth"]) == 1)
    none = sum(1 for r in rows if r["got"] is None)
    lat.sort()

    print(f"\n  exact  {exact}/{n}  ({100*exact/n:.0f}%)")
    print(f"  off by one {off1}/{n}")
    print(f"  unparsed   {none}/{n}")
    print(f"  latency  median {lat[n//2]:.2f}s  p90 {lat[int(n*0.9)]:.2f}s")

    # what 90% per-round accuracy would mean over 10 waves
    p_round = exact / n
    print(f"\n  if a wave needs one correct count: per-round {p_round:.0%} "
          f"-> 10-wave pass {p_round**10:.1%}")

    out = {"exact": exact, "n": n, "off_by_one": off1, "rows": rows,
           "latency_median": lat[n // 2], "latency_p90": lat[int(n * 0.9)]}
    if args.save:
        (args.save / "results.json").write_text(json.dumps(out, indent=1))
        print(f"\n  saved -> {args.save}/results.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
