# Roblox Alt Creator

A browser extension that turns Cloud Identity keys into Roblox accounts.
Setup instructions, key validation, the run console and the phone-gate decoder
all live inside the extension.

## Firefox — recommended

Firefox is the target browser: containers give each lane its own cookie jar, so
several sign-ins run in one window.

```
about:debugging#/runtime/this-firefox
  → Load Temporary Add-on…
  → pick extension/manifest.json
```

Temporary add-ons disappear when Firefox closes. For a permanent install, zip the
`extension/` folder and sign it:

```bash
npx web-ext sign --source-dir extension --artifacts-dir dist
```

Stable Firefox will not load an unsigned extension permanently.

## Chrome / Chromium

Chrome has one cookie jar per profile, so it runs one lane. Use the MV3 manifest
for Chrome:

```bash
rm -rf dist/chrome
cp -r extension dist/chrome
cp dist/chrome/manifest.chrome.json dist/chrome/manifest.json
```

```
chrome://extensions → Developer mode → Load unpacked → dist/chrome
```

## First run

The runner opens on install. If it does not, click the toolbar icon and choose
**Open runner**.

1. **Keys** — drop the `client_secret_*.apps.googleusercontent.com.json` you
   downloaded from the Cloud console, then press **Authorize** on the tenant row
   and approve the consent tab as the tenant admin. A ready-made key with
   `client_id`, `client_secret` and `refresh_token` can be pasted instead. The
   domain is detected from the tenant, so there is no domain field.
2. **Setup** — the checklist turns green as each step is proven. The DuckDNS
   card writes the `google-site-verification` TXT record for you if you give it
   the account token, and the Phone gate card reads the admin signup QR. Set
   tenant 2SV **Enforcement** to **On** with **Methods** = *Any except
   verification codes via text, phone call*: enforcement off makes Google ask for
   a phone number on every fresh account, and a method list that allows nothing
   enroll-able produces *"Your sign-in settings don't meet your organization's
   2-Step Verification policy"*.
3. **Run** — choose a count, press Start. Each identity gets ten minted backup
   codes; they land on the account row (`codes 10`) and in the export as
   `backup_codes:`. The run accepts the new-account terms interstitial, enrols the
   authenticator app (secret saved as `otp_secret`), finalises the Roblox account
   over HTTP, and accepts the updated Roblox terms when the setting is on.
4. **Settings** — choose whether Roblox keeps its generated username or the run
   sends one from the local generator, set the birthday and gender written at
   finalise (tick **random** under either to roll a fresh value per identity),
   turn the automatic terms acceptance on or off, and tune identity concurrency.
   Two more knobs live there: **lane surface** (own window per lane, the default,
   because Google's sign-in only advances in a selected tab) and **deletion
   pacing**, which walks a small batch of users a minute apart instead of a burst.

Accounts are written to extension storage the moment the cookie lands. Export is
a mirror; closing the tab loses nothing.

## Layout

```
extension/
  manifest.json          Firefox MV3 (containers, event page)
  manifest.chrome.json   Chrome MV3 (service worker)
  runner.html/.css/.js   the run loop and the whole UI
  background.js          opens the runner, owns the log ring
  content/google.js      fills Google's sign-in pages
  lib/                   admin, lanes, keys, consent, QR, storage
```

The Python pipeline that preceded the extension is not part of this tree; it
remains in the Git history. The extension is self-contained.
