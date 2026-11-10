# Roblox Alt Creator

One command turns a fresh DuckDNS name into a Google Cloud Identity tenant, an
authorized OAuth key and Roblox account(s).

```bash
npm install
npm run create-tenant
```

That is the whole pipeline. The browser opens only when Google needs you, and
the CLI resumes automatically when the page moves on.

## What `rac start` does

1. **Picks a DuckDNS subdomain.** It generates a hard-to-collide name and checks
   DNS first. If you pass a name that already resolves, it picks another one
   unless you own it with a saved token.
2. **Creates the Cloud Identity tenant.** The domain does **not** need to exist
   yet. It signs up `admin@<sub>.duckdns.org` and fills the form through the
   account step.
3. **Stops for Google's phone/QR gate.** This is the one unavoidable human step.
   The CLI focuses the browser, beeps, shows what Google wants, and auto-resumes
   when verification finishes.
4. **Creates the DuckDNS record with the tenant admin.** The new admin account is
   a real Google account, so it is used to sign in to DuckDNS, add the subdomain
   and capture the token. No personal Google account is needed.
5. **Verifies the domain.** It writes the TXT record through the DuckDNS API,
   waits for DNS, and lets the Admin console verify ownership.
6. **Finishes the tenant and key.** Cloud Identity Free, admin 2SV and policy,
   Cloud project, Admin SDK, consent screen, five scopes, desktop OAuth client,
   and the consent flow that stores the refresh token.
7. **Creates Roblox account(s).** Google identities are created with the Admin
   SDK, the Roblox Google-SSO lane runs in a persistent profile, the cookie and
   account details are saved, the Google identity is deleted to free the seat,
   and everything is exported.

## What still needs you

| Step | When | What to do |
|---|---|---|
| Recovery email | only if you want one | A fake `rac-<subdomain>@example.com` is used by default. Pass `--contact you@example.com` for a real recovery address. |
| Google phone/QR check | once per tenant | Scan the QR code with your phone, or send the SMS the CLI extracts. |
| Captcha / extra 2SV | occasionally | The browser is focused and the terminal beeps. Solve it there; the run resumes. |
| Roblox captcha | occasionally | Same handoff: the lane tab is focused and the run waits. |

There is no public API to create a Cloud Identity tenant. The only programmatic
path is the Cloud Channel API, which requires Google partner/reseller status and
paid subscriptions. Phone verification is Google's anti-abuse check; virtual
numbers/SMS-activation services violate Google's terms and get tenants
suspended. The CLI does not bypass it. It makes the check as small as possible
and automates everything around it.

Once a tenant exists, Cloud Identity Free provides 50 seats. `rac run` deletes
the Google identity after a successful Roblox signup, freeing the seat, so the
same tenant can keep producing accounts subject to Google/Roblox rate limits.

## The two phone gates

Google uses two opposite directions. The CLI handles both.

| What Google says | Direction | What the CLI does |
|---|---|---|
| "We'll send you a verification code" | Google → you | Fills **your** number (`--phone` / `RAC_PHONE`), then focuses the browser so you can enter the code. |
| "Send this text to Google" | you → Google | Scans the page/QR, extracts Google's number and the message body, prints them, focuses the browser and beeps. |

`--phone` is therefore **your** number, not Google's. The number Google asks you
to text is shown by the gate handler. If the QR page appears, you can simply
scan it with your phone; the CLI also tries to read the consent link and show
the destination number/message so you can send it directly. See
[docs/PHONE.md](docs/PHONE.md) for the full explanation and failure cases.

## Commands

| Command | What it does |
|---|---|
| `rac start` / `npm run create-tenant` | Full pipeline: domain → tenant → DuckDNS → verify → key → 1 account |
| `rac start --tenant-only` | Stop after the OAuth key is ready |
| `rac start --accounts 5` | Create five Roblox accounts after setup |
| `rac status` | Checklist and the next command |
| `rac run --count 5` | Create more accounts with the ready key |
| `rac export` | Write `accounts.txt` |
| `rac key …` | Import, list, check, authorize, recover, remove, export keys |
| `rac identity …` | Create/list/delete Google identities, check 2SV, mint codes |
| `rac signup` | Just the Google signup form and phone/QR gate |
| `rac duckdns` | Just the DuckDNS record and token |
| `rac setup` | Just the tenant/key sub-steps |
| `rac admin` | Open or sign in to the Admin console |
| `rac doctor` / `rac smoke` | Check Patchright, Chrome and trusted input |
| `rac state` | Show paths and a raw summary |
| `rac state scrub` | Clear admin password/company/name/phone; keep the key |

