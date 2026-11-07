#!/usr/bin/env python3
"""Grow the captured-payload corpus by driving real browser signups in a loop.

Why this matters more than solver tuning
---------------------------------------
A replayed **desktop** payload is asked for ~5.8 waves; the mobile fingerprints in
`data/fingerprints.json` are asked for 10. Waves multiply the number of visual
solves, so halving them halves the whole solver bill — and the swap is a data
problem, not a code problem.

The catch is that one payload replayed 50k times is one device identity. So this
drives `tools/bda_hook.py` repeatedly with a *different* browser shape each time
(`browser.DESKTOP_PROFILES`) and a different exit, then folds each capture into
`data/payloads/` with `tools/fp_harvest.py`.

Cost: a real browser, once per payload, offline from the runtime. Nothing here is
in the account-creation hot path.

    ./tools/harvest_corpus.py --runs 12
    ./tools/harvest_corpus.py --runs 3 --headless --wait 45
    ./tools/harvest_corpus.py --runs 0 --report      # just inventory what we have
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PY = str(ROOT / ".venv" / "bin" / "python")
STORE = ROOT / "data" / "payloads"


def report() -> int:
    """What the corpus looks like, and whether it is actually diverse."""
    files = sorted(STORE.glob("*.json"))
    if not files:
        print("  corpus is empty — run without --report")
        return 1
    rows = []
    for path in files:
        rec = json.loads(path.read_text())
        rows.append((path.name, "mobile" if rec.get("mobile") else "desktop",
                     rec.get("screen"), rec.get("pixel_ratio"),
                     (rec.get("user_agent") or "")[:40],
                     rec.get("enhanced_fp_entries")))
    print(f"  {'file':<16} {'kind':<8} {'screen':<12} {'dpr':<5} {'ua':<42} entries")
    for row in rows:
        print(f"  {row[0]:<16} {row[1]:<8} {str(row[2]):<12} {str(row[3]):<5} "
              f"{row[4]:<42} {row[5]}")
    desktops = {r[2] for r in rows if r[1] == "desktop"}
    uas = {r[4] for r in rows if r[1] == "desktop"}
    print(f"\n  {len(rows)} payload(s), {len(desktops)} distinct desktop screen(s), "
          f"{len(uas)} distinct desktop UA(s)")
    if len(rows) > 1 and len(desktops) == 1:
        print("  WARNING: every desktop payload is the same device — no diversity")
    return 0


def run_once(index: int, args: argparse.Namespace) -> Path | None:
    """One capture, retried on a different exit.

    A capture fails for boring reasons — a dead proxy times out on
    `roblox.com`, or the page closes — and each dead exit used to cost a whole
    slot. Retrying inside the slot keeps `--runs` meaning "captures wanted".
    """
    for attempt in range(args.tries):
        out = _run_once(index, args, attempt)
        if out is not None:
            return out
        if attempt + 1 < args.tries:
            print(f"    retry {attempt + 1}/{args.tries - 1} on a new exit")
    return None


def _run_once(index: int, args: argparse.Namespace,
              attempt: int = 0) -> Path | None:
    tag = f"corpus{index:02d}" + (f"r{attempt}" if attempt else "")
    cmd = [PY, str(ROOT / "tools" / "bda_hook.py"),
           "--proxy-file", str(args.proxy_file), "--tag", tag,
           "--wait", str(args.wait)]
    if args.headless:
        cmd.append("--headless")
    if args.profile is not None:
        cmd += ["--profile", str(args.profile)]

    print(f"  [{index + 1}/{args.runs}] browser capture …", flush=True)
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=args.timeout)
    out = (proc.stdout or "") + (proc.stderr or "")
    match = re.search(r"^\s*->\s*(\S.*)$", out, re.M)
    if not match:
        print(f"    no output dir (exit {proc.returncode}, {time.time() - t0:.0f}s)")
        print("    " + "\n    ".join(out.strip().splitlines()[-4:]))
        return None
    dirpath = Path(match.group(1).strip())
    if not (dirpath / "crypto.json").exists():
        print(f"    no crypto.json in {dirpath.name} — gt2 never fired")
        return None

    harvest = subprocess.run(
        [PY, str(ROOT / "tools" / "fp_harvest.py"), "--from-capture", str(dirpath)],
        capture_output=True, text=True, timeout=120)
    print("    " + " ".join((harvest.stdout or "").split()))
    return dirpath if harvest.returncode == 0 else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=60.0)
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--profile", type=int, default=None,
                    help="pin one profile instead of varying it")
    ap.add_argument("--tries", type=int, default=3,
                    help="capture attempts per slot on fresh exits")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    if args.report:
        return report()

    print(f"  corpus before: {len(list(STORE.glob('*.json')))} payload(s)")
    made = 0
    for i in range(args.runs):
        try:
            if run_once(i, args) is not None:
                made += 1
        except subprocess.TimeoutExpired:
            print(f"    timed out after {args.timeout:.0f}s — moving on")
        if i + 1 < args.runs:
            time.sleep(2)

    print(f"\n  harvested {made}/{args.runs}")
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
