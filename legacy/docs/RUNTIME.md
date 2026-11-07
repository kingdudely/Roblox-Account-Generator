# Browserless runtime — state, evidence, next steps

Goal: create Roblox accounts at ~35/min (~50k/day) with no browser in the hot
path, using sticky SOCKS5 residential exits, and with accounts that survive 18h+.

`docs/ARKOSE.md` holds the reversed protocol. This file holds the *engineering*
state: what runs, what is proven, what is guesswork, and what breaks next.

## Where the chain stands

| step | tool | status |
|---|---|---|
| signup 403 → `dataExchangeBlob` | `tools/pure_challenge.py` | **works** |
| `c` → `gt2` token, unsuppressed | `roblox_creator/bda.py` | **works**, byte-verified |
| PoW → `{"action":"challenge"}` | `tools/powrun.js` | **works**, ~50% exits blocked |
| `gfct` → game_data | `tools/pure_challenge.py` | **works** |
| wave image download | `tools/pure_ca.py` | **works** (must use gfct referer) |
| `/fc/ca/` answer accepted | `tools/pure_ca.py` | **works** — returns `not answered` + next wave |
| solve the puzzle | — | **not built** (CV + VLM planned) |
| all waves → `solved` | — | **never reached** |
| final signup resubmit | `tools/pure_signup.py` | **stale** — still posts to the dead `challenge/v1/continue` |
| survivorship | `tools/survival.py` | built, unmeasured |

So: the transport is browser-free end to end; the only missing piece between us
and a created account is answering the picture, plus rewiring the last hop.

## What was proven, and how

Each of these has a reproducer in `tools/`; do not re-derive them from scratch.

- **`c` is exact.** Re-sealing a captured plaintext with our own AES key produced
  a token from a different exit (`tools/c_selftest.py`).
- **`enhanced_fp` decides suppression.** Minimal → `sup=1`; 90 entries → no `sup`;
  3/3 both ways.
- **Suppressed tokens are worthless.** 6 encodings, fresh token each, all 403
  (`tools/suppress_probe.py`).
- **The PoW is a preimage search, and our nonces are genuine.**
  `sha512(prefix + nonce) == target` verified for all five splits.
- **`/pows/check` failures were a Content-Type bug**, not a bad nonce.
- **`/fc/ca/`'s cipher is CryptoJS-with-the-session-token**, confirmed by
  decrypting live captures and matching the derived IV.
- **`tguess` is optional** — the server accepts a body with `guess` alone.
- **The challenge is unconditional** on this pool: 0/10 straight-through signups
  even with a page-warmed session and full browser headers
  (`tools/signup_direct.py`). The same was true for a real Chrome.
- **`captchav2` has no free pass.** Six completions tested, one fresh challenge
  each — empty metadata, blank redemption token, full metadata echo, both legacy
  resubmits, and a mismatched `challengeType`. All 403
  (`tools/captchav2_probe.py`).
- **Desktop fingerprints cost half the waves.** 5.8 mean vs 10.0, same
  instruction and exits.

## Difficulty is decided by the client, not by the exit

`waves` is the number of answers a challenge costs, so it multiplies everything.
Measured with `tools/waves_probe.py`:

| client fingerprint | attempts | waves | reached `gfct` |
|---|---|---|---|
| mobile Android, as captured | 8 | 10, 10, 10, 10, 10 | 5/8 |
| mobile Android, coherence-repaired | 8 | all 10 | **8/8** |
| **desktop capture, replayed** | 6 | 5, 5, 5, 5, 6, 9 (mean 5.8) | 6/6 |

Two independent effects, and they are not the same effect:

- **Coherence fixed reliability, not difficulty.** The captured mobile
  fingerprints carry geometry that cannot exist in a live page
  (`window_inner_width`/`height` were `0`, and `window_outer` was portrait on a
  landscape screen). Repairing them to be self-consistent with `screen`, and
  matching fingerprint locale/timezone to the US exit, took the block rate from
  3/8 to 0/8. Waves stayed at 10.
- **Desktop halves the work.** Replaying the one captured desktop payload gave
  mean 5.8 waves against 10.0 for every mobile fingerprint, same instruction,
  same exits. Mobile clients are simply challenged harder.

The envelope is portable and only `n` has to be refreshed, so a captured payload
can be re-sealed and reused indefinitely — which is what
`roblox_creator/payloads.py` does.

**Open constraint:** the corpus holds exactly one desktop payload. One device
identity across 50k accounts is a clustering signal, so the next job is a corpus,
not a solver. Harvest with `tools/fp_harvest.py --all` (one browser capture per
harvested payload), then `--list` to see what is stored.

## Throughput arithmetic (why solving dominates)

```
50k accounts/day      = 0.58 accounts/s
× 10 waves            = ~5.8 solves/s sustained
× 1 VLM call each     = 5.8 calls/s, 2-5s each
                       ≈ 15-30 concurrent calls
```

Two consequences:

1. Wave count is a multiplier on everything. Dropping 10 → 5 halves the required
   concurrency. That is why `enhanced_fp` coherence is the highest-leverage work.
2. Per-identity rate limits are irrelevant at this scale *if* calls are spread
   across exits — 40k IPs is 40k identities. Batching is a proxy-count problem,
   not a solver problem.

## Invariants — break these and everything fails silently

- One Arkose **session** per attempt. `gt2`, all of `/pows/*`, `gfct` and `/fc/ca/`
  share one `tls_client` session so cookies carry.
