#!/usr/bin/env python3
"""Record what Google AI Mode sends, then try to replay it without a browser.

Two phases, run together by default:

  capture  drive a logged-out Chromium at the AI Mode URL and record every
           request: method, url, headers, body, plus the response status and
           a slice of the body for anything text-shaped.
  replay   take that recording and re-issue it with curl_cffi (Chrome TLS
           fingerprint, no browser). If the replayed responses match, AI Mode
           works from plain Python and the browser is only needed for Roblox.

  ./tools/aimode_capture.py --query "what is 2+2"
  ./tools/aimode_capture.py --query "..." --no-replay
  ./tools/aimode_capture.py --replay-only debug/aimode/capture/<stamp>

Everything lands in debug/aimode/capture/<stamp>/.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AI_MODE = "https://www.google.com/search?udm=50&aep=11&hl=en&gl=us&q={q}"

TEXTISH = ("application/json", "text/html", "text/plain", "text/xml",
           "application/xhtml", "text/javascript")

# requests worth replaying: the doc shells and the async answer endpoints
REPLAY_TYPES = ("document", "xhr", "fetch")


def _textish(content_type: str) -> bool:
    return any(t in (content_type or "").lower() for t in TEXTISH)


def _launch_real_chrome(port: int, profile: Path, headed: bool, proxy_url: str | None):
    """Start the system Chrome with a debugging port, no automation flags.

    Playwright's bundled Chromium ships --enable-automation and a headless
    signature; Google's abuse layer reads both before the page even loads.
    A hand-launched real Chrome keeps the normal fingerprint, and CDP attaches
    to it without adding the automation switch.
    """
    import subprocess
    import urllib.request

    profile.mkdir(parents=True, exist_ok=True)
    exe = None
    for cand in ("/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if Path(cand).exists():
            exe = cand
            break
    if not exe:
        raise RuntimeError("no system Chrome found")

    args = [
        exe, f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-blink-features=AutomationControlled",
        "--window-size=1366,900",
        "about:blank",
    ]
    if not headed:
        args.insert(1, "--headless=new")
    if proxy_url:
        args.insert(1, f"--proxy-server={proxy_url}")

    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 25
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2).read()
            return proc
        except Exception:
            time.sleep(0.4)
    proc.terminate()
    raise RuntimeError("chrome debugging port never came up")


def _connect_with_retry(pw, port: int):
    last = None
    for _ in range(20):
        try:
            return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as exc:
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"cdp connect failed: {last}")


def capture(query: str, outdir: Path, proxy: str | None, headed: bool,
            wait: float, no_warmup: bool, real_chrome: bool = False,
            port: int = 9333) -> dict:
    from playwright.sync_api import sync_playwright

    outdir.mkdir(parents=True, exist_ok=True)
    requests: list[dict] = []
    responses: list[dict] = []
    t0 = time.time()
    proxy_url = proxy
    bridge = None
    if proxy_url and proxy_url.startswith("socks5:") and not proxy_url.startswith("socks5://"):
        proxy_url = "socks5://" + proxy_url[len("socks5:"):]
    if proxy_url and proxy_url.startswith("socks5://"):
        from roblox_creator.socks_bridge import SocksBridge
        bridge = SocksBridge(proxy_url)
        proxy_url = bridge.start()
        print(f"  socks5 -> {proxy_url}")

    proc = None
    with sync_playwright() as pw:
        if real_chrome:
            proc = _launch_real_chrome(port, outdir / "profile", headed, proxy_url)
            browser = _connect_with_retry(pw, port)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            print(f"  attached to real Chrome over CDP :{port}")
        else:
            browser = pw.chromium.launch(headless=not headed)
            kw: dict = {"viewport": {"width": 1366, "height": 900}, "locale": "en-US"}
            if proxy_url:
                kw["proxy"] = {"server": proxy_url}
            ctx = browser.new_context(**kw)
            page = ctx.new_page()
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")

        def on_request(req):
            try:
                hdrs = dict(req.headers)
                hdrs.pop("cookie", None)
                entry = {
                    "i": len(requests), "t": round(time.time() - t0, 2),
                    "method": req.method, "url": req.url,
                    "type": req.resource_type, "headers": hdrs,
                }
                try:
                    buf = req.post_data_buffer
                    if buf:
                        entry["body_len"] = len(buf)
                        if len(buf) <= 65536:
                            entry["body"] = buf.decode("utf-8", "replace")
                            entry["body_encoding"] = "text"
                        else:
                            entry["body_note"] = "too large"
                except Exception:
                    pass
                requests.append(entry)
            except Exception:
                pass

        def on_response(resp):
            try:
                ct = resp.headers.get("content-type", "")
                req = resp.request
                if req.resource_type not in REPLAY_TYPES and not _textish(ct):
                    return
                if not _textish(ct):
                    return
                body = resp.text()
                responses.append({
                    "i": len(responses), "url": resp.url, "status": resp.status,
                    "content_type": ct.split(";")[0], "len": len(body),
                    "prefix": body[:400],
                })
                if len(responses) <= 40:
                    name = re.sub(r"[^A-Za-z0-9]+", "_", urlparse(resp.url).path)[-60:]
                    (outdir / f"resp_{len(responses):02d}{name}.txt").write_text(body[:400000])
            except Exception:
                pass

        page.on("request", on_request)
        page.on("response", on_response)

        if not no_warmup:
            try:
                print("  warm-up google.com")
                page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)
                page.mouse.move(500, 320)
                page.mouse.wheel(0, 200)
                page.wait_for_timeout(800)
            except Exception as exc:
                print(f"  warm-up issue: {exc}")

        url = AI_MODE.format(q=query)
        print(f"  -> {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"  navigation issue: {exc}")
        page.wait_for_timeout(int(wait * 1000))

        title = page.title()
        content = page.content() or ""
        blocked = [m for m in ("unusual traffic", "our systems have detected", "recaptcha", "before you continue")
                   if m in content.lower()[:20000]]
        print(f"  title: {title[:80]!r}  blocked={blocked or 'no'}")
        page.screenshot(path=str(outdir / "page.png"))

        cookies = [c for c in ctx.cookies()]
        browser.close()
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass

    data = {
        "ts": time.strftime("%Y%m%d-%H%M%S"),
        "query": query, "title": title, "blocked": blocked,
        "requests": requests, "responses": responses, "cookies": cookies,
    }
    (outdir / "capture.json").write_text(json.dumps(data, indent=1))
    (outdir / "cookies.json").write_text(
        json.dumps({c["name"]: c["value"] for c in cookies}, indent=1))

    print(f"\n  {len(requests)} requests, {len(responses)} text responses captured")
    big = sorted(responses, key=lambda r: -r["len"])[:6]
    for r in big:
        print(f"    {r['status']} {r['len']:>7}B  {r['content_type']:<18} {r['url'][:90]}")
    return data


def replay(outdir: Path, proxy: str | None, verbose: bool) -> None:
    from curl_cffi import requests as creq

    if proxy and proxy.startswith("socks5://"):
        proxy = "socks5h://" + proxy[len("socks5://"):]

    data = json.loads((outdir / "capture.json").read_text())
    cookies = {c["name"]: c["value"] for c in data.get("cookies", [])}
    s = creq.Session(impersonate="chrome", timeout=45)
    for k, v in cookies.items():
        try:
            s.cookies.set(k, v, domain=".google.com")
        except Exception:
            pass

    print(f"\nreplaying {len(data['requests'])} requests with curl_cffi (impersonate=chrome)")
    print(f"{'#':>3} {'orig':>6} {'replay':>7} {'len':>8}  url")
    print("-" * 96)

    matched = 0
    for r in data["requests"]:
        if r["type"] not in REPLAY_TYPES:
            continue
        expected = next((x for x in data["responses"] if x["url"] == r["url"]), None)
        headers = {k: v for k, v in (r.get("headers") or {}).items()
                   if k.lower() not in ("cookie", "host", "content-length")}
        kw: dict = {"headers": headers, "allow_redirects": True}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        if r.get("body") and r.get("body_encoding") == "text":
            kw["data"] = r["body"].encode()
        try:
            resp = s.request(r["method"], r["url"], **kw)
            same = expected and resp.status_code == expected["status"]
            close = expected and abs(len(resp.text) - expected["len"]) < max(400, expected["len"] * 0.25)
            ok = bool(same and close)
            matched += ok
            flag = "MATCH" if ok else ("status" if same else "DIFF ")
            print(f"{r['i']:>3} {expected['status'] if expected else '-':>6} "
                  f"{resp.status_code:>7} {len(resp.text):>8}  {flag} {r['url'][:60]}")
            if verbose and not ok:
                print(f"      orig: {(expected or {}).get('prefix', '')[:160]}")
                print(f"      mine: {resp.text[:160]}")
        except Exception as exc:
            print(f"{r['i']:>3} {'-':>6} {'ERR':>7} {0:>8}  {r['url'][:60]}")
            if verbose:
                print(f"      {exc}")

    print(f"\nmatched {matched} request(s) byte-comparable.")
    if matched:
        print("replay works — AI Mode is reachable from plain Python with curl_cffi.")
    else:
        print("replay did not match. either the tokens are session-bound and one-shot,")
        print("or the fingerprint/cookie state differs enough that Google re-renders.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default="what is 2+2")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--wait", type=float, default=9.0)
    ap.add_argument("--no-warmup", action="store_true")
    ap.add_argument("--no-replay", action="store_true")
    ap.add_argument("--replay-only", type=Path, default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "debug/aimode/capture")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--real-chrome", action="store_true",
                    help="launch /usr/bin/google-chrome and attach over CDP (best fingerprint)")
    ap.add_argument("--port", type=int, default=9333)
    args = ap.parse_args()

    if args.replay_only:
        replay(args.replay_only, args.proxy, args.verbose)
        return 0

    stamp = time.strftime("%Y%m%d-%H%M%S")
    outdir = args.out / stamp
    data = capture(args.query, outdir, args.proxy, args.headed, args.wait,
                   args.no_warmup, args.real_chrome, args.port)
    (ROOT / "debug/aimode/last").write_text(str(outdir))
    if not args.no_replay and data["requests"]:
        replay(outdir, args.proxy, args.verbose)
    print(f"\ncapture -> {outdir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
