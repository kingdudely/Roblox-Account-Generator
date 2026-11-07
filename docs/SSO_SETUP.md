# Google SSO path — from a blank machine to 50 Roblox accounts

> **Historical.** This was written for the per-tool layout. SSO helpers live in
> `tools/`; pre-SSO research probes live in `legacy/tools/`. [`README.md`](../README.md)
> is the current setup and `main.py` is the main entry point. The measurements,
> failure modes and console evidence in this document are still accurate.

This is the path that skips the captcha entirely. Not solved, not bypassed —
**never asked**. Roblox's "Sign in with Google" flow creates an account from a
Google identity and never calls `/v2/signup`, never calls `/v2/captcha`, never
loads Arkose, and never shows the press-and-hold gate. Confirmed twice by wire
capture, then again by simply using the accounts.

The cost is not money. It is **identity supply**: one Google identity produces
exactly one Roblox account, so the whole pipeline reduces to "how do I get 50
Google identities cheaply". This document answers that, end to end, including the
four dead ends measured along the way so you do not repeat them.

Everything here is measured. Where something is a guess it says so.

> **The order matters, and it is not the order Google's UI suggests.**
> Domain → **verify it** → *then* switch on Cloud Identity. Google will refuse
> Cloud Identity while the domain is unverified, so any guide that starts with
> "sign up for Cloud Identity" strands you. This one does not.

---

## Contents

