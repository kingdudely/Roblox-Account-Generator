# The auth surface — what the Cloud API index lists vs what is mounted

`https://create.roblox.com/docs/cloud/reference/domains/auth` is generated from
an internal API description. It lists every auth route the codebase *declares*,
including routes that were retired years ago and kept alive only as the old
clients' fallback. The index therefore reads like a menu of captcha-free
account creation. It is not.

Measured once, from a single clean egress, with the CSRF handshake completed on
every route (`tools/route_sweep.py`) and with bodies that are invalid on
purpose so a live route answers with a validation error instead of doing the
thing.

## The tell

`rblx-challenge-type` is emitted by the challenge middleware and by nothing
else. A route that answers at all without that header has no captcha in front
of it. Everything else is classification noise.

```
GATED      403 + rblx-challenge-type         the gate is installed
LIVE       any answer, no challenge header   route is mounted; body reached
                                             the handler
TOMB-429/501/400                             path is retired; the edge or a
                                             stub answers without touching
                                             the body
AUTH-401   9002 "Authentication token is missing"   live, needs a session
```

## Result

**Gated — the only two routes that create an account:**

| route | flavour |
|---|---|
| `POST /v2/signup` | `captcha` or `captchav2` (risk-scored per request) |
| `POST /v2/signup/linked` | `captcha` |

**Tombstones — the "forgotten" endpoints, and what they actually answer:**

| route | answer | reading |
|---|---|---|
| `POST /v1/signup` | 429 `{"code":0,"message":""}`; `GET` 404 | route not mounted; the 429 is an edge rule on the dead path |
| `POST /v1/signup/linked` | 429 | same |
| `POST /v1/external/signup` | 501 NotImplemented | stub, never implemented |
| `POST /v1/external/loginAndLink` | 501 | stub |
| `POST /v1/external/unlink` | 501 | stub |
| `POST /v1/external/access` | 400 "Unspecified identity provider/platform" **for every body shape tried** | the message never changes, so the handler never parses a body — mock |
| `POST /v1/passkey/finish-preauth-registration` | 404 | not mounted |

The body-shape test for `/v1/external/access` tried `identityProviderId`,
`identityProvider`, `provider`, `providerId`, `platform`, `platformId`, `idp`,
`idpId`, `externalProvider`, `externalProviderId`, `identityProviderPlatform`,
access/id tokens, as JSON, form-encoded, query string and headers — singly and
all at once. One message throughout. It is not a schema puzzle, it is a stub.

**Live and ungated (25 routes)** — all of them login, recovery, or validation,
none of them creation:

```
POST /v1/external/login                    SSO login; parses `platform`, then
                                           "Token Validation Failed" — never a
                                           challenge, for any platform string
POST /v1/identity/initialize-login         identifier-first; returns login
                                           methods for a user
POST /v3/users/{id}/two-step-verification/login
POST /v2/login, /v1/login
POST /v2/login/linked, /v1/login/linked
POST /v2/passwords/validate
GET  /v2/passwords/validate?username=
GET  /v2/revert/account                    ticket-gated
GET  /v2/usernames/validate                existence check — the oracle used
                                           below
GET  /v1/validators/email
GET|POST /v1/validators/username
POST /v1/passkey/StartAuthentication       returns a WebAuthn challenge
POST /v1/passkey/start-authentication-by-user
POST /v1/passkey/finish-ar-preauth-registration
GET  /v1/account-creation/metadata         {"isEligibleForALSignup":false}
GET  /v2/auth/metadata, /v2/metadata, /v2/recovery/metadata
```

**Cookie-required (13 routes):** account pin CRUD/lock/unlock, `client-assertion`,
`session/refresh`, `logout`, passkey registration and listing,
`social/connected-providers`, `xbox/*`, `account/upgrade`.

## Does the gate fire before or after the write?

It fires before. A real, fresh identity was posted to `/v2/signup`, answered
`403 Challenge is required`, and then `GET /v2/usernames/validate` on the same
name still returned `{"code":0,"message":"Username is valid"}`. Nothing was
created behind the 403. There is no window where the row lands and the
challenge is only cosmetic.

## What is actually exploitable

1. **`/v1/external/login` has no challenge middleware at all.** It reads
   `platform` from the JSON body, accepts any string as a platform, and moves
   straight to `Token Validation Failed`. That is a captcha-free *login*
   surface, not a creation surface — it needs a provider token from Xbox,
   PlayStation, Meta, Google or Apple. Worth keeping in mind for account
   recovery or for any provider-token source the project ever gets.
2. **The client shape picks the challenge flavour.** `x-roblox-platform` plus
   the matching app UA shifts `/v2/signup` between `captcha` (Arkose, carries a
   `dataExchangeBlob`, solvable by everything in this repo) and `captchav2`
   (never satisfied by any variant — see `tools/captchav2_probe.py`). Which one
   you get is risk-scored per request and the sample in
   `tools/challenge_mix.py` was poisoned by the rate limit below, so the ratio
   is not yet a number. It is a lever worth measuring on healthy exits.
3. **The ceiling is the egress, not the captcha.** Roughly twenty `POST
   /v2/signup` from one IP, then `429 {"code":0,"message":""}` on *every*
   subsequent attempt from that IP, still 429 after minutes of idle. A 35/min
   target therefore needs a large pool with a per-IP budget, not a faster
   solver. This matches the per-exit allowance already documented in
   `docs/ARKOSE.md`.

## Re-running

```
./tools/route_sweep.py                 # full table, CSRF handshake included
./tools/route_sweep.py --alive         # only routes that answer
./tools/challenge_mix.py --per-variant 12 --delay 1.5   # flavour per client shape
```

`route_sweep.py` deliberately never creates anything: every signup body it sends
is invalid. `challenge_mix.py` is the same — challenge selection happens before
field validation.

One caveat on `route_sweep.py`: it completes the CSRF handshake by replaying the
first 403 with the token from `x-csrf-token`. Without that step 22 of the routes
read as 403 walls and the sweep is blind. Any future rewrite must keep it.

Second caveat: the sweep spends the egress budget. A second run from the same IP
minutes later reported `GATED=0 / TOMB-429=3` because `/v2/signup` had started
answering 429 to everything. If a run disagrees with the table above, check
whether the exit is already throttled before believing it.

## Wired into the loop

`browser_solver.py --shape auto` asks `roblox_creator/client_shape.py` which
header set to send with `/v2/signup`. `auto` falls back to the default until the
recorded observations justify a switch, `off` sends the request untouched, and a
named shape forces it. Every real signup records its flavour, so an ordinary run
builds the evidence that `auto` needs without a separate measurement pass.

`tools/challenge_mix.py --report` prints what has been recorded and what
`preferred()` would choose.