- `data[blob]` is single-use per challenge. One token per blob.
- `pow_token` must stay constant across `action:"next"` rounds unless the response
  hands you a new one.
- The wave image URL cannot be edited — `challenge=N` is signed.
- Referer for images is the **game frame with the gfct session token**.
- `c` is portable across IPs; the blob is not.
- Report PoW timings as measured. Inflating them makes blocks worse.

## Failure taxonomy (what a given symptom means)

| symptom | cause |
|---|---|
| `400 {"error":"Invalid Request"}` on `/pows/check` | JSON body sent with a form content-type |
| `400` on `gt2` | `data[blob]` missing |
| `401` on `gt2` | blob already consumed, or a stale/hollow `c` |
| token contains `sup=1` | `enhanced_fp` too thin — Roblox will refuse it |
| `{"action":"block"}` | exit reputation; retry on a different exit |
| `403 DENIED ACCESS` on `/rtig/image` | wrong referer, wrong/edited `challenge`, or a hot exit |
| `403 Challenge failed to authorize request` | token not accepted: suppressed, or not bound to that challenge |
| `rblx-challenge-type: captchav2` | different flavour — re-request with a new username |

## Next steps, in the order they pay off

1. **Build a desktop payload corpus.** A replayed desktop payload halves the wave
   count (5.8 vs 10.0, measured). We hold exactly one, which is a device
   clustering hazard at volume. Harvest more with `tools/fp_harvest.py`, then
   `--list`. `roblox_creator/payloads.py` already prefers desktop when it has a
   choice of more than one.
2. ~~**Probe `captchav2`.**~~ **Done — negative.** No completion is accepted
   without the component's own token (`tools/captchav2_probe.py`, 6 variants, all
   403). Keep treating that flavour as a retry.
3. **Solver.** Composite contact sheet (key + 10 tiles in one image) → one VLM
   call per wave. One image is 3 upload calls instead of 33, and — because an
   anonymous identity allows ~18 uploads — it is the difference between fitting a
   10-wave challenge in one identity and not. `tools/chatgpt_anon.py`
   (`ask_many`) already does multi-attachment turns; `tools/wave_solver.py` and
   `tools/wave_montage.py` are the current implementation and spend two uploads
   per wave.
4. **Labels.** Ground truth for solver accuracy requires a real browser once —
   the only place the "correct" answer is observable. Everything after that is
   offline.
5. **Final hop.** `solved` → `decryption_key` → token into
   `rblx-challenge-metadata.captchaToken` → `/v2/signup` with `rblx-challenge-id`
   and `-type`. `tools/pure_signup.py` needs its `continue` call replaced with the
   legacy resubmit.
6. **Survivorship.** 1/3/5 accounts per IP, polled 72h with `tools/survival.py`.
   The funder's bar is 18h; that is a separate axis from everything above and has
   never been measured.

## Running it

```bash
# one attempt, prints each hop
.venv/bin/python tools/pure_challenge.py --proxy-file proxies3.txt

# answer a few waves (indices are placeholders until the solver lands)
.venv/bin/python tools/pure_ca.py --proxy-file proxies.txt --answers 0,1,2,3

# measure difficulty (waves) per attempt — the metric that matters
.venv/bin/python tools/waves_probe.py --proxy-file proxies.txt --n 8 --tag baseline

# is a suppressed token really refused?
.venv/bin/python tools/suppress_probe.py --proxy-file proxies.txt --variants all
```

All of these retry across exits internally (`--tries`); `block` and hot-exit
`403`s are expected, not bugs.

## Environment

- Python 3.13 venv at `.venv`; `tls_client` 2.0.0 (`chrome_131`), `curl_cffi`
- node v20.19.2 — required for the PoW worker only
- 8 cores / ~7 GB RAM / no GPU (rules out a local VLM at useful throughput)
- proxy pools: `proxies.txt` (evomi), `proxies1-3.txt` (legion), ~40k sticky
  SOCKS5 sessions, all residential US
- not a git repository — no history, so these docs are the memory

---

## State of the live loop (2026-10-30, end of session)

`tools/solve_live.py` runs the whole thing and `--create` runs the final hop. What
is now known by running it, rather than by reading code:

| step | status |
|---|---|
| chain to `gfct` | works, desktop payload, 5-10 waves |
| wave-0 image | **50-75% of exits** (measured: evomi 2/4, legion 3/4) |
| wave-0 answer accepted | **works** — `not answered` + next wave URL |
| subsequent wave images | **work when the exit is healthy** (5/8 header shapes 200) |
| full solve | **not yet reached** |
| final hop | implemented (`roblox_creator/finalhop.py`), never run |

Three corrections to earlier assumptions, all from measurement:

1. **The wave-1 image 403 is not a protocol problem.** A matrix probe
   (`tools/wave2_probe.py`) fetched the second wave's server-supplied URL
   successfully with five different header shapes, including the plain one we
   use. `no-referer`, `sec-fetch-site: none` and `cross-site` were the only
   failures. So the URL, the referer and the session token are all correct — the
   failures are unhealthy exits, and they show up as `403 DENIED ACCESS` on the
   *first* wave already.
2. **`_try_image(cookies=None)` was not equivalent to omitting `cookies`.** The
   signed image URL needs the cookie `gfct` just set; passing `None` explicitly
   discarded it. Fixed in `tools/pure_ca.py`.
3. **The reply parser was inverting answers.** "**Candidate 4**" parsed as 5, and
   a reply naming the *count* ("…exactly 2 times") parsed as tile 2. It now trusts
   `candidate|tile|option N`, then `answer: N`, then a leading number only.

