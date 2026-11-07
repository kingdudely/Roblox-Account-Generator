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

1. **Keys** — paste
   `[{"client_id":"…","client_secret":"…","refresh_token":"…"}]`. The domain is
   detected from the tenant, so there is no domain field.
2. **Setup** — the checklist turns green as each step is proven, and the DuckDNS
   card writes the `google-site-verification` TXT record for you if you give it
   the account token.
3. **Run** — choose a count, press Start.

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

The previous Python pipeline (`main.py`, `cloud_identity.py`, `tools/`) is still
on disk as a fallback. The extension does not import or run any of it.