1. [What actually happens](#1-what-actually-happens)
2. [Prerequisites](#2-prerequisites)
3. [Part 1 — a domain you control](#3-part-1--a-domain-you-control)
4. [Part 2 — create the Google account](#4-part-2--create-the-google-account)
5. [Part 3 — verify the domain](#5-part-3--verify-the-domain)
6. [Part 4 — switch on Cloud Identity Free](#6-part-4--switch-on-cloud-identity-free)
7. [Part 5 — create the users](#7-part-5--create-the-users)
8. [Part 6 — turn each identity into a Roblox account](#8-part-6--turn-each-identity-into-a-roblox-account)
9. [Part 7 — verify and store](#9-part-7--verify-and-store)
10. [Troubleshooting](#10-troubleshooting)
11. [Limits, costs, and what is still unknown](#11-limits-costs-and-what-is-still-unknown)
12. [File map](#12-file-map)

---

## 1. What actually happens

```
roblox.com/CreateAccount            (new UI, "Sign in with Google" present)
        │
        ├── click Google
        ▼
accounts.google.com/…/consent       you sign in as the managed identity
        │
        ├── 302
        ▼
auth.roblox.com/v1/external/3161830277039358/sso/oauth/callback?state=…
        │
        ├── 302
        ▼
roblox.com/sso-complete?sso-type=signup   ← account is created here
        │
        ▼
.ROBLOSECURITY  (httpOnly cookie — this is the credential from now on)
```

Two things fall out of that shape:

- **`sso-type=signup` vs `sso-type=login`** is the difference between a new
  account and an existing one. A Google identity that already owns a Roblox
  account returns `login` and lands on that same account. **One identity, one
  account.** This was tested by hand and is the hard constraint on throughput.
- **The cookie is the whole credential.** Google is needed once, at creation.
  After that nothing in the pipeline touches Google again, which is why a seat
  can be recycled later without touching the Roblox account.

Roblox assigns the username. The username step at `/sso-complete` is skippable;
the session works without it.

---

## 2. Prerequisites

| need | why |
|---|---|
| Python 3.11+ | the tooling |
| a phone with a real SIM, for **one** SMS | Google's admin-creation gate (see below) |
| a domain, ~$0–12/yr | Cloud Identity requires a domain you can put a TXT record on |
| nothing else | no proxies, no captcha credits, no SMS-rental accounts |

```bash
git clone <this repo> && cd Roblox-Alt-Creator
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
```

Every command in this document assumes `PYTHONPATH=.` and the venv python.

> **The one unavoidable gate.** Google will not let an automated signup create an
> identity. A fresh web signup runs into `signup/crossflowverification`, which is a
> QR code; scanning it opens a phone page that says *"Your phone will open an SMS
> message with a code you need to send to verify your phone."* It is an
> **outbound** SMS from a real SIM. Receive-only SMS services (5sim, sms-activate)
> cannot pass it, which is why every public "Gmail creator" repo is stale.
> You do this **once**, for the admin account. The other 49 users are provisioned
> by that admin with no verification per user.

---

## 3. Part 1 — a domain you control

Any domain works. The free route, and the one this repo's tooling is built for, is
**DuckDNS**: it hands out `something.duckdns.org` subdomains and — unusually for a
free dynamic-DNS service — exposes a **TXT record API**, which is the only thing
Cloud Identity actually needs.

### 3.1 Claim it

1. Go to <https://www.duckdns.org>.
2. Sign in with GitHub, Google, Reddit or Twitter. There is no email signup.
3. Add a subdomain, e.g. `acmefarm`, giving `acmefarm.duckdns.org`.
4. Copy the **token** from the top of the page.

Notes measured during setup:

- `duckdns.org` itself already carries a Google verification TXT, so the apex is
  claimed by someone else. That is fine — `duckdns.org` is on the public suffix
  list, so every subdomain is a separately registrable domain.
- The A record is irrelevant to Google's check. Only TXT matters.
- Creating a **new** subdomain through the update API fails (`KO`). New names must
  be added in the web UI; the API only updates names that already exist.

### 3.2 Why not something else

| option | verdict |
|---|---|
| DuckDNS | works, free, has a TXT API — **use it** |
| afraid.org | plausible, has TXT, slower to set up |
| `*.eu.org` | works, but manual approval takes weeks |
| a real `.xyz`/`.top` | ~$1–3/yr, zero shared-namespace risk — the safe fallback |
| a temp-mail address | **does not solve this problem at all** (see §11) |

---

## 4. Part 2 — create the Google account

You need a **Google tenant bound to your domain** plus one admin identity on it.
Two funnels both produce that, and for this pipeline it genuinely does not matter
which one you use — what matters is that the tenant exists and you can reach the
deep links in §6.

| entry point | what it gives you | card needed |
|---|---|---|
| `https://workspace.google.com/gcpidentity/signup?sku=identitybasic` | Cloud Identity Free, direct | no |
| `https://workspace.google.com/signup` (or `?type=identity`) | a Workspace trial tenant | no, until checkout |

Use whichever you land on. If you take the Workspace funnel you will be shown a
plan picker at `$8.40–$19.80` **per user per month** — **do not enter a card, do
not pick a plan.** Close that tab. §6 converts the tenant to Cloud Identity Free
afterwards; nothing is charged and nothing needs cancelling in the meantime
(Google abandons an unfinished trial on its own if the domain is never verified).

The correct Cloud Identity page has the title **"Sign Up for Cloud Identity"** — a
quick way to confirm you are not in the Workspace funnel.

### 4.1 The signup itself

Fields that matter:

- **Current email address** — your real address. It becomes the recovery contact.
  It does **not** become the account; the admin identity is `admin@yourdomain`.
- **Domain** — your DuckDNS subdomain, e.g. `acmefarm.duckdns.org`.
- **Admin username/password** — the identity you will administer with.

Then the QR gate described in §2. Scan it, send the SMS, switch back.

> Mail note: a DuckDNS subdomain has no MX records, so `admin@…duckdns.org` cannot
> receive mail. Cloud Identity's docs suggest working mail or forwarding for the
> admin address. It does not block OAuth, and recovery runs through the real
> address you supplied.

**Stop here.** The account exists; the domain is not verified yet, and Cloud
Identity will refuse to switch on until it is.

---

## 5. Part 3 — verify the domain

This step is **not** optional and **not** only for people who already had a tenant.
It comes before Cloud Identity in every case, because Google's own flow answers
*"You can't sign up for Cloud Identity because your domain is not yet verified."*

Go straight to the domains list — never the console home:

```
https://admin.google.com/ac/domains/manage
```

Google will show one line, shaped like this:

```
google-site-verification=5Qzb5Sgz_RRKsF7j2mmmO-gQ1W7ISZQlSxhrycOdTSE
```

The CNAME alternative is useless on DuckDNS — there is no CNAME support.

### 5.1 Write it

```bash
./tools/domain_txt.py --domain acmefarm --token <duckdns-token> \
  --txt google-site-verification=5Qzb5Sgz_RRKsF7j2mmmO-gQ1W7ISZQlSxhrycOdTSE
```

The tool writes the record and then queries **1.1.1.1 and 8.8.8.8** so you only
press Verify once the value is provably visible from outside DuckDNS.

### 5.2 Two DuckDNS behaviours that waste an evening

1. **Never send `ip` in the same request as `txt`.** DuckDNS treats that as an
   A-record update and silently ignores the TXT value, answering `NOCHANGE` while
   the old value stays. The tool never passes `ip`.
2. **The TXT slot is one-way.** `clear=true` and an empty `txt` both answer `OK`
   without removing anything. Overwrite is the only operation. Harmless — Google
   reads one value — but do not expect to blank it.

Propagation measured at ~4 seconds. Public resolvers cache for 600s, so if a check
goes red immediately after a change, that is TTL, not failure.

Press **Verify** in the console once `dig` agrees.

---

## 6. Part 4 — switch on Cloud Identity Free

Now, and only now, activate the free edition. Direct link:

```
https://admin.google.com/ac/billing/catalog
```

→ category **Cloud Identity** → **Cloud Identity Free** → **Get Started**.
Never the "Start free trial" button next to **Premium**.

> **Never start at `admin.google.com`.** On a tenant with no paid plan the console
> home renders the subscription surface instead of the dashboard. Deep links
> bypass it entirely — bookmark these:

| direct URL | goes to |
|---|---|
| `https://admin.google.com/ac/users` | user list |
| `https://admin.google.com/ac/user/bulkadd` | multi-row "add users" form |
| `https://admin.google.com/ac/domains/manage` | domain list + verification |
| `https://admin.google.com/ac/billing/catalog` | buy-or-upgrade, where Cloud Identity Free lives |
| `https://admin.google.com/ac/billing/subscriptions` | current subscriptions |

If **Get Started** reports the domain is unverified, §5 was skipped or has not
propagated — go back and confirm with `dig`, then retry. That message is progress,
not failure: it means the tenant is otherwise eligible.

Cloud Identity Free gives **50 users** per account, $0, no Gmail, third-party app
integration included. That last item is the one that matters — it is what lets
these identities complete a Google OAuth consent.

---

## 7. Part 5 — create the users

Two routes. The API is better; the console works and needs no Google Cloud setup.

### 7.1 What the console will *not* do

There is **no CSV bulk-upload screen** in the Cloud Identity console. Checked
directly against `https://admin.google.com/ac/users`:

- toolbar: `Add new user | Download users | More options`
- `More options` contains only: custom attributes, transfer tool for unmanaged
  accounts, allow users to edit profile, recently deleted users
- the console's own search returns *"How to create users in bulk?"* as a **help
  article**, not a page

Google's documentation for CSV upload describes a Workspace console that Cloud
Identity does not ship.

### 7.2 Route A — the bulk-add form (no Google Cloud)

Clicking **Add new user** does not open a single-user dialog; it routes to
`https://admin.google.com/ac/user/bulkadd`, a row-repeater form. Navigate there
directly rather than through the users list — it saves two round trips and avoids
the console home entirely.

```
First name* | Last name* | Primary email address* | Secondary email address
                              @yourdomain.duckdns.org ▾
+ Add another user                       Cancel | Continue
```

```bash
./tools/admin_add_users.py --mode probe                      # look, change nothing
./tools/admin_add_users.py --mode fill --start 0  --batch 10
./tools/admin_add_users.py --mode fill --start 10 --batch 10
```

Both tools drive a persistent Chromium profile at `.profiles/admin`. The first run
stops and waits for you to sign in as the admin; the session then persists.

Mechanics worth knowing, all discovered the hard way:

- **Email field takes the local part only.** The domain is fixed by the console.
- **Continue stays disabled until every row's username check returns.** Checks are
  asynchronous per row; the driver polls for `Available × rows` plus an enabled
  button.
- **The caret must leave the last field.** Continue validates on blur, and while a
  field holds focus the click silently does nothing. The driver presses Tab and
  clicks header whitespace first.
- **Rows re-render during the check**, which detaches DOM nodes. The driver reads
  values back from the DOM after typing and repairs only mismatches, so an extra
  click from a human watching the window does not desync it.

> **The password trap.** This form has no password field. Google generates one per
> user and shows it **once**, on a results page that only exists while the browser
> stays open, masked behind an eye/copy icon. Anything not read there is gone;
> there is no "view password" later. The results page is also slow — users appear
> one at a time as `Adding user…` then `User added`.
>
> If you lose one, the only fix is `users.update` with a new password (§7.3). You
> cannot delete and re-create the address: Google reserves deleted users' emails.

Route A therefore records what it can and expects to be repaired by the API.

### 7.3 Route B — the Admin SDK (recommended)

The API sets the password you specify. No race, no clipboard, no one-time secret.
It also repairs accounts whose generated passwords are already lost, because a
password update does not need the old one.

#### One-time: an OAuth client

Google will not let a program create its own credentials. Roughly six clicks:

1. `https://console.cloud.google.com/` signed in as the **admin** identity
2. `https://console.cloud.google.com/projectcreate` → name it → Create.
   **Organization: use the one offered.** A managed tenant always has an
   organization node, so "No organization" is not available and is not needed —
   projects under the org work identically for this. Do not go hunting for a
   workaround.
3. `https://console.cloud.google.com/apis/library/admin.googleapis.com` → **Enable**
4. `https://console.cloud.google.com/apis/credentials/consent` → **Get started**
   - App name: anything
   - User support email: your real address
   - Audience: **Internal** if it is offered → Create

   > Having an organization is an advantage here. **Internal** publishes the app to
   > your own domain only, so there is no "Google hasn't verified this app"
   > interstitial, no test-user list, and no 7-day refresh-token expiry. Fall back
   > to **External** only if Internal is unavailable.
5. **Data access → Add or remove scopes** → filter `admin.directory.user` → tick
   `https://www.googleapis.com/auth/admin.directory.user` → Update → Save
6. **Audience → Test users → Add users** → `admin@yourdomain`
   *(External only. Skip for an Internal app — every account in the domain is
   already allowed.)*
7. `https://console.cloud.google.com/apis/credentials` → **Create credentials →
   OAuth client ID → Desktop app** → Create → **Download JSON**

Save that file as `accounts/cloudidentity/oauth_client.json`. It must be kind
`installed` (Desktop app), not `web`.

#### Authorize

```bash
./tools/admin_api_users.py --mode auth
```

Opens your admin profile at the consent screen. The tool catches the `code=` off
the `localhost:8765` redirect and saves a refresh token to
`accounts/cloudidentity/token.json` (mode 600). No local server is needed; the
redirect failing to load *is* the mechanism.

For an **External** app expect a warning — **"Google hasn't verified this app"** —
which is normal in Testing mode: click **Advanced → Go to \<app\> (unsafe)**.
Testing-mode refresh tokens expire after **7 days**; re-run `--mode auth` when it
lapses. An **Internal** app skips both of these.

#### Populate

```bash
./tools/admin_api_users.py --mode sync --dry-run     # what it would do
./tools/admin_api_users.py --mode sync               # do it
```

Behaviour per user:

- `users.insert` with `primaryEmail`, `name`, `password`,
  `changePasswordAtNextLogin: false`
- on `409 duplicate`, falls back to `users.update` for the password — this is how
  accounts with lost generated passwords get fixed
- `--existing-only` skips creation and only resets passwords

Identities come from `accounts/cloudidentity/users.json`, a 49-entry ledger of
`{first, last, email, password}` generated by:

```bash
./tools/identity_csv.py --domain yourdomain.duckdns.org --count 49
```

(`users.csv` alongside it is for Workspace console CSV upload; Cloud Identity
does not offer that screen, so treat it as reference only.)

---

## 8. Part 6 — turn each identity into a Roblox account

This is where the captcha disappears.

```bash
PYTHONPATH=. .venv/bin/python tools/oauth_capture.py --tries 30 --minutes 40 --follow 120
```

What it does:

1. Re-rolls fresh contexts until the signup layout includes the Google button.
   Roblox rolls the layout per browser session — roughly one in three is
   `refreshSsoInline`, which has Google and Apple. Reloads do not change it; only
   a new context does.
2. Records every request, response header and cookie mutation.
3. Follows the redirect chain to `sso-complete`.
4. Reads `.ROBLOSECURITY`, validates the session **in the browser**, and writes
   `cookie.txt` plus `summary.json`.

### What you do

If the identity has never signed in, mint a backup code first (§8.1) — otherwise
the first sign-in stops at the phone challenge.

Sign in with **the Google identity you want to spend**. One identity per account —
if it already owns a Roblox account you will get `sso-type=login` and the same id
back.

To do the whole thing unattended instead of by hand, use `tools/sso_fleet.py`,
which mints a code, drives the sign-in, and writes the cookie to
`accounts/sso/accounts.jsonl`:

```bash
PYTHONPATH=. DISPLAY=:0 .venv/bin/python tools/sso_fleet.py --start 2 --limit 1 --calibrate
PYTHONPATH=. DISPLAY=:0 .venv/bin/python tools/sso_fleet.py --start 2 --limit 10
```

Leave the window open until the tool says it is done. It ends the capture when the
cookie lands.

### Why the validation has to happen in-browser

`users.roblox.com/v1/users/authenticated` returns **`403 User is moderated`** to
`curl_cffi` with a perfectly valid SSO cookie, and **`200`** with the same cookie
from page context. Trust the browser, not the HTTP client.

**But read the error before believing it.** `User is moderated` is also what Roblox
returns for an account that exists and is authenticated but has not been
*finalised*. An SSO signup creates the account immediately and then leaves it at
`/not-approved` behind a **"One last thing"** dialog — birthday, username, gender,
terms — and until that form is submitted every authenticated call answers
`403 User is moderated`. It looks exactly like a ban and is not one. If a cookie
whose account shows a username in the site header still 403s, the account needs
finalising, not throwing away (§8.2).

### Layout variants

| variant | Google button |
|---|---|
| `refreshSsoInline` | **yes** — this is the one you want |
| `refreshSsoDialog` | no |
| `control` (classic) | no |

`tools/open_signup.py --ua mac` raises the odds if you want to inspect by hand.

### 8.1 The first-sign-in challenge — and the backup code that beats it

A freshly created Cloud Identity user has no sign-in history, so Google challenges
the first password sign-in with **"Verify it's you → enter a phone number"**. This
is the single hard wall on the whole path, and three intuitive fixes do not work:

| attempt | why it fails |
|---|---|
| Set a recovery email | Google still offers phone only — recovery email is for account *recovery*, not the risk challenge |
| Enforce 2-Step Verification | with enforcement on and no method enrolled the account deadlocks: *"Your sign-in settings don't meet your organization's 2SV policy"* |
| Delete and recreate the users | identical state (new account, no history) and Google reserves deleted addresses ~20 days, so the same local parts are unusable anyway |

Enforcing 2SV with methods set to `Any except verification codes via text, phone
call` also deadlocks, because enrollment itself needs a method the policy forbids.
There is no admin-side control to enroll a TOTP secret for a user — the Admin SDK
exposes `twoStepVerification.turnOff` and nothing that turns it on, deliberately:
an admin who could mint a second factor would make 2SV decorative.

**What does work is a backup verification code.** Ten are minted per user, and
Google accepts one at the challenge screen:

1. Admin console → user → **Security** → **2-Step Verification** →
   `GET BACKUP VERIFICATION CODES` → `GENERATE NEW CODES` → **SAVE**
2. At the challenge: **Try another way → Enter one of your backup codes**

Or mint them from code, which is the version worth using:

```bash
# needs the admin.directory.user.security scope (see 7.3)
PYTHONPATH=. .venv/bin/python tools/admin_api_users.py \
    --mode codes --user admin@example.duckdns.org
```

```
admin@example.duckdns.org: 10 codes
  94410272
  25286968
  …
```

Each code is **single-use**, so an identity whose account is already created cannot
be replayed with the same one. That is fine, because the Google identity is spent
the moment `.ROBLOSECURITY` lands — the Roblox account never needs it again.

Two properties make this the right primitive rather than a workaround:

- **No phone, ever.** The code is a 2SV credential, not an SMS fallback.
- **Scriptable.** `verificationCodes.generate` then `verificationCodes.list` returns
  the codes as JSON, so there is no console clicking and no browser automation of
  the admin side at all.

The `twoStepVerification` and `verificationCodes` resources hang off the **service
root** in the Python client (`service.verificationCodes()`), not off `service.users()`,
even though the discovery paths contain `/users/`.

### 8.2 Finalising the account (the step that looks like a ban)

An SSO signup mints the account in the same redirect that sets the cookie, but
Roblox does not mark it usable. It lands on `/not-approved`, and until it is
finalised `users.roblox.com/v1/users/authenticated` answers `403 User is
moderated` for a session that is otherwise valid — the username is right there in
the site header.

**The whole dialog is one call.** Captured from a live signup:

```
POST https://auth.roblox.com/v1/account/upgrade
{"upgradeType":"OAuth","birthday":"1996-05-15T00:00:00.000Z","gender":3}

→ 200 {"userId":11944409701,"username":"ElfinPace3515"}
```

`X-CSRF-TOKEN` is learned the usual way on this host: send without it, take the
token from the `x-csrf-token` header on the 403, retry once.

`tools/roblox_http.py` does this (`finish()` is idempotent — it validates first and
only upgrades if the session is still pending). `tools/sso_fleet.py` calls it as
the primary path and keeps the browser form only as a fallback.

Before that call, the account answers `403`; after it, `200`:

```
before: 403 {"message":"User is moderated"}
after:  200 {"id":11944409701,"name":"ElfinPace3515"}
```

So pending accounts are **recoverable** — sign in again (`sso-type=login`) and
finalise, rather than discarding the identity.

<details>
<summary>Doing it through the UI (kept as fallback, and why each obvious approach fails)</summary>

| what looks right | what is actually true |
|---|---|
| `#MonthDropdown` / `#DayDropdown` / `#YearDropdown` | those ids do not exist on this page |
| `select_option()` on the birthday selects | the `<select>` elements exist but are `display:none` fallbacks; setting them changes nothing the form reads |
| click the option at its coordinates | the listbox renders in a Radix portal, so the coordinates hit the overlay and dismiss the dialog |

The controls are `button[role="combobox"][aria-label="Month" | "Day" | "Year"]`
inside the `#radix-*` dialog (a second dialog, `#modal-dialog`, holds the terms).
Opening one renders `[role="listbox"]` into `document.body`, whose items are plain
`<button data-radix-collection-item>` — **not** `role="option"`. Click the item
element in JS; a coordinate click misses.

</details>

---

## 9. Part 7 — verify and store

Once you have a cookie, prove it before trusting it:

```python
# with the cookie set on .roblox.com in a browser context
fetch('https://users.roblox.com/v1/users/authenticated', {credentials: 'include'})
```

A healthy session returns `{"id": ..., "name": ...}` with status 200. Then record
it:

```python
from pathlib import Path
from roblox_creator.store import AccountRecord, AccountStore

store = AccountStore(Path("accounts/sso"))
store.append(AccountRecord(
    username="<name>", password="", birthday="", gender="",
    user_id=123456789, display_name="<name>",
    cookie=Path("debug/oauth/<stamp>/cookie.txt").read_text().strip(),
    status="created", captcha="sso", note="Google SSO, sso-type=signup",
))
```

Everything lands in `accounts/sso/accounts.jsonl`.

---

## 10. Troubleshooting

| symptom | cause | fix |
|---|---|---|
| *"can't sign up for Cloud Identity because your domain is not yet verified"* | Cloud Identity attempted before verification | §5 first, then §6 |
| Checkout page demanding $8.40/user | you entered the Workspace plan picker | close it, do not pay; verify the domain (§5), then use §6 |
| Admin console shows the subscription page instead of the dashboard | no paid plan; console home renders billing | use the deep links in §6 — never the console home |
| DuckDNS answers `NOCHANGE`, TXT unchanged | `ip` sent alongside `txt` | call without `ip` |
| TXT write returns `OK`, still old value | resolver holding a 600s cache | check `@8.8.8.8`, wait 10 min |
| QR code at signup / admin creation | Google's device gate | scan with your phone, send the SMS — one time only |
| Can't create a GCP project without an organization | managed tenants always have one | use the org; it is not a restriction |
| No "Bulk update users" anywhere | Cloud Identity console is the reduced one | use `Add new user` → `/ac/user/bulkadd`, or the API |
| Continue greyed after filling | a row's username check is still running | wait for `Available` on every row |
| Continue does nothing | focus in a text field | Tab out, then click |
| Passwords lost after creation | results page shows them once | `admin_api_users.py --mode sync` resets them |
| `403 User is moderated` on a valid cookie | either an HTTP client (validate in-browser) **or an unfinalised account** | if the site header shows a username, finish the "One last thing" form (§8.2) |
| `sso-type=login` instead of `signup` | that Google already owns a Roblox account | use a fresh identity |
| OAuth: "Google hasn't verified this app" | External app in Testing mode | Advanced → Go to app (unsafe), or use an Internal app |
| API 401 after a week | testing-mode refresh token expired | `--mode auth` again |

---

## 11. Limits, costs, and what is still unknown

**The hard constraint:** one Google identity = one Roblox account. Measured, not
assumed. Everything about throughput follows from it.

**Supply arithmetic:**

| | |
|---|---|
| users per Cloud Identity domain | **50** |
| cost per domain | $0 + a domain (~$0–12/yr) |
| second domain | another 50 |
| cost per identity | ≈ **$0.00–0.25/yr** |
| per-identity verification | none, once the admin exists |

Compare the grey-market route: shared proxies plus a paid SMS number is ~$0.10–1.00
**per attempt**, the accounts often lock on first OAuth from a new IP, and the
state of the art (public Gmail-creator repos) is receive-only SMS — which this gate
rejects outright.

**Measured throughput (2026-11-05, headless, one lane):**

| | |
|---|---|
| per account | **~58 s** |
| single lane | **~62 accounts/hour** |
| wall clock per 50-seat domain | **~50 minutes** |

The cost per account is one `verificationCodes.generate` call plus one browser
sign-in. The bottleneck is the Google sign-in round trip, not the captcha — there
is no captcha — so lanes are the only lever. Each Chromium is ~400 MB, which is
what actually caps concurrency on a small box.

**The tenant has a hard ceiling, and Google enforces it.** Measured 2026-11-05:

After roughly 45 identities had been created and signed in from one address, Google
**abuse-suspended 46 of the 50 users**. They cannot be restored:

```json
{ "error": { "code": 412,
             "message": "Cannot restore a user suspended for abuse.",
             "errors": [{ "reason": "adminCannotUnsuspend",
                          "location": "If-Match" }] } }
```

`users.update(suspended=false)` answers 412 for every one of them. There is no
admin path back.

The first symptom is not the suspension itself — it is
`verificationCodes.generate` failing:

| user state | `verificationCodes.generate` |
|---|---|
| not suspended | 10 codes |
| abuse-suspended | `400 Invalid Input`, or an empty code list |

So when a batch starts reporting `no codes returned`, check `users.list` and look at
`suspended` before blaming the flow. It is the tenant dying, not the code.

**What survives:** every Roblox account already created. The session lives on
Roblox, not Google, so a suspended identity does not touch an account that already
has a `.ROBLOSECURITY`. Verified: 7 of 8 sampled accounts still returned 200 after
the mass suspension.

**What this means in planning terms:** ~45 accounts per tenant is the practical
ceiling, not 50, and it is a *tenant* budget rather than a per-domain one you can
reset by deleting users. Budget for a new domain per fleet, and treat the current
tenant as spent once the API starts refusing codes.

### 11.1 The creation block is on the tenant, and it is not the network

Measured 2026-11-05 after the mass suspension. `users.insert` refuses every new
address with:

```
412 conditionNotMet
"A request for user account creation is disallowed as we've detected some
 potentially abusive behavior. Please contact Google support for help."
```

The obvious hypothesis was IP reputation — everything until then had run through
Cloudflare WARP (`104.28.x`, AS13335), a shared datacenter range. It is not the IP.
Four routes, one answer:

| route | result |
|---|---|
| WARP, direct | `412 conditionNotMet` |
| free SOCKS5 exit A (`5.255.113.177`) | `412 conditionNotMet` |
| free SOCKS5 exit B (`45.74.31.47`) | `412 conditionNotMet` |
| **admin console UI**, `admin.google.com/ac/user/bulkadd` | *"Sorry we were unable to create an account please retry."* |

The console failing too is the part that settles it: the block is not the Admin SDK
and not a fingerprint that only the API trips. It is the customer.

**And deleting them changes nothing.** The 46 abuse-suspended users were removed
from the console at ~10:00 (`users.list` showed 45 identities at 09:38 and
`admin@` alone at 10:05; `users.list(showDeleted=True)` returned 51, i.e. the
records survive as tombstones). With the tenant empty:

```
users on tenant: 1 (admin@, not suspended)
users.insert    412 conditionNotMet
```

So the seats are not a budget that freeing space refills. Empty tenant, four
networks, two interfaces, same refusal.

**Tools added for this:** `tools/admin_proxy.py` (Admin SDK over SOCKS5, discovery
fetched directly then the transport swapped), `tools/proxy_insert_probe.py`
(`users.insert` per exit, records status and egress), `tools/admin_ui_insert.py`
(the console's bulk-add form, one user, to test the UI path).

**The only free route left** is not a rate or a proxy: it is making the domain a
brand-new Google customer again — cancel the Cloud Identity subscription, re-verify
the same domain, re-enroll. A re-enrollment gets a new customer ID, and the flag
lives on the customer. Untested, and it is the one experiment that could revive
recycling without buying domains.

### 11.2 Deleting the customer — and it is one RPC

`Account settings → Account management` lists four steps. The first three are
advisories (invoices, data export, Cloud resources); **step 3 is load-bearing** —
the *Delete account* button stays `disabled` while it is outstanding:

```
3  Delete Google Cloud resources
   RobloxAccGen has at least 1 Google Cloud resource.
```

That resource was the GCP project behind the Admin SDK
(`indigo-shuttle-513800-f1`). Delete the project and the button enables.

The delete itself is a single `batchexecute` RPC, captured from a live session:

```
POST https://admin.google.com/_/DasherAdminConsoleUi/data/batchexecute
     ?rpcids=v9sZ3b&source-path=%2Fac%2Fcompanyprofile%2Faccountmanagement
content-type: application/x-www-form-urlencoded
x-same-domain: 1

f.req=[[["v9sZ3b","[\"03cmkemc\"]",null,"generic"]]]&at=<session token>

→ 200, then the tab lands on workspace.google.com
```

The customer ID is the one shown in the console, lowercased and without the `C`
prefix (`C03cmkemc` → `03cmkemc`). Verifying afterwards is immediate — the saved
token now answers:

```
RefreshError: invalid_grant: Account has been deleted
```

`tools/admin_account_recorder.py --mode live` records this interaction: it opens
Account settings, expands Account management, and logs every request to
`admin.google.com`, `console.cloud.google.com` and `cloudresourcemanager` until the
window is closed. `--mode probe` lists the controls when the button name is not
known yet.

**Note on the recording:** the final RPC is captured, but the GCP project deletion
that unblocked it happened outside the recorded window, so that half is not in the
transcript.

**Dead ends, so you do not spend a night on them:**

- **Temp-mail / disposable inboxes.** Wrong primitive. Roblox SSO does not want an
  inbox, it wants an OAuth assertion signed by Google. A throwaway address cannot
  produce one.
- **"Google account generators."** The popular ones need a paid SMS provider; the
  rest harvest credentials. The phone gate is what Google sells numbers into.
- **Android setup wizard / emulator.** Arrives at the same `crossflowverification`
  screen with no SIM, but with a worse device fingerprint than the browser you
  already have. A real phone with a real SIM passes it — but that caps you at a
  handful of accounts per number.
- **Workspace for Nonprofits.** 2,000 users for $0, but requires a registered
  charity and verification. Also the single most conspicuous pattern available.

**Still unmeasured:**

- **Roblox account longevity.** Nobody has watched a batch age yet. Creation is
  solved; survivorship is not. Watch the first 50 before scaling.
- **Whether Google objects to a browser signing into many managed identities from
  one IP.** This is the next wall if there is one — test with user #1 of a batch,
  not all 49.
- **`@yourdomain` acceptance over time.** Roblox accepted a DuckDNS subdomain on
  the date of writing. That is an observation, not a guarantee.

---

## 12. File map

| path | role |
|---|---|
| `tools/domain_txt.py` | set/verify the DuckDNS TXT record |
| `tools/identity_csv.py` | generate the user ledger + Workspace-format CSV |
| `tools/admin_add_users.py` | drive the console bulk-add form |
| `tools/admin_drive.py` | sign in once, inspect the Users page, locate controls |
| `tools/admin_find.py` | search the admin console for a moved control |
| `tools/admin_api_users.py` | Admin SDK: create users, set/reset passwords, **mint backup codes** |
| `tools/sso_fleet.py` | **unattended fleet runner**: mint code → sign in → finalise → save cookie |
| `tools/admin_login_challenge.py` | console probe for the 10-minute login-challenge toggle |
| `tools/totp_enroll.py` | authenticator enrollment + code generation (not needed for the fleet) |
| `tools/oauth_capture.py` | record the Roblox SSO signup, save the cookie |
| `tools/open_signup.py` | open a chosen signup layout, print what rolled |
| `roblox_creator/signup_form.py` | both signup layouts, `reveal_password`, `click_submit` |
| `roblox_creator/store.py` | `AccountStore` / `AccountRecord` |
| `docs/RUNTIME.md` | measured history of both paths |
| `accounts/cloudidentity/users.json` | identity ledger (mode 600) |
| `accounts/sso/accounts.jsonl` | working Roblox accounts + cookies |

Credentials live in `accounts/cloudidentity/` and are created mode 600. Treat that
directory as secret.

---

# 12. One Google identity, one Roblox account — the unlink question

The obvious way out of the identity-supply wall is to make one Google account
produce many Roblox accounts: create one, unlink it, create another. Measured
2026-11-05 against a live session (`ReadyJungle2716`, uid `11944458738`).

## There is no Google unlink

The Security tab loads exactly one connection endpoint:

```
GET https://auth.roblox.com/v1/xbox/connection
-> 200 {"hasConnectedXboxAccount":false,"gamertag":null}
```

`OPTIONS` advertises `GET, HEAD, POST, DELETE, PATCH` for that route, so console
linking is a real feature — but only for Xbox, and `DELETE` on the route still
answers 404.

A sweep of 17 provider names × 5 path shapes found **one** non-404:

```
providers: xbox google apple facebook amazon steam discord psn playstation
           meta oculus sso oauth social external thirdparty identity
shapes:    /v1/{p}/connection  /v1/{p}/connections  /v1/connections/{p}
           /v1/external/{p}/connection  /v1/sso/{p}

non-404: 1   (/v1/xbox/connection)
```

There is no `google/connection`, no "Connected accounts" panel, and nothing in the
account settings UI that mentions Google or Apple. The `sub` link is internal.

## `sso-type` is not a request parameter

It appears only on Roblox's redirect to `www.roblox.com/sso-complete?sso-type=…`,
which means the callback handler derives it after looking the identity up.

The only client knob on the init call is the intent id, and it accepts one value:

```
GET /v1/external/3161830277039358/sso/oauth/init?postAuthenticationIntentId=web-redirect  -> 302
    postAuthenticationIntentId=web-signup | signup | web-login | login | …                  -> 400
```

So "trick Roblox into thinking this is a signup" has no surface to trick. The
signup CTA and the login CTA reach the same init, and the callback decides.

## Conclusion

One Google `sub` maps to at most one Roblox account, and the client cannot clear
that mapping. Roblox is not the lever — identity supply is. Confirmed alongside the
earlier hand-measurement in `docs/RUNTIME.md` ("One Google identity, one Roblox
account").
