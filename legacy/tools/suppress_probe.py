#!/usr/bin/env python3
"""Does Roblox accept a *suppressed* Arkose token?

If it does, there is nothing to solve: Arkose answers `sup=1` whenever it decides
the session is not worth challenging, and most real signups are exactly that.
Everything else in this project — the wave images, the solver, the free vision
model — is then only for the residual.

Why this is worth testing carefully
-----------------------------------
Earlier I minted a suppressed token, resent the signup with it, and got
`{"message":"Challenge failed to authorize request"}`, and concluded Roblox
refuses suppressed tokens. That conclusion is unsafe for three reasons:

    * the blob is single-use per challenge, so a token minted from a consumed
      blob is invalid for reasons that have nothing to do with suppression
    * the token may itself be single-use per verification, so a second attempt
      on the same token proves nothing
    * `useContinueMode=false` means the legacy resubmit is correct, but the
      exact encoding of `rblx-challenge-metadata` was never pinned down

So this mints a fresh challenge, a fresh blob and a fresh token *per variant*,
and reports the outcome of each. Any 200 settles it.

    ./tools/suppress_probe.py --proxy-file proxies.txt --variants all
    ./tools/suppress_probe.py --proxy-file proxies.txt           # minimal set
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

from roblox_creator import bda
from roblox_creator.arkose_py import Fingerprint, load_fingerprints
from roblox_creator.config import SIGNUP_POST, load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse as parse_proxy
from tools import pure_challenge as pc

CHALLENGE_CONTINUE = "https://apis.roblox.com/challenge/v1/continue"

SIGNUP_HEADERS = {
    "origin": "https://www.roblox.com",
    "referer": "https://www.roblox.com/CreateAccount",
    "accept": "application/json, text/plain, */*",
}


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def fresh_challenge(proxy_url: str, tries: int = 8) -> dict | None:
    """signup 403 -> a `captcha` challenge with its data-exchange blob."""
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
        kind = resp.headers.get("rblx-challenge-type", "")
        raw = resp.headers.get("rblx-challenge-metadata", "")
        if kind == "captcha" and raw:
            meta = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
            if meta.get("dataExchangeBlob"):
                return {"id": resp.headers.get("rblx-challenge-id", ""),
                        "type": kind, "raw_meta": raw, "meta": meta,
                        "body": body, "headers": headers,
                        "csrf": csrf, "session": session}
        body["username"] += str(random.randint(0, 9))
    return None


def mint(proxy_url: str, blob: str, fp: Fingerprint, suppressed: bool,
         session=None) -> str:
    enhanced = [] if suppressed else None
    token = bda.build(fp, enhanced_fp=enhanced)
    import tls_client

    if session is None:
        session = tls_client.Session(client_identifier="chrome_131")
        session.proxies = {"http": proxy_url, "https": proxy_url}
    stamp = pc.esync()
    resp = session.post(
        f"{pc.ARKOSE}/fc/gt2/public_key/{pc.SITE_KEY}",
        data={"c": token, "public_key": pc.SITE_KEY, "site": "https://www.roblox.com",
              "userbrowser": fp.user_agent, "capi_version": "4.6.1",
              "capi_mode": "lightbox", "style_theme": "modal",
              "rnd": str(random.random()), "data[blob]": blob},
        headers={"accept": "*/*", "accept-language": "en-US,en;q=0.9",
                 "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                 "origin": "https://www.roblox.com", "referer": "https://www.roblox.com/",
                 "user-agent": fp.user_agent, "x-ark-esync-value": stamp,
                 "ark-build-id": pc.BUILD_ID},
        cookies={"timestamp": stamp},
        **({"timeout": 25} if session.__class__.__module__.startswith("curl_cffi")
           else {"timeout_seconds": 25}))
    try:
        return resp.json().get("token", "")
    except Exception:
        return ""


def mint_library(proxy_url: str, blob: str) -> tuple[str, bool]:
    """Mint through the third-party arkose-solver package (fresh BDA per call).

    Its RSA-key extractor does not find the key in Roblox's api.js (4.6.2), so the
    key we already pinned is supplied explicitly. That is the one thing the
    package was supposed to make durable and, on this deployment, does not.
    """
    from arkose_solver import Solver

    from roblox_creator.bda import RSA_SPKI_B64

    solver = Solver(
        surl=pc.ARKOSE,
        public_key=pc.SITE_KEY,
        site="https://www.roblox.com",
        proxy=proxy_url,
        capi_mode="lightbox",
        style_theme="modal",
        title="Roblox",
        sitedata_location_href="https://www.roblox.com/CreateAccount",
        rsa_public_key=RSA_SPKI_B64,
    )
    solver.data_blob = blob
    result = solver.solve()
    return result.token, result.suppressed


def metadata_with(challenge: dict, **fields) -> str:
    merged = dict(challenge["meta"])
    merged.update(fields)
    return b64(json.dumps(merged))


VARIANTS: dict[str, str] = {
    "b64-full-metadata": "resend with the whole metadata, token injected",
    "raw-json-metadata": "same, but the header is plain JSON not base64",
    "b64-token-only": "header carries only {captchaToken}",
    "no-metadata-header": "just rblx-challenge-id/type, token nowhere",
    "continue-endpoint": "apis.roblox.com/challenge/v1/continue",
    "continue-b64": "continue endpoint, metadata pre-base64d",
}


def attempt(challenge: dict, token: str, variant: str) -> dict:
    session = challenge["session"]
    body = challenge["body"]
    headers = dict(challenge["headers"])
    headers.update({"rblx-challenge-id": challenge["id"],
                    "rblx-challenge-type": challenge["type"]})

    if variant == "b64-full-metadata":
        headers["rblx-challenge-metadata"] = metadata_with(challenge, captchaToken=token)
        resp = session.post(SIGNUP_POST, json=body, headers=headers)
    elif variant == "raw-json-metadata":
        merged = dict(challenge["meta"], captchaToken=token)
        headers["rblx-challenge-metadata"] = json.dumps(merged)
        resp = session.post(SIGNUP_POST, json=body, headers=headers)
    elif variant == "b64-token-only":
        headers["rblx-challenge-metadata"] = b64(json.dumps({"captchaToken": token}))
        resp = session.post(SIGNUP_POST, json=body, headers=headers)
    elif variant == "no-metadata-header":
        resp = session.post(SIGNUP_POST, json=body, headers=headers)
    elif variant in ("continue-endpoint", "continue-b64"):
        payload = {"challengeID": challenge["id"], "challengeType": challenge["type"],
                   "challengeMetadata": json.dumps({"captchaToken": token})}
        if variant == "continue-b64":
            payload["challengeMetadata"] = b64(json.dumps({"captchaToken": token}))
        resp = session.post(CHALLENGE_CONTINUE, json=payload, headers={
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": "https://www.roblox.com",
            "referer": "https://www.roblox.com/CreateAccount",
            **({"x-csrf-token": challenge["csrf"]} if challenge["csrf"] else {}),
        })
    else:
        raise ValueError(variant)

    out = {"status": resp.status_code, "body": resp.text[:160]}
    for key, value in resp.headers.items():
        if key.lower().startswith("rblx-"):
            out[key] = value[:80]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--variants", default="minimal",
                    help="'minimal', 'all', or comma-separated variant names")
    ap.add_argument("--reps", type=int, default=1,
                    help="repeat each variant (a fresh token every time)")
    ap.add_argument("--full-fp", action="store_true",
                    help="use a full enhanced_fp (control: should not suppress)")
    ap.add_argument("--same-session", action="store_true",
                    help="mint over the challenge's own HTTP session, not a new one")
    ap.add_argument("--retries", type=int, default=3,
                    help="new exit per retry when the tunnel or the mint fails")
    ap.add_argument("--minter", default="bda", choices=("bda", "library"),
                    help="'library' mints with the third-party arkose-solver package")
    args = ap.parse_args()

    if args.variants == "all":
        names = list(VARIANTS)
    elif args.variants == "minimal":
        names = ["b64-full-metadata", "continue-endpoint"]
    else:
        names = [v.strip() for v in args.variants.split(",") if v.strip()]

    pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)
    suppressed = not args.full_fp

    print(f"  suppressed fingerprint: {suppressed}  (minter: {args.minter})")
    print(f"  variants: {names}\n")

    results = []
    for name in names:
        for attempt_index in range(args.retries + 1):
            proxy = random.choice(pool)
            proxy_url = parse_proxy(proxy).url()
            challenge = fresh_challenge(proxy_url)
            if not challenge:
                print(f"  {name:<22} no challenge (exit {attempt_index + 1})")
                continue
            blob = challenge["meta"]["dataExchangeBlob"]
            if args.minter == "library":
                try:
                    token, lib_sup = mint_library(proxy_url, blob)
                except Exception as exc:
                    print(f"  {name:<22} library failed: {type(exc).__name__}: "
                          f"{str(exc)[:60]} (exit {attempt_index + 1})")
                    continue
                suppressed = lib_sup
            else:
                fp = Fingerprint.from_raw(random.choice(load_fingerprints()))
                # mint inside the *same* session that holds the challenge cookies:
                # a token from a fresh session is not bound to the challenge
                try:
                    token = mint(proxy_url, blob, fp, suppressed,
                                 session=challenge["session"] if args.same_session else None)
                except Exception as exc:
                    print(f"  {name:<22} mint transport failed: {type(exc).__name__} "
                          f"(exit {attempt_index + 1})")
                    continue
            if not token:
                print(f"  {name:<22} gt2 refused (exit {attempt_index + 1})")
                continue
            sup = "sup=1" in token
            outcome = attempt(challenge, token, name)
            results.append({"variant": name, "sup": sup, **outcome})
            flag = "ACCEPTED" if outcome["status"] in (200, 201) else ""
            print(f"  {name:<22} sup={sup!s:<5} -> {outcome['status']} "
                  f"{outcome['body'][:90]} {flag}")
            time.sleep(0.5)
            break

    wins = [r for r in results if r["status"] in (200, 201)]
    print()
    if wins:
        print(f"  {len(wins)} accepted — suppressed tokens do work:")
        for win in wins:
            print(f"    {win['variant']} (sup={win['sup']})")
    else:
        print("  none accepted; printing the field any 403 carried")
        for res in results:
            extra = {k: v for k, v in res.items()
                     if k not in ("variant", "sup", "status", "body")}
            if extra:
                print(f"    {res['variant']}: {extra}")
    return 0 if wins else 1


if __name__ == "__main__":
    raise SystemExit(main())
