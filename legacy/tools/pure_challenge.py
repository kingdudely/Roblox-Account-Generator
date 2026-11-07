#!/usr/bin/env python3
"""Fetch a live challenge with no browser, and save the images.

Chain so far, all reproduced:

    signup 403      -> dataExchangeBlob
    our `c`         -> gt2 token, unsuppressed
    POST /fc/gfct/  -> challenge id, wave count, image URLs, dapib_url

This stops after the images land on disk. Solving and submitting is the next
step; keeping it separate means a broken solver never hides a broken transport.

    ./tools/pure_challenge.py --proxy-file proxies3.txt
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import bda as bdamod
from roblox_creator import payloads
from roblox_creator.arkose_py import Fingerprint, load_fingerprints, pick_fingerprint
from roblox_creator.config import SIGNUP_POST, load_proxies
from roblox_creator.identity import make_identity
from roblox_creator.proxyurl import parse as parse_proxy

SITE_KEY = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"
ARKOSE = "https://arkoselabs.roblox.com"
BUILD_ID = "270438f7-2325-426f-9636-88978b387c8d"
GAME_CORE = "1.40.0"


def esync() -> str:
    now = time.time()
    return str(int(now - (now % 21600)))


CHROME_BRANDS = '"Google Chrome";v="{v}", "Chromium";v="{v}", "Not(A:Brand";v="24"'


def _ua_bits(ua: str) -> dict[str, str]:
    """Client hints that agree with the UA.

    Every captured fingerprint is an Android phone, so the UA says mobile while
    the request used to send no hints at all — a desktop-shaped hint set would be
    wrong, and an absent one is conspicuous. Derive platform/mobile/version from
    the UA itself so the two can never disagree.
    """
    mobile = "Android" in ua or "iPhone" in ua
    platform = "Android" if "Android" in ua else ("iOS" if "iPhone" in ua else "Linux")
    match = re.search(r"Chrome/(\d+)", ua)
    version = match.group(1) if match else "155"
    return {"sec-ch-ua": CHROME_BRANDS.format(v=version),
            "sec-ch-ua-mobile": "?1" if mobile else "?0",
            "sec-ch-ua-platform": f'"{platform}"'}


def arkose_headers(ua: str, referer: str) -> dict[str, str]:
    headers = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "origin": "https://www.roblox.com",
        "referer": referer,
        "user-agent": ua,
        "x-ark-esync-value": esync(),
        "ark-build-id": BUILD_ID,
    }
    headers.update(_ua_bits(ua))
    return headers


def json_headers(ua: str, referer: str) -> dict[str, str]:
    """Headers for the /pows/* calls, which post JSON, not a form.

    Getting this wrong is silent: `/pows/check` answers `400 {"error":"Invalid
    Request"}` when the body cannot be parsed as JSON, which looks exactly like a
    rejected proof-of-work. The browser always sends `application/json` here.
    """
    headers = arkose_headers(ua, referer)
    headers["content-type"] = "application/json"
    headers["accept"] = "*/*"
    return headers


def new_session(proxy_url: str | None):
    import tls_client

    session = tls_client.Session(client_identifier="chrome_131")
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    return session


def request_blob(proxy_url: str | None, tries: int = 10) -> dict | None:
    """signup 403 -> the Arkose-flavoured challenge and its data-exchange blob."""
    from curl_cffi import requests as creq

    session = creq.Session(impersonate="chrome", timeout=30)
    session.proxies = {"http": proxy_url, "https": proxy_url}
    identity = make_identity(random.Random())
    body = {"username": identity.username, "password": identity.password,
            "birthday": "1990-01-01"}
    headers = {"origin": "https://www.roblox.com",
               "referer": "https://www.roblox.com/CreateAccount",
               "accept": "application/json, text/plain, */*"}
    first = session.post(SIGNUP_POST, json=body, headers=headers)
    csrf = first.headers.get("x-csrf-token", "")
    if csrf:
        headers["x-csrf-token"] = csrf

    for _ in range(tries):
        resp = session.post(SIGNUP_POST, json=body, headers=headers)
        kind = resp.headers.get("rblx-challenge-type", "")
        if kind == "captcha" and resp.headers.get("rblx-challenge-metadata"):
            import base64

            raw = resp.headers["rblx-challenge-metadata"]
            meta = json.loads(base64.b64decode(raw + "=" * (-len(raw) % 4)))
            return {"challenge_id": resp.headers.get("rblx-challenge-id", ""),
                    "challenge_type": kind, "metadata": meta,
                    "raw_metadata": raw, "csrf": csrf, "body": body,
                    "headers": headers, "session": session}
        body["username"] += str(random.randint(0, 9))
    return None


