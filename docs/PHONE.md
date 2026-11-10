# Google's phone gate

Google uses two different phone checks. The CLI handles both, but they go in
opposite directions.

## 1. Google texts you

The page says something like **"Enter a phone number to get a verification
code"** or **"We'll send a verification code"**.

- `--phone <number>` is **your** number.
- The CLI fills it, clicks Send, focuses the browser and beeps.
- Google texts you a 6-digit code. Type it into the focused browser window.
- Without `--phone`, type the number directly in the browser; the CLI waits.

`--phone` is not the number Google asks you to text. That is the other gate.

## 2. You text Google

The page says **"Send this text message"**, **"Send SMS"**, or shows a QR code.
The CLI:

1. scans the page for a QR code or an `sms:` link;
2. decodes the QR with the bundled jsQR;
3. if the QR contains a Google consent link, opens it in the background,
   presses Send SMS, and captures the `sms:` URI;
4. prints the extracted destination number and message body;
5. focuses the browser and beeps.

You then do one of these:

- scan the QR code on the page with your phone camera, or
- open your SMS app and send the printed number/message exactly as shown.

The CLI keeps polling and continues automatically when Google moves the page
on. It re-beeps once a minute while it waits.

## Why there is no bypass

The phone/QR check is Google's anti-abuse control for creating a Cloud Identity
tenant. There is no public API to create a tenant. The only programmable path
is the Cloud Channel API, which requires Google partner/reseller status and paid
subscriptions. Virtual numbers and SMS-activation services violate Google's
terms, are heavily flagged, and get tenants suspended.

The realistic maximum is **one phone check per tenant**. Cloud Identity Free
includes 50 seats; `rac run` deletes the Google identity after a successful
Roblox signup, freeing the seat, so the same tenant can keep producing accounts
subject to Google/Roblox rate limits.

## If Google rejects the number

Common messages:

- **"This phone number cannot be used for verification"** — the number has been
  used too many times, or the account hit an attempt limit. Wait, or use a
  trusted different number.
- **"Sorry, we can't complete your signup"** — Google's anti-abuse decision.
  Wait, change network, or use a proxy. There is no reliable bypass.
- **"Too many unsuccessful attempts"** — wait 24 hours.

## If the QR will not scan

The CLI prints the consent link if it can extract one. Open that link on the
phone that is signed into a Google account Google accepts. If the link is not
printed, the page in the focused browser is the QR page; scan it directly.

## Sound and focus

At every human gate the CLI calls `page.bringToFront()`, rings the terminal bell,
and sends a desktop notification when `notify-send` (Linux) or `osascript`
(macOS) is available. Set `RAC_QUIET=1`, `RAC_NO_SOUND=1` or `RAC_NO_NOTIFY=1`
to turn those off.
