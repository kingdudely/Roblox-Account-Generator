#!/usr/bin/env python3
"""Register a fabricated device with Google's check-in service.

This is the honest test of the "it is only HTTP" thesis, run in isolation. No
account, no GMS, no Android: one gzipped protobuf POST to
`android.clients.google.com/checkin` claiming to be a Pixel, and the server either
mints an `android_id` + `security_token` or it does not.

If it mints one, the device-registration half of Google's trust is forgeable in
pure Python, and the wall is narrower than it looked — it is the auth call, not
check-in, that needs DroidGuard.

    python3 -m tools.gms.checkin                 # pixel8, once
    python3 -m tools.gms.checkin --device pixel6 --repeat 3
    python3 -m tools.gms.checkin --android-id 3f2a... --security-token 1f0c...
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import secrets
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import requests

from tools.gms import checkin_pb2 as pb
from tools.gms.device import DEVICES, pick

CHECKIN_URL = "https://android.clients.google.com/checkin"


def build_request(dev, android_id: int = 0, security_token: int = 0,
                  logging_id: int | None = None) -> pb.CheckinRequest:
    req = pb.CheckinRequest()
    req.androidId = android_id
    req.digest = ""
    req.locale = "en_US"
    req.loggingId = logging_id if logging_id is not None else secrets.randbits(62)
    req.timeZone = "America/New_York"
    req.version = 3
    req.serial = secrets.token_hex(8)
    req.fragment = 0
    if security_token:
        req.securityToken = security_token

    build = req.checkin.build
    build.fingerprint = dev.fingerprint
    build.hardware = dev.hardware
    build.brand = dev.brand
    build.radio = dev.radio
    build.bootloader = dev.bootloader
    build.clientId = dev.client_id
    build.time = int(time.time())
    build.packageVersionCode = dev.package_version_code
    build.device = dev.device
    build.sdkVersion = dev.sdk
    build.model = dev.model
    build.manufacturer = dev.manufacturer
    build.product = dev.product
    build.otaInstalled = False

    req.checkin.lastCheckinMs = 0
    ev = req.checkin.event.add()
    ev.tag = "event_log_start"
    ev.timeMs = int(time.time() * 1000)
    req.checkin.requestedGroup.extend([])
    req.checkin.cellOperator = ""
    req.checkin.simOperator = ""
    req.checkin.userNumber = 0
    req.checkin.roaming = "WIFI::notroaming"

    cfg = req.deviceConfiguration
    cfg.touchScreen = 3
    cfg.keyboardType = 1
    cfg.navigation = 1
    cfg.screenLayout = 2
    cfg.hasHardKeyboard = False
    cfg.hasFiveWayNavigation = False
    cfg.densityDpi = dev.density_dpi
    cfg.glEsVersion = 196610
    cfg.sharedLibrary.extend(dev.shared_libraries)
    cfg.availableFeature.extend(dev.features)
    cfg.nativePlatform.extend(dev.native_platforms)
    cfg.widthPixels = dev.width
    cfg.heightPixels = dev.height
    cfg.locale.extend(["en-US"])
    cfg.glExtension.extend(dev.gl_extensions)
    return req


def post(req: pb.CheckinRequest, dev, timeout: int = 30) -> tuple[int, bytes, dict]:
    raw = gzip.compress(req.SerializeToString())
    headers = {
        "Content-Type": "application/x-protobuf",
        "Content-Encoding": "gzip",
        "Accept-Encoding": "gzip",
        "User-Agent": dev.checkin_user_agent(),
    }
    resp = requests.post(CHECKIN_URL, data=raw, headers=headers, timeout=timeout)
    return resp.status_code, resp.content, headers


def parse(body: bytes) -> dict:
    if not body:
        return {"error": "empty response"}
    try:
        raw = gzip.decompress(body) if body[:2] == b"\x1f\x8b" else body
    except OSError:
        raw = body
    out = pb.CheckinResponse()
    try:
        out.ParseFromString(raw)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"parse failed: {type(exc).__name__}: {exc}",
                "bytes": len(raw), "head": raw[:40].hex()}
    return {
        "android_id": format(out.androidId, "x") if out.androidId else None,
        "security_token": format(out.securityToken, "x") if out.securityToken else None,
        "digest": out.digest or None,
        "settings": [{"name": s.name.decode("utf-8", "replace"),
                      "value": s.value.decode("utf-8", "replace")}
                     for s in out.setting][:8],
        "version_info": out.versionInfo or None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="pixel8", choices=sorted(DEVICES))
    ap.add_argument("--android-id", default=None, help="hex, from an earlier checkin")
    ap.add_argument("--security-token", default=None, help="hex, from an earlier checkin")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    dev = pick(args.device)
    android_id = int(args.android_id, 16) if args.android_id else 0
    security_token = int(args.security_token, 16) if args.security_token else 0

    print(f"device  : {dev.model} ({dev.device}) {dev.fingerprint}")
    print(f"ua      : {dev.checkin_user_agent()}")
    print(f"abi     : {', '.join(dev.native_platforms)}")
    print(f"android : {format(android_id, 'x') if android_id else '0 (first check-in)'}\n")

    for i in range(args.repeat):
        req = build_request(dev, android_id, security_token)
        try:
            status, body, headers = post(req, dev, args.timeout)
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}] request failed: {type(exc).__name__}: {exc}")
            return 2
        result = parse(body)
        print(f"[{i}] HTTP {status}  {len(body)} bytes")
        print("    " + json.dumps(result, ensure_ascii=False)[:400])
        if result.get("android_id"):
            android_id = int(result["android_id"], 16)
        if result.get("security_token"):
            security_token = int(result["security_token"], 16)
        if i + 1 < args.repeat:
            time.sleep(random.uniform(1.5, 4.0))

    print("\ninterpretation:")
    if android_id:
        print("  android_id minted -> device registration is forgeable in pure HTTP.")
        print("  the wall is the auth call, which carries droidguardResults.")
    else:
        print("  no android_id -> check-in itself is gated. read the body above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
