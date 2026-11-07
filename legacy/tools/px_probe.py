"""Land on PX and dump everything about the modal: DOM, buttons, progress over a hold."""
import json
import pathlib
import random
import time

from roblox_creator.browser import BrowserSession
from roblox_creator.config import Settings
from roblox_creator.creator import Creator
from roblox_creator.store import AccountStore
from roblox_creator.identity import make_identity
from roblox_creator import captcha as cap

OUT = pathlib.Path("debug/px")
OUT.mkdir(parents=True, exist_ok=True)
proxies = [l.strip() for l in pathlib.Path("proxiesithink.txt").read_text().splitlines() if l.strip()]

INFO = r"""() => {
  const walk=(r,o)=>{for(const e of r.querySelectorAll('*')){o.push(e); if(e.shadowRoot) walk(e.shadowRoot,o);} return o;};
  const els=walk(document,[]);
  const btn=els.find(e=>/hold/i.test(((e.getAttribute&&e.getAttribute('aria-label'))||'')+((e.innerText||''))));
  const p=els.find(e=>e.getAttribute&&e.getAttribute('role')==='progressbar');
  const closers=els.filter(e=>/close/i.test(((e.getAttribute&&(e.getAttribute('aria-label')||e.getAttribute('title')))||'')) || (e.tagName==='BUTTON' && /×|✕|x/i.test((e.innerText||'').trim())));
  return {
    text:(document.body.innerText||'').replace(/\n+/g,' | ').slice(0,200),
    html:document.documentElement.outerHTML.slice(0,2500),
    btn: btn ? {tag:btn.tagName, aria:btn.getAttribute('aria-label'), cls:(typeof btn.className==='string'?btn.className:'').slice(0,60), html:btn.outerHTML.slice(0,300)} : null,
    prog: p ? {now:p.getAttribute('aria-valuenow'), max:p.getAttribute('aria-valuemax')} : null,
    closers: closers.map(e=>({tag:e.tagName, aria:e.getAttribute&&e.getAttribute('aria-label'), title:e.getAttribute&&e.getAttribute('title'), text:(e.innerText||'').trim().slice(0,20), html:e.outerHTML.slice(0,150)}))
  };
}"""

for attempt in range(5):
    s = Settings(headless=False)
    cr = Creator(s, AccountStore(pathlib.Path(f"/tmp/pxd{attempt}")))
    proxy = proxies[(attempt + 20) % len(proxies)]
    try:
        with BrowserSession(s) as sess:
            ctx = sess.new_context(proxy=proxy)
            page = sess.new_page(ctx)
            cr._open(page)
            cr._fill(page, make_identity(random.Random()))
            cr._submit(page)
            gate = cap.wait_for_gate(page, timeout=40)
            print(f"[try {attempt}] gate={gate}", flush=True)
            if gate != "press_hold":
                page.wait_for_timeout(2500)
                continue
            hit = cap.find_press_hold(page)
            frame = hit["frame"]
            page.wait_for_timeout(800)
            d = frame.evaluate(INFO)
            print(" text:", d["text"], flush=True)
            print(" btn:", json.dumps(d["btn"])[:300], flush=True)
            print(" closers:", json.dumps(d["closers"])[:600], flush=True)
            (OUT / "px_frame.html").write_text(d["html"])
            print(" html head:", d["html"][:700].replace("\n", " "), flush=True)

            x, y = hit["x"], hit["y"]
            page.mouse.move(x - 4, y - 3, steps=6)
            page.wait_for_timeout(140)
            page.mouse.move(x, y, steps=3)
            page.wait_for_timeout(80)
            page.mouse.down()
            t0 = time.time()
            last = None
            while time.time() - t0 < 8:
                page.mouse.move(x + random.uniform(-1.2, 1.2), y + random.uniform(-1.0, 1.0), steps=3)
                page.wait_for_timeout(16)
                st = frame.evaluate(INFO)
                sig = (st["prog"]["now"] if st["prog"] else None, st["btn"] is not None, st["text"][:40])
                if sig != last:
                    print(f"  {time.time()-t0:4.1f}s prog={sig[0]} btn={sig[1]} text={st['text'][:60]!r}", flush=True)
                    last = sig
            page.mouse.up()
            page.wait_for_timeout(1500)
            after = frame.evaluate(INFO)
            print(" after release:", after["text"][:120], "| btn:", bool(after["btn"]), flush=True)
            page.screenshot(path=str(OUT / "px_dump.png"))
            break
    except Exception as exc:
        print("try fail:", type(exc).__name__, str(exc)[:100], flush=True)
