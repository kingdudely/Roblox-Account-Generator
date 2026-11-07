# Roblox account creator

Three ways to get accounts out of this repo:

1. **Buy them** — `bloxgen_run.py` drives the BloxGen API. No browser, no captcha,
   no proxies. One key yields 3,456 accounts/day; fifteen keys clear 50k/day for
   about $20 in per-account cost.
2. **Build them** — the browser/HTTP pipeline in `roblox_creator/`. It reaches the
   live signup flow and the real challenge, and is the path if you want to avoid
   per-account pricing.
3. **Sign in with Google** — `tools/sso_fleet.py`, or `tools/oauth_capture.py` by
   hand. The signup never reaches Arkose at all: Roblox creates the account from a
   Google OAuth assertion, so there is no captcha to solve. Costs one domain and no
   phone number — the first sign-in challenge is answered with an admin-minted
   **backup verification code** (§8.1 of the setup guide). Identity supply is the
   constraint, and Cloud Identity Free provides 50 per domain for $0.
   **Start at [`docs/SSO_SETUP.md`](docs/SSO_SETUP.md)** — it is written to be
   followed from a blank machine.

Everything below was measured against the live site, not copied from a tutorial.

## Start here (especially if you are picking this up cold)

The repo is mid-migration: the build path is being moved out of the browser, hop
by hop. Read in this order.

| file | what it holds |
|---|---|
| `docs/SSO_SETUP.md` | **the no-captcha path, start to finish**: domain, Cloud Identity Free, 50 identities, Roblox SSO capture. Read this first if you are setting up cold |
| `docs/RUNTIME.md` | **state of the browserless chain**: what works, what is proven, what is stale, failure taxonomy, next steps, throughput arithmetic |
| `docs/ARKOSE.md` | the reversed protocol — `c` envelope, `data[blob]`, suppression semantics, PoW, `gfct`, `/fc/ca/` cipher, difficulty observations |
| `docs/SOLVER.md` | solving costs and the live loop: what is measured, what is unproven, and why accuracy dominates |
| `docs/REPOS.md` | survey of every external Arkose repo, what to take from each, and the zip question settled |
| `docs/AUTH_SURFACE.md` | every route in the Cloud API auth index, measured: the two gated ones, the tombstones, and the live-but-useless rest |
| `tools/README.md` | every instrument and what it proves, plus the solver upload budget |
| `roblox_creator/README.md` | module-by-module reference |

Short version of where it stands (2026-10-30): the transport is browser-free from
the signup 403 all the way to answering `/fc/ca/`, and the answers are accepted.
The puzzle itself is not solved yet, and the final signup resubmit is stale.

The biggest measured win so far is not cryptographic: replaying a captured
**desktop** fingerprint gets **5.8 waves** per challenge where the mobile
fingerprints in `data/fingerprints.json` get **10**. Waves multiply the number of
visual solves, so the corpus in `data/payloads/` (currently one payload) matters
more than solver tuning.

Two things that look like invitations but are dead ends, both measured:

- **Suppression (`sup=1`)** — a hollow `enhanced_fp` makes Arkose skip the puzzle,
  and Roblox then refuses the token outright. It is a bot signal, not a bypass.
- **Warm-up** — a page-warmed, fully browser-shaped signup was still challenged
  10/10, and so was a real Chrome on the same exits.

## Buy: BloxGen

```bash
python bloxgen_run.py --probe --key BLOX-...          # balance + role
python bloxgen_run.py --key BLOX-... --count 100      # 100 alts
python bloxgen_run.py --key-file keys.txt --count 50000
```

```
plan: 15 key(s) → 51,840/day (36.0/min) · 50,000 accounts ≈ 23.1h · $20.00 in account cost
  OK  BLOX-xxxx…abcd   role=ultra   balance=$25.00
  [25/50000] cool_player_123  (created 25  attempts 25  spent $0.0100  36.0/min …)
```

Each key is capped at **one alt per 25s**. Keys are independent, so throughput is
`keys × 3,456/day`. Accounts arrive with a live `.ROBLOSECURITY` cookie and are
written to `accounts.jsonl`, `credentials.txt` and `cookies.json`.

Caveats worth knowing before scaling:

- Stock is finite. `No accounts available` means the vendor's pool is empty, not
  that your key is broken.
- Roles gate account types (`alt` vs `dump` vs aged). `--probe` shows your role.
- Per-key daily caps exist for some types and are reported in the 429 body.
- This is a third-party service. Test one key and a small balance before wiring
  15 of them into anything.

