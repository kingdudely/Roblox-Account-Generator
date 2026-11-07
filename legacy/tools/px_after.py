"""Hold until Roblox says "completed", release immediately, then watch what happens."""
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

STATE = r"""() => {
  const t = (document.body.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 140);
  return {
    text: t,
    completed: /completed/i.test(t),
    failed: /try again|failed|error/i.test(t),
    check: !!document.querySelector('#checkmark.draw'),
    id: (document.querySelector('[id]') || {}).id || ''
  };
}"""


def main() -> None:
    for attempt in range(5):
        s = Settings(headless=False)
        cr = Creator(s, AccountStore(pathlib.Path(f"/tmp/pxa{attempt}")))
        proxy = proxies[(attempt + 30) % len(proxies)]
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
                x, y = hit["x"], hit["y"]
                page.wait_for_timeout(500)

                page.mouse.move(x - 4, y - 3, steps=6)
                page.wait_for_timeout(140)
                page.mouse.move(x, y, steps=3)
                page.wait_for_timeout(80)
                page.mouse.down()

                t0 = time.time()
                released_at = None
                while time.time() - t0 < 12:
                    page.mouse.move(x + random.uniform(-1.1, 1.1),
                                    y + random.uniform(-0.9, 0.9), steps=3)
                    page.wait_for_timeout(40)
                    st = frame.evaluate(STATE)
                    if st["completed"] and (time.time() - t0) > 1.2:
                        released_at = time.time() - t0
                        print(f"  completed text at {released_at:.2f}s -> {st['text'][:80]!r}", flush=True)
                        break
                    if st["failed"]:
                        print(f"  FAILED at {time.time()-t0:.2f}s -> {st['text'][:80]!r}", flush=True)
                        break
                page.mouse.up()
                print(f"  released at {time.time()-t0:.2f}s", flush=True)

                for i in range(20):
                    page.wait_for_timeout(1000)
                    try:
                        st = frame.evaluate(STATE)
                        text = st["text"][:70]
                    except Exception:
                        text = "<frame gone>"
                    cookie = Creator._roblosecurity(ctx)
                    ark = [f.url[:40] for f in page.frames if "arkose" in (f.url or "")]
                    print(f"  +{i+1}s cookie={bool(cookie)} arkose={len(ark)} url={page.url[:52]} "
                          f"text={text!r}", flush=True)
                    if cookie:
                        (OUT / "win.txt").write_text(cookie)
                        print("  COOKIE CAPTURED", flush=True)
                        return
                    if ark:
                        break
                page.screenshot(path=str(OUT / "px_after2.png"))
                return
        except Exception as exc:
            print("try fail:", type(exc).__name__, str(exc)[:100], flush=True)


if __name__ == "__main__":
    main()
