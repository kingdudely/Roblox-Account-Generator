#!/usr/bin/env python3
"""Find out how Google AI Mode uploads an image.

All the public AI Mode scrapers are text-only: they build a URL with q=<text>
and read the HTML back. Nobody has published the image-upload path, so this
probe watches a real browser and records what the page actually sends.

  ./tools/aimode_probe.py --image debug/vision/<round>/cand1.img
  ./tools/aimode_probe.py --image k.png --headed --proxy http://user:pass@host:port

Output goes to debug/aimode/:
  page.png        what the page looked like
  elements.json   every file input, button and aria-label worth clicking
  network.json    every request, with the upload POSTs flagged
  requests.http   copy-pasteable curl for the interesting ones

What we are looking for: a POST to lens.google.com or an /upload /_/Upload
endpoint, plus the token it returns. That token is what an AI Mode query would
then need to reference the image.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AI_MODE = "https://www.google.com/search?udm=50&aep=11&hl=en&gl=us"

UPLOAD_HINTS = ("upload", "lens", "image", "multipart", "blob", "media", "attach", "scotty")

JS_ELEMENTS = r"""
() => {
  const out = { fileInputs: [], labelled: [], cdn: [] };
  document.querySelectorAll('input[type=file]').forEach((el, i) => {
    out.fileInputs.push({
      i, accept: el.accept || '', name: el.name || '', id: el.id || '',
      hidden: !!(el.offsetParent === null), cls: (el.className || '').slice(0, 80)
    });
  });
  document.querySelectorAll('[aria-label]').forEach((el) => {
    const a = el.getAttribute('aria-label') || '';
    if (/image|photo|upload|camera|lens|file|attach|picture/i.test(a)) {
      out.labelled.push({ tag: el.tagName, aria: a.slice(0, 90),
                          role: el.getAttribute('role') || '',
                          js: el.getAttribute('jsaction') ? el.getAttribute('jsaction').slice(0, 70) : '' });
    }
  });
  document.querySelectorAll('[data-xid], [data-ved]').forEach((el) => {
    if (out.cdn.length < 60) out.cdn.push({ tag: el.tagName, xid: el.getAttribute('data-xid') || '' });
  });
  return out;
}
"""


def interesting(url: str, method: str) -> bool:
    low = url.lower()
    if method != "GET":
        return True
    return any(h in low for h in ("upload", "lens", "/_/", "batchexecute", "scotty", "async/"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, default=None, help="image to try uploading")
    ap.add_argument("--proxy", default=None, help="http://user:pass@host:port (Chromium cannot do authed socks5)")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--wait", type=float, default=6.0, help="seconds to let the page settle")
    ap.add_argument("--out", type=Path, default=ROOT / "debug/aimode")
    ap.add_argument("--keep-open", type=float, default=0.0, help="extra seconds before closing, for manual clicking")
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip the google.com homepage visit (direct /search is what triggers /sorry)")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright

    args.out.mkdir(parents=True, exist_ok=True)
    log: list[dict] = []
    t0 = time.time()

    def on_request(req):
        try:
            method = req.method
            url = req.url
            if not interesting(url, method):
                return
            headers = req.headers
            body = None
            try:
                buf = req.post_data_buffer
                if buf:
                    body = len(buf)
            except Exception:
                pass
            log.append({
                "t": round(time.time() - t0, 2),
                "method": method,
                "url": url[:400],
                "type": req.resource_type,
                "content_type": headers.get("content-type", "")[:120],
                "body_bytes": body,
            })
        except Exception:
            pass

    proxy_url = args.proxy
    if proxy_url and proxy_url.startswith("socks5:") and not proxy_url.startswith("socks5://"):
        proxy_url = "socks5://" + proxy_url[len("socks5:"):]
    if proxy_url and proxy_url.startswith("socks5://"):
        from roblox_creator.socks_bridge import SocksBridge
        bridge = SocksBridge(proxy_url)
        local = bridge.start()
        print(f"  socks5 -> local bridge at {local}")
        proxy_url = local

    with sync_playwright() as pw:
        launch = {"headless": not args.headed}
        browser = pw.chromium.launch(**launch)
        ctx_kwargs: dict = {
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
            "user_agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"),
        }
        if proxy_url:
            ctx_kwargs["proxy"] = {"server": proxy_url}
        ctx = browser.new_context(**ctx_kwargs)
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
        page = ctx.new_page()
        page.on("request", on_request)

        if not args.no_warmup:
            print("-> https://www.google.com/  (warm-up)")
            try:
                page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2500)
                page.mouse.move(400, 300)
                page.mouse.wheel(0, 240)
                page.wait_for_timeout(1200)
            except Exception as exc:
                print(f"  warm-up problem: {exc}")

        print(f"-> {AI_MODE}")
        try:
            page.goto(AI_MODE, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            print(f"  navigation problem: {exc}")
        page.wait_for_timeout(int(args.wait * 1000))

        title = page.title()
        blocked = any(s in (page.content() or "").lower()[:4000]
                      for s in ("unusual traffic", "sorry/index", "recaptcha", "before you continue"))
        print(f"  title: {title!r}   blocked_hint={blocked}")

        page.screenshot(path=str(args.out / "page.png"), full_page=False)

        try:
            elements = page.evaluate(JS_ELEMENTS)
        except Exception as exc:
            elements = {"error": str(exc)}
        (args.out / "elements.json").write_text(json.dumps(elements, indent=1))
        print(f"  file inputs: {len(elements.get('fileInputs', []))}")
        print(f"  image-ish aria-labels: {len(elements.get('labelled', []))}")
        for f in elements.get("fileInputs", [])[:8]:
            print(f"    input accept={f['accept']!r} hidden={f['hidden']}")
        for l in elements.get("labelled", [])[:12]:
            print(f"    <{l['tag']}> {l['aria']!r}  js={l['js']!r}")

        before = len(log)
        uploaded = False
        if args.image and args.image.exists():
            data = args.image.read_bytes()
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                mime, name = "image/png", "probe.png"
            elif data[:3] == b"\xff\xd8\xff":
                mime, name = "image/jpeg", "probe.jpg"
            else:
                mime, name = "image/webp", "probe.webp"
            payload = {"name": name, "mimeType": mime, "buffer": data}
            try:
                loc = page.locator("input[type=file]")
                n = loc.count()
                if n:
                    loc.first.set_input_files(payload, timeout=10000)
                    uploaded = True
                    print(f"  attached {args.image.name} to file input #1 of {n}")
                else:
                    print("  no <input type=file> in the DOM — upload goes another route")
            except Exception as exc:
                print(f"  attach failed: {exc}")
            page.wait_for_timeout(6000)
            page.screenshot(path=str(args.out / "after.png"), full_page=False)
        elif args.image:
            print(f"  image not found: {args.image}")

        if args.keep_open:
            print(f"  page open for {args.keep_open}s — click the image button now")
            page.wait_for_timeout(int(args.keep_open * 1000))

        after = len(log)
        (args.out / "network.json").write_text(json.dumps(log, indent=1))

        lines = []
        for e in log:
            if e["method"] != "GET":
                lines.append(f"# {e['method']} {e['url']}  ({e['body_bytes']} bytes, {e['content_type']})")
                lines.append(f"curl -X {e['method']} '{e['url']}' \\")
                lines.append(f"  -H 'content-type: {e['content_type'] or "application/octet-stream"}' \\")
                lines.append("  --data-binary @payload.bin")
                lines.append("")
        (args.out / "requests.http").write_text("\n".join(lines) or "# no non-GET requests seen")

        print(f"\n  requests captured: {before} before upload, {after} total")
        posts = [e for e in log if e["method"] != "GET"]
        print(f"  non-GET requests: {len(posts)}")
        for e in posts[:15]:
            print(f"    {e['method']} {e['url'][:130]}  [{e['body_bytes'] or '?'}B]")
        if uploaded and not posts:
            print("  (!) file attached but nothing uploaded — the input is probably a decoy")

        print(f"\n  wrote {args.out}/page.png, elements.json, network.json, requests.http")
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
