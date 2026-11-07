# What it costs — Roblox accounts, every path, November 2026

The question this answers: with money, what is the cheapest route to a lot of
Roblox accounts — buying Google identities, building them, or something else.

**Short answer: buy the Roblox accounts. Google identities are a middleman tax.**
Workspace is the most expensive option on the board, and it is the one people
reach for first.

## The numbers

All prices read from the vendors' own pages/docs on 2026-11-05. FX-free, USD.

| path | unit price | 50,000 accounts | recurring | notes |
|---|---|---|---|---|
| **BloxGen API** | **$0.0004**/alt | **$20** generation + **$7,495** keys | **$20/day** | 500 Ultra keys, 100/day each |
| Marketplace alts | $0.02–0.03 | $1,000–1,500 one-time | — | batch, quality varies |
| Buy Gmail + SSO | $0.45–1.00 | $22,500–50,000 | — | + ~430 h of finalise |
| Cloud Identity Free | ~$0.20/account/yr | ~$10,000/yr | $10k/yr | 1,000 domains, 1,000 phone signups |
| SMS + web signup | $0.15–0.48/code | $7,500–24,000 | — | outbound-SMS gate breaks virtual numbers |
| **Google Workspace** | $7.20/user/mo | **$360,000/month** | — | dead on arrival |

## BloxGen, read properly

Published prices (`docs.bloxgen.net`):

```
alt              $0.0004      +30 days    $0.0006
+1 year          $0.0008      5+ years    $0.0012
dump             $0.005       18+ verified $0.01
```

Throughput is capped by **daily limit per key**, by role:

| role | alt/day | price | cost to reach 50k/day |
|---|---|---|---|
| free | 20 | $0 | 2,500 keys — not practical |
| premium | 50 | $6.99 lifetime | 1,000 keys = $6,990 |
| ultra | 100 | $14.99 lifetime | 500 keys = $7,495 |

So 50k/day = **$7,495 one-time for keys + $20/day of generation**. The keys are
reusable forever, which is why this beats every per-account purchase at volume.
The first 50k pays for itself against a $1,250 marketplace batch in about six
days of continuous production.

The repo previously claimed one key yields 3,456/day (a 25-second cooldown read as
the bound). That is wrong by ~35×: the daily cap binds first. `bloxgen_run.py
--plot-only` now prints the real arithmetic.

## Why Google identities lose

- One Google identity yields **exactly one** Roblox account. Recycling is dead:
  changing the Roblox email does not free the Google link, and deletion is
  asynchronous (`privacy/v1/users/<id>/forget` → `PROCESSING`).
- So Gmail accounts are consumed once. $0.45–1.00 each, forever.
- The conversion is not free either: `POST /v1/account/upgrade` finalise measured
  at ~31 s/account, so 50k is ~430 hours of compute.
- Cloud Identity Free is the only cheap Google source ($0.20/account/year), but it
  costs a domain, one admin phone number and a fresh tenant **per 50 accounts** —
  1,000 domains for 50k. The tenant-scale block (`412 conditionNotMet`) has already
  burned once.
- Workspace is $7.20/user/month. Nobody should compute this twice.

## What to actually spend, in order

1. **$5 on BloxGen, free role.** 12,500 alts at $0.0004, delivered 20/day. Proves
   the vendor delivers what it says before any real money moves.
2. **$149.90 — ten Ultra keys.** 1,000 alts/day, ~$0.40/day of generation. This is
   the smallest spend that changes the shape.
3. **Scale only after measuring survivorship.** `docs/SOLVER.md` flags it as
   unmeasured. Fresh alts with no email and no history may not last; if they do
   not, the $0.0006–0.0012 aged tiers are still far cheaper than any identity
   route.

## Caveats worth the paragraph

- Published price ≠ delivered account. BloxGen alts are fresh, email-unverified,
  no history. Check the 18h survival rate on a small batch first.
- Marketplace alts at $0.02–0.03 carry USA-IP and some age; BloxGen is cheaper per
  unit but thinner per account. Different products.
- 500 API keys is 500 accounts to register, verify and accept rules on. That is
  the real friction at 50k/day, not the money.
- The API is live: `GET https://core.bloxgen.net/health` → `{"status":"ok"}`.
  `prices` and `balance` require a key.
