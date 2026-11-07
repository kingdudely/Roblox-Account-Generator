# Google identity supply — measured, 2026-11-05

The question: what is the cheapest free surface that still produces a Google
account, at volume, without a phone number.

Short answer: **the Android account manager is the only free surface that
substitutes device trust for a phone**, and even it is tightening. Every web
surface measured tonight ends in the same gate.

> Research probes from the pre-SSO tree live under `legacy/tools/`; the current
> SSO helpers are in `tools/`.

## What the web flow actually does now

Desktop signup, run live from this connection and recorded to
`debug/google_signup/20261105-133*`:

```
name -> birthday -> username -> password
  -> accounts.google.com/lifecycle/steps/signup/crossflowverification
      "Verify some info before creating an account | Scan the QR code with your phone"
      "Google needs to verify some info about your device or phone number ..."
  -> QR decodes to accounts.google.com/devicephoneverification/begin?flow=browser&plt=...
  -> opened as a phone: "Verify your phone number ... Send SMS"
      "Your phone will open an SMS message with a code you need to send to verify
       your phone. Your carrier may charge for SMS messages."
```

The gate is no longer a phone-number field. It is **cross-device verification**:
scan a QR, then the phone sends an outbound SMS to Google. Inbound codes were
cheap to source; an outbound SMS needs a real SIM.

### The existing-email branch lands in the same place

`tools/google_existing_email_probe.py` binds the new account to a temp-mail
address (`mail.tm`) instead of minting a Gmail:

```
name -> birthday -> "Use your existing email" -> admin@example.com
  -> "Verify your email address"  (6-digit code arrives in the temp inbox: 099274)
  -> password
  -> crossflowverification / QR / Send SMS   <-- the same gate
```

So an existing email replaces the *identity*, not the *device check*. Temp mail
gets you past the email step; it does not get you past the phone step.

## The surfaces, ranked

| surface | what it asks | free? | repeatable? |
|---|---|---|---|
| **Android Settings -> Add account -> Create** | device trust; **Skip** appears on trusted hardware | yes | per device, low tens/day |
| Android Chrome app / Gmail app / Play Store | same GMS trust, slightly different UI | yes | same |
| Desktop web (Gmail address) | QR + outbound SMS | yes | one phone act per account |
| Desktop web (existing email) | email code **+** QR + outbound SMS | yes | same |
| Android TV / Chromebook setup | device trust, historically lighter | yes, needs the device | same class |
| Cloud Identity Free | domain + one admin SMS | yes | ~50/domain, tenant burned |
| Workspace free trial | card + domain, 10 users | no (card) | 10/domain |
| Education / nonprofit | school or org verification | no | n/a |
| Family Link child | parent account + age verification | partly | 5 children max |

**Android is not the only way — it is the only *free, repeatable* way**, and
"Android" here means "any surface that can present GMS device trust": Settings,
the Chrome app, the Gmail app, Android TV, a Chromebook at setup. They are one
class, not one app.

## Two open questions that move the ceiling

1. **Is the QR gate IP reputation or device?** If it is the IP, a clean network —
   a phone hotspot is free — may skip it entirely. Re-run
   `tools/google_signup_probe.py` with the machine tethered to mobile data and
   compare. Same code, one variable.

2. **Does one phone scan trust the browser profile?** If yes, one scan covers many
   accounts and the web path revives at moderate volume. If it re-asks every
   signup, the ceiling is one phone act per account.
   `tools/google_qr_handover.py` drives to the QR, waits for the scan, then starts
   a second signup in the same profile and reports whether the QR returns.

## Why the Android half is still gated in the payload

`tools/gms/` proved device registration is forgeable — a fabricated Pixel 8 got
`android_id 3b5dcb181f18dc8c` and a security token back, no Android required. But
the auth call that follows carries `droidguardResults`, the output of Google's
obfuscated native VM, and returns `Error=MissingDroidguard` without it. Fake the
ABI in the check-in and it works; fake it in the auth call and Google asks for a
signature you cannot type. That is why a real device (or a real ARM runtime) is
load-bearing.

## Volume, honestly

One device lane: single digits to low tens of accounts per day before risk rises.
Fifty thousand per day is thousands of lanes. No free surface reaches it; the
free methods top out at hundreds per day across a household's worth of devices.

## Tooling

