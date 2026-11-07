#!/usr/bin/env python3
"""Harvest the free proxy lists, prove which entries still carry traffic.

Free lists are not a product, they are a census of every scraped endpoint on the
internet: mostly dead, largely shared, and updated at a different rate than they
decay. That makes them useless as a promise and occasionally useful as a supply —
but only if something measures them first instead of trusting the file.

So this does three things and nothing else:

    fetch    pull a set of public lists (socks5 / socks4 / http)
    verify   open a TLS connection to roblox.com through each candidate, in
             parallel, and keep only the ones that answer 200
    write    emit a canonical file of survivors in the project's own proxy
             format, ready for `--proxy-file`

Latency is recorded, not filtered on beyond a ceiling, because a slow exit that
works beats a fast exit that is already dead by the time you read this file.

    ./tools/free_pool.py --limit 400 --out /tmp/opencode/free.txt
    ./tools/free_pool.py --per-source 150 --scheme socks5 --workers 60
    ./tools/free_pool.py --fetch-only --raw-out /tmp/opencode/free_raw.txt
"""

from __future__ import annotations

import argparse
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# Plain-text lists only. Everything here returns `host:port` one per line, with
# no auth — which is exactly the shape free endpoints come in.
SOURCES: list[tuple[str, str, str]] = [
    ("proxyscrape5",
     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks5"
     "&timeout=10000&country=all", "socks5"),
    ("proxyscrape4",
     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=socks4"
     "&timeout=10000&country=all", "socks4"),
    ("proxyscrapeh",
     "https://api.proxyscrape.com/v2/?request=displayproxies&protocol=http"
     "&timeout=10000&country=all", "http"),
    ("speedx5",
     "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks5.txt",
     "socks5"),
    ("speedx4",
     "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/socks4.txt",
     "socks4"),
    ("speedxh",
     "https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt",
     "http"),
    ("monosans5",
     "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/socks5.txt",
     "socks5"),
    ("monosansh",
     "https://raw.githubusercontent.com/monosans/proxy-list/main/proxies/http.txt",
     "http"),
    ("hookzof5",
     "https://raw.githubusercontent.com/hookzof/socks5_list/master/proxy.txt",
     "socks5"),
    ("plistd5",
     "https://www.proxy-list.download/api/v1/get?type=socks5", "socks5"),
    ("plistdh",
     "https://www.proxy-list.download/api/v1/get?type=http", "http"),
    ("rooster5",
     "https://raw.githubusercontent.com/roosterkid/openproxylist/main/SOCKS5_RAW.txt",
     "socks5"),
    ("zloi5",
     "https://raw.githubusercontent.com/zloi-user/hideip.me/main/socks5.txt",
     "socks5"),
    ("zloih",
     "https://raw.githubusercontent.com/zloi-user/hideip.me/main/http.txt",
     "http"),
    ("vakhov5",
     "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/socks5.txt",
     "socks5"),
    ("vakhovh",
     "https://raw.githubusercontent.com/vakhov/fresh-proxy-list/master/http.txt",
     "http"),
    ("shifty5",
     "https://raw.githubusercontent.com/ShiftyTR/Proxy-List/master/socks5.txt",
     "socks5"),
    # proxifly publishes CSV with `proxy,country,city` per row. That is worth
    # more than a bare list: country can be filtered before any probe is spent,
    # and matching the exit's country to the account's is not cosmetic — a US
    # signup from a VN datacenter is a stronger signal than either fact alone.
    ("pxfly5",
     "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/"
     "proxies/protocols/socks5/data.csv", "socks5", "csv"),
    ("pxfly4",
     "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/"
     "proxies/protocols/socks4/data.csv", "socks4", "csv"),
    ("pxflyh",
     "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/"
     "proxies/protocols/http/data.csv", "http", "csv"),
    ("pxflyhs",
     "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/"
     "proxies/protocols/https/data.csv", "https", "csv"),
]

