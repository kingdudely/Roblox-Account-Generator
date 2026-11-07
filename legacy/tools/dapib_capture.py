#!/usr/bin/env python3
"""Capture the whole Arkose conversation for one real challenge.

Why this exists
---------------
`boot.js` (the game core bootstrap) is plain webpack and does the protocol:
`gt`, `gfct/`, `ca/`, `ekey/`, `a/`. The obfuscated part is the *game-core*
chunk, and the piece that actually matters — the `c` payload — is not in any
statically-fetchable file at all. It is produced by a module-federation remote
entry that the server hands out per session:

    gfct response -> dapib_url = /dapib/<region>/<uuid>/578.js?expiry=..&mac=..

That URL is MAC-signed and expires in ~24h, and the string table inside the
game-core chunk says what it is for: `DAPIB`, `dapibReceive`,
`getBiometricsData`, `keyboard_biometrics`, `tbu/mbio/kbio`. So `c` is a
behavioural-biometrics attestation, not a crypto primitive.

This tool does the only thing that turns that from a guess into a fact: it runs
one real signup, and writes every request body, response body, and remote script
to disk *while the signature is valid*.

What it saves, under debug/dapib/<timestamp>/
    manifest.json          ordered record of every captured exchange
    fc-gt2-request.txt     the body we send to set up a session (has `bda`)
    fc-gfct-request.txt    the body that asks for the game
    fc-gfct-response.json  challenge + image URLs + dapib_url
    dapib-578.js           the remote entry (the thing we cannot get cold)
    script-*.js            every other chunk the page pulls
    fc-ca-request.txt      the answer submission, if we get that far
    image-*.png            the puzzle sprites, so the CV work has live data

    ./tools/dapib_capture.py
    ./tools/dapib_capture.py --headless --proxy socks5://...
"""

from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import captcha as cap          # noqa: E402
from roblox_creator.browser import BrowserSession  # noqa: E402
from roblox_creator.config import Settings, load_proxies  # noqa: E402
from roblox_creator.creator import SEL_SUBMIT, Creator  # noqa: E402
from roblox_creator.identity import make_identity  # noqa: E402
from roblox_creator.store import AccountStore      # noqa: E402

OUT_ROOT = ROOT / "debug" / "dapib"
INTERESTING = ("arkoselabs.roblox.com", "roblox.com/fc", "auth.roblox.com")


def _slug(url: str) -> str:
    """A stable, filesystem-safe name for a request URL."""
    u = urlparse(url)
    path = u.path.strip("/").replace("/", "_")
    if u.query:
        tail = re.sub(r"[^A-Za-z0-9_.-]+", "", u.query)[:40]
        path = f"{path}__{tail}"
    return path[:120] or "root"