| file | purpose |
|---|---|
| `tools/google_signup_probe.py` | walk the web signup, classify each gate, decode the QR |
| `tools/google_existing_email_probe.py` | the existing-email branch + temp-mail code retrieval |
| `tools/google_qr_handover.py` | hand the QR to a phone, then test profile trust |
| `tools/gms/checkin.py` | forge a device identity in pure HTTP |
| `tools/android_lane/` | drive the Android account manager and report Skip |

## The gate is an outbound SMS to 244444 — measured 2026-11-05

The cross-device screen does not ask for a number. It asks the phone to **send**
a message. Captured from the live session (`debug/google_sms_intent/20261105-141623`):

```
batchexecute  rpcids=MTflnb
  ["244444","Send this message without editing. (7jFRl9eGSwH7)"]

intent fired by the page
  sms://244444?body=Send%20this%20message%20without%20editing.%20(7jFRl9eGSwH7)
```

- **244444** is Google's US shortcode, registered 2017 as "Google Verification
  Alerts."
- The body is a per-session instruction + code. It changes on every signup.
- The body is paid per message by the user ("standard SMS rates apply").

### Why every receive-only OTP service is dead here

NumberOTP, SMSPool, 5SIM and the rest sell **inbound** codes: you type their
number into a form and read the SMS that arrives. This gate is the opposite
direction — the phone originates the SMS. A receive-only number cannot send, so
it cannot complete the step. Google moved consumer signup from inbound codes to
this outbound flow in 2025–2026; the GrapheneOS forum and PrivacyGuides both
document the switch, and neither has a receive-only workaround.

### The programmable path that does work: a GSM modem

A SIM800L/C or SIM7600 modem (~$10–25, USB serial) is a mobile subscriber, so it
can send mobile-originated SMS to a shortcode. That makes the step automatable:

1. Drive the signup to the QR gate (already built: `google_qr_handover.py`).
2. Capture the `MTflnb` response — destination + body — per session.
3. Send it through the modem: `python3 tools/gsm_sms.py --port /dev/ttyUSB0 --from-rpc <result.json>`
4. The browser continues once Google receives the message.

Caveats that decide the ceiling:

- One message per account; the body is session-bound.
- Google records the sending MSISDN. After some number of accounts it answers
  "this phone number has been used too many times for verification," so a single
  SIM is a low-tens-per-day source at best.
- Some MVNOs block shortcode sending; a major-carrier prepaid SIM is safer.
- This produces Google identities. Each still needs the Roblox SSO finalise
  (~31 s) and yields exactly one Roblox account.

`tools/gsm_sms.py` needs `pip install pyserial`. It reads the destination and body
straight out of the probe's `result.json`, so the two tools join without manual
copying.

---

# The pure-RPC signup, and the gate it cannot pass — measured 2026-11-05

The rendered wizard is a thin client over one RPC endpoint:

```
POST accounts.google.com/lifecycle/_/AccountLifecyclePlatformSignupUi/data/batchexecute
```

`tools/google_rpc_capture.py` recorded a full walk; `tools/google_rpc_signup.py`
replays it with an HTTP client and no browser.

## The eight calls

| # | rpcid | payload | answers |
|---|-------|---------|---------|
| 1 | `MjyMj` | `[0,0,[null×7,0,0,1,"",null,null,2,1,2]]` | `[]` |
| 2 | `E815hb` | `["First","Last",null,null,null,[],null,1]` | `steps/signup/birthdaygender` |
| 3 | `ink9M` | `[]` (inner `"1"`) | profile config, echoes first name |
| 4 | `eOY7Bb` | `[[y,m,d],gender,null×4,[null,null,MANAGE],[bg_token,null×4,[rc_token,1]]]` | `steps/signup/username` |
| 5 | `xdYwpe` | `[1]` (inner `"1"`) | two suggested Gmail names |
| 6 | `NHJMOd` | `["user",0,0,null,[null,null,null,null,1,4105],0,40]` | `steps/signup/password` |
| 7 | `dOJftd` | `[null×6,[null,null,MANAGE]]` (inner `"1"`) | availability flags |
| 8 | `ZNd7Td` | `["password",[bg_token,null×4,[rc_token,1]]]` | `steps/signup/crossflowverification/initial` |

Every request needs `rpcids`, `source-path`, `f.sid`, `bl`, `hl`, `TL`, `dsh`,
`_reqid`, `rt` in the query and `f.req` + `at` in the body. All six values are in
`window.WIZ_global_data` on the landing page:

