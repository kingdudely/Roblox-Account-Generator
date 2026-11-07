#!/usr/bin/env python3
"""Benchmark a vision model on captured Arkose dice rounds — offline, no browser.

Each round under debug/vision/ is a directory with key.img, cand1..candN.img and
label.json. This tool rebuilds the same Puzzle the live solver sees and asks a
model to answer it, so you can measure accuracy without burning a challenge.

  # 1. make a contact sheet per round so you can label them by eye
  ./tools/vlm_bench.py --sheet

  # 2. run the model (Ollama, llama.cpp, vLLM — anything OpenAI-compatible)
  ./tools/vlm_bench.py --model openbmb/minicpm-v4.6

  # 3. score against your labels
  ./tools/vlm_bench.py --model openbmb/minicpm-v4.6 --labels debug/labels/labels.json

Captured rounds carry no ground truth (accepted=null means the challenge failed
before a verdict existed), so 'index' in label.json is the old solver's guess,
NOT the right answer. Scoring only works after --sheet + human labelling.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from roblox_creator import vision  # noqa: E402

ROUND_GLOB = "*"


# --------------------------------------------------------------------------- loading


def load_round(path: Path) -> tuple[vision.Puzzle, int | None]:
    """Rebuild a Puzzle from a captured round. Returns (puzzle, old_pick)."""
    meta = json.loads((path / "label.json").read_text())
    puzzle = vision.Puzzle(
        instruction=meta.get("instruction", ""),
        total=int(meta.get("total", 0)),
        current=1,
    )
    key = path / "key.img"
    if key.exists():
        puzzle.key = key.read_bytes()
    for f in path.glob("cand*.img"):
        try:
            idx = int(f.stem[4:])
        except ValueError:
            continue
        puzzle.images[idx] = f.read_bytes()
    if not puzzle.total:
        puzzle.total = max(puzzle.images) if puzzle.images else 0
    return puzzle, meta.get("index")


def discover(rounds: Path, limit: int | None) -> list[Path]:
    found = sorted((p for p in rounds.glob(ROUND_GLOB) if (p / "label.json").exists()),
                   key=lambda p: p.name)
    return found[:limit] if limit else found


# --------------------------------------------------------------------------- sheets


def _font(size: int):
    from PIL import ImageFont
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_sheet(round_dir: Path, out_dir: Path, zoom: int = 2) -> Path:
    """One PNG: the key plus every numbered candidate, for eyeball labelling."""
    from PIL import Image, ImageDraw

    puzzle, _ = load_round(round_dir)
    tiles: list[tuple[str, Image.Image]] = []
    if puzzle.key:
        tiles.append(("KEY", Image.open(round_dir / "key.img").convert("RGB")))
    for idx in sorted(puzzle.images):
        tiles.append((str(idx), Image.open(round_dir / f"cand{idx}.img").convert("RGB")))

    tiles = [(label, im.resize((im.width * zoom, im.height * zoom), Image.LANCZOS))
             for label, im in tiles]

    pad, bar = 10, 46
    key_w = max(im.width for label, im in tiles if label == "KEY") if puzzle.key else 0
    cands = [(label, im) for label, im in tiles if label != "KEY"]
    cell_w = max(im.width for _, im in cands)
    cell_h = max(im.height for _, im in cands)
    cols = min(5, max(1, len(cands)))
    rows = (len(cands) + cols - 1) // cols

    width = pad + key_w + (pad + cell_w) * cols
    height = bar + pad + cell_h * rows
    sheet = Image.new("RGB", (width, height), (18, 18, 18))
    draw = ImageDraw.Draw(sheet)
    draw.text((pad, 12), f"{round_dir.name}   —   KEY = symbol + count, pick the candidate with that many on TOP faces",
              font=_font(20), fill=(235, 235, 235))

    x0 = pad
    for label, im in tiles:
        if label == "KEY":
            sheet.paste(im, (x0, bar + pad))
            draw.rectangle([x0 - 2, bar + pad - 2, x0 + im.width + 1, bar + pad + im.height + 1],
                           outline=(255, 210, 60), width=3)
    x = key_w + pad
    for n, (label, im) in enumerate(cands):
        r, c = divmod(n, cols)
        px = x + c * (cell_w + pad)
        py = bar + pad + r * cell_h
        sheet.paste(im, (px, py))
        draw.rectangle([px, py, px + im.width - 1, py + im.height - 1], outline=(70, 70, 70), width=2)
        draw.rectangle([px + 4, py + 4, px + 74, py + 44], fill=(0, 0, 0))
        draw.text((px + 14, py + 8), label, font=_font(28), fill=(120, 255, 140))

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{round_dir.name}.png"
    sheet.save(out)
    return out


def write_sheets(rounds: list[Path], out_dir: Path) -> None:
    labels_path = out_dir / "labels.json"
    labels = {}
    if labels_path.exists():
        try:
            labels = json.loads(labels_path.read_text())
        except json.JSONDecodeError:
            labels = {}
    for r in rounds:
        path = build_sheet(r, out_dir)
        labels.setdefault(r.name, None)
        print(f"  {path}")
    labels_path.write_text(json.dumps(labels, indent=1) + "\n")
    todo = sum(1 for v in labels.values() if v is None)
    print(f"\n{len(rounds)} sheet(s) -> {out_dir}")
    print(f"open each, read the key (symbol + 'x N'), pick the candidate with exactly N on top.")
    print(f"then fill {labels_path}  ({todo} unlabelled)")


# --------------------------------------------------------------------------- scoring


def score(rows: list[dict], labels: dict[str, int | None]) -> None:
    scored = [r for r in rows if labels.get(r["round"]) is not None and r["vlm"] is not None]
    print()
    if not scored:
        print("no labels yet — run --sheet, label by eye, then re-run with --labels.\n"
              "without ground truth this run proves nothing about accuracy.")
        return
    hits = sum(1 for r in scored if r["vlm"] == labels[r["round"]])
    acc = hits / len(scored)
    print(f"scored {len(scored)} round(s): {hits} correct  ->  {acc:.1%} per round")
    print(f"implied 10-wave challenge pass rate: {acc ** 10:.1%}")
    if len(scored) < 20:
        print(f"  (!) {len(scored)} rounds is a small sample — treat the number as a hint, not a fact.")
    wrong = [(r["round"], r["vlm"], labels[r["round"]]) for r in scored if r["vlm"] != labels[r["round"]]]
    if wrong:
        print("\nmisses:")
        for name, got, want in wrong:
            print(f"  {name}  said {got}, correct {want}")


# --------------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rounds", type=Path, default=ROOT / "debug/vision")
    ap.add_argument("--limit", type=int, default=None, help="only the first N rounds")
    ap.add_argument("--sheet", action="store_true", help="write contact sheets + labels.json template")
    ap.add_argument("--sheet-dir", type=Path, default=ROOT / "debug/labels")
    ap.add_argument("--labels", type=Path, default=None, help="human labels: {round: index}")
    ap.add_argument("--provider", default="local",
                    help="local (OpenAI-compatible) | gemini | onnx | stub")
    ap.add_argument("--base-url", default="http://localhost:11434/v1")
    ap.add_argument("--model", default="openbmb/minicpm-v4.6")
    ap.add_argument("--key", default="", help="API key for gemini")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--repeat", type=int, default=1, help="ask each round N times, report agreement")
    ap.add_argument("--json", type=Path, default=None, help="dump raw results here")
    args = ap.parse_args()

    round_dirs = discover(args.rounds, args.limit)
    if not round_dirs:
        print(f"no captured rounds under {args.rounds}")
        return 1

    print(f"{len(round_dirs)} round(s) under {args.rounds}")

    if args.sheet:
        write_sheets(round_dirs, args.sheet_dir)
        return 0

    labels: dict[str, int | None] = {}
    if args.labels:
        if not args.labels.exists():
            print(f"no labels file at {args.labels} — run --sheet first")
            return 1
        labels = json.loads(args.labels.read_text())

    try:
        solver = vision.build_vision_solver(
            args.provider, args.key, model=args.model, base_url=args.base_url)
    except Exception as exc:
        print(f"solver unavailable: {exc}")
        return 1
    solver.timeout = args.timeout

    print(f"model: {args.model}  via {args.provider} @ {args.base_url}\n")
    header = f"{'round':<34} {'onnx':>4} {'vlm':>4} {'label':>5} {'ok':>3} {'s':>6}"
    print(header)
    print("-" * len(header))

    rows: list[dict] = []
    for path in round_dirs:
        puzzle, old = load_round(path)
        picks: list[int] = []
        error = None
        t0 = time.time()
        for _ in range(max(1, args.repeat)):
            try:
                picks.append(solver.choose(puzzle))
            except vision.SolveError as exc:
                error = str(exc)
                break
        elapsed = time.time() - t0
        step = elapsed / max(1, args.repeat)

        vlm = picks[0] if picks else None
        agree = "-" if len(set(picks)) <= 1 else f"!{len(set(picks))}"
        want = labels.get(path.name)
        ok = "-" if (want is None or vlm is None) else ("Y" if vlm == want else "n")

        if error:
            print(f"{path.name:<34} {str(old):>4} {'ERR':>4} {'':>5} {'':>3} {step:>6.1f}")
            print(f"    {error[:110]}")
        else:
            vlm_cell = f"{vlm}{agree}" if agree != "-" else str(vlm)
            print(f"{path.name:<34} {str(old):>4} {vlm_cell:>4} {str(want):>5} {ok:>3} {step:>6.1f}")

        rows.append({"round": path.name, "onnx": old, "vlm": vlm,
                     "picks": picks, "label": want, "seconds": round(step, 2),
                     "instruction": puzzle.instruction, "error": error})

    if rows:
        both = [r for r in rows if r["vlm"] and r["onnx"]]
        if both:
            same = sum(1 for r in both if r["vlm"] == r["onnx"])
            print(f"\nvlm agrees with onnx on {same}/{len(both)} rounds")

    score(rows, labels)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
        print(f"\nraw results -> {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