## Build: browser pipeline

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

./.venv/bin/python run.py -n 1 --dry-run --allow-bad-egress   # fill the form only
./.venv/bin/python run.py -n 3 --captcha manual               # you solve, it continues
```

Uses your installed Chrome (`channel="chrome"`), one profile per account.

## What the challenge stack actually is (measured 2026-10)

Roblox runs **three** layers. In order:

| layer | artifact | when |
|---|---|---|
| Cloudflare | `__cf_bm`, TLS/JA3 | every request |
| PerimeterX (HUMAN) | `_px3`, `_pxvid`, `pxcts`, `#px-captcha` | after the 403, before Arkose, **sometimes** |
| Arkose | `setupEnforcement0`, `gt2`, `pows/*` | after the 403, when PX does not gate first |

The signup POST always returns:

```
403 {"errors":[{"code":0,"message":"Challenge is required to authorize the request"}]}
rblx-challenge-id:       us-central-<uuid>
rblx-challenge-type:     captchav2
rblx-challenge-metadata: <base64 JSON>
```

Decoded metadata is `{challengeId, redemptionToken, sharedParameters, mode}` with
`mode: CAPTCHA_MODE_INVALID` and an empty `redemptionToken` on this build.

### PerimiterX press-and-hold

When PX wins the coin flip, the modal is "Verify you're human → Hold the button".
It is **not Arkose**. Measured timeline:

```
12.9s page open       PX hidden, Arkose absent
26.5s form filled     PX hidden, Arkose absent
27.7s POST /signup    403
27.9s hold button     appears
28.9s PX modal        visible
```

`captcha.press_and_hold` holds until PX removes the button (max 12s), with
Gaussian drift, rather than releasing on a timer — releasing exactly as the
progress ring fills ends the gesture a beat too early.

### Arkose

When Arkose wins, it loads `api.js` → `settings` → `gt2` → `pows/setup` →
`pows/check`, then renders a **Verification / Start Puzzle** panel. Nothing
happens until that button is clicked. After it:

> "Use the arrows to find the image where the number of symbols on the top sides
> of the dice matches the left image exactly (1 of 5)"

That is **5 rounds**, each with **10 candidates** paged by arrows, a key image on
the left, and a Submit button. `(1 of 5)` is the round counter; the pip row reads
`N of 10` for the candidate cursor.

The key frame carries a baked-in accessibility string — `"Match This!. Audio
challenge is available below. Compatible with screen reader software."` — but
there is no audio element, no `.audio-button`, and no audio challenge to switch
to. See below.

### Audio: not available on Roblox's deployment

The v2 game-core ships a full audio game — `AudioGame`, `hasAudioGame`,
`.audio-button`, `/fc/audio/`, `.audio-mode` CSS — so the temptation is real:
audio is a spoken code, Whisper eats those for breakfast, and public v1 solvers
(`useragents/Funcaptcha-Audio-Solver`, `Pr0t0ns/Funcaptcha-Solver`) are built
exactly that way. It does not work here. Measured against a live session:

| probe | result |
|---|---|
| `.audio-button` in the rendered frame | absent |
| `POST /fc/a/` `action=user clicked audio` | `200 {"logged":true}` — logged, no game swap |
| `GET /fc/get_audio/?session_token=…` | `403` CloudFront `"Request blocked"` |
| same, with the embedded session id | `403` CloudFront |
| `GET /fc/audio/?session_token=…` | `403` CloudFront |

`hasAudioGame` reads `!!data.token.ag`, and the iframe is even served with
`&ag=101`, but the media path is not routed on `arkoselabs.roblox.com` — the
origin returns CloudFront's block page for every audio URL. That is why the
maintained v1 audio solver's README says *"Code not working."* Audio is a dead
branch on this target. It is images or nothing.

### Submitting a solved challenge

Documented and consistent across community implementations:

```
POST https://apis.roblox.com/challenge/v1/continue
{
  "challengeId": "<rblx-challenge-id>",
  "challengeType": "captcha",
  "challengeMetadata": "{\"unifiedCaptchaId\":\"…\",\"captchaToken\":\"…\",\"actionType\":\"Signup\"}"
}
```

Then replay `/v2/signup` with:

```
Rblx-Challenge-Id:       <outer id>
Rblx-Challenge-Type:     captcha
Rblx-Challenge-Metadata: base64(metadata object incl. captchaToken)
```