class Recorder:
    """Collects request/response pairs off one Playwright page."""

    def __init__(self, out: Path, page):
        self.out = out
        self.page = page
        self.manifest: list[dict] = []
        self.saved: dict[str, Path] = {}
        self.session_token: str | None = None
        self.game_token: str = ""
        self.dapib_url: str | None = None
        self.serial = 0
        page.on("request", self._on_request)
        page.on("response", self._on_response)

    # -- listeners ------------------------------------------------------------

    def _wanted(self, url: str) -> bool:
        return any(k in url for k in INTERESTING)

    def _on_request(self, request) -> None:
        try:
            url = request.url
            if not self._wanted(url):
                return
            body = None
            if request.method in ("POST", "PUT"):
                try:
                    body = request.post_data
                except Exception:
                    body = None
            self.serial += 1
            entry = {
                "n": self.serial,
                "kind": "request",
                "method": request.method,
                "url": url,
                "headers": dict(request.headers),
                "body_len": len(body or ""),
            }
            self.manifest.append(entry)

            if body and "/fc/" in url:
                # this is the interesting class: gt2 / gfct / ca / ekey bodies
                name = f"fc-{_slug(url)}-request.txt"
                (self.out / name).write_text(body, encoding="utf-8", errors="replace")
                entry["saved"] = name
                # pick up the field names we care about without dumping values
                entry["fields"] = sorted({k for k in re.findall(r"(?:^|&)([A-Za-z0-9_\[\]%.]+)=", body)})
                if "dapib" in body or re.search(r"(?:^|&)c=", body):
                    entry["has_c"] = True
        except Exception:
            pass

    def _on_response(self, response) -> None:
        try:
            url = response.url
            if not self._wanted(url):
                return
            status = response.status
            ctype = ""
            try:
                ctype = response.headers.get("content-type", "")
            except Exception:
                pass

            entry = {
                "n": self.serial,
                "kind": "response",
                "status": status,
                "url": url,
                "content_type": ctype,
            }
            self.serial += 1
            self.manifest.append(entry)

            if "/dapib/" in url:
                self.dapib_url = url
                entry["DAPIB"] = True

            if status != 200:
                return

            if "json" in ctype or url.rstrip("/").endswith(("gfct", "gt", "gt2", "ekey")):
                body = response.text()
                name = f"{_slug(url)}.json"
                (self.out / name).write_text(body, encoding="utf-8", errors="replace")
                entry["saved"] = name
                try:
                    doc = json.loads(body)
                    if "session_token" in doc:
                        self.session_token = doc["session_token"]
                    if doc.get("challengeID"):
                        self.game_token = doc["challengeID"]
                    if doc.get("dapib_url"):
                        self.dapib_url = doc["dapib_url"]
                        entry["dapib_in_json"] = True
                    if doc.get("game_data"):
                        gd = doc["game_data"]
                        entry["game_type"] = gd.get("gameType")
                        entry["waves"] = gd.get("waves")
                        imgs = (gd.get("customGUI") or {}).get("_challenge_imgs") or []
                        entry["challenge_imgs"] = len(imgs)
                except Exception:
                    pass
            elif "javascript" in ctype:
                body = response.text()
                if "/dapib/" in url:
                    name = "dapib-578.js"
                else:
                    name = f"script-{_slug(url)}.js"
                (self.out / name).write_text(body, encoding="utf-8", errors="replace")
                entry["saved"] = name
                entry["bytes"] = len(body)
            elif "image" in ctype:
                body = response.body()
                name = f"image-{_slug(url)}.png"
                (self.out / name).write_bytes(body)
                entry["saved"] = name
                entry["bytes"] = len(body)
        except Exception as exc:  # body can vanish mid-navigation; not fatal
            try:
                self.manifest.append({"kind": "error", "url": response.url,
                                      "error": type(exc).__name__})
            except Exception:
                pass

    # -- follow-up ------------------------------------------------------------

    def refetch_dapib(self) -> str:
        """Pull the remote entry through the page context while the MAC is live."""
        if not self.dapib_url:
            return "no dapib_url seen"
        for attempt in range(3):
            try:
                resp = self.page.request.get(self.dapib_url, timeout=30_000)
                if resp.status == 200:
                    body = resp.text()
                    (self.out / "dapib-578.js").write_text(body, encoding="utf-8", errors="replace")
                    return f"ok {len(body)} bytes"
                time.sleep(1.5)
            except Exception as exc:
                last = f"{type(exc).__name__}: {exc}"
                time.sleep(1.5)
        return f"failed ({last})"


