#!/usr/bin/env python3
"""Prove we can mint `c` ourselves, without a browser.

Two modes:

    --from-capture   take the plaintext a browser produced, refresh only `n`,
                     and re-seal it with our own AES key + RSA wrap. Proves the
                     envelope is right and that nothing else in the blob is
                     signed over.

    (default)        build a synthetic fingerprint from data/fingerprints.json
                     and seal that. Proves the whole generator.

    ./tools/c_selftest.py --from-capture debug/bda/<ts> --proxy-file proxies3.txt
    ./tools/c_selftest.py --proxy-file proxies3.txt --tries 3
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from roblox_creator import bda                      # noqa: E402
from roblox_creator.arkose_py import Fingerprint, build_bda, load_fingerprints  # noqa: E402
from roblox_creator.config import load_proxies      # noqa: E402
from roblox_creator.proxyurl import parse as parse_proxy  # noqa: E402

SITE_KEY = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
API = "https://arkoselabs.roblox.com"
ENFORCEMENT = f"{API}/v2/4.6.1/enforcement.6d450e22d179b1343143a539d5b90815.html"
BUILD_ID = "270438f7-2325-426f-9636-88978b387c8d"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/155.0.0.0 Safari/537.36")


def esync() -> str:
    now = time.time()
    return str(int(now - (now % 21600)))


def post(c_blob: str, proxy: str | None, ua: str = UA) -> dict:
    import tls_client

    s = tls_client.Session(client_identifier="chrome_131")
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    stamp = esync()
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.roblox.com",
        "referer": "https://www.roblox.com/",
        "user-agent": ua,
        "x-ark-esync-value": stamp,
        "ark-build-id": BUILD_ID,
        "sec-ch-ua": '"Google Chrome";v="155", "Chromium";v="155", "Not(A:Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    }
    body = {
        "c": c_blob,
        "public_key": SITE_KEY,
        "site": "https://www.roblox.com",
        "userbrowser": ua,
        "capi_version": "4.6.1",
        "capi_mode": "lightbox",
        "style_theme": "modal",
        "rnd": str(random.random()),
    }
    started = time.time()
    try:
        r = s.post(f"{API}/fc/gt2/public_key/{SITE_KEY}", data=body, headers=headers,
                   cookies={"timestamp": stamp}, timeout_seconds=25)
    except Exception as exc:
        return {"verdict": "TRANSPORT", "detail": f"{type(exc).__name__}: {str(exc)[:90]}"}
    out = {"status": r.status_code, "seconds": round(time.time() - started, 2)}
    if r.status_code != 200:
        out["verdict"] = "DENIED" if r.status_code in (400, 401) else "HTTP"
        out["body"] = r.text[:160]
        return out
    try:
        data = r.json()
    except Exception:
        out.update(verdict="NONJSON", body=r.text[:160])
        return out
    token = data.get("token", "")
    out.update(verdict="TOKEN", sup="sup=1" in token, token=token[:80],
               region=[p for p in token.split("|") if p.startswith("r=")][:1])
    return out


def plaintext_from_capture(dirpath: Path) -> list[dict]:
    """Rebuild the plaintext by decrypting nothing — read it off the hook log."""
    log = json.loads((dirpath / "crypto.json").read_text())
    enc = [x for x in log if x["kind"] == "encrypt"]
    if len(enc) < 2:
        raise SystemExit("crypto.json has no AES-GCM encrypt entry")
    raw = base64.b64decode(enc[1]["dataB64"])
    return json.loads(raw)


def refresh_n(payload: list[dict], now: int | None = None) -> list[dict]:
    stamp = int(now if now is not None else time.time())
    encoded = base64.b64encode(str(stamp).encode()).decode()
    return [dict(item, value=encoded) if item.get("key") == "n" else item
            for item in payload]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-capture", type=Path, default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=None)
    ap.add_argument("--tries", type=int, default=3)
    ap.add_argument("--old-bda", action="store_true",
                    help="also send the retired AES-CBC bda, as a control")
    args = ap.parse_args()

    proxies: list[str | None] = [None]
    if args.proxy:
        proxies = [args.proxy]
    elif args.proxy_file:
        pool = load_proxies(args.proxy_file)
        random.shuffle(pool)
        proxies = pool[: args.tries]

    if args.from_capture:
        payload = plaintext_from_capture(args.from_capture)
        print(f"mode: re-seal captured plaintext ({len(payload)} keys)")
        blob = bda.encode(refresh_n(payload))
        print(f"  c len={len(blob)} (fresh AES key, fresh IV, fresh n)")
        for proxy in proxies:
            label = parse_proxy(proxy).masked() if proxy else "direct"
            print(f"  -> {label}")
            print(f"     {post(blob, proxy)}")
        return 0

    fps = load_fingerprints()
    if not fps:
        print("no fingerprints in data/fingerprints.json")
        return 2
    print(f"mode: synthetic fingerprint ({len(fps)} available)")

    for proxy in proxies:
        label = parse_proxy(proxy).masked() if proxy else "direct"
        fp = Fingerprint.from_raw(random.choice(fps))
        payload = bda.build_payload(fp)
        blob = bda.encode(payload)
        print(f"\n  -> {label}")
        print(f"     fp ua={fp.user_agent[:56]} wh={fp.wh[:16]}")
        print(f"     c len={len(blob)}")
        print(f"     new: {post(blob, proxy)}")
        if args.old_bda:
            old = build_bda(fp, SITE_KEY, "https://www.roblox.com", API,
                            "4.6.1", ENFORCEMENT.split("enforcement.")[1].split(".")[0])
            print(f"     retired bda builder produced {len(old)} chars (control, not sent)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
