#!/usr/bin/env python3
"""Walk Android's Google account creation over adb and report the phone gate.

The measurement that matters is a single boolean: when the flow reaches the
phone-number page, is there a *Skip* control? On a trusted device Google shows
it. On a browser, and on most emulators, it is replaced by a hard number field.

This drives the device flow — Settings -> Accounts -> Add account -> Google ->
Create account — by reading the UI hierarchy (`uiautomator dump`) and tapping the
centre of matched nodes. It is deliberately dumb: match text, tap, re-dump. If a
step is unknown it prints the current screen and stops, so the transcript tells
you exactly where it stood.

    adb connect 127.0.0.1:5555
    python3 account_probe.py --first James --last Carter

Exit codes:
    0  reached the phone page and found a Skip control
    1  reached the phone page and Skip was absent
    3  stalled before the phone page (screen printed)
    4  adb could not reach the device
"""

from __future__ import annotations

import argparse
import re
import secrets
import subprocess
import string
import sys
import time
import xml.etree.ElementTree as ET

DUMP = "/sdcard/window_dump.xml"


def adb(*args: str, check: bool = True) -> str:
    p = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=90)
    if check and p.returncode != 0:
        raise RuntimeError(f"adb {' '.join(args)} -> {p.returncode}: {p.stderr.strip()}")
    return p.stdout


def nodes() -> list[dict]:
    adb("shell", "uiautomator", "dump", DUMP)
    root = ET.fromstring(adb("shell", "cat", DUMP))
    out = []
    for n in root.iter("node"):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = map(int, m.groups())
        text = f"{n.get('text') or ''} {n.get('content-desc') or ''}".strip()
        out.append({
            "text": text,
            "class": n.get("class", ""),
            "clickable": n.get("clickable") == "true",
            "enabled": n.get("enabled") != "false",
            "x": (x1 + x2) // 2,
            "y": (y1 + y2) // 2,
        })
    return out


def show(title: str) -> list[str]:
    ns = nodes()
    texts = [n["text"] for n in ns if n["text"]]
    print(f"\n--- {title} ---")
    for t in texts[:45]:
        print("   ", t[:110])
    print("    (no text)" if not texts else "")
    return texts


def find(pattern: str, clickable_only: bool = True) -> dict | None:
    rx = re.compile(pattern, re.I)
    ns = nodes()
    for n in ns:
        if clickable_only and not (n["clickable"] and n["enabled"]):
            continue
        if rx.search(n["text"]):
            return n
    return None


def tap(node: dict) -> None:
    adb("shell", "input", "tap", str(node["x"]), str(node["y"]))
    time.sleep(1.4)


def tap_text(pattern: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        n = find(pattern) or find(pattern, clickable_only=False)
        if n:
            tap(n)
            return True
        time.sleep(1.0)
    return False


def type_into(node: dict, value: str) -> None:
    adb("shell", "input", "tap", str(node["x"]), str(node["y"]))
    time.sleep(0.4)
    adb("shell", "input", "keyevent", "KEYCODE_MOVE_END")
    adb("shell", "input", "text", value.replace(" ", "%s"))
    time.sleep(0.4)


def fills() -> list[dict]:
    return [n for n in nodes() if "EditText" in n["class"]]


def random_username() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "u" + "".join(secrets.choice(alphabet) for _ in range(11))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="127.0.0.1:5555")
    ap.add_argument("--first", default="James")
    ap.add_argument("--last", default="Carter")
    ap.add_argument("--user", default=None, help="gmail local-part to try")
    ap.add_argument("--password", default=None)
    ap.add_argument("--day", default="14")
    ap.add_argument("--month", default="3")
    ap.add_argument("--year", default="1994")
    ap.add_argument("--skip-year", type=int, default=1994,
                    help="birth year to use when the flow accepts one")
    args = ap.parse_args()

    adb("connect", args.serial, check=False)
    devices = adb("devices", check=False)
    if args.serial not in devices and "offline" not in devices:
        print(f"adb cannot see {args.serial}:\n{devices}")
        return 4

    user = args.user or random_username()
    password = args.password or (user[::-1] + "Aa1!")

    print(f"identity: {args.first} {args.last} / {user}")

    adb("shell", "am", "start", "-a", "android.settings.ADD_ACCOUNT_SETTINGS",
        check=False)
    time.sleep(2.5)
    show("add account")

    if not tap_text(r"^google$"):
        print("no Google row on the add-account screen")
        return 3
    time.sleep(2.5)
    show("google provider")

    if not tap_text(r"create (a )?new account|create account|add account"):
        print("no create-account entry point")
        return 3
    time.sleep(3.0)

    # Some builds ask "for myself / for my child / for work" before the form.
    tap_text(r"for my (personal )?use|myself|personal", timeout=6)

    for step in range(14):
        texts = show(f"step {step}")
        joined = " ".join(texts).lower()

        if re.search(r"\bskip\b", joined) and re.search(r"phone|number|mobile", joined):
            print("\nRESULT: phone page WITH a Skip control")
            return 0
        if re.search(r"phone number|mobile number|enter.*number", joined) and \
           not re.search(r"\bskip\b", joined):
            print("\nRESULT: phone page WITHOUT a Skip control")
            return 1

        # name
        if re.search(r"first name", joined) and re.search(r"last name", joined):
            edits = fills()
            if len(edits) >= 2:
                type_into(edits[0], args.first)
                type_into(edits[1], args.last)
            elif len(edits) == 1:
                type_into(edits[0], args.first)
            tap_text(r"next|continue")
            continue

        # birthday
        if re.search(r"birthday|birth date|date of birth", joined):
            edits = [e for e in fills()]
            if len(edits) >= 3:
                type_into(edits[0], args.month)
                type_into(edits[1], args.day)
                type_into(edits[2], str(args.skip_year))
            tap_text(r"next|continue")
            continue

        # username / email
        if re.search(r"choose.*(gmail|username|email)|username", joined) and \
           not re.search(r"password", joined):
            edits = fills()
            if edits:
                type_into(edits[0], user)
            else:
                tap_text(r"create your own|choose your own")
            tap_text(r"next|continue")
            continue

        # password
        if re.search(r"password", joined):
            edits = fills()
            if len(edits) >= 2:
                type_into(edits[0], password)
                type_into(edits[1], password)
            elif edits:
                type_into(edits[0], password)
            tap_text(r"next|continue")
            continue

        # generic advance
        if tap_text(r"next|continue|agree|i agree|accept", timeout=5):
            continue

        print("\nRESULT: stalled — screen above is the last state")
        print(f"  username {user}")
        print(f"  password {password}")
        return 3

    print("\nRESULT: ran out of steps without reaching the phone page")
    print(f"  username {user}")
    print(f"  password {password}")
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
