# GMS over HTTP — what is forgeable and what is not

The question this answers: can a Google account be created by sending the right
HTTP requests from a Linux box, with an Android device faked in the payload?

The short answer, measured: **the device half is forgeable. The account half is
not, because one field in the auth request is the output of Google's native code.**

## What was proven live (2026-11-05)

`tools/gms/checkin.py` sends a fabricated Pixel 8 to
`https://android.clients.google.com/checkin` — one gzipped protobuf POST, no
Android, no GMS, no account:

```
device  : Pixel 8 (shiba) google/shiba/shiba:14/UQ1A.240205.004/11206848:user/release-keys
abi     : arm64-v8a, armeabi-v7a, armeabi
[0] HTTP 200  91143 bytes
    android_id     3b5dcb181f18dc8c
    security_token 9269aee13f920cf
    settings       90 KB of GServices flags (account_recovery_*, ads:jams:*, ...)
```

Re-check-in with that id and token returns the same id. A second profile
(Pixel 6) minted `30ba6536bf68b9ae` on the next request. No `arkose`, no
`droidguard`, no challenge, no rate limit after three calls from a residential IP.

So device registration — the thing that gives a device its identity — is fully
reproducible in ~200 lines of Python. The build fingerprint, ABI list, sensor
list, screen metrics and shared libraries are all client-supplied and all
accepted.

## The protocol, end to end

```
1. checkin        POST android.clients.google.com/checkin   (protobuf, gzip)
                  -> android_id, security_token, GServices config
                  FORGEABLE. Proven above.

2. auth           POST android.clients.google.com/auth      (form-urlencoded)
                  accountType=HOSTED_OR_GOOGLE
                  service=ac2dm
                  app=com.google.android.gsf
                  androidId=<from step 1>
                  Email=..., Passwd=... / EncryptedPasswd=...
                  droidguardResults=<opaque blob>            <-- the wall
                  -> Auth= token, accountId, ...

3. account        the creation flow itself (WebView to accounts.google.com, or
                  the setup-wizard native flow)
                  -> may or may not ask for a phone number
```

The auth endpoint checks credentials first — a bogus email returns
`Error=BadAuthentication` regardless of what else is present. That is why the
DroidGuard requirement is invisible until you have a real credential to send.

## The wall: `droidguardResults`

Google added a DroidGuard requirement to the auth call in late 2025 / early
2026. The microG project tracked it:

- **GmsCore #3195** (2025-12): "Google sign-in fails with new devices" —
  `NotOkayException: Error=MissingDroidguard`. A valid session, correct device
  registration, and the call still fails because `droidguardResults` is null.
- **GmsCore PR #3257** (merged 2026-02): "fix google login missing droidguard" —
  the auth request now has to carry the blob.
- **GmsCore #3079**: when `androidId` is *present* but DroidGuard is absent, the
  server answers `Error=ServerError`. When `androidId=null`, the request is
  accepted — that is the workaround, and it is also the tell: stripping the
  androidId strips the device identity, which is the exact signal that makes
  Google offer *Skip* instead of a phone number.

DroidGuard is Google's obfuscated native VM. It is fetched at runtime, executed,
and returns a blob. You cannot compute it in Python; you have to run their code.
That is the part of "it is only HTTP" that is not HTTP.

## The only architecture that keeps it HTTP

Separate the oracle from the client:

```
   arm64 Android runtime (real phone, or Oracle Always-Free Ampere + redroid)
        |
        |  real GMS + real DroidGuard
        v
   tiny bridge  ->  droidguardResults for a given request context
        |
        v
   N Python clients  ->  checkin (forged) -> auth (with blob) -> token
```

microG already implements the remote half (`PR #3707`, Aug 2026: "Support
multi-step remote DroidGuard"). Its own maintainer states the limit:

> the number of requests a physical device can pass for play integrity is
> limited, so this really scales badly

So even the oracle route is hardware-bound. One device mints a finite number of
attestations. The androidId is free; the attestation is not.

## Where the phone number actually comes from

Not from the client. The phone page is a **server-side risk decision** made after
auth: IP reputation, account-creation history on that exit, device age. A perfect
DroidGuard blob moves the decision — it does not remove it. That is why real
phones still get the prompt on a bad network, and why a clean home IP sometimes
skips it even on the web.

## Files

- `checkin.proto` — the wire schema (from `nborrmann/gcmreverse`, microG's field
  layout).
- `checkin_pb2.py` — generated with `grpcio-tools`.
- `device.py` — coherent Pixel 8 / 7 / 6 profiles.
- `checkin.py` — mints a forged device identity.

```bash
python3 -m tools.gms.checkin                  # mint an android_id
python3 -m tools.gms.checkin --device pixel6  # a different device
python3 -m tools.gms.checkin --android-id <hex> --security-token <hex>  # re-checkin
```

## What this buys

A supply of `(androidId, securityToken)` pairs that Google accepts as devices.
That is real, and it is free. Whether it changes the *account creation* risk
decision is the next thing to measure — by feeding a forged id into the Android
creation flow and watching for *Skip*. Until that is measured, treat the androidId
as a prerequisite, not a bypass.
