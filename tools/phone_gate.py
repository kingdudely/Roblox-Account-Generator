#!/usr/bin/env python3
"""Read the number and message body out of Google's "verify your phone" QR gate.

Some signup flows stop at `signup/crossflowverification`: instead of a field for
a code, you get a QR code. Scanning it opens a page that pre-fills an *outbound*
SMS -- a destination number and a short body. The desktop page never shows you
that text, so the only visible route is a physical phone with a camera.

It is a link. Decode the QR and the number and body are in it, either directly
(`sms:+1...?body=...`) or behind one page load.

    python tools/phone_gate.py                  # open signup, wait for the QR
    python tools/phone_gate.py --qr shot.png    # decode a QR you already have
    python tools/phone_gate.py --url <link>     # read the page behind a QR

Prints `to:` and `body:`, and writes the QR, the page and its text to
`debug/phone_gate/<timestamp>/`. `--json` prints the record only.

Whoever sends the message needs a line that can actually send SMS. Receive-only
rental services and most VoIP numbers cannot reach a shortcode.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, unquote

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "debug" / "phone_gate"
SIGNUP = ("https://accounts.google.com/signup/v2/createaccount"
          "?flowName=GlifWebSignIn&flowEntry=SignUp")
MOBILE_UA = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36")

SMS_RE = re.compile(r"^sms:([^?]*)(?:\?(.*))?$", re.I | re.S)
DATA_RE = re.compile(r"^data:image/[\w.+-]+;base64,(.*)$", re.S | re.I)
SHORTCODE_RE = re.compile(r"(?<![\d+])\d{5,6}(?![\d-])")
BODY_HINT_RE = re.compile(
    r"(?:send|text|message|saying|texting)\D{0,60}([A-Z0-9]{4,12})", re.I)
SMS_IN_HTML_RE = re.compile(r"sms:(?:&#43;|\+)?[\d][^\"'<>\s]{4,}", re.I)


def log(msg: str = "") -> None:
    print(msg, flush=True)


# ------------------------------------------------------------------------ decode

def decode_image(raw: bytes) -> str | None:
    """First barcode in the image, or None. zxing-cpp is a wheel, no system lib."""
    try:
        import io

        import zxingcpp
        from PIL import Image
    except Exception:
        log("  decoder missing: pip install zxing-cpp Pillow")
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        for result in zxingcpp.read_barcodes(image):
            if result.text:
                return result.text.strip()
    except Exception as exc:
        log(f"  decode failed: {type(exc).__name__}: {str(exc)[:80]}")
    return None


def decode_file(path: Path) -> str | None:
    return decode_image(path.read_bytes())


def qr_blobs(page) -> list[bytes]:
    """Every QR-sized image on the page, from canvas or img, data-URI or remote."""
    blobs: list[bytes] = []
    try:
        urls = page.evaluate("""() => {
            const out = [];
            for (const c of document.querySelectorAll('canvas')) {
                try { out.push(c.toDataURL('image/png')); } catch (e) {}
            }
            for (const i of document.querySelectorAll('img')) {
                if (i.src && (i.naturalWidth || 0) > 80) out.push(i.src);
            }
            return out;
        }""")
    except Exception:
        urls = []

    for url in urls or []:
        match = DATA_RE.match(url or "")
        if match:
            try:
                blobs.append(base64.b64decode(match.group(1)))
            except Exception:
                pass
            continue
        if url.startswith("http"):
            try:
                blobs.append(page.request.get(url, timeout=20000).body())
            except Exception:
                pass
    return blobs


# ----------------------------------------------------------------------- extract

def parse_sms(href: str) -> tuple[str | None, str | None]:
    match = SMS_RE.match(href.strip())
    if not match:
        return None, None
    number = unquote(match.group(1)).strip(" ;") or None
    query = parse_qs(match.group(2) or "")
    body = (query.get("body") or query.get("Body") or [""])[0]
    return number, unquote(body).strip() or None


def extract(page) -> dict:
    """Find an sms: link on this page, and fall back to text heuristics."""
    record = {"page": page.url, "sms": None, "number": None, "body": None,
              "text": None}

    hrefs: list[str] = []
    try:
        hrefs = page.evaluate(
            "() => [...document.querySelectorAll('a,area')]"
            ".map(a => a.href).filter(Boolean)")
    except Exception:
        pass

    for href in hrefs:
        if href.lower().startswith("sms:"):
            record["sms"] = href
            break

    try:
        html = page.content()
    except Exception:
        html = ""
    if not record["sms"]:
        match = SMS_IN_HTML_RE.search(html)
        if match:
            record["sms"] = unquote(match.group(0))

    if record["sms"]:
        record["number"], record["body"] = parse_sms(record["sms"])

    try:
        text = page.inner_text("body")
    except Exception:
        text = ""
    record["text"] = text[:4000]

    if not record["number"]:
        # a shortcode (like 244444) or a full number next to send/text words
        window = text.lower()
        if any(word in window for word in ("send", "text", "message")):
            match = SHORTCODE_RE.search(text)
            if match:
                record["number"] = match.group(0)
    if not record["body"]:
        match = BODY_HINT_RE.search(text)
        if match:
            record["body"] = match.group(1)

    return record


def report(record: dict, artifacts: Path | None, as_json: bool) -> int:
    ok = bool(record.get("number") and record.get("body"))
    if as_json:
        print(json.dumps({k: v for k, v in record.items() if k != "text"},
                         indent=2))
    else:
        if record.get("page"):
            log(f"page:   {record['page']}")
        if record.get("sms"):
            log(f"sms:    {record['sms'][:160]}")
        log(f"to:     {record.get('number') or '?'}")
        log(f"body:   {record.get('body') or '?'}")
        if not ok:
            log("")
            log("could not read both fields -- the page text is saved below; "
                "look for the number and the message there")
        if artifacts:
            log(f"saved:  {artifacts}")
    return 0 if ok else 1


def show(payload: str, page, ctx, args, artifacts: Path) -> dict:
    """Turn a decoded payload into a record: sms: directly, or follow the link."""
    if payload.lower().startswith("sms:"):
        number, body = parse_sms(payload)
        return {"page": page.url, "sms": payload, "number": number,
                "body": body, "text": None}

    if not payload.lower().startswith("http"):
        log(f"decoded payload is not a link: {payload[:160]}")
        return {"page": page.url, "sms": None, "number": None, "body": None,
                "text": payload}

    log(f"decoded qr -> {payload[:140]}")
    target = ctx.new_page()
    if args.mobile:
        # some of these pages only render the sms: button for a phone UA
        target.set_extra_http_headers({"User-Agent": MOBILE_UA})
    try:
        target.goto(payload, wait_until="domcontentloaded", timeout=45000)
        target.wait_for_timeout(args.settle * 1000)
        record = extract(target)
        (artifacts / "phone_page.html").write_text(target.content())
        if record.get("text"):
            (artifacts / "phone_page.txt").write_text(record["text"])
    finally:
        try:
            target.close()
        except Exception:
            pass
    return record


def wait_for_qr(page, seconds: int, artifacts: Path) -> tuple[str | None, bytes]:
    deadline = time.time() + seconds
    seen: set[str] = set()
    last_note = 0.0
    while time.time() < deadline:
        for raw in qr_blobs(page):
            payload = decode_image(raw)
            if payload and payload not in seen:
                seen.add(payload)
                (artifacts / "qr.png").write_bytes(raw)
                return payload, raw
        # some layouts offer the number without a QR at all
        record = extract(page)
        if record.get("number") and record.get("body"):
            return None, b""
        if time.time() - last_note > 20:
            last_note = time.time()
            log(f"  waiting for the qr... ({int(deadline - time.time())}s left)")
        page.wait_for_timeout(1500)
    return None, b""


# -------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--qr", default=None,
                    help="decode a saved image instead of driving a browser")
    ap.add_argument("--url", default=None,
                    help="read this page instead of starting at signup")
    ap.add_argument("--profile", default=".profiles/gate")
    ap.add_argument("--start", default=SIGNUP,
                    help="page to open (default: the Google signup form)")
    ap.add_argument("--wait", type=int, default=600,
                    help="seconds to wait for the QR to appear")
    ap.add_argument("--settle", type=float, default=3.0,
                    help="seconds to let the mobile page render")
    ap.add_argument("--mobile", action="store_true",
                    help="open the decoded link with an Android user agent")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--debug", action="store_true",
                    help="keep the browser open after the run")
    args = ap.parse_args()

    stamp = time.strftime("%Y%m%d-%H%M%S")
    artifacts = OUT_DIR / stamp
    artifacts.mkdir(parents=True, exist_ok=True)

    if args.qr:
        payload = decode_file(Path(args.qr))
        if not payload:
            log(f"no barcode found in {args.qr}")
            return 1
        log(f"decoded qr -> {payload[:160]}")
        if payload.lower().startswith("sms:"):
            number, body = parse_sms(payload)
            return report({"page": None, "sms": payload, "number": number,
                           "body": body, "text": None}, artifacts, args.json)
        if not payload.lower().startswith("http"):
            return report({"page": None, "sms": None, "number": None,
                           "body": None, "text": payload}, artifacts, args.json)
        args.url = payload

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(ROOT / args.profile), headless=False,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-first-run", "--no-default-browser-check",
                  "--disable-dev-shm-usage", "--window-size=1366,900"],
            locale="en-US", viewport={"width": 1366, "height": 900})

        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            if args.url:
                page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(args.settle * 1000)
                record = extract(page)
                (artifacts / "page.html").write_text(page.content())
            else:
                page.goto(args.start, wait_until="domcontentloaded",
                          timeout=60000)
                log("drive the signup until the QR appears -- this window "
                    "watches for it")
                payload, _ = wait_for_qr(page, args.wait, artifacts)
                if payload is None:
                    record = extract(page)
                    (artifacts / "page.html").write_text(page.content())
                    log("no qr appeared; saved whatever the page had")
                else:
                    record = show(payload, page, ctx, args, artifacts)

            return report(record, artifacts, args.json)
        finally:
            if args.debug:
                log("--debug: leaving the browser open, ctrl-c to exit")
                try:
                    page.wait_for_timeout(3600_000)
                except KeyboardInterrupt:
                    pass
            ctx.close()


if __name__ == "__main__":
    raise SystemExit(main())