def gt2(proxy_url: str | None, blob: str, fp: Fingerprint,
        session=None) -> dict:
    """gt2, with a transport retry.

    tls_client occasionally gets `unexpected EOF` from Arkose under a SOCKS
    tunnel — the connection is dropped before a response. It is not a rejection
    (that is a 401 with a body), so retrying on a fresh session is safe. The
    blob survives the retry only because the failed attempt never completed.
    """
    form = {"c": bdamod.build(fp), "public_key": SITE_KEY,
            "site": "https://www.roblox.com", "userbrowser": fp.user_agent,
            "capi_version": "4.6.1", "capi_mode": "lightbox",
            "style_theme": "modal", "rnd": str(random.random()),
            "data[blob]": blob}
    last: dict = {}
    for attempt in range(4):
        try:
            http = session or new_session(proxy_url)
            resp = http.post(
                f"{ARKOSE}/fc/gt2/public_key/{SITE_KEY}", data=form,
                headers=arkose_headers(fp.user_agent, "https://www.roblox.com/"),
                cookies={"timestamp": esync()}, timeout_seconds=25)
        except Exception as exc:
            last = {"status": "EOF", "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}
            time.sleep(0.6)
            continue
        out = {"status": resp.status_code}
        try:
            out.update(resp.json())
        except Exception:
            out["raw"] = resp.text[:200]
        return out
    return last


def gt2_with(proxy_url: str | None, blob: str, c_blob: str, ua: str,
             session=None) -> dict:
    """gt2 using an already-built `c` (i.e. a re-sealed captured payload)."""
    form = {"c": c_blob, "public_key": SITE_KEY,
            "site": "https://www.roblox.com", "userbrowser": ua,
            "capi_version": "4.6.1", "capi_mode": "lightbox",
            "style_theme": "modal", "rnd": str(random.random()),
            "data[blob]": blob}
    last: dict = {}
    for attempt in range(4):
        try:
            http = session or new_session(proxy_url)
            resp = http.post(
                f"{ARKOSE}/fc/gt2/public_key/{SITE_KEY}", data=form,
                headers=arkose_headers(ua, "https://www.roblox.com/"),
                cookies={"timestamp": esync()}, timeout_seconds=25)
        except Exception as exc:
            last = {"status": "EOF", "detail": f"{type(exc).__name__}: {str(exc)[:80]}"}
            time.sleep(0.6)
            continue
        out = {"status": resp.status_code}
        try:
            out.update(resp.json())
        except Exception:
            out["raw"] = resp.text[:200]
        return out
    return last


def pows(proxy_url: str | None, token: str, ua: str,
          workdir: Path | None = None, session=None) -> dict:
    """Solve the proof-of-work stage that gates gfct.

    Arkose will not hand out a game until the session has ground a nonce. The
    work itself is a self-contained Web Worker served from a signed URL — no DOM
    — so Node runs it directly in ~80 ms. That keeps the whole path browser-free
    while using Arkose's own algorithm instead of a reimplementation that would
    have to be re-derived every time they bump `pow/2.5.0`.

    Protocol, from the pow bundle:

        GET  /pows/setup?session_token=..   -> pow_token + signed sequence + config
        (worker: {seed, starting_nonce, splits[]} -> nonce per split)
        POST /pows/started {session_token, pow_token, round, time}
        POST /pows/split   {.., split_num, execution_time, hash_rate, result}
        POST /pows/check   {.., hash_rate, execution_time, transform, result}

    Each round returns a *new* pow_token, so this loops until the server stops
    asking for more work.
    """
    import subprocess

    workdir = workdir or (ROOT / "debug" / "pure" / time.strftime("%Y%m%d-%H%M%S"))
    workdir.mkdir(parents=True, exist_ok=True)
    session = session or new_session(proxy_url)
    session_token = token.split("|")[0]
    rounds: list[dict] = []

    setup_resp = session.get(
        f"{ARKOSE}/pows/setup", params={"session_token": session_token},
        headers=arkose_headers(ua, f"{ARKOSE}/"), timeout_seconds=25)
    if setup_resp.status_code != 200:
        return {"ok": False, "stage": "setup", "status": setup_resp.status_code}
    setup = setup_resp.json()
    workdir.joinpath("pows-setup-0.json").write_text(json.dumps(setup, indent=1))

    # the worker script is the same for every round; only the work changes
    seq_resp = session.get(setup["sequence"], headers={"user-agent": ua}, timeout_seconds=25)
    if seq_resp.status_code != 200:
        return {"ok": False, "stage": "sequence", "status": seq_resp.status_code}
    seq_path = workdir / "powseq.js"
    seq_path.write_bytes(seq_resp.content)

    # `check` answers `next` with a fresh work_config and keeps the same
    # pow_token. Rotating the token by re-fetching /pows/setup instead makes the
    # session inconsistent and every later call 400s.
    spec = {
        "pow_token": setup["pow_token"],
        "timeout": setup.get("timeout"),
        "work_type": setup.get("work_type"),
        "work_config": setup.get("work_config"),
        "url": setup.get("url"),
    }

    for round_index in range(10):
        setup_path = workdir / f"work-{round_index}.json"
        setup_path.write_text(json.dumps(spec, indent=1))

        try:
            proc = subprocess.run(
                ["node", str(ROOT / "tools" / "powrun.js"), str(seq_path), str(setup_path)],
                capture_output=True, text=True,
                timeout=max((spec.get("timeout") or 75000) / 1000 + 20, 30))
        except Exception as exc:
            return {"ok": False, "stage": "node", "detail": f"{type(exc).__name__}: {exc}"}
        line = ""
        for candidate in reversed(proc.stdout.splitlines()):
            candidate = candidate.strip()
            if candidate.startswith("{"):
                line = candidate
                break
        if not line:
            return {"ok": False, "stage": "node",
                    "detail": f"stdout={proc.stdout[-160:]!r} stderr={proc.stderr[-160:]!r}"}
        try:
            result = json.loads(line)
        except Exception:
            return {"ok": False, "stage": "node", "detail": line[:200]}
        done = next((m for m in result.get("messages", []) if m.get("type") == "done"), None)
        if not done:
            return {"ok": False, "stage": "work", "detail": str(result)[:200]}

        data = done["data"]
        pow_token = spec["pow_token"]
        common = {"session_token": session_token, "pow_token": pow_token,
                  "round": round_index, "time": int(time.time() * 1000)}

        # Timings stay exactly as the worker measured them. Renormalising them to
        # look browser-like was tried and made the block rate worse: the server
        # compares the reported execution_time against its own clock between
        # /pows/started and /pows/check, so an inflated figure is a stronger bot
        # signal than a fast one. Node just hashes slower than Chrome does.
        starting_nonce = int((spec.get("work_config") or {}).get("starting_nonce") or 0)
        started = session.post(f"{ARKOSE}/pows/started", json=common,
                               headers=json_headers(ua, f"{ARKOSE}/"),
                               timeout_seconds=25)
        posts = {"started": started.status_code}

        reported = set((spec.get("work_config") or {}).get("report_splits") or [])
        for message in result.get("messages", []):
            if message.get("type") != "split_done":
                continue
            if reported and message["data"]["splitNum"] not in reported:
                continue
            body = dict(common)
            body.update({"split_num": message["data"]["splitNum"],
                         "execution_time": message["data"]["executionTime"],
                         "hash_rate": message["data"]["hashRate"],
                         "result": message["data"]["result"]})
            split = session.post(f"{ARKOSE}/pows/split", json=body,
                                 headers=json_headers(ua, f"{ARKOSE}/"),
                                 timeout_seconds=25)
            posts[f"split{message['data']['splitNum']}"] = split.status_code

        check_body = {
            "pow_token": pow_token,
            "session_token": session_token,
            "hash_rate": data.get("hashRate"),
            "execution_time": data.get("time"),
            "transform": data.get("finalTransform"),
            "result": [{"target_hash": item.get("targetHash"),
                        "attempt_count": item.get("iterations")}
                       for item in data.get("targetHashData", [])],
        }
        check = session.post(f"{ARKOSE}/pows/check", json=check_body,
                             headers=json_headers(ua, f"{ARKOSE}/"),
                             timeout_seconds=25)
        try:
            check_doc = check.json()
        except Exception:
            check_doc = {"raw": check.text[:200]}
        action = check_doc.get("action") if isinstance(check_doc, dict) else None
        rounds.append({"round": round_index, "status": check.status_code, "action": action})

        if action == "challenge":
            return {"ok": True, "rounds": rounds, "workdir": str(workdir)}
        if action == "next" and check_doc.get("work_config"):
            spec = {
                "pow_token": check_doc.get("pow_token", pow_token),
                "timeout": check_doc.get("timeout"),
                "work_type": check_doc.get("work_type"),
                "work_config": check_doc["work_config"],
                "url": spec.get("url"),
            }
            continue
        if check.status_code != 200:
            return {"ok": False, "stage": "check", "status": check.status_code,
                    "body": check_doc, "rounds": rounds}
        return {"ok": False, "stage": "unexpected", "body": check_doc, "rounds": rounds}

    return {"ok": False, "stage": "rounds", "rounds": rounds}


def gfct(proxy_url: str | None, token: str, ua: str, session=None) -> dict:
    session = session or new_session(proxy_url)
    sid = ""
    for part in token.split("|"):
        if part.startswith("r="):
            sid = part[2:]
    referer = (f"{ARKOSE}/fc/assets/ec-game-core/game-core/{GAME_CORE}/standard/"
               f"index.html?session={token.split('|')[0]}")
    resp = session.post(
        f"{ARKOSE}/fc/gfct/",
        data={"token": token.split("|")[0], "sid": sid, "render_type": "canvas",
              "lang": "", "isAudioGame": "false",
              "is_compatibility_mode": "false", "apiBreakerVersion": "green",
              "analytics_tier": "40"},
        headers=arkose_headers(ua, referer),
        cookies={"timestamp": esync()}, timeout_seconds=25)
    out = {"status": resp.status_code}
    try:
        out.update(resp.json())
    except Exception:
        out["raw"] = resp.text[:300]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies3.txt")
    ap.add_argument("--tries", type=int, default=1,
                    help="retry the whole chain on a fresh exit until it lands")
    ap.add_argument("--bda", default="ours", choices=("ours", "library", "suppressed"),
                    help="which fingerprint to mint with; 'library' uses arkose-solver")
    args = ap.parse_args()

    pool = [args.proxy] if args.proxy else load_proxies(args.proxy_file)
    for attempt in range(args.tries):
        proxy = random.choice(pool)
        print(f"  [{attempt}] proxy: {parse_proxy(proxy).masked()}")
        code = attempt_once(proxy, bda_source=args.bda)
        if code == 0:
            return 0
    return 1


def library_c(blob: str) -> tuple[str, str]:
    """arkose-solver's BDA, encrypted with the pinned Roblox key."""
    from arkose_solver.bda import generate_bda
    from arkose_solver.crypto import encrypt, marshal_bda

    from roblox_creator.bda import RSA_SPKI_B64

    cfg = {
        "surl": ARKOSE,
        "language": "en-US",
        "title": "Roblox",
        "window_location_href": "https://www.roblox.com/CreateAccount",
        "sitedata_location_href": "https://www.roblox.com/CreateAccount",
        "document_referrer": "https://www.roblox.com/",
        "enforcement_hash": "",
    }
    items = generate_bda(cfg, BUILD_ID)
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")
    return encrypt(marshal_bda(items), RSA_SPKI_B64), ua


def attempt_once(proxy: str, bda_source: str = "ours") -> int:
    proxy_url = parse_proxy(proxy).url()

    challenge = request_blob(proxy_url)
    if not challenge:
        print("  no Arkose-flavoured challenge")
        return 1
    print(f"  challenge id: {challenge['challenge_id']}")

    session = new_session(proxy_url)
    payload_pool = payloads.load_payloads()
    record = payloads.pick_payload(payload_pool)

    if bda_source == "library":
        c_blob, ua = library_c(challenge["metadata"]["dataExchangeBlob"])
        print("  payload: arkose-solver BDA")
        minted = gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                          c_blob, ua, session=session)
    elif bda_source == "suppressed":
        fp = Fingerprint.from_raw(pick_fingerprint(load_fingerprints()))
        ua = fp.user_agent
        print("  payload: suppressed (no enhanced_fp)")
        minted = gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                          bdamod.build(fp, enhanced_fp=[]), ua, session=session)
    elif record is not None:
        c_blob, ua = payloads.reseal(record)
        kind = "mobile" if record.get("mobile") else "desktop"
        print(f"  payload: {kind} corpus entry ({len(payload_pool)} stored)")
        minted = gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                          c_blob, ua, session=session)
    else:
        fp = Fingerprint.from_raw(pick_fingerprint(load_fingerprints()))
        ua = fp.user_agent
        minted = gt2(proxy_url, challenge["metadata"]["dataExchangeBlob"], fp,
                     session=session)
    token = minted.get("token", "")
    print(f"  gt2: {minted['status']} sup={'sup=1' in token} len={len(token)}")
    if not token:
        return 1

    pow_result = pows(proxy_url, token, ua, session=session)
    print(f"  pows: ok={pow_result.get('ok')} {pow_result.get('rounds')}")
    if not pow_result.get("ok"):
        stage = pow_result.get("stage") or (pow_result.get("body") or {}).get("action")
        print(f"    pows stopped: {str({k: v for k, v in pow_result.items() if k != 'rounds'})[:160]}")
        last = (pow_result.get("rounds") or [{}])[-1]
        if last.get("action") == "block":
            print("    blocked by the pow stage (risk decision, not a bad nonce)")
        elif stage:
            print(f"    stage={stage}")
        return 1

    game = gfct(proxy_url, token, ua, session=session)
    print(f"  gfct: {game['status']}")
    if game["status"] != 200:
        print(f"    {str(game)[:300]}")
        return 1

    data = game.get("game_data") or {}
    gui = data.get("customGUI") or {}
    images = gui.get("_challenge_imgs") or []
    print(f"  challengeID : {game.get('challengeID')}")
    print(f"  gameType    : {data.get('gameType')}  waves: {data.get('waves')}")
    print(f"  instruction : {data.get('instruction_string')}")
    print(f"  images      : {len(images)}")
    print(f"  dapib_url   : {str(game.get('dapib_url'))[:80]}")

    out = ROOT / "debug" / "pure" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    (out / "gfct.json").write_text(json.dumps(game, indent=1))
    (out / "gt2.json").write_text(json.dumps(
        {k: v for k, v in minted.items() if k != "token"}, indent=1))

    # Every wave is the same signed URL with `challenge=<wave index>`; the
    # signature covers the path, not the query, so only the index changes.
    saved = []
    waves = int(data.get("waves") or 1)
    # The signed image URL is only honoured from inside the game frame, so the
    # referer has to be the game-core page itself, session token included.
    game_referer = (f"{ARKOSE}/fc/assets/ec-game-core/game-core/{GAME_CORE}/standard/"
                    f"index.html?session={game.get('session_token')}")
    for index in range(waves):
        url = images[0].replace("challenge=0", f"challenge={index}")
        try:
            resp = session.get(url, headers={
                "user-agent": ua,
                "referer": game_referer,
                "accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "sec-fetch-dest": "image",
                "sec-fetch-mode": "no-cors",
                "sec-fetch-site": "same-origin",
            }, timeout_seconds=25)
            magic = resp.content[:4]
            is_image = magic[:3] == b"\xff\xd8\xff" or magic == b"\x89PNG"
            if resp.status_code == 200 and is_image:
                ext = "png" if magic == b"\x89PNG" else "jpg"
                name = f"wave{index}.{ext}"
                (out / name).write_bytes(resp.content)
                saved.append((name, len(resp.content)))
            else:
                print(f"    wave {index}: {resp.status_code} "
                      f"{resp.content[:40]!r}")
        except Exception as exc:
            print(f"    wave {index} failed: {type(exc).__name__}")

    for name, size in saved:
        print(f"    {name}: {size} bytes")
    print(f"  -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