Open, in order of what unblocks the most:

1. **Index convention.** Whether Arkose indexes `guesses` 0-based is undocumented
   and there is no per-wave feedback to infer it from. Exposed as
   `--label-offset`; the first full solve names it.
2. **Solver accuracy per wave.** Dominates everything: 90% per wave passes a
   6-wave challenge 53% of the time, 70% passes 12%.
3. **Exit health.** A challenge needs one healthy exit for its whole life; ~60% of
   exits are healthy at any moment, so budget 1.6 attempts per challenge.

---

## The image budget: measured, and it is not what we assumed (2026-10-30, late)

The signed wave image (`/rtig/image`) is the binding constraint on the whole chain,
and four separate explanations were tested and killed tonight. Recording them so they
are not re-tested:

| hypothesis | test | verdict |
|---|---|---|
| signature is single-use | fetch the same URL 3x on the minting exit | **no** — it served 3 times |
| signature expires on a clock | `expires` says +3599s; fetch at +0/15/30/45/60/90 | **no** — served at +60s |
| request is bound to the cookie jar | fresh session on same exit, cookies copied | **no** — served |
| fetch must come from the minting *IP* | different exit, cookies copied | **yes** — 403 |
| exit IP rotates mid-session | ipify through 8 fresh connections | **no** — stable |
| retrying after a 403 recovers it | 1.5s, 40s, 120s, fresh jar, fresh session | **never** — 403 is final |
| slower requests buy more waves | interleaved A/B, 6 exits/arm, 0s vs 2.5s | **no** — depth 0.5 vs -0.3 |
| `analytics_tier` sets difficulty | gfct with tiers 0/5/10/20/40 | **no** — 5 waves every time |

A single early run reached wave 4 at 2.5s pacing and I nearly shipped "pace the
transport" as the fix. The interleaved A/B on one pool showed that was an
unusually good exit, not an effect of pacing — which is why the A/B exists.

### What the budget actually looks like

A challenge costs **seven Arkose requests before its first image** (gt2, five
proof-of-work calls, gfct), then one fetch plus one submit per wave. Those five PoW
calls cannot be trimmed: skipping them and calling `gfct` directly returns
`200` with `game_content.customGUI._challenge_imgs` **empty** — the proof of work
gates the image grant rather than merely being checked. So 17 requests is a floor for
a five-wave challenge, not an estimate. Measured
directly by wrapping the session in `tools/throttle_probe.py`. The wall lands at
roughly the eighteenth request, which is why a five-wave challenge (7 + 10 = 17)
usually just fits and a ten-wave one (7 + 20 = 27) never does.

Frugal run, 12 attempts, no solver, fixed answers (`--fixed 0`):

```
images per challenge: {0: 4, 1: 2, 2: 3, 3: 1, 5: 2}
challenges that could have been completed: 2/12
```

So **roughly one challenge in six can be played to the end**, and the distribution
is bimodal — either the exit serves nothing, or it serves the whole thing. Two of
those twelve answered all five waves and were only stopped by wrong answers.

Two of them also had `waves=6` and `waves=10`. Wave count is server-side and is
either 5 or 10; it is not influenced by `analytics_tier`.

### Consequences for the loop

1. **`fetch_image` sweeps two shapes, not four.** Four shapes on every failure
   spends the *next* challenge's allowance on being wrong. `attempts=2` is the
   default now.
2. **A 403 abandons the challenge immediately.** There is no retry that helps
   (`solve_live.py`, `died_at_wave`). Retrying was actively harmful: it burned the
   allowance without ever recovering the URL.
3. **Throughput arithmetic** is now: `challenges/hour x 0.17 x accuracy^5`. At 90%
   per wave that is ~10% per challenge; at 70% it is ~3%. The transport is not the
   limiter for throughput any more — per-wave accuracy is.

## The image gate is a lottery, not a quota (2026-11-01)

Measured after the budget section above was written. It revises the model rather
than contradicting the numbers in it. `tools/image_lottery.py` ran 8 mints against
each of 3 sticky exits and fetched wave 0 for every one, recording the cumulative
Arkose request count so a quota would show up as decay:

```
exit 82.40.83.118    200 200 200 x x x x x      (mint itself started failing at ~28 requests)
exit 174.55.176.97   403 403 403 200 200 403 200 200
exit 50.126.162.46   200 403 403 403 200 403 403 200
                                     overall 10/19 = 53% per mint
```

Three things follow, and the third is the important one.

1. **A fresh mint on a spent exit usually works.** The budget section above says
   "a 403 abandons the challenge immediately ... there is no retry that helps", and
   that is true *of the URL*: re-fetching the same signed URL after a 403 did not
   recover here either. It is not true of the exit. A new challenge issued to the
   same IP answered 200 roughly half the time.
2. **It does not decay with request count.** Exit 2 was granted at mint 7 with ~49
   requests already spent on that IP; exit 3 likewise. A quota would have died long
   before. The `~18 requests` wall above was measured by `tools/throttle_probe.py`
   against a different shape of traffic and must not be generalised to mint loops.
3. **So the gate is stochastic, not consumable.** Each mint is close to an
   independent draw with p ≈ 0.5 (n=19 — re-run before quoting it as a constant).
   The consequences invert the old plan: retrying is not wasted motion, one exit
   hosts many accounts rather than one, and pool *size* stops being the binding
   constraint.

