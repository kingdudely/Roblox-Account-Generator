#!/usr/bin/env bash
# Present one consistent, real device identity to the account manager.
#
# Google does not judge a single property in isolation. It checks that the
# story is coherent: brand/model/device/fingerprint agree, the build is a
# release build, the bootloader looks locked, and the ABI matches the hardware
# it claims to be. A mismatch anywhere is the tell.
#
# Usage: sudo ./fake_device_props.sh [pixel6|pixel7|s23|chromebook]
#   chromebook is the only x86 story in this list — see the README.

set -euo pipefail

PROFILE="${1:-pixel6}"
BASE=/var/lib/waydroid/waydroid_base.prop
CFG=/var/lib/waydroid/waydroid.cfg

if [ ! -f "$BASE" ]; then
  echo "no $BASE — run waydroid_setup.sh first" >&2
  exit 1
fi
[ -f "$BASE.orig" ] || cp "$BASE" "$BASE.orig"

case "$PROFILE" in
  pixel6)
    FP="google/oriole/oriole:14/UQ1A.240205.004/11206848:user/release-keys"
    BRAND=google MODEL="Pixel 6" DEVICE=oriole PRODUCT=oriole MANUF=Google
    HARDWARE=oriole SOC=google_tensor ;;
  pixel7)
    FP="google/panther/panther:14/UQ1A.240205.004/11206848:user/release-keys"
    BRAND=google MODEL="Pixel 7" DEVICE=panther PRODUCT=panther MANUF=Google
    HARDWARE=panther SOC=google_tensor ;;
  s23)
    FP="samsung/dm3qxxx/dm3q:14/UP1A.231005.007/S911BXXU3BWLM:user/release-keys"
    BRAND=samsung MODEL="SM-S911B" DEVICE=dm3q PRODUCT=dm3qxxx MANUF=samsung
    HARDWARE=qcom SOC=kalama ;;
  chromebook)
    # x86_64 device. The only profile whose ABI can be truthful on this host.
    FP="google/zork/zork:14/UQ1A.240205.004/11206848:user/release-keys"
    BRAND=google MODEL="Zork" DEVICE=zork PRODUCT=zork MANUF=Google
    HARDWARE=zork SOC=amd64 ;;
  *)
    echo "unknown profile: $PROFILE" >&2; exit 2 ;;
esac

python3 - "$BASE" "$FP" "$BRAND" "$MODEL" "$DEVICE" "$PRODUCT" "$MANUF" "$HARDWARE" "$SOC" <<'PY'
import sys
path, fp, brand, model, device, product, manuf, hardware, soc = sys.argv[1:]
props = {
    "ro.product.brand": brand,
    "ro.product.model": model,
    "ro.product.device": device,
    "ro.product.name": product,
    "ro.product.manufacturer": manuf,
    "ro.product.board": hardware,
    "ro.board.platform": soc,
    "ro.build.fingerprint": fp,
    "ro.build.tags": "release-keys",
    "ro.build.type": "user",
    "ro.build.user": "android-build",
    "ro.build.host": "abfarm-release",
    "ro.debuggable": "0",
    "ro.secure": "1",
    "ro.adb.secure": "1",
    "ro.kernel.qemu": "0",
    "ro.boot.qemu": "0",
    "ro.hardware": hardware,
    "ro.boot.hardware": hardware,
    "ro.boot.verifiedbootstate": "green",
    "ro.boot.flash.locked": "1",
    "ro.boot.veritymode": "enforcing",
}
text = open(path).read().splitlines()
out, seen = [], set()
for line in text:
    key = line.split("=", 1)[0].strip()
    if key in props:
        out.append(f"{key}={props[key]}"); seen.add(key)
    else:
        out.append(line)
for key, value in props.items():
    if key not in seen:
        out.append(f"{key}={value}")
open(path, "w").write("\n".join(out) + "\n")
print(f"patched {len(props)} properties")
PY

echo "restarting waydroid"
waydroid session stop 2>/dev/null || true
sudo systemctl restart waydroid-container
sleep 2
waydroid session start >/dev/null 2>&1 &
sleep 4

echo "applied fingerprint:"
adb connect 127.0.0.1:5555 >/dev/null 2>&1 || true
adb shell getprop ro.build.fingerprint 2>/dev/null || echo "  (adb not up yet)"
adb shell getprop ro.product.cpu.abi 2>/dev/null || true
adb shell getprop ro.dalvik.vm.native.bridge 2>/dev/null || true

cat <<'EOF'

If native.bridge is non-empty this device is an x86 host pretending to run ARM
apps, and that string is part of the story Google reads. See README.
EOF
