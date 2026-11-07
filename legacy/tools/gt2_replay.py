#!/usr/bin/env python3
"""Replay a captured gt2 request verbatim, through an arbitrary proxy.

The question this answers is narrow and decides the whole architecture: is the
`c` blob session-bound, IP-bound, or fully static?

    STATIC      -> capture once, ship in the repo, no browser ever again
    IP-BOUND    -> capture the blob set per exit when the pool is warmed
    ONE-SHOT    -> every signup needs a fresh browser run; browserless is dead

    ./tools/gt2_replay.py debug/dapib/<ts>/fc-fc_gt2_...-request.txt \
        --proxy-file proxies3.txt --tries 4
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from roblox_creator.config import load_proxies  # noqa: E402
from roblox_creator.proxyurl import parse as parse_proxy  # noqa: E402

GT2 = "https://arkoselabs.roblox.com/fc/gt2/public_key/A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"


def esync() -> str:
    now = time.time()
    return str(int(now - (now % 21600)))


def load_body(path: Path) -> dict[str, str]:
    raw = path.read_text().strip()
    parsed = parse_qs(raw, keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def load_capture(request: Path, manifest: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    """Body + the browser's own header set for that exchange."""
    body = load_body(request)
    headers: dict[str, str] = {}
    if manifest and manifest.exists():
        data = json.loads(manifest.read_text())
        for entry in data.get("exchanges", []):
            if "gt2" in str(entry.get("url", "")):
                headers = dict(entry.get("headers", {}))
                break
    return body, headers


def replay(body: dict[str, str], proxy: str | None,
           extra: dict[str, str] | None = None) -> dict:
    import tls_client

    sid = tls_client.Session(client_identifier="chrome_131")
    if proxy:
        sid.proxies = {"http": proxy, "https": proxy}

    stamp = esync()
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.roblox.com",
        "referer": "https://www.roblox.com/",
        "user-agent": body.get("userbrowser", "Mozilla/5.0"),
        "x-ark-esync-value": stamp,
        "x-requested-with": "XMLHttpRequest",
    }
    if extra:
        # the captured header set is the ground truth: drop volatile ones, keep
        # the signing ones (ark-build-id is what a bare POST is missing)
        for key, value in extra.items():
            if key.lower() in ("x-ark-esync-value", "content-length", "host"):
                continue
            headers[key] = value
    headers["x-ark-esync-value"] = stamp
    started = time.time()
    try:
        r = sid.post(GT2, data=body, headers=headers,
                     cookies={"timestamp": stamp}, timeout_seconds=25)
        out = {
            "status": r.status_code,
            "seconds": round(time.time() - started, 2),
            "body": r.text[:220],
        }
        try:
            j = r.json()
            if "token" in j:
                tok = j["token"]
                out["verdict"] = "TOKEN"
                out["sup"] = "sup=1" in tok
                out["token"] = tok[:90]
                out["sid"] = [p for p in tok.split("|") if p.startswith("r=")]
            elif "error" in j or "err" in j:
                out["verdict"] = "ERROR"
            else:
                out["verdict"] = "UNKNOWN"
        except Exception:
            out["verdict"] = "NONJSON"
        return out
    except Exception as exc:
        return {"verdict": "TRANSPORT", "detail": f"{type(exc).__name__}: {str(exc)[:90]}",
                "seconds": round(time.time() - started, 2)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("request", type=Path)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=None)
    ap.add_argument("--tries", type=int, default=1)
    ap.add_argument("--manifest", type=Path, default=None,
                    help="manifest.json next to the captured request (supplies headers)")
    args = ap.parse_args()

    manifest = args.manifest or (args.request.parent / "manifest.json")
    body, captured_headers = load_capture(args.request, manifest)
    print(f"captured headers: {sorted(captured_headers)}")
    print(f"captured fields: {sorted(body)}")
    print(f"  c       len={len(body.get('c', ''))}")
    print(f"  blob    len={len(body.get('data[blob]', ''))}")
    print(f"  ua      {body.get('userbrowser', '')[:70]}")

    proxies: list[str | None] = [None]
    if args.proxy:
        proxies = [args.proxy]
    elif args.proxy_file:
        pool = load_proxies(args.proxy_file)
        proxies = pool[: args.tries]

    for proxy in proxies:
        label = parse_proxy(proxy).masked() if proxy else "direct"
        print(f"\n  -> {label}")
        out = replay(body, proxy, captured_headers)
        print(f"     {out['verdict']:<10} {out.get('status', '')} "
              f"{out.get('detail') or out.get('body', '')}")
        if out.get("token"):
            print(f"     sup={out.get('sup')} {out['token']}")
            print(f"     {out.get('sid')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
