#!/usr/bin/env python3
"""Find and replay Google AI Mode's image-upload path.

`aimode_api.py` proved the text path: one shell fetch, one `/async/folwr` fetch,
answer back, no browser. Images are the missing half — no public AI Mode scraper
uploads one, so this tool watches a real headed Chrome and records what the page
sends when a file goes in.

    ./tools/aimode_image.py --dom                       # just list the affordances
    ./tools/aimode_image.py --image k.png               # attach + record
    ./tools/aimode_image.py --image k.png --query "how many icons"

Writes to debug/aimode/image/<stamp>/:
    dom.json        every file input, button, aria-label and jsaction on the page
    page.png        what the page looked like
    network.json    every request, with POST bodies flagged
    uploads.json    only the upload-shaped requests, with body previews
    answer.txt      visible text after the attach settles

Why headed: Google serves a `/httpservice/retry/enablejs` stub to headless
Chrome and to curl without a trusted cookie jar. The upload UI is JS-built, so
it only exists on the real page.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

AI_MODE = "https://www.google.com/search?udm=50&aep=11&hl=en&gl=us"

UPLOAD_HINTS = ("upload", "lens", "scotty", "/_/", "batchexecute", "blobstore",
                "media", "attach", "async/")

JS_INVENTORY = r"""
() => {
  const grab = (el, i) => {
    const a = {};
    for (const at of el.attributes || []) a[at.name] = (at.value || '').slice(0, 160);
    return {
      i, tag: el.tagName.toLowerCase(),
      aria: el.getAttribute('aria-label') || '',
      role: el.getAttribute('role') || '',
      jsaction: (el.getAttribute('jsaction') || '').slice(0, 120),
      jsname: el.getAttribute('jsname') || '',
      type: el.getAttribute('type') || '',
      accept: el.getAttribute('accept') || '',
      hidden: el.offsetParent === null,
      cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 100),
      text: (el.innerText || '').trim().slice(0, 60),
      attrs: a,
    };
  };
  const out = { fileInputs: [], buttons: [], labelled: [], areas: [], uploads: [] };
  document.querySelectorAll('input[type=file]').forEach((el, i) => out.fileInputs.push(grab(el, i)));
  document.querySelectorAll('button, [role=button], [jsaction]').forEach((el, i) => {
    if (out.buttons.length < 120) out.buttons.push(grab(el, i));
  });
  document.querySelectorAll('[aria-label]').forEach((el, i) => {
    const a = el.getAttribute('aria-label') || '';
    if (/image|photo|upload|camera|lens|file|attach|picture|visual|scan/i.test(a) && out.labelled.length < 60)
      out.labelled.push(grab(el, i));
  });
  document.querySelectorAll('textarea, [contenteditable=true], [role=textbox], form').forEach((el, i) => {
    if (out.areas.length < 40) out.areas.push(grab(el, i));
  });
  // anything whose id/class hints at an uploader
  document.querySelectorAll('*[id*=upload], *[class*=upload], *[id*=lens], *[class*=lens]').forEach((el, i) => {
    if (out.uploads.length < 40) out.uploads.push(grab(el, i));
  });
  return out;
}
"""

# Fire a paste event carrying a File at the composer. AI Mode listens for paste
# on the search box, and this is the path that works when no <input type=file>
# is present in the DOM at all.
JS_PASTE_FILE = r"""
async (arg) => {
  const bytes = Uint8Array.from(atob(arg.b64), c => c.charCodeAt(0));
  const file = new File([bytes], arg.name, {type: arg.mime});
  const dt = new DataTransfer();
  dt.items.add(file);
  const targets = [
    document.querySelector('textarea'),
    document.querySelector('[contenteditable=true]'),
    document.querySelector('[role=textbox]'),
    document.activeElement,
    document.body,
  ].filter(Boolean);
  const seen = new Set();
  for (const t of targets) {
    if (seen.has(t)) continue;
    seen.add(t);
    t.focus && t.focus();
    for (const type of ['paste', 'drop']) {
      const ev = new ClipboardEvent(type, {bubbles: true, cancelable: true, clipboardData: dt});
      try { t.dispatchEvent(ev); } catch (e) {}
    }
  }
  return {tried: [...seen].map(e => e.tagName + '#' + (e.id || '')), files: dt.files.length};
}
"""


def sniff(path: Path) -> tuple[str, str]:
    head = path.read_bytes()[:12]
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", "probe.png"
    if head[:3] == b"\xff\xd8\xff":
        return "image/jpeg", "probe.jpg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp", "probe.webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif", "probe.gif"
    return "image/png", path.name


def normalise_proxy(proxy: str | None) -> str | None:
    if not proxy:
        return None
    proxy = proxy.strip()
    if proxy.startswith("socks5://"):
        return "socks5h://" + proxy[len("socks5://"):]
    return proxy


def pick_proxy(path: Path, limit: int = 8, deadline: float = 10.0) -> str | None:
    """Sticky residential lines die silently. Probe before paying for a Chrome launch."""
    from curl_cffi import requests as creq

    if not path.exists():
        return None
    lines = [l.strip() for l in path.read_text().splitlines() if l.strip()]
    for i, line in enumerate(lines[:limit]):
        p = normalise_proxy(line)
        t0 = time.time()
        try:
            s = creq.Session(impersonate="chrome", timeout=deadline,
                             proxies={"http": p, "https": p})
            r = s.get("https://www.google.com/generate_204")
            dt = time.time() - t0
            if r.status_code == 204:
                print(f"  proxy[{i}] live in {dt:.1f}s")
                s.close()
                time.sleep(0.8)
                return p
            s.close()
        except Exception as exc:
            print(f"  proxy[{i}] dead ({str(exc)[:44]})")
        continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", type=Path, default=None)
    ap.add_argument("--query", default="")
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=None,
                    help="probe these in order and use the first that answers")
    ap.add_argument("--port", type=int, default=0,
                    help="CDP port; 0 picks a free one (a stale Chrome on a fixed port silently wins the attach)")
    ap.add_argument("--watch-idx", type=int, default=None,
                    help="index into dom.json#fileInputs to attach to")
    ap.add_argument("--dom", action="store_true", help="inventory only, never attach")
    ap.add_argument("--settle", type=float, default=14.0)
    ap.add_argument("--blank", action="store_true",
                    help="land on an empty composer so nothing auto-submits before the attach")
    ap.add_argument("--type-text", default=None,
                    help="text to type into the composer after the attach")
    ap.add_argument("--send-query", action="store_true",
                    help="press Enter after the attach so the upload+query round-trip is captured")
    ap.add_argument("--out", type=Path, default=ROOT / "debug/aimode/image")
    args = ap.parse_args()

    from playwright.sync_api import sync_playwright
    from tools.aimode_capture import _connect_with_retry, _launch_real_chrome

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = args.out / stamp
    out.mkdir(parents=True, exist_ok=True)

    if not args.port:
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            args.port = s.getsockname()[1]

    requests: list[dict] = []
    responses_kept: list[dict] = []
    t0 = time.time()
    if not args.proxy and args.proxy_file:
        args.proxy = pick_proxy(args.proxy_file)
        if not args.proxy:
            print("no live proxy in the file", file=sys.stderr)
            return 5
    proxy_url = normalise_proxy(args.proxy)
    bridge = None
    if proxy_url and proxy_url.startswith("socks5"):
        # Chrome rejects the socks5h scheme outright (ERR_NO_SUPPORTED_PROXIES)
        # and cannot send RFC 1929 credentials, so tunnel through the loopback
        # HTTP-CONNECT bridge exactly as the Roblox path does.
        from roblox_creator.socks_bridge import SocksBridge
        bridge = SocksBridge(proxy_url)
        proxy_url = bridge.start()
        print(f"  socks5 -> {proxy_url}")

    def on_request(req):
        try:
            url = req.url
            method = req.method
            interesting = method != "GET" or any(h in url.lower() for h in UPLOAD_HINTS)
            if not interesting:
                return
            entry = {
                "i": len(requests), "t": round(time.time() - t0, 2),
                "method": method, "url": url, "type": req.resource_type,
                "content_type": (req.headers or {}).get("content-type", "")[:120],
                "headers": {k: v for k, v in (req.headers or {}).items()},
            }
            try:
                buf = req.post_data_buffer
                if buf:
                    entry["body_len"] = len(buf)
                    entry["body_b64"] = base64.b64encode(buf[:200000]).decode()
                    try:
                        entry["body_text"] = buf[:4000].decode("utf-8")
                        entry["body_encoding"] = "text"
                    except UnicodeDecodeError:
                        entry["body_encoding"] = "binary"
            except Exception:
                pass
            requests.append(entry)
        except Exception:
            pass

    def on_response(resp):
        """Keep the bodies that matter: the upload reply carries the image id."""
        try:
            url = resp.url
            interesting = (resp.request.method != "GET"
                           or any(h in url.lower() for h in UPLOAD_HINTS))
            if not interesting:
                return
            entry = {
                "t": round(time.time() - t0, 2), "url": url,
                "status": resp.status,
                "content_type": resp.headers.get("content-type", "")[:120],
                "headers": dict(resp.headers or {}),
            }
            try:
                body = resp.body()
                entry["len"] = len(body)
                if len(body) <= 300000:
                    entry["body_b64"] = base64.b64encode(body).decode()
                    try:
                        entry["body_text"] = body.decode("utf-8")[:20000]
                    except UnicodeDecodeError:
                        pass
            except Exception as exc:
                entry["body_error"] = str(exc)[:120]
            responses_kept.append(entry)
        except Exception:
            pass

    proc = None
    with sync_playwright() as pw:
        proc = _launch_real_chrome(args.port, out / "profile", True, proxy_url)
        # an exception anywhere below must not leave a Chrome window behind
        import atexit
        atexit.register(lambda: proc.terminate() if proc else None)
        browser = _connect_with_retry(pw, args.port)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on("request", on_request)
        page.on("response", on_response)

        print("  warm-up google.com")
        try:
            page.goto("https://www.google.com/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(2000)
            page.mouse.move(500, 330)
            page.mouse.wheel(0, 160)
        except Exception as exc:
            print(f"  warm-up issue: {str(exc)[:80]}")

        target = AI_MODE + ("" if args.blank else f"&q={args.query}")
        print(f"  -> {target}")
        try:
            page.goto(target, wait_until="domcontentloaded", timeout=60000)
        except Exception as exc:
            print(f"  navigation issue: {str(exc)[:90]}")
        page.wait_for_timeout(5000)

        title = page.title()
        try:
            body = page.content() or ""
        except Exception:
            body = ""
        visible_text = page.evaluate("() => (document.body.innerText || '').slice(0, 4000)").lower()
        blocked = any(m in visible_text for m in
                      ("our systems have detected", "unusual traffic", "recaptcha",
                       "before you continue"))
        print(f"  title={title[:70]!r}  blocked={blocked}")
        page.screenshot(path=str(out / "page.png"), full_page=False)

        dom = page.evaluate(JS_INVENTORY)
        (out / "dom.json").write_text(json.dumps(dom, indent=1))
        try:
            composer_html = page.evaluate(r"""
            () => {
              const ta = document.querySelector('textarea');
              if (!ta) return '';
              let n = ta.parentElement;
              for (let i = 0; i < 4 && n && n.parentElement; i++) n = n.parentElement;
              return (n ? n.outerHTML : ta.outerHTML).slice(0, 120000);
            }
            """)
            (out / "composer.html").write_text(composer_html or "")
            print(f"  composer html: {len(composer_html or '')} bytes")
        except Exception as exc:
            print(f"  composer dump failed: {exc}")
        print(f"  file inputs : {len(dom['fileInputs'])}")
        print(f"  buttons     : {len(dom['buttons'])}")
        print(f"  image-ish   : {len(dom['labelled'])}")
        print(f"  composer    : {len(dom['areas'])}")

        snapshots: dict[str, str] = {}

        def snap(name: str) -> None:
            try:
                snapshots[name] = page.url
                (out / "urls.json").write_text(json.dumps(snapshots, indent=1))
            except Exception:
                pass

        snap("before_attach")
        for b in dom["labelled"][:14]:
            print(f"    <{b['tag']}> aria={b['aria'][:50]!r} jsname={b['jsname']!r} hidden={b['hidden']}")
        for a in dom["areas"][:6]:
            print(f"    composer <{a['tag']}> role={a['role']!r} cls={a['cls'][:40]!r}")

        attached = "none"
        if args.image and not args.dom:
            if not args.image.exists():
                print(f"  image missing: {args.image}")
            else:
                mime, name = sniff(args.image)
                data = args.image.read_bytes()
                payload = {"name": name, "mimeType": mime, "buffer": data}
                before = len(requests)

                # Every file chooser that fires gets our file, so it does not
                # matter whether the input exists before or after the menu click.
                choosers: list[str] = []

                def on_chooser(fc):
                    try:
                        choosers.append(fc.is_multiple())
                        fc.set_files(payload)
                    except Exception as exc:
                        choosers.append(f"ERR {exc}"[:80])

                page.on("filechooser", on_chooser)

                # The composer's "+" opens a menu: Add images / Add files /
                # Create images / Canvas. "Add images" is the one that uploads.
                menu_items: list[dict] = []
                opened = False
                try:
                    # three "Add files and tools" nodes exist, two hidden — a
                    # bare selector grabs the hidden one and the menu never opens
                    plus = page.locator('[aria-label="Add files and tools"]:visible').first
                    plus.click(timeout=10000)
                    page.wait_for_timeout(1500)
                    opened = True
                    page.screenshot(path=str(out / "menu.png"))
                    menu_items = page.evaluate(r"""
                    () => [...document.querySelectorAll('[role=menuitem],[role=menuitemradio],[role=option]')]
                      .map(e => ({t: (e.innerText || '').trim().slice(0, 60),
                                  a: e.getAttribute('aria-label') || ''}))
                    """)
                    (out / "menu.json").write_text(json.dumps(menu_items, indent=1))
                    print(f"  + menu: {[m['t'] for m in menu_items]}")
                except Exception as exc:
                    print(f"  + menu failed: {str(exc)[:140]}")

                if opened and "none" == attached:
                    for label in ("Add images", "Add files"):
                        try:
                            page.get_by_text(label, exact=True).first.click(timeout=6000)
                            page.wait_for_timeout(1800)
                            if choosers:
                                attached = f"menu->{label} (chooser fired)"
                                break
                        except Exception as exc:
                            print(f"  click {label!r} failed: {str(exc)[:110]}")
                    if attached == "none" and dom["fileInputs"]:
                        try:
                            page.locator("input[type=file]").first.set_input_files(
                                payload, timeout=15000)
                            attached = "input after menu"
                        except Exception as exc:
                            print(f"  post-menu input failed: {str(exc)[:110]}")

                if attached == "none" and dom["fileInputs"]:
                    idx = args.watch_idx if args.watch_idx is not None else 0
                    try:
                        page.locator("input[type=file]").nth(idx).set_input_files(
                            payload, timeout=15000)
                        attached = f"file-input#{idx}"
                    except Exception as exc:
                        print(f"  set_input_files failed: {str(exc)[:140]}")

                if attached == "none":
                    try:
                        res = page.evaluate(JS_PASTE_FILE, {
                            "b64": base64.b64encode(data).decode(),
                            "name": name, "mime": mime,
                        })
                        attached = f"paste-event {res.get('tried')}"
                    except Exception as exc:
                        print(f"  paste failed: {str(exc)[:140]}")

                print(f"  attach: {attached}  (choosers={choosers})")
                try:
                    print(f"  URL after attach: {page.url[:400]}")
                except Exception:
                    pass
                print(f"  settling {args.settle}s")
                page.wait_for_timeout(int(args.settle * 1000))
                page.screenshot(path=str(out / "after.png"), full_page=False)
                snap("after_attach")

                if args.type_text or args.send_query:
                    try:
                        if args.type_text:
                            ta = page.locator("textarea:visible").first
                            ta.click(timeout=8000)
                            page.wait_for_timeout(400)
                            page.keyboard.type(args.type_text, delay=40)
                            page.wait_for_timeout(1200)
                            page.screenshot(path=str(out / "typed.png"))
                            print("  send buttons visible: "
                                  f"{page.locator('[aria-label=\"Send\"]:visible').count()}")
                        if page.locator('[aria-label="Send"]:visible').count():
                            page.locator('[aria-label="Send"]:visible').first.click(timeout=6000)
                        else:
                            page.keyboard.press("Enter")
                        page.wait_for_timeout(12000)
                        page.screenshot(path=str(out / "submitted.png"))
                        snap("after_submit")
                        try:
                            from urllib.parse import urlparse, parse_qs
                            u = page.url
                            print(f"  URL after submit ({len(u)} chars):")
                            for k, v in parse_qs(urlparse(u).query).items():
                                print(f"     {k:12} = {str(v[0])[:70]}")
                            (out / "submitted_url.txt").write_text(u)
                        except Exception as exc:
                            print(f"  url dump failed: {exc}")
                    except Exception as exc:
                        print(f"  submit failed: {str(exc)[:160]}")

                new = requests[before:]
                print(f"  {len(new)} new request(s) after attach")

        page.screenshot(path=str(out / "final.png"), full_page=False)
        visible = page.evaluate(
            "() => (document.body.innerText || '').replace(/\\s+/g,' ').slice(0,4000)")
        (out / "answer.txt").write_text(visible or "")
        if visible:
            print(f"\n  page text: {visible[:400]}")

        cookies = {c["name"]: c["value"] for c in ctx.cookies()
                   if "google.com" in c.get("domain", "")}
        browser.close()
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass

    (out / "network.json").write_text(json.dumps(requests, indent=1))
    (out / "responses.json").write_text(json.dumps(responses_kept, indent=1))
    (out / "cookies.json").write_text(json.dumps(cookies, indent=1))

    uploads = [r for r in requests
               if r["method"] != "GET"
               and re.search(r"upload|lens|scotty|blobstore|media|batchexecute", r["url"], re.I)]
    (out / "uploads.json").write_text(json.dumps(uploads, indent=1))

    print(f"\n  non-GET: {sum(1 for r in requests if r['method'] != 'GET')}  upload-shaped: {len(uploads)}")
    for r in uploads[:12]:
        print(f"    {r['method']} {r['url'][:110]}  [{r.get('body_len', '?')}B {r.get('content_type', '')[:40]}]")

    for r in requests:
        if "crupload" in r["url"] or "/v1/gsessionid" in r["url"]:
            print(f"\n  --- {r['method']} {r['url'][:100]}")
            for k, v in (r.get("headers") or {}).items():
                print(f"      {k}: {v[:130]}")
    for r in responses_kept:
        if "crupload" in r["url"] or "gsessionid" in r["url"]:
            print(f"\n  --- RESP {r['status']} {r['url'][:100]}")
            print(f"      ct={r.get('content_type')} len={r.get('len')}")
            if r.get("body_text"):
                print(f"      body={r['body_text'][:300]!r}")
            elif r.get("body_b64"):
                print(f"      body_b64={r['body_b64'][:200]}")
    print(f"\n  -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