`rac` with no arguments prints the status dashboard. `rac help` prints the
grouped command list.

### Common options

- `--subdomain <name>` — use a specific DuckDNS name instead of a generated one.
- `--token <token>` — reuse an existing DuckDNS account token.
- `--contact <email>` — real recovery email (default: a fake `example.com` placeholder).
- `--password <value>` — admin password; generated and saved by default.
- `--minimal-state` — after the key is verified, clear the admin password, company
  name, first/last name and phone. `--forget-admin` is an alias.
- `--phone <number>` — your number for the "Google texts you" gate.
- `--from <step>` — force `signup`, `duckdns`, `setup` or `accounts`.
- `--dry-run` — print the plan without opening a browser.
- `--yes` — use saved values without prompting.
- `--proxy <url>` — proxy every browser step.
- `--headless` — fine for `rac run`; not for the first-time phone/QR gate.

## State and profiles

```
~/.local/share/roblox-alt-creator/state.json
~/.local/share/roblox-alt-creator/keys/<domain>.json
~/.local/share/roblox-alt-creator/profiles/setup
~/.local/share/roblox-alt-creator/profiles/lane-1 … lane-N
```

Override the root with `RAC_HOME=/path/to/dir`. The state file is written with
mode `0600`. `rac start` also writes the durable OAuth key JSON (client id,
secret and refresh token) to `keys/<domain>.json`, mode `0600`; that file is
enough to run accounts later.

The admin password and company/name fields are only used to drive the one-time
setup (DuckDNS, Admin console and consent). Pass `--minimal-state` to clear them
after the key is verified. The key can reset the admin password later with
`rac key recover`.

The `setup` profile is shared by signup, DuckDNS and setup, so the Google session
survives between steps. Lane profiles are separate cookie jars for Roblox runs.
Chrome may close and reopen between setup steps; the login survives in the
profile.

## Troubleshooting

**"Sorry, we can't complete your signup"** — Google's anti-abuse decision, not a
CLI bug. Wait, try a different network/phone, or use a proxy. There is no
reliable bypass.

**"This phone number cannot be used for verification"** — the number has been
used too many times or the attempt limit was hit. Wait, or use a trusted
different number.

**The admin already has 2SV but no saved authenticator** — `rac setup` refuses to
change the tenant policy, because a phone-only admin could be locked out. Set the
policy manually only if another allowed factor exists.

**The phone/QR gate sits there** — the browser is focused; scan the QR or send
the printed SMS. The CLI re-beeps every minute and continues when the page
moves on.

**A Roblox lane needs a captcha** — the lane tab is focused and the run waits.
Finish it there. If the lane times out, rerun with `--keep-open`.

## Advanced

Use an existing key:

```bash
rac key import client_secret_*.json
rac key authorize
rac run --count 5
```

Resume after a human step:

```bash
npm run create-tenant
```

Everything already checked is skipped. To force a step:

```bash
rac start --from setup
```

## Optional Firefox extension

The extension remains in `extension/` for Firefox-container lanes and a GUI. The
CLI is the primary surface and reuses the same `extension/lib` and
`extension/content` code. See [docs/LEGACY.md](docs/LEGACY.md) for the extension
and legacy Python notes; the CLI commands in that file are outdated.

## Testing

```bash
npm test
node bin/rac.js doctor --headless
node bin/rac.js smoke --headless
node bin/rac.js start --dry-run
node bin/rac.js signup --check-form --subdomain rac-checkform --contact fake@example.com
```

`signup --check-form` fills the real Google form through the admin step and
stops before "Agree and continue", so it never creates a tenant. Use it to
verify the selectors after a Google page change.
