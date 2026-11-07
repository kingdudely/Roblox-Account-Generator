#!/usr/bin/env python3
"""Score every proxy file by what actually matters, not by whether it pings.

The question this exists to answer is narrow: *for each proxy file, what
fraction of exits can reach Roblox at all, and of those, what fraction get
Arkose to serve a wave image?* Two stages, because they fail for different
reasons and conflating them is how a good pool gets called dead.

    stage 1  reach   one HTTPS GET to www.roblox.com through the exit.
                     Evomi expiry shows up here as a TLS alert from the proxy
                     itself (code 35, ACCESS_DENIED) — the exit is gone, the
                     file is stale.
    stage 2  image   the real chain: signup 403 -> blob -> gt2 -> PoW -> gfct,
                     then two shapes of `/rtig/image`. Legion failures show up
                     here as 403 DENIED ACCESS on the image while the transport
                     is perfectly healthy — the per-exit allowance is spent.

Sampling is per *session token*, and every line in these files carries its own
token, so a random sample is a genuine sample of distinct exits.

    ./tools/pool_audit.py --sample 12
    ./tools/pool_audit.py --file proxies2.txt --sample 20 --stage both
    ./tools/pool_audit.py --sample 8 --json /tmp/opencode/audit.json
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy

DEFAULT_FILES = ["proxies.txt", "proxies1.txt", "proxies2.txt", "proxies3.txt",
                 "proxiesithink.txt"]


def log(msg: str) -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------- stage 1

def check_reach(proxy_url: str, timeout: float = 20.0) -> dict:
    """Can this exit carry a TLS request to roblox.com at all?"""
    from curl_cffi import requests as creq

    t0 = time.time()
    session = creq.Session(impersonate="chrome", timeout=timeout)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    try:
        resp = session.get("https://www.roblox.com/CreateAccount",
                           headers={"accept": "text/html",
                                    "user-agent": ("Mozilla/5.0 (Windows NT 10.0; "
                                                   "Win64; x64) AppleWebKit/537.36 "
                                                   "(KHTML, like Gecko) "
                                                   "Chrome/131.0.0.0 Safari/537.36")})
        return {"reach": True, "status": resp.status_code,
                "elapsed": round(time.time() - t0, 2), "error": None}
    except Exception as exc:
        name = type(exc).__name__
        text = str(exc)
        # Trim curl's boilerplate so the failure class is visible at a glance.
        for needle in ("TLS connect error", "TLSV1_ALERT_ACCESS_DENIED",
                       "Could not connect", "SOCKS", "timed out",
                       "Proxy CONNECT", "CONNECT tunnel"):
            if needle.lower() in text.lower():
                text = needle
                break
        return {"reach": False, "status": None,
                "elapsed": round(time.time() - t0, 2),
                "error": f"{name}: {text[:60]}"}
    finally:
        try:
            session.close()
        except Exception:
            pass


# --------------------------------------------------------------------- stage 2

def check_image(raw_proxy: str, outdir: Path) -> dict:
    """The full chain, then whether `/rtig/image` serves a strip."""
    from tools import pure_ca as pca
    from tools import pure_challenge as pc

    proxy_url = parse_proxy(raw_proxy).url()
    result: dict = {"image": False, "stage": "blob", "status": None,
                    "waves": None, "elapsed": None}
    t0 = time.time()
    try:
        challenge = pc.request_blob(proxy_url)
        if not challenge:
            result["stage"] = "blob-empty"
            return result

        from roblox_creator import payloads
        from roblox_creator.arkose_py import (Fingerprint, load_fingerprints,
                                              pick_fingerprint)
        session = pc.new_session(proxy_url)
        record = payloads.pick_payload(payloads.load_payloads())
        if record is not None:
            c_blob, ua = payloads.reseal(record)
            minted = pc.gt2_with(proxy_url,
                                 challenge["metadata"]["dataExchangeBlob"],
                                 c_blob, ua, session=session)
        else:
            fp = Fingerprint.from_raw(pick_fingerprint(load_fingerprints()))
            ua = fp.user_agent
            minted = pc.gt2(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                            fp, session=session)
        token = minted.get("token", "")
        if not token:
            result["stage"] = f"gt2-{minted.get('status')}"
            return result

        pow_result = pc.pows(proxy_url, token, ua, session=session)
        if not pow_result.get("ok"):
            result["stage"] = "pow"
            return result

        game = pc.gfct(proxy_url, token, ua, session=session)
        if game.get("status") != 200 or "game_data" not in game:
            result["stage"] = "gfct"
            return result

        data = game["game_data"]
        result["waves"] = data.get("waves")
        result["difficulty"] = data.get("game_difficulty")

        session_token = game.get("session_token") or ""
        referer = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/"
                   f"{pc.GAME_CORE}/standard/index.html?session={session_token}")
        images = data.get("customGUI", {}).get("_challenge_imgs") or []
        if not images:
            result["stage"] = "no-image-url"
            return result

        blob = pca.fetch_image(session, images[0], referer, ua, attempts=2)
        if blob:
            result["image"] = True
            result["stage"] = "ok"
            result["bytes"] = len(blob)
            (outdir / "sample.jpg").write_bytes(blob) if outdir else None
        else:
            result["stage"] = "image-403"
        return result
    except Exception as exc:
        result["stage"] = f"{type(exc).__name__}: {str(exc)[:50]}"
        return result
    finally:
        result["elapsed"] = round(time.time() - t0, 2)


# ------------------------------------------------------------------------ main

def audit_file(path: Path, sample: int, stage: str, workers: int,
               outdir: Path) -> dict:
    pool = load_proxies(path)
    if not pool:
        return {"file": path.name, "error": "empty", "lines": 0}
    picked = random.sample(pool, min(sample, len(pool)))
    log(f"\n{path.name}  ({len(pool)} lines, sampling {len(picked)})")

    reach_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(check_reach, parse_proxy(r).url()): r for r in picked}
        for fut in futs:
            r = fut.result()
            reach_rows.append(r)
            mark = f"{r['status']}" if r["reach"] else r["error"][:34]
            log(f"    reach {mark:<36} {r['elapsed']:>5.1f}s")

    ok = [p for p, r in zip(picked, reach_rows) if r["reach"]]
    summary = {
        "file": path.name,
        "lines": len(pool),
        "sampled": len(picked),
        "reach_ok": len(ok),
        "reach_errors": {},
        "image_ok": None,
        "image_rows": [],
    }
    for r in reach_rows:
        if not r["reach"]:
            key = (r["error"] or "unknown").split(":")[0]
            summary["reach_errors"][key] = summary["reach_errors"].get(key, 0) + 1

    if stage in ("both", "image") and ok:
        room = min(len(ok), max(2, sample // 3))
        probe = random.sample(ok, room)
        log(f"  -> image stage on {len(probe)} reachable exits")
        with ThreadPoolExecutor(max_workers=min(workers, len(probe))) as ex:
            futs = {ex.submit(check_image, p, outdir): p for p in probe}
            for fut in futs:
                r = fut.result()
                summary["image_rows"].append(r)
                mark = "IMAGE OK" if r["image"] else r["stage"]
                log(f"    image {mark:<34} waves={r.get('waves')} "
                    f"{r.get('elapsed')}s")
        summary["image_ok"] = sum(1 for r in summary["image_rows"] if r["image"])
        summary["image_tried"] = len(summary["image_rows"])
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", action="append", default=None,
                    help="proxy file to audit (repeatable); default: all")
    ap.add_argument("--sample", type=int, default=10,
                    help="exits sampled per file")
    ap.add_argument("--stage", default="both", choices=("reach", "image", "both"))
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    if args.seed is not None:
        random.seed(args.seed)

    names = args.file or [f for f in DEFAULT_FILES if (ROOT / f).exists()]
    outdir = ROOT / "debug" / "audit" / time.strftime("%Y%m%d-%H%M%S")
    outdir.mkdir(parents=True, exist_ok=True)

    rows = []
    for name in names:
        path = Path(name) if Path(name).is_absolute() else ROOT / name
        if not path.exists():
            log(f"\n{name}  missing")
            continue
        sub = outdir / path.stem
        sub.mkdir(parents=True, exist_ok=True)
        rows.append(audit_file(path, args.sample, args.stage, args.workers, sub))

    log("\n" + "=" * 62)
    log(f"{'file':<20}{'lines':>7}{'sampled':>9}{'reach':>8}{'image':>9}")
    for r in rows:
        if r.get("error"):
            log(f"{r['file']:<20}{'--':>7}{'--':>9}{'--':>8}{'--':>9}")
            continue
        img = (f"{r['image_ok']}/{r.get('image_tried', 0)}"
               if r["image_ok"] is not None else "-")
        log(f"{r['file']:<20}{r['lines']:>7}{r['sampled']:>9}"
            f"{r['reach_ok']:>8}{img:>9}")
    log("=" * 62)
    for r in rows:
        if r.get("reach_errors"):
            log(f"  {r['file']}: {r['reach_errors']}")

    payload = {"stamp": outdir.name, "rows": rows}
    (outdir / "audit.json").write_text(json.dumps(payload, indent=1))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(payload, indent=1))
    log(f"-> {outdir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
