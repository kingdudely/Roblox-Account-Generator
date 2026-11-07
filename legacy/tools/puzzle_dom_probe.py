#!/usr/bin/env python3
"""Find where the dice puzzle keeps its images, inside the live page.

The browserless path fetches `/rtig/image` itself, and that endpoint is metered
per exit — which is what caps the whole pipeline at roughly one completable
challenge in six. The browser does not have that problem: it loads the strip
naturally, once, as part of rendering.

So the question is only *where* the strip ends up in the DOM. The game core draws
each tile from one 2000x400 sprite, so the candidates are:

    a <canvas> the game drew tiles onto        -> read back with toDataURL
    divs with background-image + background-position -> one sprite URL, ten offsets
    ten <img> elements pointing at the sprite  -> same URL, CSS-cropped

All three resolve to the same thing: one sprite URL. If we can read that URL from
inside the frame and fetch it in page context, we get the exact bytes with no
extra `/rtig/image` request, and the strip splits with the tooling that already
exists (`tools/wave_montage.split_strip`).

This dumps the structure and saves any sprite it finds.

    ./tools/puzzle_dom_probe.py --proxy-file proxies.txt --rounds 4
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import config
from roblox_creator.arkose_ui import probe_state
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools.browser_creator import (LAUNCH_ARGS, STEALTH_JS, UA, fill_form,
                                   make_credentials)

# Collect every URL the game might be using for its imagery, plus how each
# element is laid out, so the shape is obvious from the dump.
DUMP_JS = r"""
() => {
  const out = {urls: [], canvases: [], imgs: [], bg: [], counts: {}};
  const push = (k) => { out.counts[k] = (out.counts[k] || 0) + 1; };

  document.querySelectorAll('*').forEach((el) => {
    const st = getComputedStyle(el);
    const bgImg = st.backgroundImage || '';
    if (bgImg && bgImg !== 'none' && bgImg.includes('url(')) {
      push('bg');
      if (out.bg.length < 16) out.bg.push({
        tag: el.tagName,
        cls: (el.className || '').toString().slice(0, 60),
        url: bgImg.slice(4, -1).slice(0, 160),
        pos: st.backgroundPosition,
        size: st.backgroundSize,
        w: Math.round(el.getBoundingClientRect().width),
        h: Math.round(el.getBoundingClientRect().height),
      });
    }
  });

  document.querySelectorAll('img').forEach((el) => {
    push('img');
    if (out.imgs.length < 24) out.imgs.push({
      src: (el.currentSrc || el.src || '').slice(0, 180),
      w: el.naturalWidth, h: el.naturalHeight,
      cw: Math.round(el.getBoundingClientRect().width),
      ch: Math.round(el.getBoundingClientRect().height),
      cls: (el.className || '').toString().slice(0, 50),
    });
  });

  document.querySelectorAll('canvas').forEach((el) => {
    push('canvas');
    if (out.canvases.length < 12) {
      let data = '';
      try { data = el.toDataURL('image/png').length.toString(); } catch (e) {
        data = 'tainted:' + e.name;
      }
      out.canvases.push({
        w: el.width, h: el.height,
        cw: Math.round(el.getBoundingClientRect().width),
        ch: Math.round(el.getBoundingClientRect().height),
        png_len: data,
      });
    }
  });

  return out;
}
"""

# Read the sprite as bytes *inside the frame*, so the request is same-origin and
# carries the frame's own cookies and referer — exactly what the game already did.
FETCH_JS = r"""
async (url) => {
  try {
    const r = await fetch(url, {credentials: 'include'});
    const b = await r.blob();
    const buf = await b.arrayBuffer();
    let bin = '';
    const bytes = new Uint8Array(buf);
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
    }
    return {ok: true, status: r.status, type: b.type, b64: btoa(bin)};
  } catch (e) {
    return {ok: false, error: String(e).slice(0, 120)};
  }
}
"""


async def one(playwright, raw: str, out: Path, idx: int, timeout: float) -> dict:
    p = parse_proxy(raw)
    relay = await SocksRelay.start(f"{p.host}:{p.port}", p.user, p.password)
    result: dict = {"proxy": p.masked(), "index": idx}
    browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
    try:
        ctx = await browser.new_context(
            proxy={"server": relay.url}, user_agent=UA,
            viewport={"width": 1366, "height": 900}, locale="en-US",
            timezone_id="America/New_York")
        await ctx.add_init_script(STEALTH_JS)
        page = await ctx.new_page()

        username, password = make_credentials()
        await fill_form(page, username, password, human=True)

        # wait for the ladder to reach the puzzle (or anything else)
        deadline = asyncio.get_event_loop().time() + timeout
        state = {"kind": "unknown"}
        while asyncio.get_event_loop().time() < deadline:
            state = await probe_state(page)
            if state["kind"] in ("puzzle", "created", "hold"):
                break
            await page.wait_for_timeout(700)
        result["kind"] = state["kind"]
        if state["kind"] != "puzzle":
            return result

        # The game frame mounts empty and fills in later, so poll it rather than
        # sleeping a fixed amount and calling whatever we find the truth.
        for _ in range(30):
            ready = 0
            for f in page.frames:
                if "game-core" not in (f.url or ""):
                    continue
                try:
                    n = await f.evaluate(
                        "() => document.querySelectorAll('*').length")
                except Exception:
                    n = 0
                ready = max(ready, n)
            if ready > 2:
                break
            await page.wait_for_timeout(700)
        await page.wait_for_timeout(800)

        # The challenge is three frames deep (enforcement -> pow -> game core) and
        # which one owns the tiles is not fixed, so dump every frame and keep the
        # richest. A frame we cannot evaluate is skipped, not fatal.
        frame = None
        dump = None
        best = -1
        per_frame = []
        for f in list(page.frames):
            fu = (f.url or "")[:110]
            entry = {"url": fu, "arkose": "arkoselabs" in fu}
            try:
                entry["ready"] = await f.evaluate(
                    "() => document.readyState + ':' + "
                    "(document.body ? document.body.innerHTML.length : -1)")
            except Exception:
                entry["ready"] = "?"
            try:
                cand = await f.evaluate(DUMP_JS)
                entry["counts"] = cand.get("counts")
                entry["score"] = sum((cand.get("counts") or {}).values())
                # Only the Arkose frames are candidates for the game; the host
                # page always has more elements, so scoring by size alone picks
                # Roblox's own signup form.
                if entry["arkose"] and entry["score"] > best:
                    best, frame, dump = entry["score"], f, cand
            except Exception as exc:
                entry["eval_error"] = f"{type(exc).__name__}: {str(exc)[:70]}"
            per_frame.append(entry)
        result["per_frame"] = per_frame
        if frame is None or not dump or best <= 0:
            result["error"] = f"no frame with elements (best={best})"
            result["frame_urls"] = [(f.url or "")[:90] for f in page.frames]
            return result

        result["frame_url"] = (frame.url or "")[:150]
        result["frames_seen"] = len(page.frames)
        result["dom"] = dump

        # try every candidate sprite URL until one decodes as an image
        candidates = []
        for b in dump.get("bg", []):
            if "rtig/image" in b["url"] or "image?" in b["url"]:
                candidates.append(b["url"])
        for im in dump.get("imgs", []):
            if "rtig/image" in (im["src"] or "") or "image?" in (im["src"] or ""):
                candidates.append(im["src"])

        saved = 0
        for n, url in enumerate(dict.fromkeys(candidates)):
            got = await frame.evaluate(FETCH_JS, url)
            result.setdefault("fetches", []).append(
                {k: v for k, v in got.items() if k != "b64"})
            if not got.get("ok"):
                continue
            raw_bytes = base64.b64decode(got["b64"])
            path = out / f"{idx:02d}_sprite{n}.bin"
            path.write_bytes(raw_bytes)
            result.setdefault("sprites", []).append(
                {"file": path.name, "bytes": len(raw_bytes),
                 "type": got.get("type")})
            saved += 1
            if saved >= 2:
                break

        await page.screenshot(path=str(out / f"{idx:02d}_puzzle.png"))
        return result
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:120]}"
        return result
    finally:
        try:
            await browser.close()
        except Exception:
            pass
        await relay.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxies.txt")
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=60.0)
    ap.add_argument("--out", type=Path, default=Path("/tmp/opencode/domprobe"))
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    proxies = load_proxies(args.proxy_file)
    random.shuffle(proxies)

    from playwright.async_api import async_playwright

    summary = []
    async with async_playwright() as p:
        for i in range(args.rounds):
            print(f"\n== round {i + 1}/{args.rounds} ==")
            r = await one(p, proxies[i], args.out, i, args.timeout)
            summary.append(r)
            print(f"  kind: {r.get('kind')}")
            if r.get("error"):
                print(f"  error: {r['error']}")
            for pf in r.get("per_frame", []):
                flag = "ARKOSE" if pf.get("arkose") else "page  "
                print(f"    [{flag}] score={pf.get('score')} {pf.get('counts')} "
                      f"ready={pf.get('ready')} {pf.get('eval_error','')} "
                      f"{pf['url'][:70]}")
            dom = r.get("dom")
            if dom:
                print(f"  elements: {dom.get('counts')}")
                for c in dom.get("canvases", []):
                    print(f"    canvas {c}")
                for b in dom.get("bg", [])[:5]:
                    print(f"    bg {b['tag']} {b['w']}x{b['h']} pos={b['pos']} "
                          f"url={b['url'][:70]}")
                for im in dom.get("imgs", [])[:5]:
                    print(f"    img {im['w']}x{im['h']} shown={im['cw']}x{im['ch']} "
                          f"{im['src'][:70]}")
            for s in r.get("sprites", []):
                print(f"  sprite {s}")
            if r.get("kind") == "puzzle":
                break

    (args.out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"\n  -> {args.out / 'summary.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
