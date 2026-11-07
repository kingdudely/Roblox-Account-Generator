# The Android lane — faking a trusted device for Google account creation

Goal: get Google's **Skip** button on the phone-number page so an account can be
created without a SIM. That button is not a checkbox — it is Google's risk engine
deciding the device is real. This directory builds the most plausible device this
hardware can present, and measures whether Google agrees.

## What actually gets checked

| signal | what a browser gives | what a device gives | what an emulator leaks |
|---|---|---|---|
| IP reputation | whatever you route through | carrier/home IP | datacenter if hosted |
| device fingerprint | UA + JS surfaces | full `ro.*` set | x86 ABI, qemu props |
| account history on IP/device | cookies | GMS account list | fresh every boot |
| Play Integrity | n/a | BASIC/DEVICE/STRONG | BASIC at best, often none |
| sensors | none | real accelerometer/gyro | synthetic or absent |

The phone prompt is a *risk* decision, not an integrity gate. That is why
`Settings -> Accounts -> Add account -> Google` sometimes shows Skip where the web
cannot: the device supplies the trust signals the browser lacks. It is also why
emulators fail — they supply *wrong* ones.

## The x86 problem, stated plainly

This host is x86_64 (Debian 13, 8 cores, ~3 GB free, Wayland + `/dev/dri`). Under
waydroid the Android instance is x86_64. Every real phone Google has data on is
arm64. Two options, both compromised:

1. **Claim an ARM phone.** Then `ro.build.fingerprint` says `oriole` while
   `ro.product.cpu.abi` says `x86_64` and `ro.dalvik.vm.native.bridge` says
   `libndk_translation.so`. One coherent story, three contradictions.
2. **Claim an x86 device.** Truthful ABI, but almost no consumer x86 Android
   devices exist — Chromebooks are the closest. `fake_device_props.sh chromebook`
   is that profile. It is the only one whose ABI is not a lie.

Neither is likely to pass. Measure it anyway — one evening, zero cost, and the
answer is worth more than another week of guessing.

## The free hardware that avoids the problem

- **An old Android phone.** Factory reset, then create the account in the setup
  wizard or via Settings. Real TEE, real ABI, real sensors. Highest probability,
  zero cost. Reports converge on a handful of accounts per device before risk
  rises.
- **Oracle Cloud Always Free (Ampere A1).** 4 ARM cores / 24 GB RAM, free
  forever, real arm64. Run `redroid` arm64 there and the ABI is truthful. Needs a
  card for identity verification; the compute itself stays free. Datacenter IP is
  the remaining tell — tunnel the traffic out through home.
- **Samsung Remote Test Lab.** Real Samsung devices, free with a Samsung account.
  Interactive, not scriptable at volume.
- **AWS Device Farm free tier.** 1,000 real-device minutes, one time. Appium can
  drive it. ~16 hours of real hardware — enough for a one-time burst, not a farm.

## Ceiling, without dressing

- One Google identity still yields **exactly one Roblox account**. Recycling was
  tested and failed: changing the Roblox email does not free the Google link, and
  account deletion is asynchronous (`privacy/v1/users/<id>/forget` → `PROCESSING`,
  ~30 days).
- So accounts/day ≈ identities/day. An emulator instance produces single digits to
  low tens per day before Google raises the risk score on that IP and device.
- 50,000/day is thousands of instances. No free path reaches it.

This lane is worth building because it answers *whether a free identity exists*.
It is not the route to volume. Volume is direct signup with a live proxy pool.

## Run it

```bash
./waydroid_setup.sh                 # install + GApps + ARM translation
sudo ./fake_device_props.sh pixel6  # apply one consistent identity
python3 account_probe.py            # walk the account manager, report Skip
```

`account_probe.py` exits `0` when Skip appears, `1` when the phone page is hard,
`3` when the flow stalls (it prints the screen), `4` when adb is down.

## If the probe comes back `1`

That is the answer for x86. Move to a real device or the Oracle ARM box, and run
the same probe there — the code is device-agnostic once adb connects.