# One shape conversion for every scraped artifact: `host:port` somewhere in the
# line, optionally behind a scheme and optionally carrying creds.
LINE_RE = re.compile(
    r"^(?:(?P<scheme>socks5h?|socks4a?|https?)://)?"
    r"(?:(?P<user>[^:@/\s]+):(?P<pass>[^@/\s]+)@)?"
    r"(?P<host>\[[0-9a-fA-F:]+\]|[A-Za-z0-9._-]+)"
    r":(?P<port>\d{2,5})$")


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch(url: str, timeout: float = 25.0) -> str:
    from curl_cffi import requests as creq

    resp = creq.get(url, impersonate="chrome", timeout=timeout,
                    headers={"user-agent": UA})
    if resp.status_code != 200:
        return ""
    return resp.text or ""


def harvest(per_source: int, country: str = "any") -> dict[str, tuple[str, list[str]]]:
    """source -> (scheme, candidate lines)."""
    out: dict[str, tuple[str, list[str]]] = {}
    for entry in SOURCES:
        name, url, scheme = entry[0], entry[1], entry[2]
        kind = entry[3] if len(entry) > 3 else "txt"
        t0 = time.time()
        try:
            text = fetch(url)
        except Exception as exc:
            log(f"  {name:<12} fetch failed: {type(exc).__name__}: {str(exc)[:44]}")
            continue
        seen: list[str] = []
        dropped_country = 0
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue

            if kind == "csv":
                # `socks5://ip:port,CC,city`. The city is unused; the country is
                # the whole reason this source is here.
                parts = line.split(",")
                line = parts[0].strip()
                cc = (parts[1].strip().upper() if len(parts) > 1 else "")
                if country != "any":
                    if cc != country:
                        dropped_country += 1
                        continue

            m = LINE_RE.match(line)
            if not m:
                continue
            # keep the declared scheme unless the line carries its own
            line_scheme = (m.group("scheme") or scheme).lower()
            if line_scheme == "socks5h":
                line_scheme = "socks5"
            if line_scheme == "socks4a":
                line_scheme = "socks4"
            creds = ""
            if m.group("user"):
                creds = f"{m.group('user')}:{m.group('pass')}@"
            seen.append(f"{line_scheme}://{creds}{m.group('host')}:{m.group('port')}")
        if seen:
            random.shuffle(seen)
            seen = seen[:per_source]
        note = f" (dropped {dropped_country} non-{country})" if dropped_country else ""
        log(f"  {name:<12} {len(seen):>4} candidates  {time.time() - t0:.1f}s{note}")
        if seen:
            out[name] = (scheme, seen)
    return out


def verify(proxy_url: str, timeout: float = 18.0) -> tuple[bool, float, str]:
    from curl_cffi import requests as creq

    t0 = time.time()
    session = creq.Session(impersonate="chrome", timeout=timeout)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = session.get("https://www.roblox.com/CreateAccount",
                           headers={"accept": "text/html", "user-agent": UA})
        ok = resp.status_code == 200 and "roblox" in (resp.text or "").lower()
        return ok, time.time() - t0, str(resp.status_code)
    except Exception as exc:
        return False, time.time() - t0, type(exc).__name__
    finally:
        try:
            session.close()
        except Exception:
            pass


