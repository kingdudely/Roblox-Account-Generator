#!/usr/bin/env python3
"""BloxGen runner — buy accounts instead of solving captchas.

    python bloxgen_run.py --probe --key BLOX-...
    python bloxgen_run.py --key BLOX-... --count 100
    python bloxgen_run.py --key-file keys.txt --count 50000

One key is limited to one alt per 25 s (3,456/day). Fifteen keys clear 50k/day
for roughly $20 in per-account cost.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from roblox_creator.bloxgen import BloxGenError, BloxGenPool

# Daily `alt` cap per key, by role (docs.bloxgen.net/guides/roles). The cap,
# not the cooldown, is what bounds throughput.
PER_KEY_PER_DAY = {"free": 20, "premium": 50, "ultra": 100}
ALT_PRICE = 0.0004  # USD per generated alt


def collect_keys(args) -> list[str]:
    keys = list(args.key or [])
    if args.key_file:
        path = Path(args.key_file)
        if not path.exists():
            print(f"key file not found: {path}")
            raise SystemExit(2)
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                keys.append(line)
    # dedupe, preserve order
    seen, out = set(), []
    for key in keys:
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def plan(keys: int, count: int, role: str = "ultra") -> None:
    per_key = PER_KEY_PER_DAY.get(role.lower(), PER_KEY_PER_DAY["ultra"])
    per_day = keys * per_key
    if per_day <= 0:
        return
    days = count / per_day
    print(f"plan: {keys} {role} key(s) @ {per_key}/day = {per_day:,}/day")
    print(f"      {count:,} accounts ≈ {days:.1f} day(s) at that rate"
          f" · ${count * ALT_PRICE:,.2f} in generation")
    if per_day < count:
        needed = -(-count // per_key)
        print(f"      to finish {count:,} in one day you need {needed} keys"
              f" · ${needed * 14.99:,.0f} one-time (Ultra)" if role.lower()=="ultra"
              else f"      to finish {count:,} in one day you need {needed} keys")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bloxgen_run.py",
                                description="Generate Roblox accounts through the BloxGen API.")
    p.add_argument("--key", action="append", default=[], help="BLOX-… API key (repeatable)")
    p.add_argument("--key-file", default=None, help="file with one API key per line")
    p.add_argument("--count", type=int, default=10, help="how many accounts to generate")
    p.add_argument("--type", default="alt",
                   choices=["alt", "+30 days old", "+1 year old", "5+ years old", "dump", "18+ age verified"])
    p.add_argument("--region", default=None, help="2-letter country code (Ultra plan)")
    p.add_argument("--out", default="accounts/bloxgen", help="output directory")
    p.add_argument("--workers", type=int, default=None, help="parallel keys (default: all)")
    p.add_argument("--probe", action="store_true", help="check balance for each key and exit")
    p.add_argument("--role", default="ultra", choices=["free", "premium", "ultra"],
                   help="key role, sets the per-key daily cap used in the plan")
    p.add_argument("--plot-only", action="store_true", help="show throughput math and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    keys = collect_keys(args)

    if args.plot_only or not keys:
        plan(max(1, len(keys)), args.count, args.role)
        if not keys:
            print("\nadd a key:  --key BLOX-…  or  --key-file keys.txt")
            return 0 if args.plot_only else 2
        if args.plot_only:
            return 0

    print(f"keys={len(keys)} type={args.type} count={args.count} out={args.out}")
    plan(len(keys), args.count)

    pool = BloxGenPool(keys, account_type=args.type, region=args.region, out_dir=args.out)
    if args.probe:
        good = pool.probe()
        return 0 if good else 1

    try:
        made = pool.run(args.count, workers=args.workers)
    except BloxGenError as exc:
        print(f"error: {exc}")
        return 1

    print()
    print(pool.stats())
    if made:
        print(f"\ncredentials: {pool.store.credentials_file()}")
        print(f"cookies:     {pool.store.cookies_file()}")
    if made < args.count:
        print(f"\nshorter than requested: {made}/{args.count}. "
              "check key status, stock and daily caps above.")
    return 0 if made else 1


if __name__ == "__main__":
    sys.exit(main())
