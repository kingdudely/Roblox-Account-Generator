#!/usr/bin/env python3
"""Talk to Google AI Mode over plain HTTP — no Chrome, no Selenium.

AI Mode answers are served as an HTML fragment from /async/folif, reachable with
a GET once you have the bootstrap tokens from the /search shell page. The only
thing a browser gives you that curl does not is a real TLS/HTTP2 fingerprint, and
curl_cffi impersonates that directly.

  ./tools/aimode_http.py                    # direct (WARP)
  ./tools/aimode_http.py --proxy socks5://user:pass@host:port
  ./tools/aimode_http.py --cookies cookies.json

Prints a verdict per stage so you can see exactly where it stops: shell page,
tokens, or answer fragment.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36")

BLOCK_MARKERS = (
    "unusual traffic",
    "sorry/index",
    "our systems have detected",
    "recaptcha",
    "before you continue",
    "enablejs",
    "g-recaptcha",
)

TOKEN_PATTERNS = {
    "xsrf_folif": r'data-xsrf-folif-token="([^"]+)"',
    "xsrf_folwr": r'data-xsrf-folwr-token="([^"]+)"',
    "stkp": r'data-stkp="([^"]+)"',
    "elrc": r'data-elrc="([^"]+)"',
    "ei": r'data-ei="([^"]+)"',
    "mstk": r'data-mstk="([^"]+)"',
}


def make_session(proxy: str | None, cookies: dict | None):
    from curl_cffi import requests as creq

    kw = {"impersonate": "chrome", "timeout": 45, "allow_redirects": True}
    if proxy:
        # socks5h, not socks5: the pool needs remote DNS. With local resolution
        # libcurl sends a bare IP and the upstream refuses the connect.
        if proxy.startswith("socks5://"):
            proxy = "socks5h://" + proxy[len("socks5://"):]
        kw["proxies"] = {"http": proxy, "https": proxy}
    s = creq.Session(**kw)
    s.headers.update({
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="146", "Not:A-Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "upgrade-insecure-requests": "1",
    })
    if cookies:
        for k, v in cookies.items():
            s.cookies.set(k, v, domain=".google.com")
    return s


def verdict(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'OK ' if ok else 'BLOCKED'}] {name}{('  ' + detail) if detail else ''}")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--cookies", type=Path, default=None,
                    help="JSON {name: value} of Google cookies")
    ap.add_argument("--query", default="what is 2+2")
    ap.add_argument("--aep", default="11")
    ap.add_argument("--out", type=Path, default=ROOT / "debug/aimode/http")
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    cookies = json.loads(args.cookies.read_text()) if args.cookies else None
    s = make_session(args.proxy, cookies)

    print(f"proxy: {'direct' if not args.proxy else 'via ' + args.proxy.split('@')[-1]}")
    print(f"cookies: {len(cookies) if cookies else 0}\n")

    # who are we
    try:
        ip = s.get("https://ipinfo.io/json", timeout=25).json()
        print(f"exit ip: {ip.get('ip')}  {ip.get('city')}, {ip.get('country')}  {ip.get('org', '')[:40]}")
    except Exception as exc:
        print(f"exit ip: unknown ({exc})")

    # stage 1: warm up on the homepage so we arrive with cookies
    print("\nstage 1 — homepage warm-up")
    ok_home = False
    try:
        r = s.get("https://www.google.com/", timeout=40)
        body = r.text or ""
        blocked = any(m in body.lower() for m in BLOCK_MARKERS)
        ok_home = r.status_code == 200 and not blocked
        verdict("google.com", ok_home, f"status={r.status_code} len={len(body)}")
        (args.out / "home.html").write_text(body)
    except Exception as exc:
        verdict("google.com", False, str(exc))

    time.sleep(1.5)

    # stage 2: the AI Mode shell page
    print("\nstage 2 — AI Mode shell (/search?udm=50)")
    url = f"https://www.google.com/search?udm=50&aep={args.aep}&hl=en&gl=us&q={args.query}"
    shell = ""
    try:
        r = s.get(url, timeout=45, headers={"referer": "https://www.google.com/"})
        shell = r.text or ""
        low = shell.lower()
        hit = [m for m in BLOCK_MARKERS if m in low]
        ok = r.status_code == 200 and not hit and len(shell) > 20000
        verdict("/search?udm=50", ok,
                f"status={r.status_code} len={len(shell)}" + (f" markers={hit}" if hit else ""))
        (args.out / "shell.html").write_text(shell)
    except Exception as exc:
        verdict("/search?udm=50", False, str(exc))

    tokens = {}
    for name, pat in TOKEN_PATTERNS.items():
        m = re.search(pat, shell)
        if m:
            tokens[name] = m.group(1)
    print(f"  tokens found: {', '.join(tokens) or 'none'}")

    if not shell or not tokens.get("xsrf_folif"):
        print("\nstopped: no folif xsrf token, so the conversation endpoint is unreachable.")
        print("this is what a login cookie is for — AI Mode will not mint tokens without a session.")
        return 1

    # stage 3: the answer fragment
    print("\nstage 3 — answer fragment (/async/folif)")
    from urllib.parse import urlencode
    params = {
        "ei": tokens.get("ei", ""), "yv": "3", "udm": "50", "hl": "en",
        "stkp": tokens.get("stkp", ""), "cs": "0", "csuir": "0",
        "elrc": tokens.get("elrc", ""), "q": args.query,
        "async": f"_fmt:adl,_xsrf:{tokens['xsrf_folif']}",
    }
    try:
        r = s.get("https://www.google.com/async/folif?" + urlencode(params),
                  timeout=60, headers={"referer": url})
        frag = r.text or ""
        (args.out / "folif.html").write_text(frag)
        text = re.sub(r"<[^>]+>", " ", frag)
        text = re.sub(r"\s+", " ", text).strip()
        ok = r.status_code == 200 and len(text) > 40
        verdict("/async/folif", ok, f"status={r.status_code} text={len(text)}")
        if ok:
            print(f"\n  answer text: {text[:300]}")
            return 0
        print(f"\n  fragment head: {frag[:200]}")
        return 1
    except Exception as exc:
        verdict("/async/folif", False, str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
