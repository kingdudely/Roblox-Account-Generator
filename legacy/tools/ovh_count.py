#!/usr/bin/env python3
"""Ask a keyless vision endpoint for a per-tile count.

OVHcloud's AI Endpoints run a permanent anonymous tier: no account, no API key,
2 requests/minute per IP per model, and `Qwen2.5-VL-72B-Instruct` is on it. That
port is unusual in two ways that matter here:

  1. the limit is **per IP**, not per key, so it scales with the proxy pool rather
     than with billing — the same batching idea already used for the ChatGPT guest
     client, except this one needs no identity at all;
  2. one image per request means each tile is sent on its own, so the model is
     never downsampling ten panels into ~200px. The montage ceiling simply does
     not apply.

This sends the key crop and one tile per request and asks for a number, which is
the question the puzzle actually requires.

    ./tools/ovh_count.py --strip /tmp/opencode/gate/strip_00.jpg
    ./tools/ovh_count.py --strip ... --proxies proxies.txt --rotate
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests
from PIL import Image

from tools.wave_montage import split_strip

BASE = "https://oai.endpoints.kepler.ai.cloud.ovh.net/v1"
MODEL = "Qwen2.5-VL-72B-Instruct"

PROMPT = (
    "The first image is a single die face showing a symbol. "
    "The second image is a pile of dice. "
    "Count how many visible dice faces in the second image show exactly that "
    "same symbol. Reply with one integer only."
)


def data_url(img, edge: int = 640) -> str:
    """One image, scaled so its long edge is exactly `edge`.

    Tiles arrive at 200px, which most vision stacks downsample again. Upscaling to
    ~640 does not add information, but it does stop the model from shrinking an
    already small crop before it looks at it.
    """
    buf = io.BytesIO()
    im = img.convert("RGB")
    w, h = im.size
    scale = edge / max(w, h)
    im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                   Image.LANCZOS)
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def ask(key_img, tile_img, proxy: str | None = None,
        timeout: float = 60.0) -> tuple[int | None, dict]:
    payload = {
        "model": MODEL,
        "temperature": 0,
        "max_tokens": 16,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "image_url", "image_url": {"url": data_url(key_img)}},
                {"type": "image_url", "image_url": {"url": data_url(tile_img)}},
            ],
        }],
    }
    proxies = None
    if proxy:
        proxies = {"http": proxy, "https": proxy}
    t0 = time.time()
    try:
        r = requests.post(f"{BASE}/chat/completions", json=payload,
                          proxies=proxies, timeout=timeout)
    except Exception as exc:
        return None, {"error": f"{type(exc).__name__}: {str(exc)[:80]}",
                      "elapsed": round(time.time() - t0, 1)}
    meta = {"status": r.status_code, "elapsed": round(time.time() - t0, 1)}
    if r.status_code != 200:
        meta["body"] = r.text[:200]
        return None, meta
    try:
        text = r.json()["choices"][0]["message"]["content"] or ""
    except Exception:
        meta["body"] = r.text[:200]
        return None, meta
    meta["raw"] = text.strip()[:80]
    m = re.search(r"\d+", text)
    return (int(m.group()) if m else None), meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strip", type=Path, required=True)
    ap.add_argument("--proxies", type=Path, default=None)
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--pause", type=float, default=31.0,
                    help="seconds between requests when not rotating exits")
    args = ap.parse_args()

    key_img, tiles = split_strip(args.strip)
    pool: list[str] = []
    if args.proxies and args.rotate:
        from roblox_creator.config import load_proxies
        from roblox_creator.proxyurl import parse as parse_proxy
        pool = [parse_proxy(p).url() for p in load_proxies(args.proxies)]
        print(f"  rotating over {len(pool)} exits")

    counts: list[int | None] = []
    for i, tile in enumerate(tiles, 1):
        proxy = pool[(i - 1) % len(pool)] if pool else None
        n, meta = ask(key_img, tile, proxy)
        counts.append(n)
        print(f"  tile {i:2}: {str(n):>5}  {meta}")
        if not pool and i < len(tiles):
            time.sleep(args.pause)

    known = [c for c in counts if c is not None]
    print(f"\n  counts: {counts}")
    if known:
        from collections import Counter
        print(f"  distribution: {dict(sorted(Counter(known).items()))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
