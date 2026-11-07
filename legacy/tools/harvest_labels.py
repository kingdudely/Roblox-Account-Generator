#!/usr/bin/env python3
"""Fold verdict captures into the label set the matcher trains on.

`tools/verdict_probe.py` proves that the grading screen carries the correct tile.
This turns a directory of those captures into `debug/labels/verdict.jsonl`, in the
same schema `tools/label_ui.py` writes, so `tools/train_matcher.py` consumes both
without caring which teacher produced a row.

Precision beats recall here, deliberately. A wrong label teaches the student a
wrong rule and there is no signal downstream that catches it — Wazza0's measured
failure was 73% validation against a self-consistent teacher and 22% in
production. So a grading-screen capture is only accepted when:

    kind == "tile"          the crop is a candidate, not the reference card
    revealed is not None    the matcher named an index
    score >= --min-score    it is the same source pixels, not a lookalike
    margin >= --min-margin  it beat the runner-up by enough to be a match

A challenge that ends in `created` is accepted unconditionally: the account is
the server confirming all ten submitted answers, so the whole board is a positive
set. Failures give hard labels; successes give bulk ones.

A human label for the same strip always wins, and a strip labelled two different
ways is reported as a conflict rather than silently averaged.

    ./tools/harvest_labels.py                    # fold every capture
    ./tools/harvest_labels.py --dry-run
    ./tools/harvest_labels.py --report           # what the set looks like now
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VERDICT_DIR = ROOT / "debug" / "verdict"
SOLVER_DIR = ROOT / "debug" / "browser_solver"
HUMAN = ROOT / "debug" / "labels" / "labels.jsonl"
OUT = ROOT / "debug" / "labels" / "verdict.jsonl"


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def load_answers(case: Path) -> list[dict]:
    """Answers in either tool's shape.

    `verdict_probe.py` writes `report.json` (`answers`, keyed by round);
    `browser_solver.py` writes `run.json` (`waves`, keyed by wave). Both carry the
    instruction, which becomes the label's `rule`.
    """
    for name in ("report.json", "run.json"):
        path = case / name
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        rows = payload.get("answers") or payload.get("waves")
        if rows:
            return rows
    return []


def instruction_by_wave(case: Path) -> dict[int, str]:
    out: dict[int, str] = {}
    for row in load_answers(case):
        wave = row.get("round", row.get("wave"))
        if wave is None:
            continue
        out[int(wave)] = row.get("instruction", "") or ""
    return out


def analyse_case_cached(case: Path) -> dict | None:
    """The analysis for a capture, computed on first use and reused after.

    This is what lets a normal `browser_solver.py` run feed the label set: it
    writes `verdict/frames.json` and `run.json`, and this turns those into the
    same `analysis.json` the probe writes.
    """
    path = case / "analysis.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
    if not (case / "verdict" / "frames.json").exists():
        return None
    try:
        from tools.verdict_probe import analyse_case
    except Exception:
        return None
    analysis = analyse_case(case, load_answers(case))
    path.write_text(json.dumps(analysis, indent=1))
    return analysis


def confirmed_by_outcome(case: Path) -> list[dict]:
    """Labels a *successful* challenge gives for free.

    A created account is the server confirming every submitted answer, so a run
    that ends in `created` is ten verified positives — no grading screen needed.
    That is the other half of the flywheel: failures supply hard negatives,
    successes supply bulk positives.
    """
    report = None
    for name in ("report.json", "run.json"):
        path = case / name
        if not path.exists():
            continue
        try:
            report = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        break
    if not report or report.get("outcome") != "created":
        return []
    rows = report.get("answers") or report.get("waves") or []
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = []
    for row in rows:
        strip = case / (row.get("strip") or "")
        tile = row.get("answer")
        if not strip.exists() or not isinstance(tile, int):
            continue
        out.append({
            "strip": rel(strip),
            "tile": int(tile),
            "rule": (row.get("instruction") or "")[:240],
            "count": int(row.get("total") or 0),
            "ms": 0,
            "when": stamp,
            "who": "solved",
            "wave": int(row.get("round", row.get("wave")) or 0),
            "score": 1.0,
            "margin": 1.0,
        })
    return out


def harvest(cases: list[Path], min_score: float, min_margin: float) -> tuple[list[dict], list[dict], int]:
    """Return (accepted rows, rejected rows with a reason, captures skipped)."""
    accepted: list[dict] = []
    rejected: list[dict] = []
    skipped = 0
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")

    for case in cases:
        confirmed = confirmed_by_outcome(case)
        accepted.extend(confirmed)

        analysis = analyse_case_cached(case)
        if analysis is None:
            if not confirmed:
                skipped += 1
            continue

        rules = instruction_by_wave(case)
        for shot in analysis.get("shots") or []:
            if shot.get("kind") != "tile":
                rejected.append({"case": rel(case), "file": shot.get("file"),
                                 "reason": "not a candidate crop"})
                continue
            tile = shot.get("revealed")
            if tile is None:
                rejected.append({"case": rel(case), "file": shot.get("file"),
                                 "reason": "no revealed index"})
                continue
            best = shot.get("tile") or {}
            score = float(best.get("score") or 0.0)
            margin = float(best.get("margin") or 0.0)
            if score < min_score:
                rejected.append({"case": rel(case), "file": shot.get("file"),
                                 "reason": f"score {score:.2f} < {min_score}"})
                continue
            if margin < min_margin:
                rejected.append({"case": rel(case), "file": shot.get("file"),
                                 "reason": f"margin {margin:.3f} < {min_margin}"})
                continue
            strip = case / (shot.get("strip") or "")
            if not strip.exists():
                rejected.append({"case": rel(case), "file": shot.get("file"),
                                 "reason": "strip missing"})
                continue
            wave = int(shot.get("wave") or 0)
            accepted.append({
                "strip": rel(strip),
                "tile": int(tile),
                "rule": (rules.get(wave) or "")[:240],
                "count": int(best.get("index") or 0),
                "ms": 0,
                "when": stamp,
                "who": "verdict",
                "wave": wave,
                "score": round(score, 4),
                "margin": round(margin, 4),
            })
    return accepted, rejected, skipped


def resolve(existing: list[dict], fresh: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """Human labels win; a second, different label for one strip is a conflict."""
    chosen: dict[str, dict] = {}
    order: dict[str, int] = {}
    for row in existing:
        key = row.get("strip")
        if not key:
            continue
        chosen[key] = row
        order[key] = 0
    conflicts: list[dict] = []
    for row in fresh:
        key = row["strip"]
        if key in chosen:
            if int(chosen[key].get("tile", -1)) != int(row["tile"]):
                conflicts.append({
                    "strip": key,
                    "human_or_first": chosen[key].get("tile"),
                    "verdict": row["tile"],
                    "kept": chosen[key].get("tile"),
                    "who": chosen[key].get("who"),
                })
            continue
        chosen[key] = row
        order[key] = 1
    return chosen, conflicts


def report(rows: list[dict]) -> None:
    if not rows:
        print("  no labels yet")
        return
    by_who = Counter(row.get("who", "?") for row in rows)
    by_run = Counter(Path(row["strip"]).parent.name for row in rows)
    rules = Counter((row.get("rule") or "(blank)")[:52] for row in rows)
    print(f"  {len(rows)} labelled strips")
    print("  by teacher: " + ", ".join(f"{k}={v}" for k, v in by_who.items()))
    print("  by run:     " + ", ".join(f"{k}={v}" for k, v in by_run.most_common(8)))
    print("  by rule:")
    for rule, count in rules.most_common(6):
        print(f"    {count:>4}  {rule}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="sources", type=Path, nargs="*",
                    default=[VERDICT_DIR, SOLVER_DIR],
                    help="capture roots to read (default: verdict + browser_solver)")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--min-score", type=float, default=0.35)
    ap.add_argument("--min-margin", type=float, default=0.03)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--report", action="store_true",
                    help="print the current label set and exit")
    args = ap.parse_args()

    human = load_jsonl(HUMAN)
    fresh_written = load_jsonl(args.out)

    if args.report:
        merged, conflicts = resolve(human + fresh_written, [])
        report(list(merged.values()))
        if conflicts:
            print(f"  {len(conflicts)} conflicts")
        return 0

    cases: list[Path] = []
    for source in args.sources:
        if not source.exists():
            continue
        cases.extend(sorted(p for p in source.iterdir() if p.is_dir()))
    if not cases:
        print(f"no capture directories under {', '.join(str(s) for s in args.sources)}")
        return 1

    accepted, rejected, skipped = harvest(cases, args.min_score, args.min_margin)
    merged, conflicts = resolve(human + fresh_written, accepted)
    kept = [row for key, row in merged.items()]
    verdict_rows = [row for row in kept if row.get("who") == "verdict"]

    print(f"  {len(cases)} captures scanned "
          f"({skipped} without a grading-screen capture)")
    print(f"  {len(accepted)} candidate labels accepted, "
          f"{len(rejected)} rejected")
    for row in rejected[:8]:
        print(f"    reject {row.get('file', row.get('case'))}: {row['reason']}")
    if conflicts:
        print(f"  {len(conflicts)} conflicts (human label kept):")
        for row in conflicts[:5]:
            print(f"    {row['strip']}: kept {row['kept']}, verdict said "
                  f"{row['verdict']}")

    if args.dry_run:
        print("  dry run: nothing written")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("".join(json.dumps(row) + "\n" for row in verdict_rows))
    print(f"  wrote {len(verdict_rows)} rows -> {rel(args.out)}")
    print("  retrain with: ./tools/train_matcher.py --labels "
          f"{rel(HUMAN)},{rel(args.out)} --cv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
