"""Dump the full PX challenge document + its scripts so the verifier can be read."""
import pathlib
import random

from roblox_creator import captcha as cap
from roblox_creator.browser import BrowserSession
from roblox_creator.config import Settings
from roblox_creator.creator import Creator
from roblox_creator.identity import make_identity
from roblox_creator.store import AccountStore

OUT = pathlib.Path("debug/px")
OUT.mkdir(parents=True, exist_ok=True)
proxies = [l.strip() for l in pathlib.Path("proxiesithink.txt").read_text().splitlines() if l.strip()]

DUMP = r"""() => {
  const txt = s => { try { return s || ''; } catch (e) { return ''; } };
  return {
    html: document.documentElement.outerHTML,
    scripts: [...document.querySelectorAll('script')].map(s => ({
      src: s.src, type: s.type, len: (s.textContent || '').length,
      text: txt(s.textContent).slice(0, 40000)
    })),
    links: [...document.querySelectorAll('link')].map(l => l.href),
    iframes: [...document.querySelectorAll('iframe')].map(f => ({src: f.src, srcdoc: (f.srcdoc||'').slice(0,500)}))
  };
}"""


def main() -> None:
    for attempt in range(6):
        s = Settings(headless=False)
        cr = Creator(s, AccountStore(pathlib.Path(f"/tmp/pxdump{attempt}")))
        proxy = proxies[(attempt + 40) % len(proxies)]
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
                    page.wait_for_timeout(1500)
                    continue
                for frame in page.frames:
                    try:
                        d = frame.evaluate(DUMP)
                    except Exception:
                        continue
                    if "Press and hold" not in (d["html"] or ""):
                        continue
                    (OUT / "full.html").write_text(d["html"])
                    (OUT / "scripts.txt").write_text(
                        "\n\n".join(f"### src={x['src']} type={x['type']} len={x['len']}\n{x['text']}"
                                    for x in d["scripts"]))
                    (OUT / "meta.json").write_text(
                        __import__("json").dumps({"links": d["links"], "iframes": d["iframes"]}, indent=2))
                    print("saved html", len(d["html"]), "scripts", len(d["scripts"]), flush=True)
                    return
        except Exception as exc:
            print("try fail:", type(exc).__name__, str(exc)[:100], flush=True)


if __name__ == "__main__":
    main()
