# Roblox Alt Creator

A browser extension that turns Cloud Identity keys into Roblox accounts.
Setup instructions, key validation, the run console and the phone-gate decoder
all live inside the extension.

## Chrome / Chromium

Chrome is the target browser. Load the unpacked extension directly:

```
chrome://extensions → Developer mode → Load unpacked → extension
```

The extension asks for the **debugger** permission. That is what lets the
automatic setup type and click through Chrome's own input pipeline instead of
synthetic DOM events. Chrome shows a “started debugging this browser” bar while
the signup forms are being filled; the extension detaches as soon as the signup
moves to the Google account step.

Chrome has one cookie jar per profile, so the extension runs **one lane** in a
profile. Running several lanes means one Chrome profile per lane (a later
addition); it is not a container replacement.

## First run

The runner opens on install. If it does not, click the toolbar icon and choose
**Open runner**.

1. **Keys** — drop the `client_secret_*.apps.googleusercontent.com.json` you
   downloaded from the Cloud console, then press **Authorize** on the tenant row
   and approve the consent tab as the tenant admin. A ready-made key with
   `client_id`, `client_secret` and `refresh_token` can be pasted instead. The
   domain is detected from the tenant, so there is no domain field.
2. **Setup** — pick one of three modes:
   - **Buy** — DM `g_oose` or `kingdudely` for a ready key; paste it in Keys.
   - **Manual** — the five-step guide. The checklist turns green as each step is
     proven. The DuckDNS card writes the `google-site-verification` TXT record
     for you, and the Phone gate card reads the admin signup QR.
   - **Automatic** — needs only a recovery email, a DuckDNS token and a DuckDNS
     subdomain. It opens the Cloud Identity signup, generates the business name
     and admin credentials (or uses the optional fields), writes the DuckDNS TXT
     record, verifies the domain, activates Cloud Identity Free, enrols the admin
     authenticator, and drives the Cloud console. It pauses at the phone gate:
     the QR image and any decoded number/message are shown in the Setup tab, and
     the run resumes when Google lets the page through. A captcha is handled the
     same way. If a console page changes under it, the tab logs where it stopped;
     use **Go to Keys** to finish the key manually. The Setup tab also explains
     how to get the DuckDNS token and subdomain; the business/admin fields have
     **random** buttons.
   The key row has **Admin** / **Recover admin** (show or reset the tenant admin
   password and mint fresh backup codes using the key) and **JSON** (download the
   key so it is backed up outside extension storage).
   Set tenant 2SV **Enforcement** to **On** with **Methods** = *Any except
   verification codes via text, phone call*: enforcement off makes Google ask for
   a phone number on every fresh account, and a method list that allows nothing
   enroll-able produces *"Your sign-in settings don't meet your organization's
   2-Step Verification policy"*.
3. **Run** — choose a count, press Start. Each identity gets ten minted backup
   codes; they land on the account row (`codes 10`) and in the export as
   `backup_codes:`. The run accepts the new-account terms interstitial, enrols the
   authenticator app (secret saved as `otp_secret`), then finalises the Roblox
   account by driving `www.roblox.com/not-approved` itself: I Agree, username,
   birthday, Continue — and only closes the tab once the URL is `/home`. If the
   page freezes, it reloads and retries a few times. After a successful signup it
   also checks the identity's Google 2SV status and warns if enrolment did not
   finish.
4. **Settings** — choose whether Roblox keeps its generated username or the run
   sends one from the local generator, set the birthday and gender written at
   finalise (tick **random** under either to roll a fresh value per identity),
   turn the automatic terms acceptance on or off, and tune identity concurrency.
   Two more knobs live there: **lane surface** (a background tab by default —
   a `world: "MAIN"` shim supplies the frame clock, so no window appears; switch
   to *own window* only if a page manages to stall it) and **deletion pacing**,
   which walks a small batch of users a minute apart instead of a burst.
   **2-Step Verification** has no switch: every lane mints backup codes before it
   opens, and asks for a set mid-challenge if Google wants one the runner does
   not hold, because an identity with neither codes nor an authenticator has no
   second factor and is refused outright. It also offers the `moderated`
   re-auth retry; **Egress** takes
   one SOCKS5 per lane, cycling through a list, with each lane logging the exit
   IP it actually got.

Accounts are written to extension storage the moment the cookie lands. Export is
a mirror; closing the tab loses nothing.

## Layout

```
extension/
  manifest.json          Chrome MV3 (service worker)
  manifest.chrome.json   mirror of the same manifest, kept for old build scripts
  runner.html/.css/.js   the run loop and the whole UI
  background.js          opens the runner, owns the log ring
  content/google.js      fills Google's sign-in pages
  content/setup.js       drives the automatic Cloud Identity signup / console
  content/awake.js       main-world shim: frame clock + visibility for hidden lanes
  lib/cdp.js             chrome.debugger transport
  lib/cdp-setup.js       trusted-input signup driver
  lib/                   admin, lanes, keys, consent, QR, storage
```

The Python pipeline that preceded the extension is not part of this tree; it
remains in the Git history. The extension is self-contained.

## Testing

Load the unpacked extension in Chrome, add a key, and run one identity. The
automatic setup can be tested separately from the Setup tab.

`tools/e2e.py` is the older Firefox harness; it is legacy and not part of the
Chrome target.
