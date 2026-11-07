#!/usr/bin/env python3
"""Roblox account creator — CLI entry point."""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from roblox_creator import egress
from roblox_creator.browser import BrowserSession
from roblox_creator.config import Settings, load_proxies
from roblox_creator.keypool import load_keys
from roblox_creator.creator import Creator, summarize
from roblox_creator.proxy_pool import ProxyPool
from roblox_creator.store import AccountStore


def mask(url: str) -> str:
    if "@" in url:
        scheme = url.split("://")[0] if "://" in url else "http"
        return f"{scheme}://***@{url.rsplit('@', 1)[1]}"
    return url


def check_proxies(proxies: list[str], workers: int = 8) -> int:
    """Audit a proxy list before spending accounts on it."""

    def one(p: str):
        e = egress.probe(p, timeout=20)
        ok, reasons = egress.verdict(e)
        return p, e, ok, reasons

    print(f"probing {len(proxies)} proxies...\n")
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(proxies)))) as ex:
        for p, e, ok, reasons in ex.map(one, proxies):
            rows.append((p, e, ok))
            mark = "OK " if ok else "BAD"
            print(f"  {mark} {mask(p):<44} {e.ip or '-':<16} {e.country or '-':<3} "
                  f"{(e.timezone or '-')[:19]:<20} {e.label}")
            for r in reasons:
                print(f"        {r}")

    good = sum(1 for _, _, ok in rows if ok)
    print(f"\n  {good}/{len(rows)} usable")
    if good < len(rows):
        print("  the BAD ones will be challenged on every signup — drop or replace them")
    return 0 if good else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run.py",
        description="Create Roblox accounts through a real Chrome session.",
    )
    p.add_argument("-n", "--count", type=int, default=1, help="how many accounts to attempt")
    p.add_argument("--captcha", choices=["auto", "vision", "api", "manual", "off"], default="auto",
                   help="auto = vision if a model is configured, else api if a key is set, else manual")
    p.add_argument("--provider", choices=["capsolver", "2captcha", "solvex"], default="capsolver")
    p.add_argument("--key", default=None, help="solver API key (or CAPSOLVER_KEY / TWOCAPTCHA_KEY env)")
    p.add_argument("--vision-provider", choices=["gemini", "local", "stub", "file", "onnx"], default=None,
                   help="in-UI solver: gemini API, a local VLM, or stub (plumbing test)")
    p.add_argument("--vision-model", default=None, help="gemini-2.5-flash | qwen2.5vl:3b | ...")
    p.add_argument("--vision-base-url", default=None,
                   help="local OpenAI-compatible endpoint, e.g. http://localhost:11434/v1")
    p.add_argument("--vision-key", default=None, help="gemini key (or GEMINI_API_KEY env)")
    p.add_argument("--vision-keys", default=None,
                   help="file with one Gemini key per line, each from its own project")
    p.add_argument("--headless", action="store_true", help="run without a window (more detectable)")
    p.add_argument("--browser", default=None, help="path to chrome executable")
    p.add_argument("--proxy", action="append", default=[], help="host:port[:user:pass] — repeatable")
    p.add_argument("--proxy-file", default=None, help="file with one proxy per line")
    p.add_argument("--check-proxies", action="store_true",
                   help="audit the proxy list (ASN, country, timezone) and exit")
    p.add_argument("--allow-bad-egress", action="store_true",
                   help="run even on WARP/datacenter egress (dev only)")
    p.add_argument("--delay", type=float, nargs=2, metavar=("MIN", "MAX"), default=(6.0, 18.0),
                   help="seconds to wait between accounts")
    p.add_argument("--out", default="accounts", help="output directory")
    p.add_argument("--profiles", default="profiles", help="profile root directory")
    p.add_argument("--reuse-context", action="store_true", help="reuse one browser profile for all accounts")
    p.add_argument("--timeout", type=float, default=45.0, help="per-action timeout")
    p.add_argument("--dry-run", action="store_true", help="fill the form, do not submit")
    p.add_argument("--no-verify", action="store_true", help="skip the authenticated-user check")
    p.add_argument("--no-press-hold", action="store_true",
                   help="do not attempt the synthetic press-and-hold behavioural gate")
    return p


def collect_proxies(args) -> list[str]:
    proxies = list(args.proxy)
    if args.proxy_file:
        proxies += load_proxies(Path(args.proxy_file))
    return proxies


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    proxies = collect_proxies(args)

    # -- audit-only mode ------------------------------------------------------
    if args.check_proxies:
        if not proxies:
            print("no proxies given — use --proxy or --proxy-file")
            return 2
        return check_proxies(proxies)

    # -- egress preflight -----------------------------------------------------
    if not proxies:
        direct = egress.probe()
        ok, reasons = egress.verdict(direct)
        print(f"direct egress: {direct.describe()}")
        if not ok:
            for reason in reasons:
                print(f"  ! {reason}")
            if not args.allow_bad_egress:
                print("\nrefusing to run on this egress.")
                print("  point it at clean proxies:  --proxy-file proxies.txt")
                print("  or audit first:             --check-proxies --proxy-file proxies.txt")
                print("  override for dev only:      --allow-bad-egress")
                return 2
            print("  continuing anyway (--allow-bad-egress)")

    settings = Settings.from_env(
        count=args.count,
        captcha_mode=args.captcha,
        captcha_provider=args.provider,
        captcha_key=args.key,
        vision_provider=args.vision_provider,
        vision_model=args.vision_model or "",
        vision_base_url=args.vision_base_url or "",
        vision_key=args.vision_key,
        vision_keys=load_keys(args.vision_keys or os.getenv("GEMINI_API_KEYS")),
        headless=args.headless,
        executable_path=args.browser,
        delay_min=args.delay[0],
        delay_max=args.delay[1],
        out_dir=Path(args.out),
        profile_root=Path(args.profiles),
        proxies=proxies,
        reuse_context=args.reuse_context,
        action_timeout=args.timeout,
        dry_run=args.dry_run,
        verify=not args.no_verify,
        solve_press_hold=not args.no_press_hold,
    )

    store = AccountStore(settings.out_dir)
    pool = ProxyPool(proxies) if proxies else None
    mode = settings.effective_captcha_mode
    print(f"mode={mode} provider={settings.captcha_provider} accounts={settings.count} "
          f"proxies={len(settings.proxies)} headless={settings.headless}")

    if mode == "manual":
        print("captcha=manual — keep the window visible; solve when the challenge appears")

    records = []
    rng = random.Random()

    try:
        with BrowserSession(settings) as session:
            creator = Creator(settings, store, proxy_pool=pool)
            for index in range(settings.count):
                print(f"[{index + 1}/{settings.count}] creating...")
                record = creator.create_one(session, index)
                records.append(record)
                flag = "ok " if record.status == "created" else "ERR"
                print(f"    {flag} {record.username} -> {record.status}"
                      + (f" ({record.note[:120]})" if record.note else ""))

                if index + 1 < settings.count:
                    pause = rng.uniform(settings.delay_min, settings.delay_max)
                    print(f"    waiting {pause:.1f}s")
                    time.sleep(pause)
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        if records:
            print()
            print(summarize(records))
        if pool:
            print(pool.stats())
        if any(r.status == "created" for r in records):
            print(f"\ncredentials: {store.credentials_file()}")
            print(f"cookies:     {store.cookies_file()}")

    return 0 if any(r.status == "created" for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