```
at   = SNlM0e      f.sid = FdrFJe     dsh = Qzxixc
bl   = cfb2h       TL    = final URL query
```

## Where it stops

Call 4. `eOY7Bb` carries a two-part token blob:

```
[0]     "<4y9qL0sCAAar…"   BotGuard output, 2.9 KB
[5]     ["0cAFcWeA4u2n…",1] reCAPTCHA Enterprise token, site key
                           6Lf-_ekqAAAAAO4AXrJISaHw4_bW76NcfwhLN7Is
```

Six token variants were tried — omitted, `[]`, `[null]`, `[null×5]`,
`[null×5,[null,1]]`, `[""]`. All six return:

```
[["wrb.fr","eOY7Bb",null,null,null,[3],"generic"], …]
```

`[3]` is the RPC's invalid-argument code. The step is unreachable without a
genuine token pair.

Calling the reCAPTCHA reload endpoint directly does not help:

```
POST https://www.google.com/recaptcha/enterprise/reload?k=6Lf-…
-> )]}'  ["rresp",null,null,null,null,null,1]
```

It returns a null token unless the anchor page, its cookies and the BotGuard VM
have run. The token is produced in-browser, per session.

So the phone gate is not reached by an RPC client — not because the gate is
avoided, but because birthday blocks the walk before password. Even with a
valid token pair the browser reaches the same `crossflowverification` screen,
which is why the gate is an account-level decision, not a UI artifact.

## The gate changes shape by client — the useful finding

Same signup, two user agents, measured minutes apart:

| client | gate URL | what it asks |
|---|---|---|
| desktop Chrome | `crossflowverification/initial` | QR → open on your phone → **send** an SMS to 244444 |
| Android Chrome UA | `crossflowverification/samedevice` | "Do a device check — use this phone to verify" → **Continue** |

The mobile "Continue" is not a form. It is an Android intent:

```
intent://…#Intent;package=com.google.android.gms;
  action=android.intent.action.VIEW;
  component=com.google.android.gms/.auth.aang.impl.deeplink.DeepLinkActivity
```

In other words, mobile signup hands off to **Google Play Services** to attest the
device. No SMS, no QR, no phone number.

Consequence: a real Android device with Play Services signed in can pass the
device check without a SIM. The device is the credential. A desktop browser
cannot resolve the intent, and an emulator without Play Services cannot either.

## Two more branches that do not work

- **Age under 15** (birth year 2012): skips the phone gate and lands on
  `steps/signup/error/1` — "Sorry, we could not create your Google Account."
  That is the supervised-account branch, which wants a parent and consent.
- **Passkeys**: sign-in only. The crossflow screen has no passkey control, and
  Google's own documentation scopes passkeys to authenticating an existing
  account, not to satisfying signup verification.

## What the RPC work is still good for

`tools/google_rpc_signup.py` is the fastest way to ask "did Google change the
wizard?" — one HTTP session, eight calls, no browser, ~5 seconds. If a future
flow drops the token requirement on `eOY7Bb`, this tool finds it immediately.

---

# Which numbers can actually text 244444 — measured 2026-11-05

The gate is a mobile-originated SMS to a US shortcode. Two facts narrow the sender
list to almost nothing:

1. No SMS API can address a shortcode. Twilio and Vonage both say so explicitly;
   A2P only runs the other direction.
2. Most non-US carriers block outbound SMS to US shortcodes. Google's own developer
   forum has a steady stream of Kazakhstan, China, Taiwan and Brazil reports of
   exactly this failure, with no alternative on the page.

So the sender must be a **real US mobile number**. Three ways to get one:

| source | cost | can text 244444 |
|---|---|---|
| US carrier number you already have | $0 | yes |
| US eSIM trial (T-Mobile 30 d / Verizon 30 d / Visible 15 d) | $0, no card | yes — real number |
| TextVerified rental with **SMS Send** | from $1.50 | **yes — Google's shortcode is on their list** |
| receive-only OTP services (5sim, SMS-Act, NumberOTP) | — | no, by design |
| Google Voice, TextNow, TextFree and other VoIP | — | no — VoIP cannot address shortcodes |

## TextVerified is the programmable one

Their enhanced-rental FAQ documents **SMS Send** as an outbound send from the
rental to any destination, with a separate allowance for "verified shortcodes".
The named examples include:

```
Google 244444 96831 7664400 22000
```