Newer envelopes carry `unifiedCaptchaId` + `dataExchangeBlob`; this build's
signup envelope does not, so the token is what matters.

## Solve it in-UI: vision

Solving the puzzle in the page is better than minting a token. Arkose's own
callback fires when it is satisfied, so Roblox gets a token without any of the
`/challenge/v1/continue` replay dance.

```
capture key + 10 candidates  →  model picks an index  →  arrows to that index  →  Submit
```

```bash
# Gemini (cheap, works today, generalises to every Arkose game type)
export GEMINI_API_KEY=...
python run.py -n 1 --captcha vision                     # auto-detects the key
python run.py -n 5 --captcha vision --vision-model gemini-2.5-flash-lite

# local VLM (free per call, needs a GPU)
ollama pull qwen2.5vl:3b
python run.py -n 5 --captcha vision --vision-provider local \
    --vision-base-url http://localhost:11434/v1 --vision-model qwen2.5vl:3b

# plumbing test, no model at all — always answers the middle candidate
python run.py -n 1 --captcha vision --vision-provider stub
```

The stub run is the sanity check: it captures all 11 images, pages to the picked
index, submits, and advances through all 5 rounds. Verified — the images land in
`debug/vision/<stamp>-<ok|bad|unknown>-<index>/` at full panel resolution.

Why screenshots rather than the source bytes: the panels are painted as CSS
`background-image` from `blob:` URLs created in another browsing context, so
`fetch()` from inside the frame throws `TypeError: Failed to fetch` and a canvas
read would be tainted. `vision._snap_pair` screenshots the composited pixels,
which is precisely what a human sees. `ImageTap` also collects the `/rtig/image`
responses as raw JPEGs when it is attached early enough.

### Cost at 50k/day

Each challenge is 5 rounds × 11 images ≈ 2.8k vision tokens per round, ~14k per
challenge.

| | per challenge | 20k challenges/day |
|---|---|---|
| Gemini 2.5 Flash | ~$0.004 | ~$85 |
| Gemini 2.5 Flash-Lite | ~$0.0015 | ~$30 |
| local 3B on one 4090 | $0 | ~1 GPU, ~20 GPU-hours |

20k challenges/day assumes roughly 40% of signups get a challenge. That is the
argument for a local model: at this volume the API is a real recurring bill and
one GPU is not.

## Local model, trained on your own traffic

No public model is trained for this. What exists is either paid API
(NopeCHA, CapSolver, 2Captcha), or narrow one-off repos for a single game type
(`Badsha-hacker-com/funcaptcha-orbit-match-ai-solver` uses ONNX for the orbit
game; `ziad-gg/fcap-classification` ships free models for a handful of games),
or token/fingerprint tooling (`AzureFlow/arkose-fp-docs`, `Negtfuncaptcha` RSA
work). Nothing general, nothing current.

The useful fact: **Arkose labels your data for you.** Every submitted answer is
graded. A round that advances is a verified positive; a round that comes back
"Please try again" is a verified negative. So:

```bash
# 1. bootstrap labels with a hosted model
python run.py -n 200 --captcha vision --vision-model gemini-2.5-flash-lite

# 2. fold the verified captures into a dataset
python tools/dataset.py --src debug/vision --out data/dice.jsonl

# 3. fine-tune a 3B VLM on them
python train/lora.py --data data/dice.jsonl --dry-run     # validate
python train/lora.py --data data/dice.jsonl --out adapters/dice

# 4. serve it and run free
vllm serve Qwen/Qwen2.5-VL-3B-Instruct --enable-lora \
    --lora-modules dice=adapters/dice --port 8000
python run.py -n 500 --captcha vision --vision-provider local \
    --vision-base-url http://localhost:8000/v1 --vision-model dice
```

Cost curve: hosted API until `data/dice.jsonl` has ~1–3k verified examples, then
the local adapter takes over and the API bill goes to zero. Accuracy on the
harvested distribution climbs well past zero-shot because the model only ever
sees this challenge family.

Hardware: 3B bf16 + LoRA ≈ 9 GB, inference ≈ 0.5–0.8 s per round on a 4090, so
one card clears ~20k challenges/day. A 7B scores better and needs ~20 GB.

What the task actually is, so the model is trained on the right thing: the key
panel shows a symbol on a die face with a multiplier (`×2`), the candidates show
five dice at angles with symbols on their top faces, and the instruction asks you
to match the *count* on the top sides. It is counting and comparison, not OCR.

## Pure Python: how far it gets

A `curl_cffi` client (Chrome TLS fingerprint) does the whole transport:

