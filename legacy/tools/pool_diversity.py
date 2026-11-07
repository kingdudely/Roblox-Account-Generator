#!/usr/bin/env python3
"""How much of a proxy list is actually distinct addresses?

A free list looks enormous because ports are cheap. One host behind a thousand
ports is a thousand lines and one address, and address diversity is the entire
property that matters for anti-fraud scoring — a thousand ports on one /24 are
worth less than ten exits on ten different networks.

This reads any proxy file and reports distinct hosts, /24s, and /16s, plus the
heaviest subnets so the shape is visible rather than inferred.

    ./tools/pool_diversity.py /tmp/opencode/free.txt
    ./tools/pool_diversity.py proxies.txt proxies2.txt --top 12
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.proxyurl import ProxyParseError, parse


def subnet(host: str, bits: int) -> str:
    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host
    nets = addr.networks if False else None
    prefix = bits if addr.version == 4 else 64
    try:
        net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
    except ValueError:
        return host
    return str(net)


def report(path: Path, top: int) -> dict:
    hosts: Counter = Counter()
    n24: Counter = Counter()
    n16: Counter = Counter()
    lines = 0
    bad = 0
    for raw in path.read_text(errors="replace").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        lines += 1
        try:
            p = parse(raw)
        except ProxyParseError:
            bad += 1
            continue
        hosts[p.host] += 1
        n24[subnet(p.host, 24)] += 1
        n16[subnet(p.host, 16)] += 1

    print(f"\n{path.name}")
    print(f"  lines {lines}   parsed {sum(hosts.values())}   unparsed {bad}")
    print(f"  distinct hosts {len(hosts)}   /24s {len(n24)}   /16s {len(n16)}")
    if hosts:
        ratio = len(hosts) / max(lines, 1)
        print(f"  host/line ratio {ratio:.2f}")
    print(f"  heaviest /24s:")
    for net, n in n24.most_common(top):
        print(f"    {net:<22} {n:>5} lines")
    return {"file": path.name, "lines": lines, "hosts": len(hosts),
            "n24": len(n24), "n16": len(n16)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    rows = [report(p, args.top) for p in args.files if p.exists()]
    print("\n" + "=" * 58)
    print(f"{'file':<22}{'lines':>8}{'hosts':>8}{'/24s':>8}{'/16s':>7}")
    for r in rows:
        print(f"{r['file']:<22}{r['lines']:>8}{r['hosts']:>8}"
              f"{r['n24']:>8}{r['n16']:>7}")
    print("=" * 58)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
