#!/usr/bin/env python3
"""Turn captured puzzles into a training set.

Every solved round is written by vision.solve_visual to:

    debug/vision/<stamp>-<ok|bad|unknown>-<index>/
        key.img  cand1.img .. candN.img  label.json

`accepted` is Arkose's own verdict, so `-ok-` directories are ground truth that
cost nothing to label. This script folds them into one JSONL.

    python tools/dataset.py --src debug/vision --out data/dice.jsonl
    python tools/dataset.py --src debug/vision --out data/dice.jsonl --include-unknown
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


def load_case(case: Path) -> dict | None:
    label_path = case / "label.json"
    if not label_path.exists():
        return None
    try:
        label = json.loads(label_path.read_text())
    except json.JSONDecodeError:
        return None
    key = case / "key.img"
    cands = sorted(case.glob("cand*.img"),
                   key=lambda p: int(p.stem.replace("cand", "") or 0))
    if not key.exists() or not cands:
        return None
    index = int(label.get("index") or 0)
    if not 1 <= index <= len(cands):
        return None
    return {
        "instruction": label.get("instruction", ""),
        "key": str(key),
        "candidates": [str(p) for p in cands],
        "answer": index,          # 1-based
        "accepted": label.get("accepted"),
        "source": label.get("solver", "?"),
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dataset.py")
    p.add_argument("--src", default="debug/vision", help="directory of captured cases")
    p.add_argument("--out", default="data/dice.jsonl")
    p.add_argument("--include-unknown", action="store_true",
                   help="keep cases where Arkose gave no verdict (unverified labels)")
    p.add_argument("--include-bad", action="store_true",
                   help="keep rejected cases too (labels are wrong, useful as negatives)")
    args = p.parse_args(argv)

    src = Path(args.src)
    if not src.exists():
        print(f"no captures at {src} — run the vision solver first")
        return 1

    rows: list[dict] = []
    stats: Counter[str] = Counter()
    for case in sorted(p for p in src.iterdir() if p.is_dir()):
        row = load_case(case)
        if row is None:
            stats["unusable"] += 1
            continue
        verdict = row["accepted"]
        if verdict is True:
            stats["accepted"] += 1
        elif verdict is False:
            stats["rejected"] += 1
        else:
            stats["unknown"] += 1
            if not args.include_unknown:
                continue
        if verdict is False and not args.include_bad:
            continue
        rows.append(row)

    if not rows:
        print(f"nothing usable in {src} ({dict(stats)})")
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")

    print(f"wrote {len(rows)} examples to {out}")
    print(f"  accepted {stats['accepted']}  rejected {stats['rejected']}  "
          f"unknown {stats['unknown']}  unusable {stats['unusable']}")
    if stats["accepted"] < 500:
        print(f"  note: {stats['accepted']} verified examples is thin — "
              "aim for 1-3k before trusting a fine-tune")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