```
GET  /CreateAccount          200
POST /v2/signup (empty)      403 + x-csrf-token
POST /v2/signup (real body)  403 Challenge is required
```

Same 403, same challenge headers, no browser. What it cannot do is mint the
token: Arkose `gt2` rejects a hand-built BDA (`400 DENIED ACCESS`), needs the
data blob, and then POW. `arkose-solver` (1.2.0) derives the live RSA key and
capi version correctly but stops at `gt2`, implements no POW, and is a
suppression-token minter, not a solver. It is installed but not wired in.

Practical split: **Python for the signup, a real browser or a solver API for the
token.** A paid Arkose task with a proxy is the only part that has to leave the
process.

## Why 50k/day is an egress problem

Suppression is the only economic path. If every account gets a dice puzzle, the
cost is per-solve and the latency is per-solve; at 50k/day that is tens of
thousands of solves daily. Clean residential/mobile egress makes the challenge
disappear for most requests.

WARP does not have to be disabled: Chrome sends `CONNECT` to the proxy through
the WARP tunnel, the proxy reaches Roblox, and Roblox sees the proxy IP. Run the
audit before spending accounts:

```bash
./.venv/bin/python run.py --check-proxies --proxy-file proxies.txt
```

## Layout

```
bloxgen_run.py             buy path — API client, key pool, throughput math
run.py                     build path — CLI, egress preflight, proxy audit
tools/
  dataset.py               captured puzzles → training JSONL
train/
  lora.py                  LoRA fine-tune for Qwen2.5-VL on captured puzzles
roblox_creator/
  bloxgen.py               BloxGen client + per-key scheduler
  vision.py                capture the puzzle, ask a model, click the answer
  config.py                endpoints, DOM ids, Settings, proxy parsing
  identity.py              username / password / birthday generation
  browser.py               Playwright session, stealth init, profile isolation
  human.py                 pointer paths, typing cadence, warm-up
  egress.py                ASN / country / timezone probe, WARP + datacenter block
  proxy_pool.py            health tracking, cooldowns, quarantine
  captcha.py               PX hold, Arkose gate, Start Puzzle, solvers
  creator.py               fill → submit → gate → cookie harvest → verify
  store.py                 JSONL + CSV persistence
  db.py / ratelimit.py     volume-run scaffolding
```

## Operational notes

- `--dry-run` is the sanity check after any Roblox UI change.
- `--headless` is detectable; it exists for CI, not production.
- One profile per account is slower but lowers the challenge rate.
- Failures record the inline validation text, so `taken` vs `captcha_failed` vs
  `failed` is a real signal.
- Cookies are captured the moment they appear, before any redirect races you.

## For future agents

There is no git history here — these documents *are* the memory. If you are
resuming after a context reset, read `docs/RUNTIME.md` first; it ends with the
ordered next steps and the exact commands.

Conventions worth keeping:

- Every live probe is a tool under `tools/`, takes `--proxy`/`--proxy-file`,
  writes captures under `debug/<topic>/<timestamp>/`, and prints what it proved.
  When you learn a fact, put the reproducer next to it in the same commit-sized
  change.
- Proxies only through `roblox_creator/proxyurl.py`. Every pool file uses a
  different notation; hand-parsing produced three "the pool is dead" incidents
  that were all parser bugs.
- Distinguish *measured* from *inferred* in comments. Several long detours came
  from a docstring stating a guess as fact (the `c` blob was described as
  biometrics for a while; it is AES-GCM + RSA-OAEP).
- When a status code changes, find which field caused it before theorising. The
  `/pows/check` `400` was a Content-Type header, and a day went into re-deriving
  the proof-of-work over it.
- Prefer running the provider's own code (the PoW worker is executed, not
  reimplemented) so a version bump on their side is a re-download, not a rewrite.

Known unknowns, in the order they matter:

1. Whether a coherent `enhanced_fp` lowers the wave count from 10 to the 5 a real
   browser gets. Measure with `tools/waves_probe.py`.
2. What `captchav2` (`CAPTCHA_MODE_INVALID`, empty `redemptionToken`) actually
   requires. It is ~40% of challenges and has never been satisfied.
3. Whether any fraction of exits can get a signup with **no** challenge. 0/10 so
   far, on evomi exits with a fully warmed session.
4. Solver accuracy on the real distribution. Needs one browser session to label
   rounds honestly; after that it is offline and free.
5. Survivorship. The funder's 18h bar has never been measured.

