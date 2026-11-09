# Roblox Alt Creator

A local Node.js CLI that turns Cloud Identity keys into Roblox accounts. It runs
real Chrome through [Patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright),
reuses the extension's JavaScript core and content scripts, and keeps state in a
local JSON file.

The CLI can:

- sign in to the Admin console with a saved admin password and authenticator
  secret, or leave the password to you;
- sign in to DuckDNS with the tenant admin Google account, add the subdomain and
  capture the token;
- prefill the Cloud Identity signup through the account step, then stop at
  Google's phone/captcha gate;
- verify the domain, activate Cloud Identity Free, enrol the admin authenticator,
  set the tenant 2SV policy, create the Cloud project, enable the Admin SDK, set
  up the consent screen, add the five scopes, create the desktop OAuth client and
  authorize the key;
- import/check/recover keys and create Google identities with the Admin SDK;
- run Roblox Google-SSO lanes in separate persistent Chrome profiles, including
  the Google prime, authenticator capture, cookie capture and Roblox finalise.

The Workspace tenant signup and the phone/captcha gate remain external. The CLI
does not bypass them. Everything after the gate can be automatic.

## Node CLI

```bash
npm install
node bin/rac.js doctor
node bin/rac.js smoke --headless
node bin/rac.js help
```

`npm link` is optional; it makes `rac` available as a command:

```bash
npm link
rac doctor
```

Patchright drives the system Chrome by default (`--channel chrome`). If Chrome
is not installed, run `npx patchright install chromium` and pass
`--channel none`.

### Commands

| Command | What it does |
|---|---|
| `rac doctor` | checks Node, Patchright, Chrome and trusted input |
| `rac smoke` | verifies `navigator.webdriver: false` and a trusted click |
| `rac admin` | opens or signs in to the Admin console |
| `rac duckdns` | signs in with Google, adds a subdomain and captures the token |
| `rac signup` | prefills the Cloud Identity signup and stops at the phone gate |
| `rac setup` | runs the automatic tenant/key sub-steps |
| `rac key …` | import, list, check, authorize, recover, remove, export |
| `rac identity …` | create/list/delete identities, check 2SV, mint codes |
| `rac run` | create identities and run Roblox Google-SSO lanes |
| `rac export` | write the captured accounts to `accounts.txt` |
| `rac state` | show the state path and a summary |

### Typical flow

```bash
# 1. One-time tenant signup. Finish the phone/captcha gate in the window.
node bin/rac.js signup --subdomain mytenant --contact you@example.com

# 2. Create the DuckDNS subdomain and capture its token.
node bin/rac.js duckdns --subdomain mytenant --email admin@mytenant.duckdns.org

# 3. Finish the tenant and build/authorize the OAuth key.
#    Add --totp <base32 secret> if the admin already has 2SV.
node bin/rac.js setup --subdomain mytenant \
  --admin-email admin@mytenant.duckdns.org \
  --admin-password '<admin password>'

# 4. Run a Roblox account.
node bin/rac.js run --count 1

# 5. Export.
node bin/rac.js export
```

If you already have a key:

```bash
node bin/rac.js key import client_secret_*.json
node bin/rac.js key authorize
node bin/rac.js run --count 1
```

`rac run` deletes the Google identity after a successful Roblox signup by
default, which frees the Cloud Identity seat. Pass `--keep-google` if you want
to keep Google sign-in for that account.

State lives outside the repo:

```
~/.local/share/roblox-alt-creator/state.json
~/.local/share/roblox-alt-creator/profiles/<name>
```

Override the root with `RAC_HOME=/path/to/dir`. `state.json` is written with
mode `0600`. The CLI never writes credentials into the repository.

Admin credentials can also come from the environment instead of the command
line: `RAC_ADMIN_EMAIL`, `RAC_ADMIN_PASSWORD`, `RAC_TOTP`.

### Profiles

- `setup` is the default profile for `admin`, `duckdns`, `signup` and `setup`.
- `lane-1` … `lane-N` are the Roblox lane profiles used by `rac run`.
- Profiles are persistent, so a Google login or DuckDNS login survives a re-run.
- `--proxy` is supported on the browser commands; `rac run` cycles
  `settings.proxyUrl` / `settings.proxyList` across lanes.

### Safety boundaries

- No anti-detection guarantee. Patchright hides common automation leaks and
  produces trusted input, but Google's signup decision is driven by reputation,
  IP/ASN, phone and behavioural signals as well.
- The phone/captcha gate is left to a human. `rac signup` fills the form and
  submits the account step; it does not solve or bypass verification.
- If the admin already has 2SV but no saved authenticator secret, `rac setup`
  refuses to change the tenant policy. A phone-only admin would be locked out.

## Firefox extension (optional)

The extension is still in the tree as a GUI and a Firefox-containers lane runner.
For the CLI, skip to the commands above. Firefox containers give each lane its
own cookie jar, so several sign-ins run in one window. The extension's automatic
setup also opens its worker tab in a dedicated **RAC setup** container, so the
admin/console/consent session is isolated from normal browsing.

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

### Chrome extension build

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

## Legacy Python Patchright companion

The Node CLI above supersedes this. `tools/rac_cli.py` remains as a small
diagnostic runner for screenshots, console logging and trusted-input checks.

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

## Extension first run

The runner opens on install. If it does not, click the toolbar icon and choose
**Open runner**.

This section describes the extension only. CLI users should use the commands
above.

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
bin/rac.js               CLI entry point
cli/
  main.js                command dispatch
  browser.js             Patchright launch, logging, screenshots
  content-bridge.js      injects content/google.js + content/setup.js
  lane-api.js            Playwright adapter for extension/lib/lane.js
  consent.js             OAuth consent flow and code exchange
  google.js              Google sign-in driver
  key-flow.js            key import/check/recover helpers
  state.js               atomic JSON state store
  commands/              doctor, smoke, admin, duckdns, signup, setup,
                         key, identity, run, export, state
extension/
  manifest.json          Firefox MV3 (containers, background scripts)
  manifest.chrome.json   Chrome MV3 (service worker, one lane)
  runner.html/.css/.js   the run loop and the whole extension UI
  background.js          opens the runner, owns the log ring
  content/google.js      fills Google's sign-in pages and the 2SV wizard
  content/setup.js       finishes the tenant: Admin console + Cloud console
  content/awake.js       main-world shim: frame clock + visibility for hidden lanes
  lib/                   admin, lanes, keys, consent, QR, storage
tools/
  rac_cli.py             legacy Python Patchright diagnostic runner
  requirements-cli.txt   patchright (Python)
  e2e.py                 older Firefox lane harness
package.json             Node CLI dependencies and bin entry
```

The Python pipeline that preceded the extension is not part of this tree; it
remains in the Git history. The Node CLI reuses `extension/lib` and
`extension/content` directly.

## Testing

Automated checks:

```bash
npm test
```

CLI smoke checks:

```bash
node bin/rac.js doctor --headless
node bin/rac.js smoke --headless
node bin/rac.js setup --dry-run --subdomain test --token 00000000-0000-0000-0000-000000000000
```

The full CLI integration was exercised against real Chrome: Roblox SSO opened
Google, the injected content script filled the identifier, and the lane timed out
at the expected rejected-account screen. Setup and consent paths require a real
tenant to exercise end to end.

The extension is tested by loading it in Firefox (or the Chrome build) and
running the same checklist. `tools/e2e.py` is the older Firefox harness; it is
legacy but still useful for the extension lane path.