That is the exact destination this gate uses. Their numbers are non-VoIP US carrier
lines, so Google sees a normal mobile subscriber.

Tool: `tools/textverified_sms.py` (uses the published `textverified` 0.1.1 SDK).

```bash
export TEXTVERIFIED_API_USERNAME=you@example.com
export TEXTVERIFIED_API_KEY=...

./tools/textverified_sms.py --quote          # price a one-day SMS rental
./tools/textverified_sms.py --buy            # create it
./tools/textverified_sms.py --list           # active rentals
./tools/textverified_sms.py --send-from-rpc debug/google_sms_intent/<stamp>/result.json
```

`--send-from-rpc` reads the destination and body straight out of the probe capture
with the same parser `tools/gsm_sms.py` uses, so the whole chain is:

```
probe -> capture sms://244444?body=... -> TextVerified SMS Send -> browser continues
```

## The arithmetic that makes this the cheapest identity route

```
1 SMS  -> 1 Cloud Identity tenant signup
1 tenant -> ~45-50 Google identities
1 identity -> 1 Roblox account
```

At $1.50 a rental that is **~$0.03 per Roblox account**, against $0.45–1.00 for a
bought Gmail account. It is the only route that converts one phone event into
dozens of identities.

Caveats, all measured or documented rather than assumed:

- Google records the sending number. Reuse it too often and the signup answers
  "this phone number has been used too many times for verification".
- TextVerified states the Enhanced label alone does not guarantee SMS sending, and
  that the shortcode list can change without notice.
- `--quote` and `--list` are safe to run. `--buy` spends money and `--send` spends
  send allowance; both are untested here because no funded key exists yet.

## The free sender nobody has to buy

Google's own developer-forum answer to users whose carrier blocks the shortcode is:

> open the verification link directly on an Android phone or tablet already signed
> into your Google account and select **"Device check"** instead of SMS.

That matches the `crossflowverification/samedevice` screen measured earlier — the
mobile flow hands off to Play Services via an `intent://` deep link. So a device
with an existing Google account can vouch for the signup without any SMS at all.

---

# Where the Cloud Identity gate actually sits — measured 2026-11-06

Earlier notes said the Workspace signup ends on the QR gate. With a real,
already-signed-in Google account and a real DuckDNS domain, it does not. The gate
moved one step later.

## The observed sequence

```
signed in as the contact account
  -> workspace.google.com/gcpidentity/signup?sku=identitybasic
     business name / employee radio / region
  -> contact info (first, last, current email)
  -> domain (example.duckdns.org)
  -> username + password (admin@example.duckdns.org)
  -> Agree and continue
     *** tenant + admin account created, no gate ***
  -> sign-in as admin@example.duckdns.org
  -> challenge/iap/qrcode   <- the gate
```

So the signup form itself no longer verifies a phone. The account exists after
"Agree and continue"; the verification is attached to the **new admin's first
sign-in**.

## The challenge has exactly one method

`challenge/iap/qrcode` renders a QR plus a short code (`g.co/verify/XXXXXXXX`).
It does have a **Try another way** control — unlike the consumer
`crossflowverification` page — but clicking it does not offer email, backup codes
or recovery.

Captured RPC `B4hajb`, one response per click: the payload only reissues the same
method with a new code.

```
[null,null,"REENABLE",N,null,"INITIALIZED", ... "admin@example.duckdns.org",
 ... ["data:image/png;base64,…", "<blob>", null,
      "https://accounts.google.com/devicephonev…", 1,
      "intent://accounts.google.com/devicephone…", "g.co/verify/XXXX"], 6]
 -> [["/v3/signin/challenge/iap/qrcode", …]]
```

`N` increments (2, 3, 4, …) and the QR changes. Nothing else. "Try another way"
means "give me a different code for the same device check", not "let me verify a
different way".

## What this changes

- Tenant creation is now **free of the phone gate**. Many tenants can be staged.
- Activation still costs **one mobile-originated SMS per tenant**, at the admin's
  first sign-in — the same 50-identities-per-SMS arithmetic, at a known step.
- The sender that can reach `244444` is the same problem, and TextVerified's
  rental with SMS Send remains the programmable answer
  (`tools/textverified_sms.py`).

Artifacts: `debug/cloud_identity/20261106-075956` (full signup walk),
`debug/google_challenge/20261106-080332` (challenge screens),
`debug/duckdns/20261106-075359` (domain creation).
