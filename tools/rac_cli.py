#!/usr/bin/env python3
"""Patchright CLI companion for Roblox Alt Creator.

This is deliberately a separate, debuggable browser runner. It opens the same
pages the extension's setup flow uses, in a persistent Chrome profile, with
console/page-error logging and screenshots. It does not bypass Google's phone
or anti-abuse checks; it pauses there and leaves the page open for a human.

Usage:
    python3 tools/rac_cli.py doctor
    python3 tools/rac_cli.py smoke --headless
    python3 tools/rac_cli.py admin
    python3 tools/rac_cli.py duckdns
    python3 tools/rac_cli.py signup
    python3 tools/rac_cli.py open https://example.com --wait 5

Install:
    python3 -m venv .venv
    .venv/bin/pip install -r tools/requirements-cli.txt
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from importlib import metadata
from pathlib import Path

try:
    import patchright
    from patchright.sync_api import Error as PatchrightError
    from patchright.sync_api import sync_playwright
except ImportError:  # pragma: no cover - the user gets a useful message
    patchright = None
    PatchrightError = Exception
    sync_playwright = None


APP_HOME = Path(
    os.environ.get(
        "RAC_HOME", Path.home() / ".local" / "share" / "roblox-alt-creator"
    )
).expanduser()
DEFAULT_PROFILE = APP_HOME / "patchright-profile"
DEBUG_DIR = Path.cwd() / "debug"

URLS = {
    "admin": "https://admin.google.com/",
    "duckdns": "https://www.duckdns.org/",
    "signup": "https://workspace.google.com/gcpidentity/signup?sku=identitybasic",
    "keys": "https://console.cloud.google.com/apis/credentials",
    "consent": "https://console.cloud.google.com/apis/credentials/consent",
}


def find_chrome() -> str | None:
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        path = shutil.which(name)
        if path:
            return path
    return None


def fail(message: str, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code


def require_patchright() -> int | None:
    if sync_playwright is not None:
        return None
    return fail(
        "patchright is not installed. Run:\n"
        "  python3 -m venv .venv\n"
        "  .venv/bin/pip install -r tools/requirements-cli.txt"
    )


def make_launcher(args):
    chrome = find_chrome()
    profile = Path(args.profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)

    options = {
        "user_data_dir": str(profile),
        "headless": bool(args.headless),
        # A persistent, normal-sized window is the point of this runner.
        "no_viewport": True,
    }
    if args.channel and args.channel != "none":
        options["channel"] = args.channel
    elif not chrome:
        print(
            "warning: no system Chrome found; Patchright will use its bundled "
            "Chromium (run `patchright install chromium` if it is missing).",
            file=sys.stderr,
        )

    if args.proxy:
        proxy = {"server": args.proxy}
        if args.proxy_user:
            proxy["username"] = args.proxy_user
        if args.proxy_pass:
            proxy["password"] = args.proxy_pass
        options["proxy"] = proxy

    return options, profile, chrome


def attach_logging(page, args):
    if not args.log_console:
        return

    def console(message):
        text = message.text
        if len(text) > 1200:
            text = text[:1200] + "…"
        print(f"[console:{message.type}] {text}")

    def page_error(error):
        print(f"[pageerror] {error}")

    page.on("console", console)
    page.on("pageerror", page_error)


def open_page(args, url: str):
    missing = require_patchright()
    if missing is not None:
        return missing

    options, profile, chrome = make_launcher(args)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)

    print(f"profile: {profile}")
    print(f"chrome:  {chrome or 'bundled chromium'}")
    print(f"headless: {bool(args.headless)}")
    print(f"url:     {url}")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(**options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                attach_logging(page, args)
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(1200)
                print(f"title:   {page.title()}")
                print(f"landed:  {page.url}")

                if args.screenshot:
                    target = Path(args.screenshot).expanduser()
                    if not target.is_absolute():
                        target = DEBUG_DIR / target
                    target.parent.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(target), full_page=True)
                    print(f"shot:    {target}")

                if args.wait:
                    print(f"waiting {args.wait}s…")
                    page.wait_for_timeout(int(args.wait * 1000))
                    return 0

                print("the browser is open — press Ctrl+C to close it")
                try:
                    while True:
                        time.sleep(0.4)
                except KeyboardInterrupt:
                    print("\nclosing")
                return 0
            finally:
                context.close()
    except PatchrightError as error:
        return fail(f"patchright/browser error: {error}")


def command_doctor(args):
    print(f"python:      {sys.version.split()[0]}")
    print(f"executable:  {sys.executable}")
    try:
        version = metadata.version("patchright") if patchright else "missing"
    except metadata.PackageNotFoundError:
        version = "unknown"
    print(f"patchright:  {version}")
    print(f"chrome:      {find_chrome() or 'not found'}")
    print(f"profile:     {Path(args.profile).expanduser().resolve()}")
    print(f"debug dir:   {DEBUG_DIR}")
    print(f"display:     {os.environ.get('DISPLAY', '(none)')}")
    print(f"headless:    {bool(args.headless)}")

    missing = require_patchright()
    if missing is not None:
        return missing

    options, profile, _ = make_launcher(args)
    print(f"launching a smoke browser with {profile}…")
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(**options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("about:blank", timeout=30_000)
                print(
                    json.dumps(
                        {
                            "ok": True,
                            "navigator.webdriver": page.evaluate("navigator.webdriver"),
                            "userAgent": page.evaluate("navigator.userAgent"),
                        },
                        indent=2,
                    )
                )
            finally:
                context.close()
    except PatchrightError as error:
        return fail(f"browser launch failed: {error}")
    return 0


def command_smoke(args):
    missing = require_patchright()
    if missing is not None:
        return missing

    options, profile, chrome = make_launcher(args)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    print(f"profile: {profile}")
    print(f"chrome:  {chrome or 'bundled chromium'}")
    print(f"headless: {bool(args.headless)}")

    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(**options)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                attach_logging(page, args)
                # A data page makes this test independent of network and anti-bot
                # challenges; the point is the input pipeline, not the target.
                page.goto(
                    "data:text/html,"
                    "<title>Patchright smoke</title>"
                    "<h1>Patchright smoke</h1>"
                    "<button id='b' style='width:180px;height:48px'>Click me</button>",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                page.evaluate(
                    """() => {
                        window.__racTrusted = null;
                        document.addEventListener(
                            'click',
                            (event) => { window.__racTrusted = event.isTrusted; },
                            { once: true, capture: true }
                        );
                    }"""
                )
                box = page.locator("#b").bounding_box()
                if box:
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    page.mouse.click(20, 20)
                page.wait_for_timeout(300)
                result = {
                    "url": page.url,
                    "title": page.title(),
                    "navigator.webdriver": page.evaluate("navigator.webdriver"),
                    "trusted_click": page.evaluate("window.__racTrusted"),
                    "userAgent": page.evaluate("navigator.userAgent"),
                }
                print(json.dumps(result, indent=2))

                shot = DEBUG_DIR / "patchright-smoke.png"
                page.screenshot(path=str(shot), full_page=True)
                print(f"screenshot: {shot}")
                return 0
            finally:
                context.close()
    except PatchrightError as error:
        return fail(f"patchright/browser error: {error}")


def command_open(args):
    url = args.url
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
    return open_page(args, url)


def command_named(args):
    return open_page(args, URLS[args.command])


def add_common(parser, headless_default=False):
    parser.add_argument(
        "--profile",
        default=str(DEFAULT_PROFILE),
        help=f"persistent browser profile (default: {DEFAULT_PROFILE})",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        default=headless_default,
        help="run headless (headful is the default and is much less detectable)",
    )
    parser.add_argument(
        "--channel",
        default="chrome",
        help='Chrome channel (default: chrome; use "none" for bundled Chromium)',
    )
    parser.add_argument("--proxy", help="proxy server, e.g. socks5://host:1080")
    parser.add_argument("--proxy-user", help="proxy username")
    parser.add_argument("--proxy-pass", help="proxy password")
    parser.add_argument(
        "--log-console",
        action="store_true",
        help="print page console messages and page errors",
    )


def add_wait_flags(parser):
    parser.add_argument(
        "--wait",
        type=float,
        default=0,
        help="seconds to keep the page open, then exit (default: stay open)",
    )
    parser.add_argument(
        "--screenshot",
        default="",
        help="save a full-page screenshot to debug/ (or an absolute path)",
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Patchright CLI companion for Roblox Alt Creator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "This tool automates the browser UI and leaves phone/captcha "
            "verification to a human. It is a debugging companion, not an "
            "anti-detection guarantee."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check Python, Patchright and browser launch")
    add_common(doctor, headless_default=True)
    doctor.set_defaults(func=command_doctor)

    smoke = sub.add_parser(
        "smoke", help="verify Patchright launches and produces trusted input"
    )
    add_common(smoke, headless_default=True)
    smoke.set_defaults(func=command_smoke)

    admin = sub.add_parser("admin", help="open the Google Admin console")
    add_common(admin)
    add_wait_flags(admin)
    admin.set_defaults(func=command_named)

    duckdns = sub.add_parser("duckdns", help="open DuckDNS")
    add_common(duckdns)
    add_wait_flags(duckdns)
    duckdns.set_defaults(func=command_named)

    signup = sub.add_parser("signup", help="open the Cloud Identity signup")
    add_common(signup)
    add_wait_flags(signup)
    signup.set_defaults(func=command_named)

    open_cmd = sub.add_parser("open", help="open an arbitrary URL")
    open_cmd.add_argument("url")
    add_common(open_cmd)
    add_wait_flags(open_cmd)
    open_cmd.set_defaults(func=command_open)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