def probe_ekey(session_token: str, game_token: str, out: Path) -> dict:
    """`ekey/` is advertised in the bootstrap's endpoint table but never called
    from it — the call site lives in the game-core chunk. Worth one shot: it is
    literally named "get encryption key"."""
    from curl_cffi import requests as creq

    s = creq.Session(impersonate="chrome", timeout=25)
    base = "https://arkoselabs.roblox.com/fc"
    results = {}
    variants = [
        ("post-token", "post", f"{base}/ekey/", {"session_token": session_token}),
        ("post-both", "post", f"{base}/ekey/", {"session_token": session_token, "game_token": game_token}),
        ("get-token", "get", f"{base}/ekey/", None),
        ("post-gt", "post", f"{base}/ekey/{game_token}", {"session_token": session_token}),
    ]
    for name, verb, url, data in variants:
        try:
            r = s.post(url, data=data) if verb == "post" else s.get(url, params={"session_token": session_token})
            results[name] = {"status": r.status_code, "body": r.text[:400]}
        except Exception as exc:
            results[name] = {"error": type(exc).__name__}
    s.close()
    (out / "ekey-probe.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


def capture_once(proxy: str | None, out: Path, args) -> bool:
    """One attempt. Returns True if we got far enough to learn something."""
    settings = Settings(headless=args.headless)
    settings.solve_press_hold = True
    store = AccountStore(Path("/tmp/opencode/dapib_capture"))
    creator = Creator(settings, store)
    rng = random.Random()

    recorder = None
    ekey: dict = {}
    ok = False
    with BrowserSession(settings) as sess:
        ctx = sess.new_context(proxy=proxy)
        page = sess.new_page(ctx)
        recorder = Recorder(out, page)

        creator._open(page)
        identity = make_identity(rng)
        creator._fill(page, identity)
        creator._submit(page)

        gate = cap.wait_for_gate(page, timeout=args.wait)
        print(f"  gate    -> {gate}", flush=True)

        # PX hold is a coin flip; dismissing and resubmitting often lands Arkose
        for attempt in range(3):
            if gate != "press_hold":
                break
            hit = cap.find_press_hold(page)
            if hit:
                cap.press_and_hold(page, rng, hit=hit)
                page.wait_for_timeout(1500)
            if cap.dismiss_challenge(page):
                page.wait_for_timeout(rng.randint(800, 1500))
                try:
                    page.click(SEL_SUBMIT, timeout=8000)
                except Exception:
                    pass
                gate = cap.wait_for_gate(page, timeout=30.0)
                print(f"  gate    -> {gate} (retry {attempt + 1})", flush=True)

        if gate == "start_puzzle":
            print("  clicking Start Puzzle to trigger gfct", flush=True)
            page.wait_for_timeout(rng.randint(800, 1500))
            cap.wait_for_gate(page, timeout=10.0)

        # let the game mount: that is when gfct, dapib and rtig fire
        deadline = time.time() + 30.0
        while time.time() < deadline:
            page.wait_for_timeout(500)
            if recorder.dapib_url and recorder.session_token:
                page.wait_for_timeout(2500)  # let the remaining chunks land
                break

        print(f"  dapib   -> {'seen' if recorder.dapib_url else 'NOT SEEN'}", flush=True)
        if recorder.dapib_url:
            print(f"             {recorder.dapib_url[:110]}", flush=True)
            print(f"  refetch -> {recorder.refetch_dapib()}", flush=True)

        page.wait_for_timeout(2000)
        if recorder.session_token:
            print("  probing ekey/", flush=True)
            ekey = probe_ekey(recorder.session_token, recorder.game_token, out)
            for name, res in ekey.items():
                print(f"    {name:12} {res.get('status', res.get('error'))}", flush=True)
        ok = bool(recorder.session_token or recorder.dapib_url)

    if recorder:
        payload = {
            "proxy": (proxy.split("@")[-1] if proxy else "direct"),
            "dapib_url": recorder.dapib_url,
            "session_token": recorder.session_token,
            "exchanges": recorder.manifest,
            "ekey_probe": ekey,
        }
        (out / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

        scripts = sorted(p.name for p in out.glob("script-*.js"))
        images = sorted(p.name for p in out.glob("image-*.png"))
        print(f"\n  saved: {len(scripts)} script(s), {len(images)} image(s), "
              f"{(out / 'dapib-578.js').exists() and 'dapib yes' or 'dapib no'}")
        for name in scripts[:20]:
            size = (out / name).stat().st_size
            print(f"    {size:>8}  {name}")
        if (out / "dapib-578.js").exists():
            size = (out / "dapib-578.js").stat().st_size
            print(f"    {size:>8}  dapib-578.js   <-- the one we wanted")
        print(f"\n  manifest: {out / 'manifest.json'}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxiesithink.txt")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--wait", type=float, default=45.0, help="seconds to wait for a gate")
    ap.add_argument("--tag", default="")
    ap.add_argument("--tries", type=int, default=6, help="proxies to burn through")
    args = ap.parse_args()

    if args.proxy:
        candidates: list[str | None] = [args.proxy]
    else:
        candidates = []
        if args.proxy_file.exists():
            pool = load_proxies(args.proxy_file)
            random.shuffle(pool)
            candidates = pool[: args.tries]
        if not candidates:
            candidates = [None]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + (f"-{args.tag}" if args.tag else "")
    for i, proxy in enumerate(candidates, 1):
        out = OUT_ROOT / f"{stamp}-t{i}"
        out.mkdir(parents=True, exist_ok=True)
        tail = "(direct)" if not proxy else proxy.split("@")[-1]
        print(f"\n[{i}/{len(candidates)}] {tail}\n  capture -> {out}", flush=True)
        try:
            if capture_once(proxy, out, args):
                print("\n  got what we came for; stopping.", flush=True)
                return 0
        except Exception as exc:
            print(f"  attempt failed: {type(exc).__name__}: {str(exc)[:120]}", flush=True)
            continue
    print("\n  no attempt produced a session. proxies may all be dead.", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

