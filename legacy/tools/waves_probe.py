#!/usr/bin/env python3
"""Measure how hard Arkose makes each challenge, per attempt.

`gfct` tells us the thing that actually matters for throughput:

    waves                how many answers this account will cost
    gameType            1 = tile pick, 4 = match-the-key, ...
    instruction_string  the human sentence ("MatchDiceIconCount")

So this runs the browserless chain to `gfct` and prints one row per attempt.
Nothing is solved and no guess is submitted — the point is to see whether a
change to the fingerprint lowers `waves`, which is the single biggest lever on
how many visual solves 50k accounts/day would need.

A session that Arkose dislikes gets 10 waves; the same instruction served to a
real desktop Chrome got 5. That gap is what this tool hunts.

    ./tools/waves_probe.py --proxy-file proxies.txt --n 8

Baseline rows are appended to debug/waves/<stamp>/rows.jsonl so before/after
comparisons survive a session restart.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator.arkose_py import Fingerprint, load_fingerprints, pick_fingerprint
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_challenge as pc


def capture_fingerprint(dirpath: Path) -> tuple[list[dict], str]:
    """Rebuild a captured browser plaintext, with a fresh `n`.

    The `c` envelope is portable across IPs, so a captured desktop fingerprint can
    be re-sealed and replayed. That is the only way to ask "was the 5-wave
    challenge because the client was desktop?" without a browser.
    """
    import base64

    log = json.loads((dirpath / "crypto.json").read_text())
    enc = [x for x in log if x["kind"] == "encrypt"]
    payload = json.loads(base64.b64decode(enc[1]["dataB64"]))
    stamp = base64.b64encode(str(int(time.time())).encode()).decode()
    payload = [dict(item, value=stamp) if item.get("key") == "n" else item
               for item in payload]
    ua = next((x["value"] for x in payload if x["key"] == "wh"), "")
    return payload, ua


def one(proxy_url: str, fp_index: int | None,
        captured: tuple[list[dict], str] | None = None) -> dict:
    started = time.time()
    row: dict = {"t": time.strftime("%H:%M:%S")}

    challenge = pc.request_blob(proxy_url)
    if not challenge:
        row["verdict"] = "no-challenge"
        return row
    row["challenge_type"] = challenge.get("metadata", {}).get("actionType") or "captcha"

    if captured is not None:
        payload, ua = captured
        row["fp"] = "captured"
        row["ua"] = "mobile" if "Android" in ua else "desktop"
        session = pc.new_session(proxy_url)
        from roblox_creator import bda
        token = mint_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                          bda.encode(payload), ua)
        row["gt2"] = 200 if token else "fail"
        row["sup"] = "sup=1" in token
        if not token:
            row["verdict"] = "no-token"
            return row
        pow_result = pc.pows(proxy_url, token, ua, session=session)
        if not pow_result.get("ok"):
            last = (pow_result.get("rounds") or [{}])[-1]
            row["verdict"] = f"pow-{last.get('action') or pow_result.get('stage')}"
            return row
        game = pc.gfct(proxy_url, token, ua, session=session)
        if game.get("status") != 200 or "game_data" not in game:
            row["verdict"] = "gfct-fail"
            return row
        data = game["game_data"]
        row.update(verdict="ok", waves=data.get("waves"), gameType=data.get("gameType"),
                   instruction=data.get("instruction_string"),
                   difficulty=data.get("game_difficulty"),
                   seconds=round(time.time() - started, 1))
        return row

    raws = load_fingerprints()
    raw = raws[fp_index] if fp_index is not None else pick_fingerprint(raws)
    fp = Fingerprint.from_raw(raw)
    row["fp"] = raws.index(raw)
    row["ua"] = "mobile" if "Android" in fp.user_agent else "desktop"
    fe = {x.split(":", 1)[0]: x.split(":", 1)[1] for x in raw["fe"] if ":" in x}
    row["locale"] = fe.get("L")
    row["tz"] = fe.get("TO")

    session = pc.new_session(proxy_url)
    minted = pc.gt2(proxy_url, challenge["metadata"]["dataExchangeBlob"], fp, session=session)
    token = minted.get("token", "")
    row["gt2"] = minted.get("status")
    row["sup"] = "sup=1" in token
    if not token:
        row["verdict"] = "no-token"
        return row

    pow_result = pc.pows(proxy_url, token, fp.user_agent, session=session)
    if not pow_result.get("ok"):
        last = (pow_result.get("rounds") or [{}])[-1]
        row["verdict"] = f"pow-{last.get('action') or pow_result.get('stage')}"
        return row

    game = pc.gfct(proxy_url, token, fp.user_agent, session=session)
    if game.get("status") != 200 or "game_data" not in game:
        row["verdict"] = "gfct-fail"
        return row

    data = game["game_data"]
    row.update(verdict="ok", waves=data.get("waves"), gameType=data.get("gameType"),
               instruction=data.get("instruction_string"),
               difficulty=data.get("game_difficulty"),
               seconds=round(time.time() - started, 1))
    return row


def mint_with(proxy_url: str, blob: str, c_blob: str, ua: str) -> str:
    import tls_client

    session = tls_client.Session(client_identifier="chrome_131")
    session.proxies = {"http": proxy_url, "https": proxy_url}
    stamp = pc.esync()
    resp = session.post(
        f"{pc.ARKOSE}/fc/gt2/public_key/{pc.SITE_KEY}",
        data={"c": c_blob, "public_key": pc.SITE_KEY, "site": "https://www.roblox.com",
              "userbrowser": ua, "capi_version": "4.6.1", "capi_mode": "lightbox",
              "style_theme": "modal", "rnd": str(random.random()),
              "data[blob]": blob},
        headers={**pc.arkose_headers(ua, "https://www.roblox.com/"),
                 "user-agent": ua},
        cookies={"timestamp": stamp}, timeout_seconds=25)
    try:
        return resp.json().get("token", "")
    except Exception:
        return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--fp", type=int, default=None, help="pin one fingerprint index")
    ap.add_argument("--tag", default="", help="label for this run, e.g. 'baseline'")
    ap.add_argument("--from-capture", type=Path, default=None,
                    help="replay a captured browser fingerprint (desktop) instead")
    args = ap.parse_args()

    pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)
    out = ROOT / "debug" / "waves" / (time.strftime("%Y%m%d-%H%M%S")
                                      + (f"-{args.tag}" if args.tag else ""))
    out.mkdir(parents=True, exist_ok=True)
    log = (out / "rows.jsonl").open("a")

    captured = capture_fingerprint(args.from_capture) if args.from_capture else None

    print(f"  {'#':>2} {'verdict':<12} {'waves':>5} {'type':>4} "
          f"{'ua':<7} {'locale':<6} {'tz':>5}  instruction")
    rows = []
    for i in range(args.n):
        row = one(parse_proxy(random.choice(pool)).url(), args.fp, captured)
        rows.append(row)
        log.write(json.dumps(row) + "\n")
        log.flush()
        print(f"  {i:>2} {str(row.get('verdict')):<12} {str(row.get('waves', '-')):>5} "
              f"{str(row.get('gameType', '-')):>4} {row.get('ua', '-'):<7} "
              f"{str(row.get('locale', '-')):<6} {str(row.get('tz', '-')):>5}  "
              f"{row.get('instruction') or ''}")

    log.close()
    ok = [r for r in rows if r.get("verdict") == "ok"]
    if ok:
        waves = [r["waves"] for r in ok]
        print(f"\n  {len(ok)}/{len(rows)} reached gfct; waves "
              f"min={min(waves)} max={max(waves)} mean={sum(waves)/len(waves):.1f}")
        for inst in sorted({r["instruction"] for r in ok}):
            print(f"    {inst}")
    print(f"  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
