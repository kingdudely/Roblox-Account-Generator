#!/usr/bin/env python3
"""Can the game core mount as a top-level page, without the Roblox viewport?

`docs/REPOS.md` records pyProjects3/RobloxPyApi3, which opened a standalone
challenge URL — `/fc/gc/?token=…` — so the browser only had to load the captcha
instead of all of www.roblox.com. That matters here because the Roblox viewport is
~200 assets, and `docs/PROXIES.md` measured only 2 of 18 free exits able to move a
2 MB payload. Most free exits are disqualified by page weight, not by Arkose.

`/fc/gc/` is dead — the host moved from `roblox-api.arkoselabs.com` to
`arkoselabs.roblox.com` (`docs/REPOS.md`, keepitLowkey entry). The modern
equivalent is the game-core document the browser already loads inside an iframe,
opened instead as a top-level page:

    arkoselabs.roblox.com/fc/assets/ec-game-core/game-core/1.40.0/standard/
        index.html?session=..&r=us-east-1&meta=3&pk=..&at=40&ag=101
        &cdn_url=..&surl=..&smurl=..

Two outcomes, both worth having:

  mounts      the browser path stops paying for the Roblox page, and a free exit
              only has to load the captcha. Revisit pool viability.
  needs frame the game core requires a parent, the direct mount is dead, and no
              one should build on it. Recorded so it is not re-tested.

The challenge is IP-bound (`docs/RUNTIME.md`: a signature 403s from a different
exit), so the mint and the browser share one exit: mint browserlessly through the
exit, then launch Chromium through the same one.

    ./tools/mount_direct.py --proxy-file proxies2.txt --exits 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from roblox_creator import payloads
from roblox_creator.arkose_py import Fingerprint, load_fingerprints, pick_fingerprint
from roblox_creator.config import load_proxies
from roblox_creator.proxyurl import parse as parse_proxy
from roblox_creator.socksrelay import SocksRelay
from tools import pure_challenge as pc

OUT = ROOT / "debug" / "mount_direct"

DEFAULT_PK = "A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F"


def game_core_url(session_token: str, public_key: str) -> str:
    """The game-core document, as the iframe would load it."""
    base = (f"{pc.ARKOSE}/fc/assets/ec-game-core/game-core/{pc.GAME_CORE}"
            f"/standard/index.html")
    params = (
        f"session={session_token}"
        "&r=us-east-1"
        "&meta=3"
        "&metabgclr=transparent"
        "&metaiconclr=%23757575"
        "&maintxtclr=%23b8b8b8"
        "&guitextcolor=%23474747"
        f"&pk={public_key}"
        "&at=40"
        "&ag=101"
        f"&cdn_url={quote(pc.ARKOSE + '/cdn/fc', safe='')}"
        f"&surl={quote(pc.ARKOSE, safe='')}"
        f"&smurl={quote(pc.ARKOSE + '/cdn/fc/assets/style-manager', safe='')}"
    )
    return f"{base}?{params}"


def mint(proxy_url: str) -> dict | None:
    """Walk blob -> gt2 -> pows -> gfct on one exit and return what a browser needs."""
    challenge = pc.request_blob(proxy_url)
    if not challenge:
        return None
    session = pc.new_session(proxy_url)

    record = payloads.pick_payload(payloads.load_payloads())
    if record is not None:
        c_blob, ua = payloads.reseal(record)
        minted = pc.gt2_with(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                             c_blob, ua, session=session)
    else:
        fp = Fingerprint.from_raw(pick_fingerprint(load_fingerprints()))
        ua = fp.user_agent
        minted = pc.gt2(proxy_url, challenge["metadata"]["dataExchangeBlob"],
                        fp, session=session)

    token = minted.get("token", "")
    if not token:
        return None
    if not pc.pows(proxy_url, token, ua, session=session).get("ok"):
        return None
    game = pc.gfct(proxy_url, token, ua, session=session)
    if game.get("status") != 200:
        return None

    data = game.get("game_data") or {}
    session_token = game.get("session_token", "")
    if not session_token:
        return None
    public_key = (challenge.get("metadata") or {}).get("publicKey") or DEFAULT_PK
    return {
        "session_token": session_token,
        "public_key": public_key,
        "challenge_url": game.get("challengeURL") or "",
        "waves": data.get("waves"),
        "images": (data.get("customGUI") or {}).get("_challenge_imgs") or [],
        "user_agent": ua,
    }


async def probe(playwright, proxy_raw: str, minted: dict, url: str, timeout: float) -> dict:
    """Open the challenge top-level through the minting exit and report what mounts."""
    from tools.browser_creator import LAUNCH_ARGS, STEALTH_JS

    parsed = parse_proxy(proxy_raw)
    relay = await SocksRelay.start(f"{parsed.host}:{parsed.port}",
                                   parsed.user, parsed.password)
    browser = None
    result = {"exit": parsed.masked(), "url": url, "outcome": "unknown",
              "rtig_ok": 0, "rtig_status": [], "frames": [], "notes": []}
    try:
        browser = await playwright.chromium.launch(headless=False, args=LAUNCH_ARGS)
        context = await browser.new_context(
            proxy={"server": relay.url},
            user_agent=minted["user_agent"],
            viewport={"width": 1366, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
        )
        await context.add_init_script(STEALTH_JS)
        page = await context.new_page()

        console: list[str] = []
        failed: list[str] = []

        def on_response(response):
            if "/rtig/image" in response.url:
                result["rtig_status"].append(response.status)
                if response.status == 200:
                    result["rtig_ok"] += 1
            if response.status >= 400:
                failed.append(f"{response.status} {response.url[:90]}")

        def on_console(message):
            console.append(f"{message.type}: {message.text[:120]}")

        page.on("response", on_response)
        page.on("console", on_console)
        page.on("pageerror", lambda exc: console.append(f"pageerror: {str(exc)[:120]}"))

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        except Exception as exc:
            result["outcome"] = f"goto-failed {type(exc).__name__}"
            return result

        # Give the game core time to boot and either draw a challenge or fail.
        deadline = timeout
        waited = 0.0
        while waited < deadline:
            await page.wait_for_timeout(1000)
            waited += 1.0
            buttons = []
            for button in await page.query_selector_all("button"):
                try:
                    buttons.append(((await button.inner_text()) or "").strip().lower())
                except Exception:
                    pass
            if any("start" in label for label in buttons):
                result["outcome"] = "mounts"
                result["notes"].append(f"start button at {waited:.0f}s")
                # Click through so the image fetch is exercised.
                for button in await page.query_selector_all("button"):
                    label = ((await button.inner_text()) or "").strip().lower()
                    if "start" in label:
                        try:
                            await button.click()
                        except Exception:
                            pass
                        break
                await page.wait_for_timeout(8000)
                break
            if any("hold" in label for label in buttons):
                result["outcome"] = "mounts-hold"
                result["notes"].append(f"hold rung at {waited:.0f}s")
                break

        if result["outcome"] == "unknown":
            result["outcome"] = "no-challenge"
            try:
                body = (await page.inner_text("body"))[:200].replace("\n", " ")
            except Exception:
                body = ""
            try:
                root = await page.evaluate(
                    "() => { const r = document.getElementById('root');"
                    " return r ? r.innerHTML.length : -1; }")
            except Exception:
                root = -1
            result["notes"].append(f"body: {body!r}")
            result["notes"].append(f"#root innerHTML length: {root}")
            result["notes"].extend(f"console {line}" for line in console[:8])
            result["notes"].extend(f"http {line}" for line in failed[:8])

        result["frames"] = [f.url for f in page.frames if f.url and f.url != "about:blank"]
        (OUT / "page.png").parent.mkdir(parents=True, exist_ok=True)
        try:
            await page.screenshot(path=str(OUT / "page.png"))
        except Exception:
            pass
        return result
    except Exception as exc:
        result["outcome"] = f"error {type(exc).__name__}: {str(exc)[:80]}"
        return result
    finally:
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        await relay.close()


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", type=Path, action="append", default=None)
    ap.add_argument("--exits", type=int, default=4)
    ap.add_argument("--timeout", type=float, default=45.0)
    ap.add_argument("--mode", default="challenge", choices=("challenge", "game-core"),
                    help="challenge: the server's challengeURL + session; "
                         "game-core: the ec-game-core document the iframe loads")
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    files = args.proxy_file or [ROOT / "proxies2.txt"]
    proxies: list[str] = []
    for path in files:
        proxies.extend(load_proxies(path))
    random.shuffle(proxies)

    OUT.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    minted_count = 0

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        for raw in proxies:
            if minted_count >= args.exits:
                break
            try:
                parsed = parse_proxy(raw)
            except Exception:
                continue
            print(f"\nexit {parsed.masked()}", flush=True)
            try:
                minted = await asyncio.to_thread(mint, parsed.url())
            except Exception as exc:
                print(f"  mint error {type(exc).__name__}", flush=True)
                continue
            if not minted:
                print("  mint failed", flush=True)
                continue
            if not minted["images"]:
                print("  no image URL in this challenge", flush=True)
                continue
            minted_count += 1
            print(f"  minted waves={minted['waves']}"
                  f"  challenge_url={minted['challenge_url'][:60]}", flush=True)
            if args.mode == "challenge" and minted["challenge_url"]:
                url = f"{minted['challenge_url']}?session={minted['session_token']}"
            else:
                url = game_core_url(minted["session_token"], minted["public_key"])
            row = await probe(playwright, raw, minted, url, args.timeout)
            row["mode"] = args.mode
            rows.append(row)
            print(f"  outcome: {row['outcome']}  rtig_ok={row['rtig_ok']}"
                  f"  statuses={row['rtig_status']}", flush=True)
            for note in row["notes"]:
                print(f"    {note}", flush=True)

    stamp = __import__("time").strftime("%Y%m%d-%H%M%S")
    target = args.out or (OUT / f"{stamp}.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"stamp": stamp, "rows": rows}, indent=1))

    print("\n--- result ---", flush=True)
    for row in rows:
        print(f"  {row['outcome']:<16} rtig_ok={row['rtig_ok']}  {row['exit'][:44]}", flush=True)
    print(f"\nsaved {target}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
