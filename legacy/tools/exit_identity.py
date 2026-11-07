#!/usr/bin/env python3
"""What network does each exit actually sit on?

Availability and trust are different questions. An exit can answer every request
and still be a bad place to create an account, because Roblox scores the signup
partly on the network it comes from. A datacenter ASN on a /24 with two thousand
other proxies is a signal; a residential line is not.

So this asks the exit to identify itself, through itself:

    ./tools/exit_identity.py --proxy-file /tmp/opencode/free_big.txt --sample 12
    ./tools/exit_identity.py --proxy-file proxies2.txt --sample 6
"""

from __future__ import annotations

import argparse
import random
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy

# ipinfo's keyless endpoint answers one line of JSON and carries org + hostname,
# which is where the datacenter tell lives.
LOOKUP = "https://ipinfo.io/json"


def identify(proxy_url: str, timeout: float = 18.0) -> dict:
    from curl_cffi import requests as creq

    out = {"proxy": proxy_url, "ip": None, "org": None, "country": None,
           "hostname": None, "error": None}
    session = creq.Session(impersonate="chrome", timeout=timeout)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = session.get(LOOKUP, headers={"accept": "application/json"})
        if resp.status_code != 200:
            out["error"] = f"{resp.status_code}"
            return out
        data = resp.json()
        out.update(ip=data.get("ip"), org=data.get("org"),
                   country=data.get("country"), hostname=data.get("hostname"))
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:36]}"
        return out
    finally:
        try:
            session.close()
        except Exception:
            pass


def classify(org: str | None, hostname: str | None) -> str:
    """Coarse label. Deliberately blunt: the useful question is residential or
    not, and a wrong 'residential' is more costly than a missed nuance."""
    blob = f"{org or ''} {hostname or ''}".lower()
    if not blob.strip():
        return "unknown"
    hosting = ("hosting", "datacenter", "data center", "cloud", "server",
               "vps", "colo", "amazon", "google", "microsoft", "azure",
               "alibaba", "aliyun", "tencent", "digitalocean", "ovh", "hetzner",
               "oracle", "linode", "contabo", "leaseweb", "m247", "choopa",
               "vultr", "cogent", "gcore", "scaleway")
    if any(word in blob for word in hosting):
        return "hosting"
    if any(word in blob for word in ("telecom", "broadband", "cable", "dsl",
                                     "fiber", "fibre", "comcast", "verizon",
                                     "at&t", "spectrum", "virgin", "orange",
                                     "vodafone", "telefonica", "telstra")):
        return "residential-ish"
    return "other"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, action="append", required=True)
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--timeout", type=float, default=18.0)
    args = ap.parse_args()

    for path in args.proxy_file:
        if not path.exists():
            print(f"{path}: missing")
            continue
        pool = load_proxies(path)
        picked = random.sample(pool, min(args.sample, len(pool)))
        print(f"\n{path.name}  ({len(pool)} lines, sampling {len(picked)})")

        rows: list[dict] = []
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(identify, parse_proxy(x).url(), args.timeout): x
                    for x in picked}
            for fut, raw in futs.items():
                r = fut.result()
                r["class"] = classify(r["org"], r["hostname"])
                rows.append(r)
                if r["error"]:
                    print(f"  {parse_proxy(raw).masked()[:36]:<36} {r['error']}")
                else:
                    print(f"  {r['ip']:<16} {r['country']:<3} "
                          f"{r['class']:<16} {(r['org'] or '')[:38]}")

        tally: dict[str, int] = {}
        for r in rows:
            tally[r["class"]] = tally.get(r["class"], 0) + 1
        print(f"  -> {tally}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
