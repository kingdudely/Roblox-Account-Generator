#!/usr/bin/env python3
"""Drive Google account signup with raw batchexecute RPCs -- no rendered UI.

Why this exists
---------------
`tools/google_rpc_capture.py` recorded the wizard's wire truth. The whole
signup is six RPCs against

    accounts.google.com/lifecycle/_/AccountLifecyclePlatformSignupUi/data/batchexecute

    MjyMj   bootstrap                       -> []
    E815hb  name                            -> steps/signup/birthdaygender
    ink9M   profile config                  -> [..,"Tomas"]
    eOY7Bb  birthday (+ device token)       -> steps/signup/username
    xdYwpe  username suggestions            -> ["name1","name2"]
    NHJMOd  username choice                 -> steps/signup/password
    dOJftd  availability flags              -> [true,true,...]
    ZNd7Td  password (+ device token)       -> steps/signup/crossflowverification
                                               ...or an account

The question this tool answers: is the phone gate a *server decision* attached
to `ZNd7Td`, or a browser-side artefact? If a plain HTTP client gets the same
`crossflowverification` answer, the gate is server-side and no amount of UI
automation moves it. If the RPC path instead creates the account, the browser
was the probem all along.

It never submits a phone number and never scans a QR. It stops when Google asks
for one and prints exactly which call produced the ask.

    ./tools/google_rpc_signup.py
    ./tools/google_rpc_signup.py --proxy http://host:port
    ./tools/google_rpc_signup.py --token '<captured device token>'   # replay
"""

from __future__ import annotations

import argparse
import json
import random
import re
import string
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
SIGNUP_URL = ("https://accounts.google.com/signup/v2/createaccount"
              "?flowName=GlifWebSignIn&flowEntry=SignUp")
RPC_URL = ("https://accounts.google.com/lifecycle/_/"
           "AccountLifecyclePlatformSignupUi/data/batchexecute")
MANAGE = "https://accounts.google.com/ManageAccount?nc=1"
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

FIRST_NAMES = ["Tomas", "Elliot", "Marek", "Jonas", "Adrian", "Nikolai",
               "Rafael", "Sander", "Emil", "Viktor"]
