#!/usr/bin/env python3
"""Create one Roblox account with no browser anywhere in the path.

Every hop is now known and reproduced:

    1. POST /v2/signup                     -> 403, csrf token
    2. POST /v2/signup + csrf              -> 403 + rblx-challenge-id / -type
                                              + metadata (base64 JSON) whose
                                              dataExchangeBlob is what gt2 wants
    3. POST /fc/gt2/public_key/<sk>        -> token, often with sup=1
         body: our `c` (hybrid RSA-OAEP + AES-256-GCM), + data[blob]
    4. POST apis.roblox.com/challenge/v1/continue
         {challengeID, challengeType, challengeMetadata:'{"captchaToken":"<token>"}'}
    5. POST /v2/signup + rblx-challenge-id -> the account

Steps 1-3 and 5 are pure HTTP. Step 4 is Roblox's challenge broker: it takes the
Arkose token and marks the challenge solved for that id.

    ./tools/pure_signup.py --proxy-file proxies3.txt
    ./tools/pure_signup.py --proxy socks5://... --json
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

from roblox_creator import bda as bdamod                 # noqa: E402
from roblox_creator.arkose_py import Fingerprint, load_fingerprints  # noqa: E402
from roblox_creator.config import SIGNUP_POST, load_proxies  # noqa: E402
from roblox_creator.identity import make_identity        # noqa: E402
from roblox_creator.proxyurl import parse as parse_proxy  # noqa: E402

SITE_KEY = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
ARKOSE = "https://arkoselabs.roblox.com"
CHALLENGE_CONTINUE = "https://apis.roblox.com/challenge/v1/continue"
BUILD_ID = "270438f7-2325-426f-9636-88978b387c8d"
CAPI_VERSION = "4.6.1"
ENFORCEMENT_HASH = "6d450e22d179b1343143a539d5b90815"

SIGNUP_HEADERS = {
    "origin": "https://www.roblox.com",
    "referer": "https://www.roblox.com/CreateAccount",
    "accept": "application/json, text/plain, */*",
}


def esync() -> str:
    now = time.time()
    return str(int(now - (now % 21600)))


def decode_metadata(raw: str) -> dict:
    padded = raw + "=" * (-len(raw) % 4)
    return json.loads(base64.b64decode(padded))


def encode_metadata(doc: dict) -> str:
    return base64.b64encode(json.dumps(doc).encode()).decode()


def http_session(proxy_url: str | None, impersonate: str = "chrome"):
    from curl_cffi import requests as creq

    session = creq.Session(impersonate=impersonate, timeout=30)
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def request_challenge(session, identity, tries: int = 6) -> dict:
    """Steps 1-2: get the challenge id, type and data-exchange blob.

    Roblox hands out two shapes for the same POST, roughly a coin flip each way:

        captcha      dataExchangeBlob present  -> Arkose, the path we own
        captchav2    redemptionToken, mode INVALID -> unified challenge

    Only the first is usable here, so this retries with a fresh identity until
    one lands. The retry is cheap — one 403 each.
    """
    body = {"username": identity.username, "password": identity.password,
            "birthday": "1990-01-01"}
    first = session.post(SIGNUP_POST, json=body, headers=SIGNUP_HEADERS)
    headers = dict(SIGNUP_HEADERS)
    csrf = first.headers.get("x-csrf-token", "")
    if csrf:
        headers["x-csrf-token"] = csrf

    last: dict = {}
    for _ in range(tries):
        second = session.post(SIGNUP_POST, json=body, headers=headers)
        if second.status_code != 403:
            return {"ok": False, "stage": "warm", "status": second.status_code,
                    "body": second.text[:200], "csrf": csrf}

        challenge_id = second.headers.get("rblx-challenge-id", "")
        challenge_type = second.headers.get("rblx-challenge-type", "")
        raw_metadata = second.headers.get("rblx-challenge-metadata", "")
        metadata = decode_metadata(raw_metadata) if raw_metadata else {}
        last = {"challenge_id": challenge_id, "challenge_type": challenge_type,
                "metadata": metadata, "csrf": csrf, "headers": headers,
                "body": body, "status": second.status_code,
                "raw_metadata": raw_metadata}

        if challenge_type == "captcha" and metadata.get("dataExchangeBlob"):
            return dict(last, ok=True)
        # different flavour: new username and try again
        body = {"username": body["username"] + str(random.randint(0, 9)),
                "password": body["password"], "birthday": body["birthday"]}
    return dict(last, ok=False, stage="flavour")


def mint_token(blob: str, fp: Fingerprint, proxy_url: str | None) -> dict:
    """Step 3: our own `c`, Arkose answers with a token."""
    import tls_client

    session = tls_client.Session(client_identifier="chrome_131")
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}

    stamp = esync()
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.roblox.com",
        "referer": "https://www.roblox.com/",
        "user-agent": fp.user_agent,
        "x-ark-esync-value": stamp,
        "ark-build-id": BUILD_ID,
    }
    form = {
        "c": bdamod.build(fp),
        "public_key": SITE_KEY,
        "site": "https://www.roblox.com",
        "userbrowser": fp.user_agent,
        "capi_version": CAPI_VERSION,
        "capi_mode": "lightbox",
        "style_theme": "modal",
        "rnd": str(random.random()),
        "data[blob]": blob,
    }
    resp = session.post(f"{ARKOSE}/fc/gt2/public_key/{SITE_KEY}", data=form,
                        headers=headers, cookies={"timestamp": stamp},
                        timeout_seconds=25)
    out = {"status": resp.status_code}
    try:
        body = resp.json()
    except Exception:
        out["body"] = resp.text[:200]
        return out
    token = body.get("token", "")
    out.update(token=token, sup=("sup=1" in token), body=None if token else str(body)[:220])
    return out


def continue_challenge(session, challenge_id: str, challenge_type: str,
                       token: str, csrf: str) -> dict:
    """Step 4: hand the Arkose token to Roblox's challenge broker."""
    headers = {
        "accept": "application/json, text/plain, */*",
        "content-type": "application/json",
        "origin": "https://www.roblox.com",
        "referer": "https://www.roblox.com/CreateAccount",
    }
    if csrf:
        headers["x-csrf-token"] = csrf
    payload = {
        "challengeId": challenge_id,
        "challengeType": challenge_type or "captcha",
        "challengeMetadata": json.dumps({"captchaToken": token}),
    }
    resp = session.post(CHALLENGE_CONTINUE, json=payload, headers=headers)
    body: object
    try:
        body = resp.json()
    except Exception:
        body = resp.text[:300]
    return {"status": resp.status_code, "body": body}


