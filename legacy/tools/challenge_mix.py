#!/usr/bin/env python3
"""What challenge flavour does each client shape get on /v2/signup?

`captcha` (Arkose, carries a dataExchangeBlob) is solvable by everything in this
repo. `captchav2` is not — it has never been satisfied by any variant in
`tools/captchav2_probe.py`. So the ratio between the two is throughput.

The flavour is chosen per request by the risk engine, and the client shape is
one of its inputs. This samples every variant round-robin so that risk drift
over the run hits each variant equally instead of penalising whichever one was
sampled last.

    ./tools/challenge_mix.py --per-variant 12
    ./tools/challenge_mix.py --per-variant 20 --delay 1.5
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import client_shape
from roblox_creator.identity import make_identity

SIGNUP = "https://auth.roblox.com/v2/signup"

# The body is deliberately invalid. Challenge selection happens before field
# validation, so this samples the flavour without creating anything.
BAD_BODY = {"username": "!!", "password": "x",
            "birthday": "1990-01-01", "gender": 2}

VARIANTS: dict[str, dict[str, str]] = client_shape.SHAPES

BASE = {
    "accept": "application/json, text/plain, */*",
    "content-type": "application/json;charset=UTF-8",
    "origin": "https://www.roblox.com",
    "referer": "https://www.roblox.com/CreateAccount",
}


def sample(session, headers: dict) -> dict:
    r = session.post(SIGNUP, json=BAD_BODY, headers=headers)
    # first 403 hands out the CSRF token; the real answer needs it replayed
    token = r.headers.get("x-csrf-token")
    if r.status_code == 403 and token and not r.headers.get("rblx-challenge-type"):
        r = session.post(SIGNUP, json=BAD_BODY, headers={**headers, "x-csrf-token": token})

    flavour = r.headers.get("rblx-challenge-type")
    blob = False
    raw = r.headers.get("rblx-challenge-metadata", "")
    if raw:
        try:
            meta = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
            blob = bool(meta.get("dataExchangeBlob"))
        except Exception:
            pass
    return {"status": r.status_code, "flavour": flavour or "-", "blob": blob}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-variant", type=int, default=12)
    ap.add_argument("--delay", type=float, default=0.9)
    ap.add_argument("--only", nargs="*", default=list(VARIANTS))
    ap.add_argument("--report", action="store_true",
                    help="print everything recorded so far and exit")
    args = ap.parse_args()

    if args.report:
        print(client_shape.report())
        return 0

    from curl_cffi import requests as creq

    names = [n for n in VARIANTS if n in args.only]
    tally: dict[str, dict[str, int]] = {n: {} for n in names}
    blobbed: dict[str, int] = {n: 0 for n in names}

    for round_no in range(args.per_variant):
        order = names[:]
        random.shuffle(order)
        for name in order:
            session = creq.Session(impersonate="chrome", timeout=25)
            try:
                out = sample(session, {**BASE, **VARIANTS[name]})
            except Exception as exc:
                out = {"status": None, "flavour": f"ERR{type(exc).__name__}", "blob": False}
            key = out["flavour"]
            tally[name][key] = tally[name].get(key, 0) + 1
            blobbed[name] += int(out["blob"])
            if key in (client_shape.SOLVABLE, client_shape.UNSOLVABLE):
                client_shape.record(name, key, blob=out["blob"])
            print(f"  [{round_no:>2}] {name:<8} {str(out['status']):<5} "
                  f"{key:<10} blob={'y' if out['blob'] else 'n'}")
            time.sleep(args.delay)

    print("\n  variant   n    " + "  ".join(f"{k:<10}" for k in ("captcha", "captchav2", "other")))
    for name in names:
        t = tally[name]
        n = sum(t.values())
        solvable = t.get("captcha", 0)
        v2 = t.get("captchav2", 0)
        other = n - solvable - v2
        print(f"  {name:<8} {n:<4} {solvable:<10} {v2:<10} {other:<10}"
              f"  solvable={solvable / n:.0%}" if n else f"  {name}: no samples")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