What one exit did hit was a **mint-level** failure, not an image one: exit 1 served
three grants and then failed `gt2`/`pows`/`gfct` for five consecutive attempts. So
there is a ceiling on mints per exit, it is lower than 8, and it is a different
limit from the image grant. That is the next thing to measure, not the image rate.

Still unresolved: whether a granted challenge serves *all* its waves. The bimodal
distribution above says yes when granted, but that was measured before mints were
retried, so it is worth re-running with retry in place.

### Two things that look like causes and are not

**Solver latency.** The frugal fixed-answer run completed 2/12 challenges on evomi;
the VLM run on the same pipeline reached only wave 3 in 18 attempts, which looked like
the 20 s spent solving. An interleaved A/B on one pool (think 0 s vs 20 s, 6 exits per
arm, the delay inserted exactly where a solver would sit — between fetching an image
and submitting the answer) says no: mean depth `-0.3` vs `-0.2`. So the VLM does not
need to be fast to keep a challenge alive, and the difference between those two runs
was pool state.

**Pool state, stated honestly.** The `1 in 6 completable` number is one 12-attempt
sample on the evomi pool while it was reasonably fresh. By the end of the session the
legion pool was returning depth 0-1 on essentially every exit, in both arms of a
controlled test, after several hundred challenges of probing. Treat 1 in 6 as an
optimistic endpoint measured on a healthy pool, not a constant — and measure it again
before sizing anything against it.

### Where that leaves the two open problems

1. **`solved` has still never been seen.** The loop is correct end to end and answers
   five waves when an exit serves five images; every attempt so far has been cut off by
   the image allowance or by wrong answers. The index convention (`--label-offset`)
   and `finalhop.py` both stay untested until one challenge completes with five
   correct answers.
2. **The solver needs a trained model, and the labels need the solver.** `solved` is
   one bit per five answers, which is far too slow. The honest way out is the one
   Wazza0 used: real labels, a small CNN on the 200x200 tiles, and never pseudo-labels
   from a hosted VLM.

---

## The browser path works, and it was never the exit (2026-11-01)

An account was created. The capture is in `debug/human/20261101-171342/`, taken by
`tools/human_solve.py` while a human solved a real challenge through a legion exit.

The number that matters:

| path | `/rtig/image` 200s | `/rtig/image` 403s | outcome |
|---|---|---|---|
| browserless (`solve_live.py`) | 2, then dead | every wave after | never completes |
| real browser, same pool | **10** | **0** | **account created** |

So the earlier conclusion — "the exit is spent, the pool is the product" — was
wrong for the third time. The exit serves every image the browser asks for. Our
own request path is what gets refused after the second one, and no amount of
pool-hunting fixes that.

### The working sequence

Reported by the human and consistent with the trace: submit the form, press and
hold **twice**, then **close the popup and press Sign Up again**. The *second*
challenge is the one that serves all ten waves. The browserless loop always takes
the first challenge it is handed, which is the starved one, and dies at wave 2.
That is the whole difference, and it is a client-side retrigger, not a proxy.

### The index convention, finally measured

Guesses for the ten solved waves, captured by wrapping `JSON.stringify` in the page
before `encryptECData` encrypts it:

```
[2, 4, 6, 6, 5, 9, 7, 5, 3, 3]
```

Every value is a 0-based candidate index. The first candidate is `0`, so the
`--label-offset -1` default in `solve_live.py` was correct and the open question in
the older notes above is closed.

### Never validate against a page screenshot

The first capture attempt screenshotted the page on each guess. Those frames land
during the challenge's own progress animation, which reads `3 done`, `4 done` — and
a vision model shown one of them answers `3`, `4`, `5` in sequence while looking
completely confident. It scored 0/10. The puzzle is the `/rtig/image` body, not the
viewport. `tools/browser_solver.py` reads the strip off the response stream for
exactly this reason.

### Where the architecture landed

Browserless stays blocked on the image grant — it needs a retrigger it cannot
perform without a page. The productive path is the browser: drive it, intercept the
strip per wave, answer with `roblox_creator/captcha_vision.py`, page the carousel,
submit. The vision solver is layout-agnostic (it answered both `MatchDiceIconCount`
and `icongrouping`), so new game types do not break it.

## Some exits pass with no challenge at all (2026-11-01)

Watching a run live: the form was submitted, and the account was simply created —
no consent screen, no hold, no puzzle. The run's own log kept waiting for a hold
widget for its full 420 s timeout because that loop had no creation check, so the
account looked like a stall.

That is the `passive pass` rung in `docs/ARKOSE.md` seen from the outside, and it
changes the shape of the funnel: not every account costs a solve. Measure the
passive-pass rate before designing anything around a solver, because the cheap
half of the pool may not need one at all.

Two things every driver must therefore check *first*, in every loop:

```python
if "/home" in page.url or "/discover" in page.url:
    # account exists
```

## `proxies2.txt` is dead; `proxies.txt` works (2026-11-01)

`budget.legionproxy.io` answers `SOCKS5: User was rejected by the SOCKS server` for
every line in `proxies2.txt` — the billing account is spent, not a code fault.
`proxies.txt` (evomi, `udp.evomi.com`) connected on every line tried. A browser run
against a dead pool fails with `net::ERR_SOCKS_CONNECTION_FAILED` before any of the
signup logic executes, which is easy to mistake for a bug in the creator.

## One image, not two — and the card was never unreadable (2026-11-01)

