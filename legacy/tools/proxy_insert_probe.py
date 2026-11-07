#!/usr/bin/env python3
"""Does the 412 on user creation follow the network or the tenant?

One call answers it. `users.insert` is attempted from a handful of residential
exits, each with a unique address, and the status is recorded per exit.

    ./tools/proxy_insert_probe.py --count 5
    ./tools/proxy_insert_probe.py --count 1 --proxy socks5://user:pass@host:port

If a residential exit returns 200, the block was network-scored and supply
reopens. If every exit returns the same 412, the block is on the tenant/domain
and no amount of proxying changes it.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from googleapiclient.errors import HttpError

from tools.admin_proxy import build_service_via, egress_ip

DOMAIN = "example.duckdns.org"
ALPHABET = string.ascii_letters + string.digits + "_-"


def load_pool(path: Path) -> list[str]:
    lines = [ln.strip() for ln in path.read_text().splitlines() if ln.strip()]
    return [ln for ln in lines if ln.startswith("socks5://") or ln.startswith("http")]


def describe(exc: HttpError) -> dict:
    out = {"status": exc.status_code, "reason": None, "message": None}
    try:
        payload = json.loads(exc.content.decode())
        err = payload.get("error", {})
        out["message"] = err.get("message")
        errors = err.get("errors") or []
        if errors:
            out["reason"] = errors[0].get("reason")
    except Exception:  # noqa: BLE001 - diagnostics only
        out["message"] = str(exc.content[:160])
    return out


def attempt(proxy_url: str, tag: str) -> dict:
    email = f"px{tag}.{secrets.token_hex(3)}@{DOMAIN}"
    password = "".join(secrets.choice(ALPHABET) for _ in range(20))
    started = time.time()
    egress = egress_ip(proxy_url)
    record = {"proxy": proxy_url, "email": email, "egress": egress}

    try:
        service = build_service_via(ROOT / "accounts/cloudidentity/token.json", proxy_url)
    except Exception as exc:  # noqa: BLE001
        record.update(outcome="build_failed", detail=f"{type(exc).__name__}: {str(exc)[:140]}")
        record["seconds"] = round(time.time() - started, 1)
        return record

    try:
        created = service.users().insert(body={
            "primaryEmail": email,
            "name": {"givenName": "Proxy", "familyName": f"Probe{tag}"},
            "password": password,
            "changePasswordAtNextLogin": False,
            "orgUnitPath": "/",
        }).execute()
        record.update(outcome="created",
                      gid=created.get("id"), password=password)
    except HttpError as exc:
        record.update(outcome="http_error", **describe(exc))
    except Exception as exc:  # noqa: BLE001
        record.update(outcome="failed", detail=f"{type(exc).__name__}: {str(exc)[:140]}")

    record["seconds"] = round(time.time() - started, 1)
    return record


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=5, help="how many exits to try")
    ap.add_argument("--pool", default="proxies2.txt")
    ap.add_argument("--proxy", default=None, help="test exactly this one exit")
    ap.add_argument("--out", default="debug/proxy_insert")
    args = ap.parse_args()

    pool = [args.proxy] if args.proxy else load_pool(ROOT / args.pool)
    if not pool:
        print("no proxies found")
        return 2

    picks = pool if args.proxy else secrets.SystemRandom().sample(pool, min(args.count, len(pool)))
    out_dir = ROOT / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []

    for i, proxy_url in enumerate(picks, 1):
        print(f"[{i}/{len(picks)}] {proxy_url.split('@')[-1]}", flush=True)
        record = attempt(proxy_url, f"{i:02d}")
        results.append(record)
        print(f"    egress {record['egress']:<20} -> {record['outcome']}"
              f" {record.get('status', '')} {record.get('reason') or ''}"
              f"  ({record['seconds']}s)", flush=True)
        if record.get("message"):
            print(f"    {record['message'][:120]}", flush=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = out_dir / f"probe-{stamp}.json"
    path.write_text(json.dumps(results, indent=2))

    created = [r for r in results if r["outcome"] == "created"]
    print(f"\ncreated {len(created)} / {len(results)}  ->  {path}")
    return 0 if created else 1


if __name__ == "__main__":
    raise SystemExit(main())
