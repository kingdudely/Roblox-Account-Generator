#!/usr/bin/env python3
"""Ask NopeCHA's Arkose recognition model to solve a Roblox dice wave.

Why NopeCHA and not a token service
-----------------------------------
Every other solver sells a *token*: you hand it a URL and sitekey, it drives the
whole Arkose flow from its own IP, and returns a token. That fights our
architecture — we already own the transport, the token is bound to the session
and exit, and a token minted elsewhere is exactly the inconsistency that gets a
signup refused.

NopeCHA's Recognition API is the opposite shape: images in, indices out, no
proxy, no session, no token. That drops straight into the slot our own VLM
occupies and costs one HTTP round trip.

The contract, though, is narrower than the site it serves
---------------------------------------------------------
    POST /v1/recognition/funcaptcha        one image holding a 3x2 grid
      -> {"data": [false, false, true, false, false, false]}

so the whole challenge must be expressed as 6 tiles in one picture. Roblox's
`MatchDiceIconCount` wave is 1 key + 10 candidates, which is 11 tiles. There is
no 11-tile shape, so this composes **two** 3x2 grids per wave:

    grid A = key, cand1..cand5     grid B = key, cand6..cand10

The key is repeated in both because it is the entire question. Tile 0 of each
mask is the key and is discarded; the first true among tiles 1..5 is the answer.

Their docs' own Tile example is `"Pick the dice pair whose top sides add up to
14"`, so dice arithmetic is in-distribution. Whether *counting occurrences of a
symbol* is, is the thing this probe measures.

    ./tools/nopecha_probe.py --status --key $NOPECHA_KEY
    ./tools/nopecha_probe.py --wave debug/vision/20261029-084526-unknown-1 --key ...
    ./tools/nopecha_probe.py --all --key ... --save /tmp/opencode/nopecha-labels.json
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API = "https://api.nopecha.com/v1"
TILE = 200
SCALE = 2
PAD = 8
COLS, ROWS = 3, 2

TASK = ("Tile 1 is the KEY: it shows one symbol and a number N written on it. "
        "Select the tile among the others that shows that same symbol exactly "
        "N times.")


# ------------------------------------------------------------------ image work


def _tile_image(path: Path):
    from PIL import Image

    with Image.open(path) as im:
        return im.convert("RGB").resize((TILE * SCALE, TILE * SCALE), Image.LANCZOS)


def compose_grid(images: list[Path], cols: int = COLS, rows: int = ROWS) -> str:
    """Tiles in a uniform grid, numbered — returned as a data URI.

    The documented shape is 3x2. A wave has 11 tiles (key + 10 candidates), which
    does not fit, so the default path sends two 3x2 grids. `--grid 5x2` instead
    sends all eleven at once and takes whatever mask length comes back — if the
    model segments a 5x2 grid it halves the cost per wave, which at 50k
    accounts/day is the difference between two Enterprise subscriptions and four.
    """
    from PIL import Image, ImageDraw, ImageFont

    cell = TILE * SCALE
    label_h = 26
    width = cols * cell + (cols + 1) * PAD
    height = rows * (cell + label_h) + (rows + 1) * PAD
    canvas = Image.new("RGB", (width, height), (18, 18, 22))
    draw = ImageDraw.Draw(canvas)
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
    except Exception:
        font = ImageFont.load_default()

    for i, path in enumerate(images[:cols * rows]):
        row, col = divmod(i, cols)
        x = PAD + col * (cell + PAD)
        y = PAD + row * (cell + label_h + PAD)
        draw.text((x + 4, y + 1), str(i + 1), fill=(255, 255, 0), font=font)
        canvas.paste(_tile_image(path), (x, y + label_h))

    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=92)
    encoded = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/jpeg;base64,{encoded}"


# ----------------------------------------------------------------------- http


def _session():
    try:
        from curl_cffi import requests as creq
        return creq.Session(impersonate="chrome", timeout=90)
    except Exception:
        import requests
        return requests.Session()


def status(key: str | None) -> dict:
    http = _session()
    params = {"key": key} if key else {}
    resp = http.get(f"{API}/status", params=params)
    try:
        return resp.json()
    except Exception:
        return {"error": resp.status_code, "body": resp.text[:200]}


def solve_grid(payload: str, task: str, key: str | None, timeout: float = 90.0) -> list | dict:
    """Submit one 3x2 grid and poll until it answers. Returns the mask or a dict."""
    http = _session()
    headers = {"content-type": "application/json"}
    body = {"task": task, "image_data": [payload]}
    if key:
        body["key"] = key
    resp = http.post(f"{API}/recognition/funcaptcha", headers=headers,
                     json=body)
    try:
        job = resp.json()
    except Exception:
        return {"error": "submit", "status": resp.status_code, "body": resp.text[:200]}
    if "data" not in job or not isinstance(job["data"], str):
        return {"error": "submit", "body": json.dumps(job)[:200]}

    job_id = job["data"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        params = {"id": job_id}
        if key:
            params["key"] = key
        r = http.get(f"{API}/recognition/funcaptcha", params=params)
        try:
            out = r.json()
        except Exception:
            time.sleep(1.2)
            continue
        data = out.get("data")
        if isinstance(data, list):
            return data
        if isinstance(data, int):          # 409 = still working
            time.sleep(1.2)
            continue
        if out.get("error"):
            return {"error": out.get("error"), "body": json.dumps(out)[:200]}
        time.sleep(1.2)
    return {"error": "timeout"}


# ---------------------------------------------------------------------- solve


def wave_files(wave: Path) -> tuple[Path, list[Path]]:
    key = wave / "key.img"
    cands = sorted(
        [p for p in wave.glob("cand*.img")],
        key=lambda p: int("".join(c for c in p.stem if c.isdigit()) or 0),
    )
    return key, cands


def pick(mask: list) -> tuple[int | None, str]:
    """First true among candidate tiles. Tile 0 is the key, always ignored."""
    if not isinstance(mask, list) or len(mask) < 2:
        return None, "bad mask"
    hits = [i for i, flag in enumerate(mask[1:], start=1) if flag]
    if not hits:
        return None, "all false"
    if len(hits) > 1:
        return hits[0], f"ambiguous {hits}"
    return hits[0], ""


def solve_wave(wave: Path, key: str | None, layout: str = "3x2") -> dict:
    key_img, cands = wave_files(wave)
    if not key_img.exists() or not cands:
        return {"wave": wave.name, "answer": None, "error": "incomplete wave"}

    if layout == "5x2":
        # one grid, key + all ten — unproven, but it halves the bill if the
        # model segments a 5x2 layout and returns an 11-entry mask
        groups = [[key_img] + cands[:10]]
        cols, rows, per = 5, 2, 10
    else:
        groups = [[key_img] + cands[:5], [key_img] + cands[5:10]]
        cols, rows, per = 3, 2, 5
    results: list[dict] = []
    t0 = time.time()
    for n, group in enumerate(groups):
        if not group:
            continue
        mask = solve_grid(compose_grid(group, cols, rows), TASK, key)
        if isinstance(mask, dict):
            results.append({"grid": n, "error": mask})
            continue
        tile, note = pick(mask)
        offset = n * per
        results.append({"grid": n, "mask": mask, "tile": tile, "note": note,
                        "answer": None if tile is None else offset + tile})

    answers = [r["answer"] for r in results if r.get("answer") is not None]
    answer = answers[0] if len(set(answers)) == 1 else (answers[0] if answers else None)
    return {
        "wave": wave.name,
        "answer": answer,
        "agree": len(set(answers)) <= 1,
        "grids": results,
        "elapsed": round(time.time() - t0, 1),
        "cost": len(results),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--key", default=os.environ.get("NOPECHA_KEY", ""))
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--wave", type=Path, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--base", type=Path, default=ROOT / "debug" / "vision")
    ap.add_argument("--limit", type=int, default=8,
                    help="stop after this many waves — the free tier is ~200/day")
    ap.add_argument("--grid", default="3x2", choices=("3x2", "5x2"),
                    help="3x2 = documented shape, two calls per wave. "
                         "5x2 = one call per wave, unproven")
    ap.add_argument("--save", type=Path, default=None)
    args = ap.parse_args()

    if not args.key:
        print("  no API key — pass --key or set NOPECHA_KEY")
        print("  free tier: key is optional and spends credits against your IP")
    if args.status or (not args.wave and not args.all):
        st = status(args.key or None)
        print(f"  status: plan={st.get('plan')} credit={st.get('credit')} "
              f"quota={st.get('quota')} ttl={st.get('ttl')}")

    if args.wave:
        waves = [args.wave]
    elif args.all:
        waves = sorted(d for d in args.base.iterdir()
                       if d.is_dir() and (d / "key.img").exists())[:args.limit]
    else:
        return 0
    if not waves:
        print("  no waves found")
        return 2

    per_wave = 1 if args.grid == "5x2" else 2
    print(f"  {len(waves)} wave(s), {per_wave} call(s) each "
          f"= {len(waves) * per_wave} credits, grid {args.grid}\n")
    rows = []
    for wave in waves:
        row = solve_wave(wave, args.key or None, args.grid)
        rows.append(row)
        mask = row["grids"][0].get("mask") if row["grids"] else None
        print(f"  {wave.name:32} ans={row['answer']} "
              f"{'agree' if row.get('agree') else 'disagree'} "
              f"{row['elapsed']}s  {row.get('error') or ''}  mask={mask}")

    answered = sum(1 for r in rows if r["answer"] is not None)
    print(f"\n  answered {answered}/{len(rows)}")
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(rows, indent=1))
        print(f"  labels -> {args.save}")
    return 0 if answered else 1


if __name__ == "__main__":
    raise SystemExit(main())