The solver used to send two images: the reference card blown up on its own, and
the candidates reflowed into a grid. The card was then a ~136 px crop upscaled
seven times, sitting next to brick, stipple and nothing that gave it scale, and
four naming passes over it returned **"jet"**, **"compass"**, **"Chinese character
水"** and **"watering can"**. That was read as "the art is unreadable at this
resolution" and the family got gated out of the loop.

Both of those conclusions were wrong, and the fix was layout rather than pixels.

`roblox_creator.strip_montage.single_image()` puts the card in a 400 px column on
the left with the candidate tiles pasted beside it at **native** 200x190, each
carrying a small digit. The canvas lands at 1442x600 — inside the 1568 px ceiling
that most vision APIs apply — so nothing is resampled on the way in and the tiles
are the exact source pixels.

Same model, same strips, same rule sentence. What changed:

| strip | reference, as the model read it | answer |
|---|---|---|
| `195547/strip_10` | "a rounded, cloud-like shape" over "a rocket-like shape with fins" | 6 |
| `195547/strip_01` | a watering can | 10 |
| `190344/strip_11` | a hat and a pair of shoes | 8 |

All three match what the pixels actually show. The per-candidate reasoning is
still thin and repetitious, so this is not proof of accuracy — it is proof that
the pixels arrived. Without that there is no point measuring anything else.

Two rules that fall out of it:

- **Never upscale a candidate tile.** The source is 200x190; anything bigger is
  interpolation, and it is paid for out of the card's resolution budget.
- **Upscale only the reference.** It is the smallest thing on the canvas and the
  only panel that carries the question.

`--presentation split` still exists for a family whose tiles are too small to
survive one canvas. Nothing measured so far needs it.

## Teaching a small model instead of asking one (2026-11-01)

Every wave currently costs ~2.5 s of vision API. A local matcher answers the same
board in **53 ms** (measured, 10 tiles, onnxruntime CPU, 4 threads; 1 thread 108 ms,
8 threads 127 ms), so the appeal is obvious. Three things have to be true first,
and only the first is.

**1. There must be labels.** There are none. `tools/label_ui.py` serves every saved
strip with the card beside the numbered tiles and records one click per wave into
`debug/labels/labels.jsonl`. 68 raw strips exist across 7 runs, which is roughly
five minutes of clicking.

**2. The labels must agree with themselves.** This is the real gate. The vision
model is not a usable teacher — it called one card four different objects — so the
teacher is the human. If the human cannot label a family consistently, the pixels
do not contain the answer and no student will find it. Labelling the same strip
twice, days apart, measures that directly; nothing else does.

**3. The split must be by run.** Strips inside one run share a session, a family
and a resolution, so a random strip-level split puts near-duplicates on both sides
and reports a number that means nothing. `tools/train_matcher.py --cv` is
leave-one-run-out for that reason, and it is the only accuracy worth quoting.

The architecture is a **siamese matcher**, not a classifier. One small encoder maps
the reference and every candidate into the same 256-d space and the answer is the
nearest candidate. Tile count stops existing, both families share weights, and new
art does not invalidate the output slots — because there are no output slots.

`roblox_creator/matcher.py` loads the exported pair. `browser_solver.py --solver
auto` uses it only when the top two cosine scores differ by more than `ONNX_MARGIN`,
and hands torn boards to the API. That split is the point of having both: cheap
model for the confident majority, API for the tail.

## The challenge flavour is a choice, and one header moves it (2026-11-03)

`/v2/signup` answers with one of two challenges. `captcha` is Arkose: it carries a
`dataExchangeBlob` and every transport in this repo can solve it. `captchav2`
carries no blob, and six completion variants in `tools/captchav2_probe.py` have
never satisfied it. So the ratio between them is throughput, and the server picks
it per request.

The client shape is one of the inputs. Sending `x-roblox-platform: Android` with
the Android UA, or `PCGDK`, produced `captcha` in an interleaved sample where the
plain desktop shape also did — the sampling was destroyed by the per-IP signup
limit (see `docs/AUTH_SURFACE.md`), so the ratio is not a number yet. It is a
lever, unmeasured.

`roblox_creator/client_shape.py` holds the shapes, records every observed flavour
to `debug/shapes/observations.jsonl`, and exposes `preferred()`. The rule inside
`preferred()` matters more than the module: it returns a shape only when that
shape has at least `min_samples` resolved observations **and** beats the default
by `min_gain`. Ties, sparse data, and missing baselines all return `None`, and
`None` means the caller sends the request the way the browser wanted to. A lever
that fires on noise is worse than no lever.

Only `x-roblox-platform` is safe to impose from inside the browser. The UA stays
whatever Chromium actually is: overriding the header without the matching TLS
fingerprint and `navigator.userAgent` is a stronger tell than the flavour it
might buy back. `browser_headers()` filters on exactly that.

`browser_solver.py --shape auto` consults `preferred()` and falls back to the
default; `--shape off` sends the request untouched, which is what every build
before this one did. The response listener records the flavour of every real
signup, so a normal run quietly builds the evidence that `auto` needs. Until
there is enough of it, `auto` behaves exactly like `off`.

`run.py` / `roblox_creator/creator.py` do **not** have the lever yet — that
pipeline builds its own context in `browser.py` and nothing there routes
`/v2/signup`. Only the browser_solver path is instrumented.

## The grading screen is a labeller (2026-11-03)

