#!/usr/bin/env python3
"""Record what the admin console sends, so a UI-only operation can become a call.

Some Google admin operations have no public API — cancelling a customer account
is one. This tool exists to answer "what did the console actually send?" by
attaching to a live session and logging every request/response that touches an
admin or identity host, with bodies.

    ./tools/admin_account_recorder.py --mode probe
    ./tools/admin_account_recorder.py --mode probe --url https://admin.google.com/ac/accountsettings
    ./tools/admin_account_recorder.py --mode record --click "Delete account"

`probe` lists what is clickable in the account-management area so the control can
be named precisely before anything irreversibly useful happens. `record` clicks
one control by accessible name and writes the transcript.

Every transcript lands in debug/admin_account/<stamp>/ and is replayable: method,
url, headers, body in; status, headers, body out.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright

from tools.admin_add_users import LAUNCH_ARGS

# hosts worth keeping. everything else (fonts, images, analytics) is noise that
# buries the two or three calls that matter.
INTERESTING = ("admin.google.com", "googleapis.com", "googleusercontent.com",
               "accounts.google.com", "console.cloud.google.com",
               "cloudresourcemanager.googleapis.com")

# headers that carry meaning for Google's own endpoints
KEEP_HEADERS = ("content-type", "x-goog-authuser", "x-goog-apiary-method-id",
                "x-same-domain", "authorization", "x-client-data")


class Recorder:
    def __init__(self, path: Path):
        self.path = path
        self.started = time.time()

    def _keep(self, url: str) -> bool:
        return any(host in url for host in INTERESTING)

    def _write(self, row: dict) -> None:
        row["t"] = round(time.time() - self.started, 3)
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")

    def on_request(self, req) -> None:
        if not self._keep(req.url) or req.resource_type not in ("xhr", "fetch", "document"):
            return
        try:
            headers = {k: v for k, v in req.all_headers().items()
                       if k.lower() in KEEP_HEADERS}
        except Exception:  # noqa: BLE001
            headers = {}
        self._write({"dir": "req", "method": req.method, "url": req.url,
                     "type": req.resource_type, "headers": headers,
                     "body": (req.post_data or "")[:6000]})

    def on_response(self, res) -> None:
        if not self._keep(res.url):
            return
        try:
            status = res.status
            headers = {k: v for k, v in res.all_headers().items()
                       if k.lower() in KEEP_HEADERS}
        except Exception:  # noqa: BLE001
            return
        body = ""
        try:
            if res.request.resource_type in ("xhr", "fetch"):
                body = res.text()[:2500]
        except Exception:  # noqa: BLE001
            body = "<unreadable>"
        self._write({"dir": "res", "status": status, "url": res.url,
                     "headers": headers, "body": body})


def probe(page, out: Path) -> None:
    """Print the account-management controls and the text around them."""
    page.wait_for_timeout(4000)
    body = re.sub(r"\s+", " ", page.inner_text("body"))

    mark = body.lower().find("account management")
    print("\n=== account management region ===")
    print(body[mark:mark + 700] if mark > -1 else body[:700])

    print("\n=== clickable controls ===")
    seen = set()
    for role in ("button", "link", "menuitem"):
        for el in page.get_by_role(role).all()[:80]:
            try:
                if not el.is_visible():
                    continue
                name = (el.get_attribute("aria-label") or el.inner_text() or "").strip()
                if not name:
                    continue
                key = (role, name)
                if key in seen:
                    continue
                seen.add(key)
                print(f"  {role:<8} {name[:70]!r}")
            except Exception:  # noqa: BLE001
                continue

    page.screenshot(path=str(out / "probe.png"), full_page=True)
    html = page.content()
    (out / "probe.html").write_text(html)
    print(f"\nsaved probe.png / probe.html -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["probe", "record", "live"], default="probe")
    ap.add_argument("--url", default="https://admin.google.com/ac/accountsettings")
    ap.add_argument("--profile", default=".profiles/admin")
    ap.add_argument("--expand", default=None,
                    help="comma-separated accessible names of collapsed sections to "
                         "open before probing, e.g. 'Expand Account management'")
    ap.add_argument("--click", default=None,
                    help="accessible name of the control to click in record mode")
    ap.add_argument("--confirm-click", default=None,
                    help="accessible name of a confirmation button to click after --click")
    ap.add_argument("--minutes", type=int, default=45,
                    help="live mode: how long to keep recording before giving up")
    ap.add_argument("--hold", type=int, default=0,
                    help="seconds to keep the window open at the end")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    out = Path(args.out) if args.out else (
        ROOT / "debug" / "admin_account" / time.strftime("%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    rec = Recorder(out / "requests.jsonl")

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=False,
            args=LAUNCH_ARGS, locale="en-US",
            viewport={"width": 1440, "height": 1100})
        ctx.on("request", rec.on_request)
        ctx.on("response", rec.on_response)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)

        if "accounts.google.com" in page.url:
            print("signed out — log in in this window, then press Enter here")
            input()
            page.goto(args.url, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(6000)

        if args.expand:
            for name in [n.strip() for n in args.expand.split(",") if n.strip()]:
                btn = page.get_by_role("button", name=name)
                if not btn.count():
                    btn = page.get_by_text(name, exact=False)
                if btn.count():
                    btn.first.click()
                    print(f"expanded {name!r}")
                    page.wait_for_timeout(2500)
                else:
                    print(f"no control matching {name!r}")

        if args.mode == "live":
            # hand the window over: the operator drives, this only watches.
            print(f"recording -> close the window when done (cap {args.minutes} min)")
            try:
                page.wait_for_event("close", timeout=args.minutes * 60_000)
            except Exception:  # noqa: BLE001 - timeout or teardown both end it
                pass
            print("window closed")
            ctx.close()
            rows = sum(1 for _ in (out / "requests.jsonl").open()) if (out / "requests.jsonl").exists() else 0
            print(f"\nrecorded {rows} rows -> {out / 'requests.jsonl'}")
            return 0

        if args.mode == "probe":
            probe(page, out)
        else:
            if not args.click:
                print("record mode needs --click")
                ctx.close()
                return 2
            control = page.get_by_role("button", name=args.click)
            if not control.count():
                control = page.get_by_text(args.click, exact=False)
            print(f"clicking {args.click!r} ({control.count()} match)")
            control.first.click()
            page.wait_for_timeout(5000)
            page.screenshot(path=str(out / "01_after_click.png"), full_page=True)

            if args.confirm_click:
                confirm = page.get_by_role("button", name=args.confirm_click)
                if not confirm.count():
                    confirm = page.get_by_text(args.confirm_click, exact=False)
                print(f"confirming {args.confirm_click!r} ({confirm.count()} match)")
                confirm.first.click()
                page.wait_for_timeout(6000)
                page.screenshot(path=str(out / "02_after_confirm.png"), full_page=True)

            print(f"url now: {page.url}")
            print(re.sub(r"\s+", " ", page.inner_text("body"))[:600])

        if args.hold:
            page.wait_for_timeout(args.hold * 1000)
        ctx.close()

    rows = sum(1 for _ in (out / "requests.jsonl").open()) if (out / "requests.jsonl").exists() else 0
    print(f"\nrecorded {rows} rows -> {out / 'requests.jsonl'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
