#!/usr/bin/env python3
"""Ask the model for a count per tile instead of one answer for ten.

`wave_solver.py` sends a single montage. That is budget-efficient, but a vision
model downsamples to roughly 1.5-2k pixels on the long edge, so a 2060px montage of
eleven panels leaves each panel about 200px — and the symbols inside a panel are
~30px. The model is being asked to count things it can barely resolve.

This sends the key plus each tile as its own attachment in one conversation with
labels, and asks for a count per tile. Each panel is then processed at its own
resolution, and the question is easier: counting one pile at a time beats comparing
ten piles at once.

The trade is uploads: 11 per wave against an ~18 budget. That only works because a
fresh anonymous identity is minted per wave (`solve_live` already does this) — the
identity is flagged after a single turn anyway, so there is nothing to save.

    ./tools/tile_vote.py --strip /tmp/opencode/gate/strip_00.jpg
    ./tools/tile_vote.py --strip ... --repeat 2      # agreement between attempts
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.chatgpt_anon import AnonChat
from tools.wave_montage import split_strip

COUNT_PROMPT = (
    "The first image is the KEY: one die symbol plus a required count 'x N'. "
    "The other ten images are numbered tiles, each a pile of dice. "
    "For every tile, count how many visible dice faces show exactly the KEY symbol. "
    "Reply with ten numbers separated by spaces, tiles 1 to 10 in order, "
    "numbers only, no words."
)

PICK_PROMPT = (
    "The first image is the KEY: one die symbol plus a count 'x N'. "
    "The other ten images are numbered tiles. Which single tile shows that symbol "
    "exactly N times? Reply with the tile number only."
)


def digits(text: str) -> list[int]:
    """Ten tile counts, or whatever the reply actually contains."""
    return [int(d) for d in re.findall(r"\d", text or "")]


def one_round(chat: AnonChat, images: list[Path], labels: list[str]) -> dict:
    out: dict = {}
    try:
        r = chat.ask_many(COUNT_PROMPT, images, labels)
        out["counts_reply"] = (r.get("text") or "")[:220]
        out["counts"] = digits(r.get("text"))[:10]
        out["counts_elapsed"] = r.get("elapsed")
    except Exception as exc:
        out["counts_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    try:
        r = chat.ask_many(PICK_PROMPT, images, labels)
        text = r.get("text") or ""
        m = re.search(r"\b(10|[1-9])\b", text)
        out["pick_reply"] = text[:200]
        out["pick"] = int(m.group(1)) if m else None
        out["pick_elapsed"] = r.get("elapsed")
    except Exception as exc:
        out["pick_error"] = f"{type(exc).__name__}: {str(exc)[:80]}"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strip", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/tiles"))
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--n", type=int, default=None,
                    help="target count, if known; otherwise inferred from the reply")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    key_img, tiles = split_strip(args.strip)
    size = 200 * args.scale
    key_path = args.out / "key.png"
    key_img.convert("RGB").resize((size, int(size * key_img.size[1] / key_img.size[0]))).save(key_path)
    paths = [key_path]
    for i, tile in enumerate(tiles, 1):
        p = args.out / f"tile{i:02d}.png"
        tile.convert("RGB").resize((size, size)).save(p)
        paths.append(p)
    labels = ["KEY (symbol + count)"] + [f"TILE {i}" for i in range(1, len(paths))]

    rounds = []
    for r in range(args.repeat):
        chat = AnonChat(proxy=None, timeout=90)
        print(f"  round {r + 1}: identity {chat.device_id[:8]}")
        t0 = time.time()
        out = one_round(chat, paths, labels)
        out["seconds"] = round(time.time() - t0, 1)
        rounds.append(out)
        if out.get("counts"):
            print(f"    counts {out['counts']}")
        else:
            print(f"    counts failed: {out.get('counts_error') or out.get('counts_reply')}")
        print(f"    pick   {out.get('pick')}  {str(out.get('pick_reply'))[:90]}")
        print(f"    {out['seconds']}s")

    if len(rounds) > 1 and all(r.get("counts") for r in rounds):
        agree = sum(1 for i in range(10)
                    if len({r["counts"][i] for r in rounds if len(r["counts"]) > i}) == 1)
        print(f"  per-tile agreement across rounds: {agree}/10")

    (args.out / "vote.json").write_text(json.dumps(rounds, indent=1))
    print(f"  -> {args.out / 'vote.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