The blocker was never the transport. Five `browser_solver.py` runs reached 10/10
waves with every `/rtig/image` at 200; the account did not exist because the
answers were wrong. Measured against the hand labels, the vision model matched
**1/28 waves** — 3.6%, which is chance. A ten-round challenge at 90% per wave
already passes only 35% of the time; at 3.6% it never passes.

What changes the shape of the problem is the failure screen. When a challenge is
graded wrong, Arkose replaces the board with *"That was not quite right."* and one
candidate tile checked green (`debug/browser_solver/20261101-195306/final.png`).
If that tile is the correct answer, the system has a dense, free, server-authored
label on every failure — the thing `SOLVER.md` said did not exist.

### What is measured, and what is not

Measured offline, on the 40 hand-labelled waves, with each query perturbed the way
the verdict panel perturbs it (0.92 scale, 5 px offset, green-check overlay):

| reader | result |
|---|---|
| `verdict.match_tile` within the right strip | **40/40** |
| `verdict.match_board` across the whole capture | **40/40** (wave + index + crop kind) |
| `verdict.match_card` by plain NCC at 64 px | 13/40 — too weak to use as a constraint |
| `verdict.match_card` with a white-icon mask + shift search | 34/40 |

So the tile is recoverable from pixels. Not measured, because it needs a live
attempt: whether the checked tile is the *correct* answer or the tile that was
submitted, whether the verdict is also in the `/fc/ca/` body or the game frame's
JS state (cheaper than pixels if it is), and whether **Try again** re-serves the
same board. Those are the four questions `tools/verdict_probe.py` exists to ask,
in that order, in one run.

### The pipeline

```
tools/verdict_probe.py     live attempt -> strips, /fc bodies, verdict crops, retry result
tools/harvest_labels.py    analysis -> debug/labels/verdict.jsonl   (gated, human wins)
tools/train_matcher.py     labels.jsonl + verdict.jsonl -> --cv / --export
roblox_creator/matcher.py  the ONNX student, 53 ms/board, already wired into browser_solver
```

`browser_solver.py` now recognises the grading screen instead of dying as
`submit-failed`, captures it before the retry control destroys it, and clicks
**Try again** up to twice. A normal run therefore feeds the labeller without a
separate probe.

### The rule that keeps it honest

Never train on the model's own answers. Only on the grading screen, and only
after the harvest gate. Wazza0 measured the alternative: pseudo-labels from a
VLM, 73% validation, 22% production, because the student learned the teacher's
per-count bias. The same trap is one line away here — `captcha_vision.py` will
happily produce a confident index for every wave, and none of it is a label.

## Local vision models: measured and rejected (2026-11-03)

The open question was whether a local VLM could replace the hosted one — either as
a solver or as a labeller. Three were run through `tools/local_vlm_bench.py` on the
human-labelled boards, same canvas `browser_solver.py` sends to the API, same
`ANSWER=<n>` contract.

| model | quant | correct | no-answer | s/board |
|---|---|---|---|---|
| SmolVLM-500M-Instruct | Q8_0 | 0/4 | 4 | 12 |
| LFM3-VL-2B | Q4_K_M | 0/2 | 2 | 153 |
| Qwen2.5-VL-3B | Q4_K_M | 0/3 | 0 | 125 |
| hosted VLM (`captcha_vision.py`) | — | 1/28 | 0 | — |

Chance is 10%. The failure modes are more informative than the numbers:

- **SmolVLM-500M** reads the tile numbers correctly and then answers the wrong
  question — *"The reference panel is numbered 1."* The image arrives; the
  reasoning does not exist at 500M.
- **Qwen2.5-VL-3B** answered **`4` on all three boards**. A constant, not a
  decision. It never engaged with the comparison.
- **LFM3-VL-2B** is a reasoning model and spent its whole budget speculating about
  which *dataset* the puzzle came from ("Visual Mosaic", "shadow matching") without
  ever grounding in the pixels. It never emitted an answer at 768 tokens.

### Why this is a rejection and not a tuning problem

Two independent reasons, and the second one does not care about accuracy at all:

1. **Capability.** 0/9 across three models, against a task where the hosted VLM
   already scores 3.6%. The task is shadow-shape matching where the reference's
   shadows are low-contrast, elongated, partly occluded by the white icons, and
   cut off at the panel edge. Nothing in the 500M–3B class reads that.
2. **Throughput.** `browser_solver.py` needs ten boards per challenge and one
   challenge per account. At 125 s/board that is 21 minutes of inference per
   account on this CPU. **Even a perfect local model here would be unusable.** A
   GPU changes this arithmetic; an i5-8250U does not have one.

### What was checked and is not the explanation

The harness was validated before the numbers were believed:

- `--log-disable` was the first bug: `llama-mtmd-cli` streams generated tokens
  through its logger, so disabling logs silenced the answer. Replaced with
  `llama-server` + `/v1/chat/completions`, where the answer arrives in a JSON
  field with nothing else in it. A regex over CLI output had already scored a
  `1` that was a chat-template artefact.
- LFM3's empty `content` was a token cap, not a refusal: reasoning models fill
  `reasoning_content` first. Fixed by reading both fields and raising the cap.
- The image is definitely arriving — SmolVLM transcribes the candidate numbers
  from it correctly.

### Rejected with the same method

Classical shape matching on this family, all measured against the same 40 labels:

| method | accuracy |
|---|---|
| raw NCC, card vs candidate | 1/40 = 2% |
| shadow-mask IoU, shifted | 5/40 = 12% |
| white icons removed, shadow NCC | 6/40 = 15% |
| bbox-normalised silhouette IoU (best threshold) | 5/40 = 12% |
| from-scratch siamese CNN, 40 boards | 2/40 = 2%, loss pinned at ln(10) |

