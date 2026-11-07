#!/usr/bin/env bash
# Provision waydroid + GApps + ARM translation so a Google account can be
# created from the Android account manager instead of the web form.
#
# The whole point is the "Skip" button on the phone page: Google offers it to a
# device it trusts, and withholds it from a browser. waydroid inherits the host
# kernel, so on x86_64 the instance is x86_64 — see fake_device_props.sh for the
# identity side and the README for why that is the hard part.
#
# Run as your normal user. It calls sudo itself.
#   ./waydroid_setup.sh            # install + init
#   ./waydroid_setup.sh --verify   # just report what is already there

set -euo pipefail

if [[ "${1:-}" == "--verify" ]]; then
  echo "waydroid: $(command -v waydroid || echo missing)"
  echo "adb:      $(command -v adb || echo missing)"
  waydroid status 2>/dev/null || true
  adb devices 2>/dev/null || true
  exit 0
fi

echo "[1/7] base packages"
sudo apt update
sudo apt install -y curl ca-certificates python3 python3-venv git android-sdk-platform-tools-common

echo "[2/7] waydroid repository"
curl -s https://repo.waydro.id | sudo bash
sudo apt install -y waydroid

echo "[3/7] initialise with GApps (this downloads ~1.2 GB)"
if [ ! -f /var/lib/waydroid/waydroid.cfg ]; then
  sudo waydroid init -s GAPPS -f
else
  echo "    already initialised, skipping"
fi

echo "[4/7] binder + container service"
sudo modprobe binder_linux devices=binder,hwbinder,vndbinder 2>/dev/null || true
sudo systemctl enable --now waydroid-container

echo "[5/7] ARM translation + Play Store (needed because the image is x86_64)"
if [ ! -d /tmp/waydroid_script/.git ]; then
  rm -rf /tmp/waydroid_script
  git clone --depth 1 https://github.com/casualsnek/waydroid_script /tmp/waydroid_script
fi
python3 -m venv /tmp/waydroid_script/.venv
/tmp/waydroid_script/.venv/bin/pip -q install --upgrade pip
/tmp/waydroid_script/.venv/bin/pip -q install -r /tmp/waydroid_script/requirements.txt
echo "    installing libndk (ARM translation)"
sudo /tmp/waydroid_script/.venv/bin/python /tmp/waydroid_script/main.py install libndk || true
echo "    installing gapps"
sudo /tmp/waydroid_script/.venv/bin/python /tmp/waydroid_script/main.py install gapps || true

echo "[6/7] start session"
waydroid session start >/dev/null 2>&1 &
sleep 5
waydroid status || true

echo "[7/7] adb bridge"
adb connect 127.0.0.1:5555 || true
adb devices || true

cat <<'EOF'

next:
  1. sudo ./fake_device_props.sh pixel6     # one consistent identity
  2. python3 account_probe.py               # walk the account manager flow
  3. read the README before believing any of this scales

If `waydroid status` says STOPPED, the GPU is the usual culprit:
  sudo systemctl restart waydroid-container && waydroid session start
On a laptop with hybrid graphics, force the iGPU:
  WAYLAND_DISPLAY=wayland-0 waydroid session start
EOF
