#!/usr/bin/env python3
"""Hook the live Arkose page and record what `dapib` is actually fed.

`tools/dapib.py` reproduces the transform exactly (verified against 578.js), but
that only matters if we know two things it cannot tell us:

  * the shape of `window.parent.ae.answer` — the plaintext the game core builds
  * how the game core submits the result — which request, which field

So this drives one real challenge, wraps `ae.dapibReceive` in every frame, clicks
candidate tiles to make the game produce an answer, and records:

  * every `dapibReceive` call: the plaintext in, the transformed string out
  * every `/fc/ca/` request body, so the wire format is known
  * whether our Python port reproduces the observed transform

Getting the last one from a real exchange is the difference between "the JS agrees
with my port" and "Arkose agrees with my port".

    ./tools/dapib_hook.py
    ./tools/dapib_hook.py --proxy socks5://... --clicks 6
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import captcha as cap            # noqa: E402
from roblox_creator.browser import BrowserSession    # noqa: E402
from roblox_creator.config import Settings, load_proxies  # noqa: E402
from roblox_creator.creator import SEL_SUBMIT, Creator  # noqa: E402
from roblox_creator.identity import make_identity    # noqa: E402
from roblox_creator.store import AccountStore        # noqa: E402
from tools.dapib import transform                   # noqa: E402

OUT_ROOT = ROOT / "debug" / "dapib-hook"

# Installed into every frame before its own scripts. Polls briefly because the
# dapib iframe can be created before `parent.ae` is populated, and the hook has
# to be in place before 578.js runs — it fires once, on load.
HOOK = r"""
(function(){
  if (window.__dapibHookInstalled) return;
  window.__dapibHookInstalled = true;

  function emit(tag, obj){
    try { console.log('__DAPIB__' + tag + JSON.stringify(obj)); } catch(e){}
  }

  // dapib mutates the answer object in place, so by the time it calls
  // dapibReceive the plaintext is gone. Intercept assignment instead and keep a
  // deep clone of every value the game writes, which is what we actually need.
  function wrapAnswer(ae){
    if (ae.__answerWrapped) return;
    ae.__answerWrapped = true;
    var current = ae.answer;
    ae.__pristine = current;
    try {
      Object.defineProperty(ae, 'answer', {
        configurable: true,
        enumerable: true,
        get: function(){ return current; },
        set: function(v){
          current = v;
          try { ae.__pristine = JSON.parse(JSON.stringify(v)); } catch(e){ ae.__pristine = v; }
        }
      });
    } catch(e){ emit('WRAPERR', {e: String(e)}); }
  }

  // dapib mutates ae.answer synchronously when its script executes, so the only
  // way to see the plaintext is to sample it continuously and keep the last few
  // distinct states. The last one before the dapibReceive call is the input.
  var __log = [];
  var __last = null;
  setInterval(function(){
    try {
      var ae = window.parent && window.parent.ae;
      if (!ae) return;
      var s = JSON.stringify(ae.answer);
      if (s !== __last) { __last = s; __log.push(s); if (__log.length > 60) __log.shift(); }
    } catch(e){}
  }, 1);

  function install(){
    var ae;
    try { ae = window.parent && window.parent.ae; } catch(e){ return false; }
    if (!ae) return false;
    wrapAnswer(ae);
    if (ae.__hooked) return true;

    var orig = ae.dapibReceive;
    ae.__hooked = true;
    ae.dapibReceive = function(o){
      var snap;
      try {
        snap = {
          rollback: __log.slice(-4),
          pristine: ae.__pristine,
          currentAfter: ae.answer,
          payloadKeys: o ? Object.keys(o) : null,
          tanswerJson: (function(){ try { return JSON.stringify(o.tanswer); } catch(e){ return null; } })()
        };
      } catch(e){ snap = {error: String(e)}; }
      emit('CALL', snap);
      try { if (orig) return orig.apply(this, arguments); } catch(e){ emit('ORIGERR', {e: String(e)}); }
    };
    emit('INSTALLED', {href: location.href, slice: (location.pathname||'').slice(0,60)});
    return true;
  }

  if (!install()) {
    var n = 0;
    var t = setInterval(function(){
      n++;
      if (install() || n > 8000) clearInterval(t);
    }, 1);
  }
})();
"""

CLICK_TILES = r"""() => {
  // Arkose renders candidates as buttons/images inside the game frame. Click
  // whatever looks like a tile, oldest-first, and report how many we hit.
  const out = [];
  const nodes = [...document.querySelectorAll('img, [role="button"], button')];
  const tiles = nodes.filter(e => {
    const r = e.getBoundingClientRect();
    return r.width >= 30 && r.height >= 30 && e.tagName !== 'BODY';
  });
  for (const e of tiles.slice(0, 3)) {
    const r = e.getBoundingClientRect();
    out.push({tag: e.tagName, w: Math.round(r.width), h: Math.round(r.height),
              alt: e.getAttribute('alt') || '', cls: (e.className||'').toString().slice(0,40)});
    try { e.click(); } catch(err) {}
  }
  return out;
}"""

FIND_SUBMIT = r"""() => {
  const want = /submit|verify|next|continue|confirm/i;
  const nodes = [...document.querySelectorAll('button, [role="button"], a')];
  const hit = nodes.find(e => want.test((e.innerText||'').trim()) ||
                              want.test(e.getAttribute('aria-label')||''));
  if (!hit) return null;
  const r = hit.getBoundingClientRect();
  if (r.width < 5) return null;
  hit.click();
  return (hit.innerText||'').trim().slice(0,30);
}"""


def verify_against_python(calls: list[dict]) -> list[dict]:
    """Replay every captured plaintext through our port and compare."""
    results = []
    for call in calls:
        inp = call.get("input")
        tanswer_json = call.get("tanswerJson")
        if not isinstance(inp, list) or tanswer_json is None:
            results.append({"comparable": False})
            continue
        try:
            want = json.loads(tanswer_json)
        except Exception:
            results.append({"comparable": False})
            continue
        got = transform(inp, q="")
        # the JS may append an environment character; compare with and without
        match = got == want
        if not match and isinstance(want, list):
            stripped = [{k: v[:-1] if isinstance(v, str) and v else v for k, v in o.items()}
                        for o in want]
            match = got == stripped
        results.append({"comparable": True, "match": match,
                        "want": json.dumps(want)[:600],
                        "got": json.dumps(got)[:600]})
    return results


def run(proxy: str | None, clicks: int, headless: bool, diagnose: bool = False) -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out = OUT_ROOT / stamp
    out.mkdir(parents=True, exist_ok=True)
    print(f"  capture -> {out}")
    print(f"  proxy   -> {'(direct)' if not proxy else proxy.split('@')[-1]}")

    settings = Settings(headless=headless)
    settings.solve_press_hold = True
    creator = Creator(settings, AccountStore(Path("/tmp/opencode/dapib_hook")))
    rng = random.Random()

    calls: list[dict] = []
    ca_bodies: list[dict] = []
    console_all: list[str] = []

    with BrowserSession(settings) as sess:
        ctx = sess.new_context(proxy=proxy)
        ctx.add_init_script(HOOK)
        page = sess.new_page(ctx)

        def on_console(msg):
            text = msg.text
            console_all.append(text[:2000])
            if text.startswith("__DAPIB__CALL"):
                try:
                    calls.append(json.loads(text[len("__DAPIB__CALL"):]))
                    print(f"  dapib CALL captured ({len(calls)})", flush=True)
                except Exception:
                    pass
            elif text.startswith("__DAPIB__INSTALLED"):
                try:
                    info = json.loads(text[len("__DAPIB__INSTALLED"):])
                    print(f"  hook installed: {info.get('slice')}", flush=True)
                except Exception:
                    pass

        def on_response(resp):
            url = resp.url
            if "/dapib/" in url and ".js" in url:
                try:
                    body = resp.text()
                    (out / "dapib-live.js").write_text(body, encoding="utf-8", errors="replace")
                    print(f"  saved live dapib ({len(body)} bytes)", flush=True)
                except Exception:
                    pass
            elif "/fc/ca/" in url:
                try:
                    (out / f"ca-response-{len(ca_bodies)}.json").write_text(
                        resp.text(), encoding="utf-8", errors="replace")
                except Exception:
                    pass

        def on_request(req):
            if "/fc/ca/" in req.url and req.method == "POST":
                try:
                    ca_bodies.append({"url": req.url, "body": req.post_data or ""})
                    print("  /fc/ca/ POST captured", flush=True)
                except Exception:
                    pass

        page.on("console", on_console)
        page.on("response", on_response)
        page.on("request", on_request)

        creator._open(page)
        creator._fill(page, make_identity(rng))
        creator._submit(page)

        gate = cap.wait_for_gate(page, timeout=45.0)
        print(f"  gate    -> {gate}", flush=True)
        for attempt in range(3):
            if gate != "press_hold":
                break
            hit = cap.find_press_hold(page)
            if hit:
                cap.press_and_hold(page, rng, hit=hit)
                page.wait_for_timeout(1200)
            if cap.dismiss_challenge(page):
                page.wait_for_timeout(rng.randint(800, 1400))
                try:
                    page.click(SEL_SUBMIT, timeout=8000)
                except Exception:
                    pass
                gate = cap.wait_for_gate(page, timeout=30.0)
                print(f"  gate    -> {gate} (retry {attempt+1})", flush=True)

        if gate == "start_puzzle":
            page.wait_for_timeout(rng.randint(800, 1500))
            cap.wait_for_gate(page, timeout=10.0)

        # wait for the match-game frame to mount, then answer it
        deadline = time.time() + 40.0
        game_frame = None
        while time.time() < deadline:
            page.wait_for_timeout(500)
            for f in page.frames:
                if "match-game" in (f.url or "") or "game-core" in (f.url or ""):
                    game_frame = f
            if game_frame:
                break

        print(f"  frames  -> {[ (f.url or '')[-40:] for f in page.frames]}", flush=True)

        if diagnose:
            for f in page.frames:
                try:
                    info = f.evaluate(r"""() => {
                      const q = s => [...document.querySelectorAll(s)];
                      const vis = e => { const r = e.getBoundingClientRect(); return r.width>4 && r.height>4; };
                      return {
                        url: location.href.slice(0,120),
                        text: (document.body ? document.body.innerText : '').replace(/\s+/g,' ').slice(0,220),
                        imgs: q('img').length,
                        visImgs: q('img').filter(vis).length,
                        buttons: q('button,[role=button]').map(e => ({
                          t: (e.innerText||'').trim().slice(0,24),
                          a: e.getAttribute('aria-label')||'',
                          w: Math.round(e.getBoundingClientRect().width)
                        })).filter(b => b.w > 4).slice(0, 12),
                        roots: q('[id]').map(e=>e.id).filter(Boolean).slice(0,15)
                      };
                    }""")
                except Exception as exc:
                    info = {"error": type(exc).__name__}
                print(f"  FRAME {json.dumps(info)[:700]}", flush=True)
            (out / "frames.json").write_text(json.dumps(
                [{"url": f.url} for f in page.frames], indent=2), encoding="utf-8")

        GAME = r"""() => {
          const byText = t => [...document.querySelectorAll('button,[role=button],a')]
            .find(e => (e.innerText||'').trim() === t);
          const byLabel = l => [...document.querySelectorAll('[aria-label]')]
            .find(e => e.getAttribute('aria-label') === l);
          const click = e => { if (!e) return false; e.click(); return true; };
          return {
            next: click(byLabel('Navigate to next image')),
            match: click(byText('Match This!')),
            submit: click(byText('Submit')),
            text: (document.body.innerText||'').replace(/\s+/g,' ').slice(0,160)
          };
        }"""

        def game_frames():
            return [f for f in page.frames if "game-core" in (f.url or "")]

        clicked = 0
        # 5 waves; answer each with a different carousel index so we exercise
        # several positions, then Submit each one.
        for wave in range(6):
            frames = game_frames()
            if not frames:
                print(f"  wave {wave}: no game frame left", flush=True)
                break
            f = frames[0]
            try:
                for _ in range(wave % 3):
                    f.evaluate(r"""() => {
                      const b=[...document.querySelectorAll('[aria-label]')]
                        .find(e=>e.getAttribute('aria-label')==='Navigate to next image');
                      if(b) b.click();
                    }""")
                    page.wait_for_timeout(250)
                    clicked += 1
                page.wait_for_timeout(300)
                st = f.evaluate(r"""() => {
                  const byText=t=>[...document.querySelectorAll('button,[role=button],a')]
                    .find(e=>(e.innerText||'').trim()===t);
                  const m=byText('Match This!'); if(m) m.click();
                  return {matched:!!m};
                }""")
                page.wait_for_timeout(400)
                st2 = f.evaluate(r"""() => {
                  const byText=t=>[...document.querySelectorAll('button,[role=button],a')]
                    .find(e=>(e.innerText||'').trim()===t);
                  const s=byText('Submit'); if(s) s.click();
                  return {submitted:!!s};
                }""")
                clicked += 1
                print(f"  wave {wave}: match={st.get('matched')} submit={st2.get('submitted')} "
                      f"calls={len(calls)} ca={len(ca_bodies)}", flush=True)
            except Exception as exc:
                print(f"  wave {wave}: {type(exc).__name__}", flush=True)
            page.wait_for_timeout(2600)
            if ca_bodies or calls:
                page.wait_for_timeout(2500)

        page.wait_for_timeout(2000)

    # -- report ---------------------------------------------------------------
    comparisons = verify_against_python(calls)
    payload = {
        "captured_at": stamp,
        "proxy": (proxy.split("@")[-1] if proxy else "direct"),
        "gate": gate,
        "clicked": clicked,
        "dapib_calls": calls,
        "ca_bodies": ca_bodies,
        "comparisons": comparisons,
        "console_tail": console_all[-40:],
    }
    (out / "hook.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print("\n  ---- result ----")
    print(f"  dapibReceive calls : {len(calls)}")
    print(f"  /fc/ca/ requests   : {len(ca_bodies)}")
    for i, (call, comp) in enumerate(zip(calls, comparisons), 1):
        print(f"\n  call {i}: input={json.dumps(call.get('input'))[:180]}")
        print(f"          tanswer={str(call.get('tanswerJson'))[:180]}")
        if comp.get("comparable"):
            print(f"          python match: {comp['match']}")
    if ca_bodies:
        print(f"\n  /fc/ca/ body: {ca_bodies[0]['body'][:400]}")
    print(f"\n  written: {out / 'hook.json'}")
    return 0 if calls else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--proxy-file", type=Path, default=ROOT / "proxiesithink.txt")
    ap.add_argument("--clicks", type=int, default=3)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--diagnose", action="store_true", help="dump frame DOM and stop clicking")
    args = ap.parse_args()

    if args.proxy:
        return run(args.proxy, args.clicks, args.headless, args.diagnose)
    if args.proxy_file.exists():
        from roblox_creator.proxyurl import parse as parse_proxy

        pool = load_proxies(args.proxy_file)
        # sample rather than scan: the list is thousands of long-lived sessions,
        # and any of them is as good as the next until one proves dead
        for raw in random.sample(pool, min(len(pool), 8)):
            p = parse_proxy(raw)
            try:
                from curl_cffi import requests as creq
                s = creq.Session(impersonate="chrome", timeout=15)
                s.proxies = {"http": p.requests_url(), "https": p.requests_url()}
                r = s.get("https://www.roblox.com/", timeout=15)
                s.close()
                if r.status_code == 200:
                    print(f"  live proxy: {p.masked()}  ({len(pool)} in pool)")
                    return run(p.url(), args.clicks, args.headless, args.diagnose)
            except Exception:
                continue
    print("  no live proxy; falling back to direct")
    return run(None, args.clicks, args.headless, args.diagnose)


if __name__ == "__main__":
    raise SystemExit(main())
