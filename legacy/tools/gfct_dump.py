"""Capture the Arkose gfct JSON: variant, gameType, waves, instruction."""
import json
import pathlib
import random

from roblox_creator import captcha as cap
from roblox_creator.browser import BrowserSession
from roblox_creator.config import Settings
from roblox_creator.creator import Creator
from roblox_creator.identity import make_identity
from roblox_creator.store import AccountStore

OUT = pathlib.Path("debug/gfct")
OUT.mkdir(parents=True, exist_ok=True)
proxies = [l.strip() for l in pathlib.Path("proxiesithink.txt").read_text().splitlines() if l.strip()]

seen = []


def main() -> None:
    for attempt in range(6):
        s = Settings(headless=False)
        cr = Creator(s, AccountStore(pathlib.Path(f"/tmp/gfct{attempt}")))
        proxy = proxies[(attempt + 50) % len(proxies)]
        try:
            with BrowserSession(s) as sess:
                ctx = sess.new_context(proxy=proxy)
                page = sess.new_page(ctx)

                def on_response(resp):
                    u = resp.url
                    if "/fc/gfct/" not in u and "/fc/gt2/" not in u:
                        return
                    try:
                        body = resp.text()
                    except Exception:
                        return
                    seen.append((u.split("?")[0], resp.status, body))
                    (OUT / f"resp-{len(seen)}.json").write_text(body)
                    print("CAPTURED", u.split("?")[0], resp.status, len(body), flush=True)

                ctx.on("response", on_response)

                cr._open(page)
                cr._fill(page, make_identity(random.Random()))
                cr._submit(page)
                gate = cap.wait_for_gate(page, timeout=45)
                print(f"[try {attempt}] gate={gate}", flush=True)
                if gate == "start_puzzle":
                    page.wait_for_timeout(1500)
                    gate = cap.wait_for_gate(page, timeout=15)
                    print(f"  after start: gate={gate}", flush=True)
                if gate == "press_hold":
                    hit = cap.find_press_hold(page)
                    if hit:
                        cap.press_and_hold(page, cr.rng, hit=hit)
                        page.wait_for_timeout(1500)
                page.wait_for_timeout(4000)
                if seen:
                    break
        except Exception as exc:
            print("try fail:", type(exc).__name__, str(exc)[:120], flush=True)

    for name, status, body in seen:
        print("=" * 60)
        print(name, status)
        try:
            data = json.loads(body)
            print(json.dumps(data, indent=1)[:4000])
        except Exception:
            print(body[:1500])


if __name__ == "__main__":
    main()
