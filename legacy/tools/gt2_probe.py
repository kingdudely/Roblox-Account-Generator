#!/usr/bin/env python3
"""Ask Arkose, per proxy, whether it will hand out a token without a puzzle.

    python tools/gt2_probe.py --proxy-file proxies.txt --limit 50 --workers 10

Each result is one of:

    SUPPRESSED   token returned with sup=1 -> no puzzle, no solver, no browser
    CHALLENGE    token returned, sup absent -> a puzzle will be shown
    DENIED       gt2 refused the request    -> that IP is not usable at all
    ERROR        transport / proxy failure

This is the measurement that decides the architecture. If SUPPRESSED dominates a
proxy pool, you never need a vision model: the account pipeline is signup +
token, both pure HTTP. If CHALLENGE dominates, the pool is wrong, or you need a
solver. If DENIED dominates, the pool is datacenter or already burned.

Nothing here signs anything up — it is one gt2 call per proxy.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from roblox_creator import proxyurl  # noqa: E402
from roblox_creator.arkose_py import (  # noqa: E402
    ArkoseClient,
    ArkoseError,
    Denied,
    load_fingerprints,
)


def read_proxies(path: str | None, inline: list[str]) -> list[str]:
    """Every vendor line shape -> explicit-scheme URLs.

    Raw lines are not URLs: `host:port:user:pass` parses as a hostname with a
    colon in it and tls_client rejects it before a packet leaves the box. That
    looked like "no suppression on this pool" and was really "no probe at all".
    """
    from roblox_creator.config import load_proxies

    out: list[str] = []
    for raw in inline:
        try:
            out.append(proxyurl.parse(raw).url())
        except Exception:
            print(f"  ! unparseable proxy: {raw[:50]}")
    if path:
        p = Path(path)
        if not p.exists():
            print(f"proxy file not found: {p}")
            raise SystemExit(2)
        out.extend(load_proxies(p))
    seen, unique = set(), []
    for proxy in out:
        if proxy not in seen:
            seen.add(proxy)
            unique.append(proxy)
    return unique


def preflight(proxy: str) -> dict:
    """Cheap sanity check before spending a gt2 call on this proxy."""
    try:
        from curl_cffi import requests as curl_requests

        s = curl_requests.Session(impersonate="chrome")
        s.proxies = {"http": proxy, "https": proxy}
        r = s.get("https://ipinfo.io/json", timeout=12)
        if r.status_code != 200:
            return {"ok": False, "detail": f"ipinfo {r.status_code}"}
        body = r.json()
        org = (body.get("org") or "").lower()
        residential = not any(k in org for k in (
            "hosting", "datacenter", "cloud", "amazon", "google", "microsoft",
            "digitalocean", "ovh", "hetzner", "contabo", "linode", "vultr",
        ))
        return {"ok": True, "ip": body.get("ip"), "org": body.get("org"),
                "country": body.get("country"), "residential": residential}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {str(exc)[:70]}"}


def probe(proxy: str | None, fingerprints: list[dict], timeout: float) -> dict:
    started = time.time()
    label = proxy or "direct"
    try:
        client = ArkoseClient(proxy=proxy, fingerprints=fingerprints, transport="tls")
        client.load_config()
        token = client.gt2()
        if token.sup:
            verdict = "SUPPRESSED"
        elif token.raw:
            verdict = "CHALLENGE"
        else:
            verdict = "ERROR"
        return {"proxy": label, "verdict": verdict, "region": token.region,
                "ag": token.ag, "token": token.raw[:60],
                "seconds": round(time.time() - started, 2)}
    except Denied:
        return {"proxy": label, "verdict": "DENIED", "seconds": round(time.time() - started, 2)}
    except ArkoseError as exc:
        return {"proxy": label, "verdict": "ERROR", "detail": str(exc)[:120],
                "seconds": round(time.time() - started, 2)}
    except Exception as exc:  # proxy down, DNS, TLS
        return {"proxy": label, "verdict": "ERROR",
                "detail": f"{type(exc).__name__}: {str(exc)[:100]}",
                "seconds": round(time.time() - started, 2)}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gt2_probe.py")
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy", action="append", default=[])
    p.add_argument("--limit", type=int, default=0, help="probe at most N proxies")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--json", default=None, help="write raw results here")
    p.add_argument("--direct", action="store_true",
                   help="probe your own connection (no proxy) — use a phone hotspot")
    p.add_argument("--preflight", action="store_true",
                   help="check exit IP/org per proxy first (refuses datacenter)")
    args = p.parse_args(argv)

    proxies = read_proxies(args.proxy_file, args.proxy)
    if args.limit:
        proxies = proxies[: args.limit]
    if args.direct and not proxies:
        fingerprints = load_fingerprints()
        print("direct probe (no proxy) — this is your own exit IP\n")
        result = probe(None, fingerprints, 25)
        print(f"  {result['verdict']:<10} {result.get('detail') or result.get('token') or ''}")
        if result["verdict"] == "SUPPRESSED":
            print("\n  this IP needs no solver. a residential/mobile pool will scale this.")
            print("  ask for rotating residential bandwidth, not static IPs.")
        elif result["verdict"] == "CHALLENGE":
            print("\n  token issued, puzzle required. this IP is watched but not blocked.")
        elif result["verdict"] == "DENIED":
            print("\n  this IP is refused outright — same as WARP. try a mobile hotspot.")
        return 0 if result["verdict"] == "SUPPRESSED" else 1
    if not proxies:
        print("no proxies given: --proxy host:port:user:pass  or  --proxy-file file")
        return 2

    fingerprints = load_fingerprints()
    print(f"probing {len(proxies)} proxy(ies) with {len(fingerprints)} fingerprints "
          f"across {args.workers} workers\n")

    if args.preflight:
        print("preflight: exit IP + org per proxy")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            infos = list(pool.map(preflight, proxies))
        alive = []
        for proxy, info in zip(proxies, infos):
            if not info["ok"]:
                print(f"  ? DEAD      {proxy[:44]:<44} {info.get('detail')}")
                continue
            tag = "res" if info.get("residential") else "DC!"
            print(f"  . {tag:<9} {proxy[:44]:<44} {info.get('ip')} {info.get('org')}")
            if info.get("residential"):
                alive.append(proxy)
        if not alive:
            print("\n  no residential proxies left to probe")
            return 2
        proxies = alive
        print(f"\n  {len(proxies)} residential proxy(ies) to probe\n")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda x: probe(x, fingerprints, 25), proxies):
            results.append(result)
            mark = {"SUPPRESSED": "++", "CHALLENGE": " +", "DENIED": " -", "ERROR": " ?"}[result["verdict"]]
            detail = result.get("detail") or f"{result.get('seconds')}s"
            print(f"  {mark} {result['verdict']:<10} {result['proxy'][:44]:<44} {detail}")

    counts: dict[str, int] = {}
    for result in results:
        counts[result["verdict"]] = counts.get(result["verdict"], 0) + 1
    total = len(results)
    suppressed = counts.get("SUPPRESSED", 0)

    print(f"\n  suppressed {suppressed}/{total} "
          f"({suppressed / total * 100:.0f}%)  "
          + "  ".join(f"{k.lower()} {v}" for k, v in sorted(counts.items()) if k != "SUPPRESSED"))
    if suppressed:
        print(f"\n  at {suppressed / total:.0%} suppression this pool needs no solver.")
        print("  accounts/day is then limited by proxies x attempts-per-IP-per-day,")
        print("  not by captcha cost: 50,000/day at 4 accounts/IP/day = 12,500 IPs.")

    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))
        print(f"\n  raw results: {args.json}")
    return 0 if suppressed else 1


if __name__ == "__main__":
    raise SystemExit(main())