def subnet24(host: str) -> str:
    """The /24 a host lives in, or the host itself when it is a name.

    Free lists are dominated by one VPS host rotating ports, so counting lines
    wildly overstates supply. Capping per /24 is what turns "2,000 proxies" into
    what it actually is.
    """
    import ipaddress

    try:
        addr = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return host
    if addr.version != 4:
        return str(ipaddress.ip_network(f"{addr}/64", strict=False))
    return str(ipaddress.ip_network(f"{addr}/24", strict=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-source", type=int, default=120,
                    help="candidates randomly kept per list (0 = all)")
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--limit", type=int, default=0,
                    help="stop probing once this many exits have passed")
    ap.add_argument("--scheme", default="any",
                    choices=("any", "socks5", "socks4", "http"))
    ap.add_argument("--timeout", type=float, default=18.0)
    ap.add_argument("--out", type=Path, default=ROOT / "free.txt")
    ap.add_argument("--raw-out", type=Path, default=None)
    ap.add_argument("--fetch-only", action="store_true")
    ap.add_argument("--max-candidates", type=int, default=0,
                    help="probe at most this many endpoints (0 = all). Used to "
                         "measure the address-space ceiling on a bounded run.")
    ap.add_argument("--country", default="any",
                    help="ISO country to keep from CSV sources, e.g. US")
    ap.add_argument("--no-cap", dest="max_per_24", action="store_const", const=0,
                    help="keep every survivor, ignoring the /24 cap")
    ap.add_argument("--max-per-24", type=int, default=2,
                    help="keep at most this many survivors per /24 (0 = no cap)")
    ap.add_argument("--shuffle-candidates", action="store_true",
                    help="interleave sources so early screening is not one list")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    log("fetching lists")
    harvested = harvest(args.per_source or 10**9, args.country.upper()
                        if args.country != "any" else "any")
    if not harvested:
        log("no candidates")
        return 2

    candidates: list[str] = []
    for scheme, lines in harvested.values():
        if args.scheme != "any" and scheme != args.scheme:
            continue
        candidates.extend(lines)
    # dedup on host:port regardless of scheme prefix
    uniq: dict[str, str] = {}
    for line in candidates:
        key = line.rsplit("@", 1)[-1]
        uniq.setdefault(key, line)
    candidates = list(uniq.values())
    random.shuffle(candidates)
    if args.max_candidates:
        candidates = candidates[:args.max_candidates]
    log(f"\n{len(candidates)} unique endpoints after dedup")

    if args.raw_out:
        args.raw_out.parent.mkdir(parents=True, exist_ok=True)
        args.raw_out.write_text("\n".join(candidates) + "\n")
        log(f"-> {args.raw_out}")
    if args.fetch_only:
        return 0

    log(f"probing with {args.workers} workers")
    passed: list[tuple[str, float]] = []
    tally: dict[str, int] = {}
    t0 = time.time()

    # Not a `with` block on purpose: `break` inside `as_completed` does not stop
    # the executor, the context manager then blocks on every queued future, and
    # a 2000-candidate run sits there for ten minutes after it has enough. Own
    # the shutdown so --limit actually means stop.
    ex = ThreadPoolExecutor(max_workers=args.workers)
    try:
        futs = {ex.submit(verify, line, args.timeout): line for line in candidates}
        done = 0
        for fut in as_completed(futs):
            done += 1
            line = futs[fut]
            ok, elapsed, note = fut.result()
            scheme = line.split("://", 1)[0]
            if ok:
                passed.append((line, elapsed))
                tally[scheme + "+"] = tally.get(scheme + "+", 0) + 1
            else:
                tally[note] = tally.get(note, 0) + 1
            if done % 100 == 0:
                log(f"  {done}/{len(candidates)}  live={len(passed)}  "
                    f"{time.time() - t0:.0f}s")
            if args.limit and len(passed) >= args.limit:
                log(f"  hit --limit {args.limit}, stopping")
                break
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    passed.sort(key=lambda row: row[1])
    log(f"\nlive {len(passed)}/{len(candidates)}  ({time.time() - t0:.0f}s)")
    for note, n in sorted(tally.items(), key=lambda kv: -kv[1])[:8]:
        log(f"  {note:<26} {n}")

    if not passed:
        log("nothing survived")
        return 1

    if args.max_per_24:
        from collections import Counter
        kept: list[tuple[str, float]] = []
        per: Counter = Counter()
        dropped = 0
        for line, elapsed in passed:
            host = line.rsplit("@", 1)[-1].split(":")[0]
            net = subnet24(host)
            if per[net] >= args.max_per_24:
                dropped += 1
                continue
            per[net] += 1
            kept.append((line, elapsed))
        log(f"  diversity cap: kept {len(kept)}, dropped {dropped} "
            f"({len(per)} distinct /24s)")
        passed = kept

    args.out.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(line for line, _ in passed) + "\n"
    args.out.write_text(body)
    log(f"\n-> {args.out}  ({len(passed)} exits)")

    by_scheme: dict[str, int] = {}
    for line, _ in passed:
        s = line.split("://", 1)[0]
        by_scheme[s] = by_scheme.get(s, 0) + 1
    log(f"   {by_scheme}")
    log(f"   median latency {passed[len(passed) // 2][1]:.1f}s, "
        f"fastest {passed[0][1]:.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
