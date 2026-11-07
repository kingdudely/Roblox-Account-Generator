"""Run the production Creator end to end and report what it drew.

The form module, the hold gate and `Creator` are now wired together; this is the
smoke test that proves the assembled path runs. It uses the real `Creator`, so a
success here means the product loop can create accounts, not just that a probe
can drive a page.

    PYTHONPATH=. .venv/bin/python tools/create_probe.py --no-proxy --count 2
    PYTHONPATH=. .venv/bin/python tools/create_probe.py --proxy-file proxies.txt --count 5
"""

from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from roblox_creator.browser import BrowserSession
from roblox_creator.config import Settings, load_proxies
from roblox_creator.creator import Creator
from roblox_creator.store import AccountStore

OUT = Path("debug/create_probe")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy-file", default="proxies.txt")
    ap.add_argument("--no-proxy", action="store_true")
    ap.add_argument("--count", type=int, default=2)
    ap.add_argument("--captcha", default="auto",
                    choices=("auto", "off", "vision", "api", "manual"))
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = OUT / stamp
    out.mkdir(parents=True, exist_ok=True)

    settings = Settings(headless=False)
    settings.captcha = args.captcha
    store = AccountStore(out)

    pool = None
    if not args.no_proxy:
        proxies = load_proxies(Path(args.proxy_file))
        random.shuffle(proxies)
        settings_pool = proxies
    else:
        settings_pool = [None]

    with BrowserSession(settings) as session:
        creator = Creator(settings, store, proxy_pool=None)
        for index, raw in enumerate(settings_pool[: args.count], 1):
            print(f"\n[{index}/{args.count}] {'direct' if raw is None else 'proxy'}",
                  flush=True)
            record = creator._create_with(session, index, raw) \
                if hasattr(creator, "_create_with") else None
            if record is None:
                # no dedicated entry point: mirror create_one with an explicit proxy
                record = create_with(session, creator, index, raw)
            print(f"  status={record.status} captcha={record.captcha!r} "
                  f"note={(record.note or '')[:90]}", flush=True)

    print(f"\nwrote {out}")


def create_with(session: BrowserSession, creator: Creator, index: int,
                raw: str | None):
    """create_one, but with the exit chosen by the caller instead of the pool."""
    from roblox_creator.creator import CreateError
    from roblox_creator.identity import make_identity
    from roblox_creator.store import AccountRecord

    identity = make_identity(creator.rng)
    record = AccountRecord(
        username=identity.username,
        password=identity.password,
        birthday=identity.birthday_display,
        gender=identity.gender,
        proxy=raw,
    )
    ctx = None
    try:
        ctx = session.new_context(proxy=raw)
        page = session.new_page(ctx)
        creator._run(page, ctx, identity, record, raw)
    except CreateError as exc:
        record.status = exc.reason
        record.note = exc.detail[:400]
    except Exception as exc:  # noqa: BLE001 - one bad account must not kill the run
        record.status = "failed"
        record.note = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass
    creator.store.append(record)
    return record


if __name__ == "__main__":
    main()
