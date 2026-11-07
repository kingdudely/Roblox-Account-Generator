#!/usr/bin/env python3
"""Ask Google AI Mode a question from plain Python. No browser per request.

The chain, verified against a live capture:

    GET /search?udm=50&aep=11&q=...        -> HTML shell, carries one div with
                                              every token the answer endpoint needs
    GET /async/folwr?...tokens...          -> HTML fragment containing the answer

The catch is that the shell only renders for a client Google already trusts. A
cold curl gets a 94 KB `/httpservice/retry/enablejs` stub with no tokens. The
same request with four cookies from a logged-out Chrome session (`AEC`, `NID`,
`__Secure-STRP`, `SEARCH_SAMESITE`) gets the full 439 KB shell and fresh tokens
for whatever query you pass. So: one headed Chrome run to mint cookies, then
curl does the rest until the cookies expire.

`/async/folwr` is one-shot. Replaying a URL returns
"Something went wrong and an AI response wasn't generated." Every question needs
a fresh shell fetch, which is fine — the shell is cheap.

MEASURED LIMITS (do not plan capacity past these)

  one minted jar, its own exit IP      ~20 queries, ~1.0-1.5s each
  the same jar, a different exit IP     fails at once (enablejs stub, 94 KB)
  rotating IPs under one jar            dies after ~4 queries

So the jar is bound to the IP that minted it, and a headed Chrome run buys about
twenty questions. `--rotate` exists and works, but pairing it with a single jar
is worse than not rotating: each exit IP needs its own mint. For anything past
tens of queries per minute this is the wrong tool — put the volume on a local
model and keep this for cheap one-off reasoning and labelling.

  ./tools/aimode_api.py --query "what is 2+2"
  ./tools/aimode_api.py --query "..." --cookies debug/aimode/cookies.json
  ./tools/aimode_api.py --query "..." --proxy socks5://user:pass@host:port
  ./tools/aimode_api.py --mint-cookies --mint-proxy-file proxiesithink.txt
  ./tools/aimode_api.py --query "..." --rotate --proxy-file proxiesithink.txt
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AI_MODE_URL = "https://www.google.com/search"
FOLWR_URL = "https://www.google.com/async/folwr"

DEFAULT_COOKIE_PATH = ROOT / "debug/aimode/cookies.json"

# Cookies that actually matter. Google sets a dozen; these four are the ones
# whose absence produced the enablejs stub in testing.
KEY_COOKIES = ("AEC", "NID", "__Secure-STRP", "SEARCH_SAMESITE")

BLOCK_MARKERS = (
    "our systems have detected",
    "unusual traffic",
    "sorry/index",
    "before you continue",
)

# the token div: <div data-ei=... data-garc=... data-lro-token=... ...>
TOKEN_ATTRS = {
    "ei": "data-ei",
    "garc": "data-garc",
    "lro_token": "data-lro-token",
    "lro_sig": "data-lro-signature",
    "srtst": "data-srtst",
    "xsrf_folif": "data-xsrf-folif-token",
    "xsrf_folwr": "data-xsrf-folwr-token",
    "stkp": "data-stkp",
    "elrc": "data-elrc",
    "mstk": "data-mstk",
}


class ShellRejected(RuntimeError):
    """Google served the JS bootstrap instead of the AI Mode shell."""


class Rotator:
    """Cycle sticky proxies, and retire one the moment Google says 429.

    The pool is sticky: each line is a fixed exit IP, so hammering one line is
    what produces 429. Rotating spreads the load across IPs. A 429 or a
    JS-stub shell puts that line on cooldown; the next call takes the next one.
    """

    def __init__(self, path: Path, cooldown_s: float = 180.0, probe_timeout: float = 10.0):
        self.path = path
        self.cooldown_s = cooldown_s
        self.probe_timeout = probe_timeout
        self.lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
        self._until: dict[int, float] = {}
        self._cursor = 0
        self._order = list(range(len(self.lines)))

    def __len__(self) -> int:
        return len(self.lines)

    def _available(self, i: int) -> bool:
        return self._until.get(i, 0.0) <= time.time()

    def penalise(self, proxy: str | None, seconds: float | None = None) -> None:
        if not proxy:
            return
        for i, line in enumerate(self.lines):
            if normalise_proxy(line) == proxy:
                self._until[i] = time.time() + (seconds or self.cooldown_s)
                return

    def next(self) -> str | None:
        """Next line whose cooldown has expired, probing past dead ones."""
        from curl_cffi import requests as creq

        for _ in range(len(self._order)):
            if not self._order:
                return None
            i = self._order[self._cursor % len(self._order)]
            self._cursor += 1
            if not self._available(i):
                continue
            p = normalise_proxy(self.lines[i])
            try:
                s = creq.Session(impersonate="chrome", timeout=self.probe_timeout,
                                 proxies={"http": p, "https": p})
                ok = s.get("https://www.google.com/generate_204").status_code == 204
                s.close()
            except Exception:
                ok = False
            if ok:
                return p
            self._until[i] = time.time() + self.cooldown_s
        return None

    def healthy(self) -> int:
        return sum(1 for i in range(len(self.lines)) if self._available(i))


@dataclass
class Tokens:
    ei: str = ""
    garc: str = ""
    lro_token: str = ""
    lro_sig: str = ""
    srtst: str = ""
    xsrf_folif: str = ""
    xsrf_folwr: str = ""
    stkp: str = "0"
    elrc: str = ""
    mstk: str = ""

    @property
    def complete(self) -> bool:
        return bool(self.garc and self.srtst and self.lro_token and self.xsrf_folwr)

    def missing(self) -> list[str]:
        return [n for n in ("garc", "srtst", "lro_token", "lro_sig", "ei", "xsrf_folwr")
                if not getattr(self, n)]


@dataclass
class Answer:
    text: str
    tokens: Tokens
    shell_bytes: int
    folwr_bytes: int
    elapsed: float
    images: list[str] = field(default_factory=list)


def normalise_proxy(proxy: str | None) -> str | None:
    """socks5:// fails against google.com — the pool needs remote DNS.

    Also strips whitespace: the proxy list is CRLF, and a trailing \\r turns
    into "port 1002\\r" which libcurl rejects as non-decimal.
    """
    if not proxy:
        return None
    proxy = proxy.strip()
    if proxy.startswith("socks5://"):
        return "socks5h://" + proxy[len("socks5://"):]
    return proxy


def make_session(cookies: dict[str, str] | None = None, proxy: str | None = None):
    from curl_cffi import requests as creq

    kw: dict = {"impersonate": "chrome", "timeout": 45, "allow_redirects": True}
    proxy = normalise_proxy(proxy)
    if proxy:
        kw["proxies"] = {"http": proxy, "https": proxy}
    s = creq.Session(**kw)
    s.headers.update({
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                  "image/avif,image/webp,image/apng,*/*;q=0.8",
        "accept-language": "en-US,en;q=0.9",
        "sec-fetch-mode": "navigate",
        "sec-fetch-dest": "document",
        "sec-fetch-site": "none",
        "sec-fetch-user": "?1",
        "upgrade-insecure-requests": "1",
    })
    for name, value in (cookies or {}).items():
        try:
            s.cookies.set(name, value, domain=".google.com")
        except Exception:
            pass
    return s


def load_cookies(path: Path | None) -> dict[str, str]:
    if not path or not path.exists():
        return {}
    raw = json.loads(path.read_text())
    if isinstance(raw, list):  # capture.json style
        return {c["name"]: c["value"] for c in raw if "name" in c}
    return {str(k): str(v) for k, v in raw.items()}


def parse_tokens(shell: str) -> Tokens:
    t = Tokens()
    for name, attr in TOKEN_ATTRS.items():
        m = re.search(rf'{attr}="([^"]+)"', shell)
        if m:
            setattr(t, name, m.group(1))
    if not t.stkp:
        m = re.search(r'data-stkp="([^"]+)"', shell)
        t.stkp = m.group(1) if m else "0"
    return t


def fetch_shell(session, query: str, hl: str = "en", gl: str = "us") -> tuple[str, Tokens]:
    params = {"udm": "50", "aep": "11", "hl": hl, "gl": "us" if gl == "us" else gl, "q": query}
    resp = session.get(AI_MODE_URL, params=params,
                       headers={"referer": "https://www.google.com/"})
    body = resp.text or ""
    if resp.status_code == 429:
        raise ShellRejected("rate limited (429) — rotate the exit IP")
    tokens = parse_tokens(body)

    if not tokens.garc:
        low = body.lower()
        hit = next((m for m in BLOCK_MARKERS if m in low), None)
        if hit:
            raise ShellRejected(f"blocked page ({hit}), {len(body)} bytes")
        if "enablejs" in low:
            raise ShellRejected(
                f"enablejs bootstrap, {len(body)} bytes — no browser cookies. "
                "run --mint-cookies with headed Chrome, or pass --cookies")
        raise ShellRejected(f"no garc token in {len(body)} bytes")

    if resp.status_code != 200:
        raise ShellRejected(f"shell status {resp.status_code}")
    return body, tokens


def build_folwr_url(tokens: Tokens, query: str, hl: str = "en", gl: str = "us") -> str:
    params = {
        "srtst": tokens.srtst,
        "garc": tokens.garc,
        "mlro": tokens.lro_token,
        "mlros": tokens.lro_sig,
        "ei": tokens.ei,
        "q": query,
        "yv": "3",
        "aep": "11",
        "gl": gl,
        "hl": hl,
        "udm": "50",
        "stkp": tokens.stkp or "0",
        "cs": "0",
        "async": f"_fmt:adl,_xsrf:{tokens.xsrf_folwr}",
    }
    return FOLWR_URL + "?" + urlencode(params)


def html_to_text(body: str) -> str:
    t = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.S | re.I)
    t = re.sub(r"<script[^>]*>.*?</script>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"</(p|div|li|h[1-6])>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = t.replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&")
    t = t.replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n\n", t)
    return t.strip()


def extract_images(body: str) -> list[str]:
    urls = re.findall(r'https://encrypted-tbn\d*\.gstatic\.com/images\?[^"\'\\\s]+', body)
    seen, out = set(), []
    for u in urls:
        u = u.replace("\\u003d", "=").replace("\\u0026", "&")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def ask(session, query: str, hl: str = "en", gl: str = "us") -> Answer:
    t0 = time.time()
    shell, tokens = fetch_shell(session, query, hl, gl)
    url = build_folwr_url(tokens, query, hl, gl)
    resp = session.get(url, headers={
        "referer": f"{AI_MODE_URL}?udm=50&aep=11&q={query}",
        "accept": "text/html,*/*",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "sec-fetch-dest": "empty",
        "x-requested-with": "XMLHttpRequest",
    })
    body = resp.text or ""
    return Answer(
        text=html_to_text(body),
        tokens=tokens,
        shell_bytes=len(shell),
        folwr_bytes=len(body),
        elapsed=time.time() - t0,
        images=extract_images(body),
    )


def mint_cookies(out: Path, proxy: str | None, headed: bool, port: int = 9344) -> dict[str, str]:
    """One headed Chrome run purely to harvest a trusted cookie jar."""
    from playwright.sync_api import sync_playwright
    from tools.aimode_capture import _connect_with_retry, _launch_real_chrome

    proxy_url = normalise_proxy(proxy)
    bridge = None
    if proxy_url and proxy_url.startswith("socks5"):
        # Chrome rejects socks5h and cannot send RFC 1929 credentials
        from roblox_creator.socks_bridge import SocksBridge
        bridge = SocksBridge(proxy_url)
        proxy_url = bridge.start()
        print(f"  socks5 -> {proxy_url}")

    profile = out.parent / "chrome-profile"
    proc = None
    with sync_playwright() as pw:
        proc = _launch_real_chrome(port, profile, headed, proxy_url)
        browser = _connect_with_retry(pw, port)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2200)
        page.mouse.move(480, 320)
        page.mouse.wheel(0, 180)
        page.goto("https://www.google.com/search?udm=50&aep=11&hl=en&gl=us&q=hello",
                  wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3500)
        jar = {c["name"]: c["value"] for c in ctx.cookies()
               if "google.com" in c.get("domain", "")}
        browser.close()
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass

    keep = {k: v for k, v in jar.items() if k in KEY_COOKIES}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(jar, indent=1))
    print(f"  {len(jar)} cookies -> {out}")
    print(f"  key cookies present: {sorted(keep)}")
    return jar


def ask_rotating(rotator: Rotator, query: str, cookies: dict[str, str],
                 attempts: int = 4, hl: str = "en", gl: str = "us") -> tuple[Answer, str]:
    """ask(), but on 429 or a JS-stub shell it retires the exit IP and retries."""
    last: Exception | None = None
    for n in range(attempts):
        proxy = rotator.next()
        if not proxy:
            raise ShellRejected(
                f"no healthy proxy left ({rotator.healthy()}/{len(rotator)} unpenalised)")
        try:
            ans = ask(make_session(cookies, proxy), query, hl, gl)
            return ans, proxy
        except ShellRejected as exc:
            print(f"  attempt {n + 1} on {proxy.split('@')[-1]}: {exc}", file=sys.stderr)
            rotator.penalise(proxy)
            last = exc
    raise last or ShellRejected("all attempts failed")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--query", default=None)
    ap.add_argument("--cookies", type=Path, default=DEFAULT_COOKIE_PATH)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=None,
                    help="proxy list for --rotate")
    ap.add_argument("--rotate", action="store_true",
                    help="cycle exit IPs, retiring any that answer 429")
    ap.add_argument("--rotate-attempts", type=int, default=4)
    ap.add_argument("--mint-cookies", action="store_true",
                    help="launch headed Chrome once and export a trusted cookie jar")
    ap.add_argument("--mint-proxy-file", type=Path, default=None)
    ap.add_argument("--headed", action="store_true", default=True)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ask-image", type=Path, default=None)
    ap.add_argument("--no-json", dest="json", action="store_false")
    args = ap.parse_args()

    if args.mint_cookies:
        mint_cookies(args.cookies, args.proxy, args.headed)
        if not args.query:
            return 0

    if not args.query:
        ap.error("--query is required unless --mint-cookies is used alone")

    cookies = load_cookies(args.cookies)
    if not cookies:
        print(f"no cookie jar at {args.cookies} — the shell will be rejected.", file=sys.stderr)
        print("run: ./tools/aimode_api.py --mint-cookies", file=sys.stderr)
        return 2

    session = make_session(cookies, args.proxy)
    used = None
    try:
        if args.rotate:
            if not args.proxy_file:
                ap.error("--rotate needs --proxy-file")
            rotator = Rotator(args.proxy_file)
            print(f"rotator: {len(rotator)} lines", file=sys.stderr)
            ans, used = ask_rotating(rotator, args.query, cookies, args.rotate_attempts)
        else:
            ans = ask(session, args.query)
    except ShellRejected as exc:
        print(f"shell rejected: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:
        print(f"request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    if args.json:
        print(json.dumps({
            "query": args.query,
            "text": ans.text,
            "images": ans.images,
            "elapsed_s": round(ans.elapsed, 2),
            "shell_bytes": ans.shell_bytes,
            "folwr_bytes": ans.folwr_bytes,
            "exit": used.split("@")[-1] if used else None,
        }, indent=1))
    else:
        print(f"[{ans.elapsed:.2f}s  shell={ans.shell_bytes}B  folwr={ans.folwr_bytes}B]")
        print(ans.text)
        if ans.images:
            print(f"\nimages: {len(ans.images)}")
            for u in ans.images[:5]:
                print(f"  {u[:110]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
