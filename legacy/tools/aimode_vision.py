#!/usr/bin/env python3
"""Ask Google AI Mode about an image. Upload works headless; attach does not.

Status, measured:

  upload    ./tools/lens_upload.py sends the image to Lens from plain Python
            (`/v1/gsessionid` then `/v1/crupload`) and gets HTTP 200 back.

  query     `folif` is the right endpoint for image queries — not `folwr`.
            It needs `async=_fmt:adl,_xsrf:<xsrf-folif-token>`, `lns_mode=cvst`,
            `elrc` from the shell and `mstk` from a prior answer, plus `vsrid`.
            With all of that it answers HTTP 200.

  attach    NOT solved. The reply renders "Shared 0 files" and the model says
            it got no image. Supplying `vsrid` (which is byte-identical in
            structure to the browser's, down to the f5 field), `gsessionid`,
            and `ved` does not register the file. The browser must pass the
            attachment some other way that has not been captured yet.

So this tool answers text questions reliably and will start answering *about*
images once the attach parameter is found. Do not route production through it.

The one browser-shaped dependency in the whole chain is minting cookies:
`aimode_api.py --mint-cookies` runs headed Chrome once and exports the jar.
Every other call here is plain curl_cffi.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import aimode_api as api
from tools import lens_upload as lens

# where `vsrid` and `vit=img` get attached
VARIANTS = {
    1: {"shell": ["vsrid"], "folwr": ["vsrid"]},
    2: {"shell": ["vsrid", "vit"], "folwr": ["vsrid", "vit"]},
    3: {"shell": ["vsrid"], "folwr": []},
    4: {"shell": [], "folwr": ["vsrid"]},
    5: {"shell": ["vsrid", "vit"], "folwr": []},
}


def build_shell_url(query: str, extra: dict, hl="en", gl="us") -> str:
    params = {"udm": "50", "aep": "11", "hl": hl, "gl": gl, "q": query}
    params.update(extra)
    return api.AI_MODE_URL + "?" + urlencode(params)


def build_folwr_url(tokens, query: str, extra: dict, hl="en", gl="us") -> str:
    params = {
        "srtst": tokens.srtst, "garc": tokens.garc,
        "mlro": tokens.lro_token, "mlros": tokens.lro_sig,
        "ei": tokens.ei, "q": query, "yv": "3", "aep": "11",
        "gl": gl, "hl": hl, "udm": "50", "stkp": tokens.stkp or "0", "cs": "0",
        "async": f"_fmt:adl,_xsrf:{tokens.xsrf_folwr}",
    }
    params.update(extra)
    return api.FOLWR_URL + "?" + urlencode(params)


def extras_for(variant: int, vsrid: str) -> dict:
    spec = VARIANTS[variant]
    out = {}
    if "vsrid" in spec["shell"]:
        out["vsrid"] = vsrid
    if "vit" in spec["shell"]:
        out["vit"] = "img"
    return out


FOLIF_URL = "https://www.google.com/async/folif"


def fetch_mstk(session, tokens, query: str, shell_url: str) -> str:
    """The first answer hands back an `mstk`; image queries must present it."""
    url = api.build_folwr_url(tokens, query)
    r = session.get(url, headers={
        "referer": shell_url, "accept": "text/html,*/*",
        "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
        "sec-fetch-dest": "empty",
    })
    body = r.text or ""
    m = re.search(r'data-mstk="([^"]+)"', body)
    if m:
        return m.group(1)
    # some replies carry it only in the redirect target
    m = re.search(r'mstk=([A-Za-z0-9_\-]{16,})', body)
    return m.group(1) if m else ""


def ask_with_image(image: Path, query: str, variant: int, proxy: str | None,
                   cookies: dict) -> dict:
    t0 = time.time()
    session = api.make_session(cookies, proxy)
    lens_session = lens.make_session(proxy, cookies)

    up = lens.upload(lens_session, image)
    if up["status"] != 200:
        raise RuntimeError(f"upload {up['status']}: {up['response'][:160]}")
    vsrid = up["vsrid"]
    gsessionid = up["session_id"]

    # the shell is fetched with the lens flag so the page boots in image mode
    shell_url = api.AI_MODE_URL + "?" + urlencode({
        "udm": "50", "aep": "11", "hl": "en", "gl": "us",
        "q": query, "lns_mode": "cvst",
    })
    resp = session.get(shell_url, headers={"referer": "https://www.google.com/"})
    shell = resp.text or ""
    tokens = api.parse_tokens(shell)
    if not tokens.garc:
        raise api.ShellRejected(f"no garc ({len(shell)} bytes)")

    mstk = tokens.mstk or fetch_mstk(session, tokens, query, shell_url)

    params = {
        "srtst": tokens.srtst,
        "ei": tokens.ei,
        "yv": "3",
        "aep": "11",
        "gl": "us",
        "hl": "en",
        "lns_mode": "cvst",
        "udm": "50",
        "stkp": tokens.stkp or "0",
        "cs": "0",
        "csuir": "0",
        "elrc": tokens.elrc,
        "mstk": mstk,
        "csui": "3",
        "q": query,
        "vsrid": vsrid,
        "gsessionid": gsessionid,
    }
    folif_url = FOLIF_URL + "?" + urlencode(params)
    r2 = session.get(folif_url, headers={
        "referer": shell_url, "accept": "text/html,*/*",
        "sec-fetch-mode": "cors", "sec-fetch-site": "same-origin",
        "sec-fetch-dest": "empty",
        "x-requested-with": "XMLHttpRequest",
    })
    raw = r2.text or ""
    text = api.html_to_text(raw)

    return {
        "query": query,
        "vsrid": vsrid,
        "gsessionid": gsessionid[:24],
        "mstk": bool(mstk),
        "elrc": bool(tokens.elrc),
        "upload_status": up["status"],
        "upload_ms": int(up["elapsed"] * 1000),
        "shell_bytes": len(shell),
        "folif_status": r2.status_code,
        "folif_bytes": len(raw),
        "elapsed_s": round(time.time() - t0, 2),
        "text": text,
        "raw_head": raw[:400],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, required=True)
    ap.add_argument("--query", default="How many dice are shown in this image?")
    ap.add_argument("--variant", type=int, default=2, choices=sorted(VARIANTS),
                    help="legacy: which request carries vsrid (see module docstring)")
    ap.add_argument("--all-variants", action="store_true")
    ap.add_argument("--cookies", type=Path, default=api.DEFAULT_COOKIE_PATH)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not args.image.exists():
        print(f"no such image: {args.image}", file=sys.stderr)
        return 2

    cookies = api.load_cookies(args.cookies)
    if not cookies:
        print(f"no cookie jar at {args.cookies} — run aimode_api.py --mint-cookies",
              file=sys.stderr)
        return 2

    proxy = args.proxy
    if not proxy and args.proxy_file:
        proxy = api_proxy(args.proxy_file)

    variants = sorted(VARIANTS) if args.all_variants else [args.variant]
    for v in variants:
        try:
            out = ask_with_image(args.image, args.query, v, proxy, cookies)
        except Exception as exc:
            print(f"variant {v}: FAILED {type(exc).__name__}: {str(exc)[:150]}")
            continue
        if args.json:
            print(json.dumps(out, indent=1))
        else:
            print(f"\n=== variant {v}  [{out['elapsed_s']}s  upload={out['upload_ms']}ms  "
                  f"shell={out['shell_bytes']}B  folif={out['folif_status']} "
                  f"{out['folif_bytes']}B]")
            print(f"vsrid: {out['vsrid'][:60]}  mstk={out['mstk']} elrc={out['elrc']}")
            print(out["text"][:800] or "(empty)")
        time.sleep(1.2)
    return 0


def api_proxy(path: Path) -> str | None:
    from curl_cffi import requests as creq

    lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    for line in lines[:6]:
        p = api.normalise_proxy(line)
        try:
            s = creq.Session(impersonate="chrome", timeout=10,
                             proxies={"http": p, "https": p})
            if s.get("https://www.google.com/generate_204").status_code == 204:
                s.close()
                return p
            s.close()
        except Exception:
            continue
    return None


if __name__ == "__main__":
    sys.exit(main())
