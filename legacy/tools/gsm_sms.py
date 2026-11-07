#!/usr/bin/env python3
"""Send Google's outbound verification SMS through a serial GSM modem.

Google's cross-device gate now asks the *phone* to send an SMS to shortcode
**244444** with a per-session code, e.g. `(7jFRl9eGSwH7)`. A receive-only OTP
number cannot do this; a modem with a SIM in it can, because a modem is a mobile
subscriber and shortcodes accept mobile-originated messages.

Tested shapes: SIM800L / SIM800C / SIM7600 on `/dev/ttyUSB0` at 115200. Needs a
SIM that can send to US shortcodes (most US prepaid can; some MVNOs block them).

    pip install pyserial
    python3 tools/gsm_sms.py --port /dev/ttyUSB0 --to 244444 --body '(7jFRl9eGSwH7)'
    python3 tools/gsm_sms.py --port /dev/ttyUSB0 --from-rpc debug/google_sms_intent/<stamp>/result.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path


def _serial(port: str, baud: int = 115200):
    try:
        import serial
    except ImportError:
        raise SystemExit("pyserial missing: .venv/bin/pip install pyserial")
    return serial.Serial(port, baud, timeout=5)


def _cmd(ser, line: str, wait: float = 1.0) -> str:
    ser.write((line + "\r").encode())
    time.sleep(wait)
    out = ser.read_all().decode("utf-8", "replace")
    return out


def send(port: str, number: str, body: str, baud: int = 115200,
         timeout: float = 40.0) -> dict:
    ser = _serial(port, baud)
    try:
        log: list[str] = []
        log.append(_cmd(ser, "AT"))
        if "OK" not in log[-1]:
            return {"ok": False, "stage": "AT", "log": log}

        # text mode
        log.append(_cmd(ser, "AT+CMGF=1"))
        if "OK" not in log[-1]:
            return {"ok": False, "stage": "CMGF", "log": log}

        # some modems need the carrier's SMSC; leave it to the SIM unless AT+CSMP
        # is required. Text mode defaults are fine on SIM800/SIM7600.
        ser.write(f'AT+CMGS="{number}"\r'.encode())
        time.sleep(1.2)
        prompt = ser.read_all().decode("utf-8", "replace")
        if ">" not in prompt:
            return {"ok": False, "stage": "CMGS-prompt", "log": log + [prompt]}

        # the body plus Ctrl-Z submits; Ctrl-Z alone cancels
        ser.write((body + "\x1a").encode())
        deadline = time.time() + timeout
        buf = ""
        while time.time() < deadline:
            buf += ser.read_all().decode("utf-8", "replace")
            if "+CMGS:" in buf:
                time.sleep(1.0)
                buf += ser.read_all().decode("utf-8", "replace")
                break
            if "ERROR" in buf or "+CMS ERROR" in buf:
                break
            time.sleep(0.5)
        log.append(buf)
        ok = "+CMGS:" in buf
        result = {"ok": ok, "number": number, "body": body, "log": log}
        if ok:
            m = re.search(r"\+CMGS:\s*(\d+)", buf)
            result["message_ref"] = int(m.group(1)) if m else None
        else:
            result["error"] = (buf or prompt).strip()[:200]
        return result
    finally:
        ser.close()


def body_from_rpc(path: Path) -> tuple[str, str]:
    """Read the destination and body out of a captured devicephone session.

    The page fires an `sms:` intent and the RPC carries the same text. The intent
    is authoritative because it is exactly what the phone's SMS app would send:

        sms://244444?body=Send%20this%20message%20without%20editing.%20(CODE)
    """
    text = path.read_text()

    def unquote(value: str) -> str:
        from urllib.parse import unquote
        return unquote(value)

    m = re.search(r'sms://(\d{5,6})\?body=([^"\\\s]+)', text)
    if m:
        return m.group(1), unquote(m.group(2))

    # fall back to the MTflnb batchexecute payload
    m = re.search(r'\\"(\d{5,6})\\",\\"(Send this message without editing\.\s*\([^)]+\))\\"',
                  text)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r'"(\d{5,6})"\s*,\s*"(Send this message without editing\.\s*\([^)]+\))"',
                  text)
    if m:
        return m.group(1), m.group(2)
    raise SystemExit("no sms intent or MTflnb destination/body found in that file")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="e.g. /dev/ttyUSB0 or COM4")
    ap.add_argument("--to", default=None, help="destination, e.g. 244444")
    ap.add_argument("--body", default=None, help="message body, e.g. (7jFRl9eGSwH7)")
    ap.add_argument("--from-rpc", type=Path, default=None,
                    help="result.json from google_sms_intent_probe.py")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.from_rpc:
        number, body = body_from_rpc(args.from_rpc)
    elif args.to and args.body:
        number, body = args.to, args.body
    else:
        ap.error("give --to and --body, or --from-rpc")

    result = send(args.port, number, body, args.baud)
    if args.json:
        print(json.dumps(result, indent=1))
    else:
        print(f"to      {number}")
        print(f"body    {body}")
        print(f"result  {'SENT' if result['ok'] else 'FAILED'}")
        if result.get("message_ref") is not None:
            print(f"msgref  {result['message_ref']}")
        if not result["ok"]:
            print(f"error   {result.get('error', '')}")
            for line in result.get("log", []):
                print("  ", line.strip()[:160])
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
