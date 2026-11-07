#!/usr/bin/env python3
"""Harvest a captured browser fingerprint into the payload corpus.

Why payloads rather than fingerprints
-------------------------------------
`data/fingerprints.json` holds the 11 *mobile* captures the builder was written
against, and `build_enhanced_fp()` reconstructs a 90-entry array from them. A real
captured payload is better in every way: it carries the exact `fe`, the exact
`wh`, and the full 97-entry `enhanced_fp` the device actually produced, including
the seven keys the builder does not know about.

It also turned out to be the thing that decides difficulty:

    mobile fingerprint  -> 10 waves, 8/8 attempts
    desktop fingerprint ->  5.8 waves mean, 6/6 attempts

so a corpus of *desktop* payloads is worth more than any solver tuning.

The envelope is portable across IPs — only `n` (epoch seconds) has to be fresh —
so a harvested payload can be re-sealed and reused indefinitely.

    ./tools/fp_harvest.py --from-capture debug/bda/20261030-112924-h1
    ./tools/fp_harvest.py --all                 # every debug/bda/*/ with crypto.json
    ./tools/fp_harvest.py --list
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

STORE = ROOT / "data" / "payloads"


def payload_from_capture(dirpath: Path) -> dict | None:
    """crypto.json has the plaintext; gt2-body.txt has the UA the browser sent."""
    crypto = dirpath / "crypto.json"
    if not crypto.exists():
        return None
    log = json.loads(crypto.read_text())
    enc = [x for x in log if x.get("kind") == "encrypt"]
    if len(enc) < 2:
        return None
    payload = json.loads(base64.b64decode(enc[1]["dataB64"]))

    fields = {x["key"]: x["value"] for x in payload}
    if "fe" not in fields or "wh" not in fields:
        return None

    # the UA is not in the payload; the gt2 form carried it as `userbrowser`
    user_agent = ""
    body_path = dirpath / "gt2-body.txt"
    if body_path.exists():
        from urllib.parse import parse_qs
        form = parse_qs(body_path.read_text(), keep_blank_values=True)
        user_agent = (form.get("userbrowser") or [""])[0]
    if not user_agent:
        user_agent = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36")

    fe = {x.split(":", 1)[0]: x.split(":", 1)[1] for x in fields["fe"] if ":" in x}
    return {
        "user_agent": user_agent,
        "mobile": "Android" in user_agent or "iPhone" in user_agent,
        "wh": fields["wh"],
        "locale": fe.get("L"),
        "tz": fe.get("TO"),
        "screen": fe.get("S"),
        "pixel_ratio": fe.get("PR"),
        "enhanced_fp_entries": len(fields.get("enhanced_fp") or []),
        "payload": payload,
        "source": dirpath.name,
    }


def save(record: dict) -> Path:
    STORE.mkdir(parents=True, exist_ok=True)
    name = record["wh"][:12] or record["source"]
    path = STORE / f"{name}.json"
    path.write_text(json.dumps(record, indent=1))
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-capture", type=Path, default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    if args.list:
        if not STORE.exists():
            print("  no payloads harvested yet")
            return 0
        for path in sorted(STORE.glob("*.json")):
            rec = json.loads(path.read_text())
            kind = "mobile" if rec["mobile"] else "desktop"
            print(f"  {path.name:<16} {kind:<8} {rec['screen']:<10} "
                  f"PR={rec['pixel_ratio']:<6} L={rec['locale']:<6} "
                  f"enhanced={rec['enhanced_fp_entries']}  ({rec['source']})")
        return 0

    sources: list[Path] = []
    if args.all:
        sources = sorted(p.parent for p in ROOT.glob("debug/bda/*/crypto.json"))
    elif args.from_capture:
        sources = [args.from_capture]
    else:
        ap.error("give --from-capture DIR, --all, or --list")

    saved = 0
    for dirpath in sources:
        record = payload_from_capture(dirpath)
        if not record:
            print(f"  {dirpath.name}: no usable plaintext")
            continue
        path = save(record)
        kind = "mobile" if record["mobile"] else "desktop"
        print(f"  {dirpath.name} -> {path.name}  [{kind}] "
              f"screen={record['screen']} enhanced={record['enhanced_fp_entries']}")
        saved += 1
    print(f"\n  {saved} payload(s) stored in {STORE}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