LAST_NAMES = ["Reid", "Volkov", "Andersen", "Kowalski", "Novak", "Brandt",
              "Silva", "Haas", "Larsen", "Moretti"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def wiz_data(html: str) -> dict:
    m = re.search(r"window\.WIZ_global_data = (\{.*?\});</script>", html, re.S)
    if not m:
        raise RuntimeError("WIZ_global_data not found in signup page")
    return json.loads(m.group(1))


def parse_batchexecute(text: str) -> list:
    """Strip the )]}' prefix and return every decoded JSON frame."""
    body = text.lstrip()
    if body.startswith(")]}'"):
        body = body[4:]
    frames = []
    for line in body.splitlines():
        line = line.strip()
        if not line or line.isdigit():
            continue
        try:
            frames.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return frames


def rpc_payload(frames: list, rpcid: str):
    """Pull the wrb.fr payload string for one rpcid and decode it."""
    for frame in frames:
        if not isinstance(frame, list):
            continue
        for item in frame:
            if (isinstance(item, list) and len(item) > 2
                    and item[0] == "wrb.fr" and item[1] == rpcid):
                raw = item[2]
                if raw in (None, ""):
                    return None
                try:
                    return json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    return raw
    return None


def find_next_step(value) -> str | None:
    """Recursively look for a `steps/signup/...` marker in a decoded payload."""
    if isinstance(value, str):
        return value if value.startswith("steps/") else None
    if isinstance(value, list):
        for item in value:
            hit = find_next_step(item)
            if hit:
                return hit
    if isinstance(value, dict):
        for item in value.values():
            hit = find_next_step(item)
            if hit:
                return hit
    return None


class RpcSignup:
    def __init__(self, proxy: str | None = None, hl: str = "en-US",
                 out: Path | None = None, verbose: bool = True):
        self.s = requests.Session()
        self.s.headers.update({
            "user-agent": USER_AGENT,
            "accept-language": f"{hl},en;q=0.9",
            "origin": "https://accounts.google.com",
            "referer": "https://accounts.google.com/",
            "x-same-domain": "1",
        })
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.hl = hl
        self.out = out
        self.verbose = verbose
        self.reqid = random.randint(10000, 99999)
        self.log: list[dict] = []
        self.at = self.fsid = self.dsh = self.bl = self.tl = ""
        self.path = ""

    # -- plumbing ---------------------------------------------------------
    def say(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    def record(self, kind: str, **kw):
        rec = {"t": now_iso(), "kind": kind, **kw}
        self.log.append(rec)
        if self.out:
            with (self.out / "trace.jsonl").open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec) + "\n")

    def bootstrap(self) -> None:
        r = self.s.get(SIGNUP_URL, allow_redirects=True, timeout=45)
        r.raise_for_status()
        d = wiz_data(r.text)
        self.at = d["SNlM0e"]
        self.fsid = d["FdrFJe"]
        self.dsh = d["Qzxixc"]
        self.bl = d["cfb2h"]
        m = re.search(r"[?&]TL=([A-Za-z0-9_\-]+)", r.url)
        self.tl = m.group(1) if m else ""
        self.path = "/lifecycle/steps/signup/name"
        self.record("bootstrap", url=r.url, at=self.at, fsid=self.fsid,
                    dsh=self.dsh, bl=self.bl, tl=self.tl,
                    cookies=[c.name for c in self.s.cookies])
        self.say(f"bootstrap at={self.at[:18]}… f.sid={self.fsid} bl={self.bl}")

    def rpc(self, rpcid: str, payload, source_path: str,
            inner: str = "generic", include_at: bool = True):
        self.reqid += 100000
        params = {
            "rpcids": rpcid,
            "source-path": source_path,
            "f.sid": self.fsid,
            "bl": self.bl,
            "hl": self.hl,
            "TL": self.tl,
            "dsh": self.dsh,
            "_reqid": str(self.reqid),
            "rt": "c",
        }
        body = {"f.req": json.dumps([[[rpcid, json.dumps(payload), None, inner]]],
                                    separators=(",", ":"))}
        if include_at and self.at:
            body["at"] = self.at
        r = self.s.post(RPC_URL, params=params, data=body, timeout=45,
                        headers={"content-type":
                                 "application/x-www-form-urlencoded;charset=utf-8"})
        frames = parse_batchexecute(r.text)
        decoded = rpc_payload(frames, rpcid)
        self.record("rpc", rpcid=rpcid, status=r.status_code,
                    request=payload, response=decoded, raw=r.text[:4000])
        nxt = find_next_step(decoded)
        self.say(f"  {rpcid:<8} {r.status_code} -> {json.dumps(decoded)[:150]}"
                 + (f"   [next={nxt}]" if nxt else ""))
        return decoded, nxt, r.status_code

    # -- flow -------------------------------------------------------------
    def run(self, first: str, last: str, password: str,
            token: str | None, year: int, month: int, day: int) -> dict:
        result = {"status": "unknown", "gate_rpc": None, "next": None}

        self.bootstrap()

        self.say("\n[1] MjyMj bootstrap")
        self.rpc("MjyMj", [0, 0, [None] * 7 + [0, 0, 1, "", None, None, 2, 1, 2]],
                 self.path)

        self.say("\n[2] E815hb name")
        _, nxt, _ = self.rpc("E815hb", [first, last, None, None, None, [], None, 1],
                             self.path)
        if nxt != "steps/signup/birthdaygender":
            self.say(f"  !! unexpected next step {nxt}")
        self.path = "/lifecycle/steps/signup/birthdaygender"

        self.say("\n[3] ink9M profile config")
        self.rpc("ink9M", [], self.path, inner="1")

        self.say("\n[4] eOY7Bb birthday")
        # token slot: [null] when we have nothing, [captured] when replaying
        tail = [[token]] if token else [None]
        decoded, nxt, _ = self.rpc(
            "eOY7Bb",
            [[year, month, day], 2, None, None, None, None,
             [None, None, MANAGE], tail],
            self.path)
        if nxt != "steps/signup/username":
            self.say(f"  !! birthday rejected (next={nxt}) -> {decoded}")
            result.update(status="birthday_rejected", gate_rpc="eOY7Bb",
                          next=nxt, response=decoded)
            return result
        self.path = "/lifecycle/steps/signup/username"

        self.say("\n[5] xdYwpe username suggestions")
        decoded, _, _ = self.rpc("xdYwpe", [1], self.path, inner="1")
        suggestions = []
        if isinstance(decoded, list) and decoded and isinstance(decoded[0], list):
            suggestions = [s for s in decoded[0] if isinstance(s, str)]
        if not suggestions:
            suggestions = [f"{first.lower()}.{last.lower()}"
                           f"{random.randint(100, 999)}"]
        username = suggestions[0]
        self.say(f"  picked username: {username}")

        self.say("\n[6] NHJMOd username choice")
        decoded, nxt, _ = self.rpc(
            "NHJMOd", [username, 0, 0, None, [None, None, None, None, 1, 4105], 0, 40],
            self.path)
        if nxt != "steps/signup/password":
            self.say(f"  !! username rejected (next={nxt}) -> {decoded}")
            result.update(status="username_rejected", gate_rpc="NHJMOd",
                          next=nxt, response=decoded, username=username)
            return result
        self.path = "/lifecycle/steps/signup/password"

        self.say("\n[7] dOJftd availability flags")
        self.rpc("dOJftd", [None, None, None, None, None, None,
                            [None, None, MANAGE]], self.path, inner="1")

        self.say("\n[8] ZNd7Td password  <-- the gate decision happens here")
        ptoken = [[token]] if token else [None]
        decoded, nxt, status = self.rpc("ZNd7Td", [password, ptoken], self.path)
        result.update(next=nxt, response=decoded, username=username,
                      gate_rpc="ZNd7Td")
        if nxt and "crossflowverification" in nxt:
            result["status"] = "phone_gate"
            self.say("  == crossflow gate. server-side decision, confirmed.")
        elif nxt:
            result["status"] = "advanced"
            self.say(f"  == advanced to {nxt}")
        else:
            result["status"] = "no_next_step"
        return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--token", default=None,
                    help="captured device token to replay in eOY7Bb/ZNd7Td")
    ap.add_argument("--first", default=random.choice(FIRST_NAMES))
    ap.add_argument("--last", default=random.choice(LAST_NAMES))
    ap.add_argument("--password", default=None)
    ap.add_argument("--year", type=int, default=1992)
    ap.add_argument("--month", type=int, default=7)
    ap.add_argument("--day", type=int, default=12)
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = ROOT / "debug" / "google_rpc_signup" / stamp
    out.mkdir(parents=True, exist_ok=True)

    password = args.password or (
        "".join(random.choice(string.ascii_letters) for _ in range(7))
        + str(random.randint(10, 99)) + "!" + random.choice("aeiou"))

    flow = RpcSignup(proxy=args.proxy, out=out, verbose=not args.quiet)
    flow.say(f"identity: {args.first} {args.last}  password: {password}")
    try:
        result = flow.run(args.first, args.last, password, args.token,
                          args.year, args.month, args.day)
    except requests.RequestException as exc:
        result = {"status": "network_error", "error": str(exc)}

    (out / "result.json").write_text(json.dumps(result, indent=2))
    flow.say(f"\nstatus: {result.get('status')}  (artifacts: {out})")
    return 0 if result.get("status") not in ("network_error",) else 1


if __name__ == "__main__":
    raise SystemExit(main())
