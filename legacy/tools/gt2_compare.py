"""Does a better fingerprint stop Arkose asking for a puzzle?

gt2 answers in one of two shapes:

    {"token": "...|sup=1"}                     no challenge — nothing to solve
    {"token": "...", "challenge": {...}}       a puzzle, with its images

Everything we have tried mints the second. If `arkose-solver`'s device corpus
(234 GPUs, 105 screens, 62 connection profiles, per-solve derivation) produces
the first, then the library is worth fixing and the whole perception track is
moot. If it also gets a challenge, the library cannot help and the question is
settled by measurement rather than by opinion.

Same transport, same exit, same headers — only the BDA differs, so the answer
belongs to the fingerprint and nothing else.

    PYTHONPATH=. .venv/bin/python tools/gt2_compare.py --reps 2
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from roblox_creator import bda as our_bda
from roblox_creator.arkose_py import Fingerprint, load_fingerprints
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse
from tools import pure_challenge as pc
from tools.suppress_probe import fresh_challenge


def post_gt2(proxy_url: str, blob: str, c_value: str, user_agent: str,
             capi_version: str, build_id: str) -> dict:
    """One gt2 call with a caller-supplied `c`, returning the whole response."""
    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=30)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    stamp = pc.esync()
    form = {
        "c": c_value, "public_key": pc.SITE_KEY, "site": "https://www.roblox.com",
        "userbrowser": user_agent, "capi_version": capi_version,
        "capi_mode": "lightbox", "style_theme": "modal",
        "rnd": str(random.random()), "data[blob]": blob,
    }
    headers = {
        "accept": "*/*", "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.roblox.com", "referer": "https://www.roblox.com/",
        "user-agent": user_agent, "x-ark-esync-value": stamp,
        "ark-build-id": build_id, "cookie": f"timestamp={stamp}",
    }
    resp = session.post(f"{pc.ARKOSE}/fc/gt2/public_key/{pc.SITE_KEY}",
                        data=form, headers=headers, timeout=30)
    out = {"status": resp.status_code}
    try:
        body = resp.json()
    except Exception:
        out["raw"] = resp.text[:160]
        return out
    if isinstance(body.get("body"), str):
        try:
            body = json.loads(body["body"])
        except Exception:
            pass
    out["keys"] = sorted(body.keys())
    out["token"] = (body.get("token") or "")[:40]
    out["sup"] = "sup=1" in (body.get("token") or "")
    challenge = body.get("challenge") or body.get("challenge_url")
    out["challenge"] = bool(challenge)
    if isinstance(challenge, dict):
        out["challenge_keys"] = sorted(challenge.keys())[:8]
    out["action"] = body.get("action")
    out["error"] = (body.get("error") or "")[:60]
    return out


def mint_ours(blob: str, suppressed: bool, proxy_url: str) -> tuple[str, str, str]:
    """The repo's own BDA; returns (c, ua, capi_version)."""
    fp = Fingerprint.from_raw(random.choice(load_fingerprints()))
    token = our_bda.build(fp, enhanced_fp=[] if suppressed else None)
    return token, fp.user_agent, "4.6.1"


def mint_library(blob: str, proxy_url: str) -> tuple[str, str, str]:
    """arkose-solver's BDA, built and encrypted with its own device corpus."""
    from arkose_solver.bda import generate_bda
    from arkose_solver.crypto import encrypt, marshal_bda
    from arkose_solver.vmkey import extract_rsa_key  # noqa: F401  (documented failure)
    from roblox_creator.bda import RSA_SPKI_B64

    cfg = {
        "surl": pc.ARKOSE,
        "language": "en-US",
        "title": "Roblox",
        "window_location_href": "https://www.roblox.com/CreateAccount",
        "sitedata_location_href": "https://www.roblox.com/CreateAccount",
        "document_referrer": "https://www.roblox.com/",
        "enforcement_hash": "",
    }
    items = generate_bda(cfg, pc.BUILD_ID)
    c_value = encrypt(marshal_bda(items), RSA_SPKI_B64)
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
    return c_value, ua, "4.4.5"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies1.txt")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--retries", type=int, default=3)
    args = ap.parse_args()

    pool = load_proxies(Path(args.proxy_file))
    random.shuffle(pool)
    minters = (
        ("ours-suppressed", lambda b, p: mint_ours(b, True, p)),
        ("ours-full", lambda b, p: mint_ours(b, False, p)),
        ("arkose-solver", mint_library),
    )
    results: dict[str, list[dict]] = {name: [] for name, _ in minters}

    for name, minter in minters:
        for rep in range(args.reps):
            for attempt in range(args.retries + 1):
                proxy = parse(random.choice(pool)).url()
                challenge = fresh_challenge(proxy)
                if not challenge:
                    continue
                blob = challenge["meta"]["dataExchangeBlob"]
                try:
                    c_value, ua, capi = minter(blob, proxy)
                except Exception as exc:
                    print(f"  {name:<16} minter failed: {type(exc).__name__}: {str(exc)[:60]}")
                    continue
                try:
                    out = post_gt2(proxy, blob, c_value, ua, capi, pc.BUILD_ID)
                except Exception as exc:
                    print(f"  {name:<16} gt2 transport failed: {type(exc).__name__}")
                    continue
                if out.get("status") != 200 and attempt < args.retries:
                    continue
                results[name].append(out)
                print(f"  {name:<16} status={out.get('status')} sup={out.get('sup')} "
                      f"challenge={out.get('challenge')} keys={out.get('keys')} "
                      f"err={out.get('error') or '-'}")
                time.sleep(0.4)
                break

    print("\n  summary")
    for name, rows in results.items():
        if not rows:
            print(f"    {name:<16} no data")
            continue
        challenged = sum(1 for r in rows if r.get("challenge"))
        print(f"    {name:<16} n={len(rows)} challenged={challenged} "
              f"sup={sum(1 for r in rows if r.get('sup'))}")

    out_path = Path("debug/gt2_compare.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=1))
    print(f"\n  -> {out_path}")


if __name__ == "__main__":
    main()