def finish_signup(session, request: dict, csrf: str) -> dict:
    """Step 5: resend signup with the challenge headers."""
    headers = dict(request["headers"])
    headers["rblx-challenge-id"] = request["challenge_id"]
    headers["rblx-challenge-type"] = request["challenge_type"]
    if csrf:
        headers["x-csrf-token"] = csrf
    resp = session.post(SIGNUP_POST, json=request["body"], headers=headers)
    out = {"status": resp.status_code, "body": resp.text[:300]}
    for key, value in resp.headers.items():
        if key.lower().startswith("rblx-"):
            out[key] = value[:120]
    return out


def create_account(proxy: str | None = None, verbose: bool = True) -> dict:
    proxy_url = parse_proxy(proxy).url() if proxy else None
    session = http_session(proxy_url)
    identity = make_identity(random.Random())
    result: dict = {"proxy": parse_proxy(proxy).masked() if proxy else "direct",
                    "username": identity.username}

    challenge = request_challenge(session, identity)
    result["challenge"] = {k: challenge.get(k) for k in
                           ("ok", "status", "stage", "challenge_id", "body")}
    if verbose:
        print(f"  challenge -> ok={challenge.get('ok')} "
              f"id={challenge.get('challenge_id')} type={challenge.get('challenge_type')}")
    if not challenge.get("ok"):
        result["error"] = "no challenge"
        return result

    fp = Fingerprint.from_raw(random.choice(load_fingerprints()))
    blob = challenge["metadata"]["dataExchangeBlob"]
    minted = mint_token(blob, fp, proxy_url)
    result["gt2"] = {"status": minted.get("status"), "sup": minted.get("sup"),
                     "token_len": len(minted.get("token", ""))}
    if verbose:
        print(f"  gt2       -> {minted.get('status')} sup={minted.get('sup')} "
              f"token={len(minted.get('token', ''))} chars")
    if not minted.get("token"):
        result["error"] = f"no token: {minted.get('body')}"
        return result

    # The broker is dead on this deployment: `continue` answers 403 "internal
    # error". The live path is the legacy resubmit with the token folded into
    # rblx-challenge-metadata. finalhop tries merged -> token-only -> continue.
    from roblox_creator import finalhop

    hop = finalhop.finish(session, challenge, minted["token"])
    result["finalhop"] = hop
    result["created"] = hop["created"]
    if verbose:
        for tried in hop["tried"]:
            final = tried.get("signup", {})
            print(f"  {tried['variant']:<10} -> {final.get('status')} "
                  f"{str(final.get('body', ''))[:110]}")
        print(f"  created   -> {hop['created']}"
              + (f" via {hop['winner']}" if hop["created"] else ""))
    if hop["created"]:
        result["username"] = identity.username
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    proxy = args.proxy
    if not proxy and args.proxy_file.exists():
        proxy = random.choice(load_proxies(args.proxy_file))
    if not args.json:
        print(f"  proxy: {parse_proxy(proxy).masked() if proxy else 'direct'}")

    result = create_account(proxy, verbose=not args.json)
    if args.json:
        print(json.dumps(result, indent=1, default=str))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("a") as handle:
            handle.write(json.dumps(result, default=str) + "\n")
    return 0 if result.get("created") else 1


if __name__ == "__main__":
    raise SystemExit(main())
