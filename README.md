# Roblox Alt Creator

A browser extension that turns Cloud Identity keys into Roblox accounts.
Setup instructions, key validation, the run console and the phone-gate decoder
all live inside the extension.

The Workspace tenant and the DuckDNS name are created by hand. Google rejects
automated Workspace signups as spam, so the extension does not try. It can,
however, finish an existing tenant: sign in once in a worker tab and it handles
domain verification, Cloud Identity Free, the admin authenticator, the tenant
2SV policy and the OAuth key.

## Firefox — primary target

Firefox is the target browser: containers give each lane its own cookie jar, so
several sign-ins run in one window. The automatic setup also opens its worker tab
in a dedicated **RAC setup** container, so the admin/console/consent session is
isolated from normal browsing.

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

## Chrome / Chromium — optional, one lane

Chrome has one cookie jar per profile, so it runs one lane. Build the Chrome
manifest into a separate folder:

```bash
rm -rf dist/chrome
cp -r extension dist/chrome
cp dist/chrome/manifest.chrome.json dist/chrome/manifest.json
```

```
chrome://extensions → Developer mode → Load unpacked → dist/chrome
```

Running several Chrome lanes means one Chrome profile per lane; it is not a
container replacement.

## Patchright CLI (debug companion)

`tools/rac_cli.py` runs the same setup pages in a persistent Chrome profile
through Patchright. It is for debugging, screenshots, console logging and
testing trusted input from a normal terminal instead of the extension UI.

```bash
python3 -m venv .venv
.venv/bin/pip install -r tools/requirements-cli.txt

.venv/bin/python tools/rac_cli.py doctor
.venv/bin/python tools/rac_cli.py smoke --headless
.venv/bin/python tools/rac_cli.py admin
.venv/bin/python tools/rac_cli.py duckdns
.venv/bin/python tools/rac_cli.py signup
.venv/bin/python tools/rac_cli.py open https://example.com --wait 5 --screenshot example.png
```

- Headful is the default for `admin`, `duckdns`, `signup` and `open`. `smoke`
  and `doctor` default to headless.
- `--log-console` prints page console output and page errors.
- `--screenshot` writes to `debug/` unless an absolute path is given.
- The persistent profile defaults to
  `~/.local/share/roblox-alt-creator/patchright-profile`, outside the repo.
- `--proxy`, `--proxy-user` and `--proxy-pass` are supported.

The CLI does not bypass Google’s phone/captcha or anti-abuse checks. It pauses
there and leaves the page open for a human. It is a companion to the extension,
not an anti-detection guarantee.

## First run

The runner opens on install. If it does not, click the toolbar icon and choose
**Open runner**.

1. **Keys** — drop the `client_secret_*.apps.googleusercontent.com.json` you
   downloaded from the Cloud console, then press **Authorize** on the tenant row
   and approve the consent tab as the tenant admin. A ready-made key with
   `client_id`, `client_secret` and `refresh_token` can be pasted instead. The
   domain is detected from the tenant, so there is no domain field.
2. **Setup** — the buy hint sits at the top; the five manual steps are always
   visible.
   - **Step 1 — DuckDNS.** Create the subdomain and copy the account token. Save
     both here. The same pair is used by step 2 and by the automatic branch.
   - **Step 2 — Cloud Identity tenant.** Create the tenant, verify the domain and
     create the admin account manually. The Phone gate card can decode the admin
     signup QR and read the SMS number/message.
   - **Sub-step checklist.** Every sub-step has a checkbox. Tick the ones you
     have already done. Ticking a later sub-step checks everything before it;
     unticking one clears everything after it. The setup bar counts sub-steps,
     so it moves on every tick. Automatable sub-steps also have an **Auto**
     button that runs only that one task; **Automate remaining** runs every
     unchecked automatable sub-step in order. The **Keys** tab has a
     **Create a key in Setup** button that opens this step.
   - **Automate remaining** opens one new tab at `admin.google.com` in the
     `RAC setup` container (Chrome: the current profile). Sign in there as the
     tenant admin; the extension takes over when it lands in the Admin console.
     It never asks for, stores or types the admin password.
   - The automatic tasks check the domain (and write the DuckDNS TXT record if
     Google shows one), activate Cloud Identity Free if it is missing, enrol the
     admin authenticator, set the tenant 2SV policy and build/authorize the key.
     Each finished task ticks its own checkbox. If a page or a login challenge
     needs a human, it waits in the worker tab; the run resumes when the page
     moves on. Use **Stop** to go back to manual.
   - Tasks with prerequisites have their Auto button disabled until the earlier
     sub-step is ticked. The five OAuth scopes are attempted automatically: the
     Auto task filters the scope picker and ticks each scope. If the picker
     differs, add the scopes manually and tick the sub-step; the remaining key
     tasks stay gated on it, so a key cannot silently miss the audit/policy
     scopes.
   - Step 5 also has Auto buttons for **Check the key**, **Run one identity**
     and **Export accounts**. Those run locally and are not part of
     **Automate remaining**.
   - **Already-enrolled admin safety.** If the admin already has 2SV but the
     extension has no authenticator secret for it, it does not change the tenant
     policy. A phone-only admin would be locked out. It marks the authenticator
     sub-step done, leaves the policy sub-step manual and continues to the key;
     set the policy manually only if another allowed factor exists.
   - The key row has **Admin** / **Recover admin** (show or reset the tenant admin
     password and mint fresh backup codes using the key) and **JSON** (download the
     key so it is backed up outside extension storage).
   - Set tenant 2SV **Enforcement** to **On** with **Methods** = *Any except
     verification codes via text, phone call*: enforcement off makes Google ask
     for a phone number on every fresh account, and a method list that allows
     nothing enroll-able produces *"Your sign-in settings don't meet your
     organization's 2-Step Verification policy"*.
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
  manifest.json          Firefox MV3 (containers, background scripts)
  manifest.chrome.json   Chrome MV3 (service worker, one lane)
  runner.html/.css/.js   the run loop and the whole UI
  background.js          opens the runner, owns the log ring
  content/google.js      fills Google's sign-in pages and the 2SV wizard
  content/setup.js       finishes the tenant: Admin console + Cloud console
  content/awake.js       main-world shim: frame clock + visibility for hidden lanes
  lib/                   admin, lanes, keys, consent, QR, storage
tools/
  rac_cli.py             Patchright debug companion (setup pages, screenshots)
  requirements-cli.txt   patchright
  e2e.py                 older Firefox lane harness
```

The Python pipeline that preceded the extension is not part of this tree; it
remains in the Git history. The extension and the Patchright CLI are
self-contained.

## Testing

Load the unpacked extension in Firefox (or the Chrome build) and add a key. From
Setup, save a DuckDNS token/subdomain, tick the step-1 sub-steps and press an
**Auto** button or **Automate remaining** to check the worker-tab handoff;
sign-in stops at `accounts.google.com` until a human signs in, which is the
expected boundary.

`tools/e2e.py` is the older Firefox harness; it is legacy but still useful for the
lane run, identity creation and code minting.