The pattern is consistent: this family is not solvable by any single-image
comparison available at this budget. The reference is occluded, the scale is
mismatched, and the discriminative signal is a few dozen pixels of low-contrast
shadow.

### What this leaves

The grading screen. It is measured at 40/40 (`verdict.match_board`) because both
sides come from the *same* source JPEG — the strip is the file the game fetched
and the verdict crop is that file re-rendered. That is not a solver, it is a
labeller, and it is the only thing on this list that works.

So the order of operations is unchanged and now better supported:

1. `tools/verdict_probe.py` — if **Try again** re-serves the board, the solver
   stops being the critical path and every rejected method above stops mattering.
2. Otherwise, harvest at scale with the grading screen and revisit the student
   with a pretrained backbone, which needs a torch upgrade this box cannot run
   cheaply.

Local VLMs are closed. Do not re-open without a GPU.

## Roblox rewrote the signup form, and the gate is now a gesture (2026-11-04)

Two independent things had gone stale, and both were invisible from the code
because both live services changed under it.

### The form is A/B'd, and both variants moved

`CreateAccount` now serves two layouts from the same URL:

| | classic | radix |
|---|---|---|
| `#MonthDropdown` | native `<select>` | `<div>` + Radix combobox wrapping a hidden `<select>` |
| password | `#signup-password`, on screen one | absent on screen one; `#signup-v2-password` after Continue |
| submit | `#signup-button`, labelled "Sign Up" | unlabelled `button[type=submit]` on step two |

`Creator._fill` assumed the classic layout only, which is why every radix exit
died with `no element for #signup-password`. That message was literal: the field
is not on screen one.

Measured behaviour of the radix birthday widget: Playwright's `select_option`
against the `<div>` fails, but the **wrapped native `<select>` still drives it** —
setting `.value` and dispatching `input` + `change` moves both the combobox label
and React's state (verified: `Month` → `March`). That is what `signup_form` does.

The step-one → step-two transition mounts the password field under a spinner and
it stays non-interactable for seconds while `offsetParent` is already non-null.
Visibility has to be decided by hit-testing the element's own centre, not by a
selector query.

Both layouts are now handled per page by `roblox_creator/signup_form.py`;
`Creator._fill` / `_submit` use it, so a pool serving both needs no routing.

### The gate is press-and-hold, and the dead state is a re-draw

On 2026-11-04 the dominant challenge was HUMAN/PX `Verify you're human` →
`Hold the button to confirm you're human` → blue `Press and hold`. No image, no
puzzle, no perception. `tools/hold_probe.py` measured the panel at 150 ms
resolution:

- completion is announced as `Human Challenge completed, please wait`, and the
  button's `aria-label` changes at the same moment;
- a 4 s hold was not enough; a 6.2 s hold completed; 6.5 s and 7.5 s holds on a
  different session did not — the gesture is scored, not timed;
- **rejection is silent.** The panel keeps reading `Press and hold ... requires
  verification`, no `try again`, no progress element. `role=progressbar` never
  exists, so any release logic keyed on a progress bar never fires.

The resolving rule, which is now `captcha.solve_hold_gate`:

1. hold, sampling the panel's text;
2. `completed` → release at once;
3. `try again` → release, let the button re-arm, hold again;
4. neither after a full window → **dead state**: close the modal, submit the
   form again, take the fresh draw (often an Arkose puzzle instead).

`_modal_visible` used to probe `#px-captcha` in the top document only. The panel
renders inside the PX/HUMAN frame, so the top document has no `#px-captcha` at
all and every gate read as "already cleared" — the solver reported a clear that
had not happened, then failed the account. It now walks every frame.

### Proxy status

`proxies.txt` (evomi, 10 000 sticky sessions) stopped working mid-session on
2026-11-04 at ~07:45. SOCKS auth still succeeds, but **every** `CONNECT` returns
`rep=0x01` — for `www.roblox.com` and `example.com` alike, with a session id the
plan has never seen. That is an account-level block (quota or expiry), not a
destination or session problem, and not something the code can work around.
`proxies1/2/3.txt` (legionproxy) were already dead on 2026-11-03.

Direct egress still reaches the form and the gate, which is enough to validate
the form and gesture code — but direct egress draws an Arkose puzzle almost
every time, so any rate measured on it understates a residential pool.

### The hold has no server-side shortcut (2026-11-04)

`tools/hold_wire.py` recorded every request across a full gate cycle. There is no
"verification result" call to make on its own behalf. The only traffic around the
gesture is telemetry:

```
ift.px-cloud.net/ns?c=<session>          beacons
dvp.px-cloud.net/v                       200, no verdict in the body
ic.px-cloud.net/bd/sync.html             beacons
collector-a.perimeterx.net/api/v2/...    telemetry + clientError
captcha.px-cdn.net/PXbf8PROpW/captcha.js the scorer itself
```

The verdict is computed inside `PXbf8PROpW/main.min.js` from the collected
signals and sealed into the `_px3` cookie, which Roblox then validates
server-side. Nothing accepts "this user passed". `_px3` is also set *before* the
hold, so its presence is not a pass signal — only its contents, and those are
signed.

Observed lifecycle on direct egress, matching the hand-driven account:

```
press  -> ring fills -> "completed, please wait" -> "Please try again"
press  -> loading -> blue button loses its label, nothing resolves  (dead)
close  -> submit again -> fresh draw
```

So a completed ring is not a pass, and the dead state is the panel's normal end
for a rejected gesture. The two levers that remain are the gesture's
plausibility and — dominantly — the exit's reputation: PX scores the session, and
a datacenter/direct IP is the worst input it can get. On a residential exit the
same code path should clear far more often; on this host it cannot be measured.

### Still true, and now measured precisely

A full hold was accepted once at **3.3 s** and rejected at 4.0, 6.2, 6.5, 6.6,
7.5 and 9.0 s across sessions, with identical pointer behaviour. Hold length is
not the variable — the score is. `solve_hold_gate` no longer treats length as
meaningful: it presses while the button is pressable and acts only on the panel's
transitions.

## Google SSO creates accounts with no captcha at all (2026-11-04)

Measured on two separate runs, both driven by a human in the new signup UI and
recorded end to end by `tools/oauth_capture.py`. The flow never touches
`/v2/signup`, `/v2/captcha`, Arkose or the press-and-hold. The only PerimeterX
traffic is `collector-px….px-cloud.net/api/v2/collector` beacons — telemetry, not
a challenge.

```
accounts.google.com  consent
  -> 302  auth.roblox.com/v1/external/3161830277039358/sso/oauth/callback?state=…
  -> 302  www.roblox.com/sso-complete?sso-type=signup
  -> .ROBLOSECURITY set (httpOnly, ~1 kB)
```

So the captcha is not bypassed — this endpoint does not have one. Account creation
is authorised by Google's OAuth assertion, and the gate lives only on the email
signup path.

Details that matter if this is ever automated:

- the callback is a **GET with `state`**, and `.auth.roblox.com` carries an
  httpOnly `RBXSsoExchangeCookie` (235 chars) that binds the state to the browser
  session — a captured callback URL cannot be replayed without it;
- the account is usable immediately, and Roblox assigns a generated username
  (`JovialFlyer4389`); the `sso-complete` username step is not required for the
  session to work;
- the Google address lands on the account **verified**:
  `/accountsettings/v1/email` returns `verified: true`.

### Trap: `users.roblox.com/v1/users/authenticated` lies from the outside

The same cookie returned `403 {"message":"User is moderated"}` via `curl_cffi`,
while a real browser session on `/home` showed the account logged in and
`/v1/users/authenticated` returned `200 {"id":11941259553,"name":"JovialFlyer4389"}`
from inside the page. Validate SSO sessions **from the browser context**, never
from a bare HTTP client: the REST result is a fingerprint verdict on the client,
not the account state.

First working account of the project: `accounts/sso/accounts.jsonl`,
cookie at `debug/oauth/20261104-142836/cookie.txt`.

### What this moves

The captcha is no longer the constraint on this path. The constraint becomes
Google identity supply: one Google account per Roblox account, and Google's own
signup/login is a harder wall than Roblox's captcha. Everything else in this
document — the solver, the hold gate, the suppressed-token work — applies only to
the email path.

### One Google identity, one Roblox account (2026-11-04)

Confirmed by hand: signing in with a Google account that already owns a Roblox
account returns `sso-type=login` and lands on that same account — it does not
create a second one. The SSO path therefore costs one Google identity per Roblox
account, and the cost model moves from "captcha solves" to "identity supply".

The identity only matters at creation. Afterwards the `.ROBLOSECURITY` cookie is
the credential, so identities can be recycled if the provider allows deleting and
re-provisioning users.

### Google identity gate (2026-11-04)

Free consumer signup was driven end to end from this machine with
`tools/google_signup_probe.py`. The wizard runs name -> birthday/gender ->
username -> password, and then stops at `signup/crossflowverification/initial`:

    "Google needs to verify some info about your device or phone number before
     you can continue. This helps keep you and others safe online by preventing
     abuse from computer programs or bots."

It is a QR code, and the QR decodes to
`accounts.google.com/devicephoneverification/begin?...`. Opening that target with
a Pixel 7 user agent works without Play Services and lands on:

    "Your phone will open an SMS message with a code you need to send to verify
     your phone. Your carrier may charge for SMS messages."

So the gate is an **outbound** SMS from a real SIM, not an inbound OTP. Every
SMS-verification service (5sim, sms-activate) is receive-only and therefore
cannot pass it. That is why the public Gmail-creator repos are stale, and why an
Android emulator does not help: it reaches the same screen without a SIM.

Consumer Gmail creation is closed. Managed identity is the remaining route:
Cloud Identity Free, 50 users per verified domain, `$0`, third-party OAuth
included. Domain verification needs a TXT record; DuckDNS supports one through
`https://www.duckdns.org/update?domains=X&token=Y&txt=Z` (value inherited by
sub-subdomains). `tools/domain_txt.py` sets and verifies it.

Resolved: Google **accepts** a `example.duckdns.org` subdomain at signup -- the
wizard advanced to admin account creation with it. The same
`crossflowverification` QR gate then appears once, for the admin, and a real SIM
passes it. After that the tenant exists and the remaining 49 users are created by
the admin with no verification per user.

DuckDNS TXT behaves as follows (measured): a write propagates to 1.1.1.1 and
8.8.8.8 within seconds, and the slot is **one-way** -- `clear=true` and an empty
`txt` both answer `OK`/`NOCHANGE` without removing the value. Overwriting is fine,
and Google reads one value, so this costs nothing.

Open question: whether Roblox accepts an SSO assertion from a non-gmail identity.
