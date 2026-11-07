# Arkose on Roblox signup — reversed reference

Everything here was measured against the live deployment on 2026-10-30, not copied
from documentation. Where a claim is inferred rather than verified it says so.

Site key: `A2A14B1D-1AF3-C791-9BBC-EE33CC7A0A6F`
API host: `https://arkoselabs.roblox.com`
Build id: `270438f7-2325-426f-9636-88978b387c8d` (static literal in `api.js`)
CAPI version: `4.6.1`

## The flow, in order

```
POST /v2/signup                    403 + rblx-challenge-id/type/metadata
        │                          metadata is base64 JSON; dataExchangeBlob lives there
        ▼
POST /fc/gt2/public_key/<sk>       token  (this is where `c` and `data[blob]` go)
        ▼
GET  /pows/setup?session_token=    pow_token + signed worker + work_config
POST /pows/started, /pows/split, /pows/check      → {"action":"challenge"}
        ▼
POST /fc/gfct/                     game_data: waves, gameType, instruction, image URL
        ▼
GET  /rtig/image?challenge=0…      one strip per wave
POST /fc/ca/                       answer; response carries the next wave's image
        ▼
        (repeat until solved)
```

## `c` — the fingerprint envelope

Recovered by hooking `crypto.subtle` at runtime (`tools/bda_hook.py`), then
verified byte-for-byte against a live capture, including the tag placement.

```
key  = getRandomValues(32)                      AES-256, encrypt-only
iv   = getRandomValues(12)
rsa  = RSA-OAEP(SHA-256, static SPKI, key)      256 bytes
gcm  = AES-GCM(key, iv, plaintext, tag=128)     ct ‖ tag

c    = b64(iv ‖ tag) + b64(rsa) + b64(ct)
```

Three base64 segments, concatenated, no separator. The 16-byte GCM tag is lifted
off the end of the ciphertext and moved into segment zero — that detail is why a
naive `b64(iv)+b64(rsa)+b64(ct‖tag)` does not reproduce a capture.

Plaintext is an **ordered array of `{key,value}` pairs**, exactly:

| key | value |
|---|---|
| `api_type` | `"js"` |
| `f` | `x64hash128(";".join(values of fe))` |
| `n` | base64 of current epoch **seconds** |
| `wh` | fingerprint's `wh` |
| `enhanced_fp` | the 90-entry device array (see below) |
| `fe` | the `fe` list, raw |
| `ife_hash` | `x64hash128(", ".join(fe), seed=38)` |
| `jsbd` | `{"HL":3,"NCE":true,"DT":"Roblox","NWD":"faked","DMTO":1,"DOTO":1}` |
| `c` | `"Copyright (c) 2026 Arkose Labs. All Rights Reserved."` |

`f` and `ife_hash` are murmur3-x64-128 over `fe`, so they cannot be invented
independently — they make the fingerprint internally consistent.

Implemented in `roblox_creator/bda.py`. The retired AES-CBC `bda` field still
builds via `arkose_py.build_bda()` for older deployments; the live site no longer
reads it.

## `enhanced_fp` decides suppression — the single most important knob

Arkose answers with a token. If it contains `|sup=1`, the challenge was
*suppressed*: no puzzle was presented.

- A short or absent `enhanced_fp` → `sup=1`.
- The full array from `build_enhanced_fp()` (~90 entries) → no `sup` parameter.
- The real browser's own token has **no `sup` parameter**.

**Roblox refuses suppressed tokens.** Six encodings of the resubmit were tested
with a fresh token each time (`tools/suppress_probe.py`): full base64 metadata,
raw JSON metadata, token-only metadata, no metadata header, and
`apis.roblox.com/challenge/v1/continue` in two shapes. All six returned
`403 {"message":"Challenge failed to authorize request"}`.

So `sup=1` is not "you look clean enough to skip the puzzle" — on this deployment
it is a bot signal. Do not chase suppression as a bypass.

## `data[blob]` — where it comes from

Not PerimeterX, not `/v1/arkose/bot-activity-ticket` (404s). It is
`dataExchangeBlob` inside `rblx-challenge-metadata` on the signup 403:

```python
meta = json.loads(base64.b64decode(header + "=" * (-len(header) % 4)))
blob = meta["dataExchangeBlob"]   # ~505 chars: base64(iv) "." base64(body)
```

- **Required.** Omit it → `400 DENIED ACCESS`. Include a stale one → `401`.
- **Single-use per challenge.** A consumed blob yields 401 on reuse.
- **`c` is portable across IPs** — verified by minting in one exit and using from
  another. The blob is what binds a token to a challenge.

## Two challenge flavours (coin flip)

| `rblx-challenge-type` | metadata | usable |
|---|---|---|
| `captcha` | `captchaToken`, `unifiedCaptchaId`, `dataExchangeBlob`, `actionType` | **yes** |
| `captchav2` | `challengeId`, `redemptionToken`, `sharedParameters`, `mode: CAPTCHA_MODE_INVALID` | **no** — see below |

Roughly 50/50. Retry the signup with a fresh username until `captcha` lands.

`captchav2` was tested for a free pass and does not have one. The client completes
it with `SET_CHALLENGE_COMPLETED { redemptionToken: <token from the CaptchaV2
component> }`, and the sibling device-integrity challenge happily completes with an
*empty* token when the native check is unavailable — so an empty redemption looked
plausible. Six completions were tried, one fresh challenge each
(`tools/captchav2_probe.py`): `continue` with empty metadata, with
`{redemptionToken:""}`, and with the full metadata echoed; the legacy resubmit
with a blank token and without metadata; and `continue` claiming
`challengeType: captcha`. All six returned 403 — the `continue` variants with
`"an internal error occurred"`, the resubmits with `"Challenge failed to authorize
request"`. It needs the real component token. Treat the flavour as a retry, not a
path.
`sharedParameters` carries `useContinueMode: false` and
`renderNativeChallenge: false`, which is why the legacy resubmit (not
`/challenge/v1/continue`) is the expected path.

## Proof of work — `action: next | challenge | block`

`/pows/*` endpoints are on the Arkose origin. **All POSTs are JSON**
(`content-type: application/json`). Posting a JSON body with a form-urlencoded
content type makes `/pows/check` answer `400 {"error":"Invalid Request"}` and
looks exactly like a rejected proof-of-work — that bug cost a day.

### Protocol

```
GET  /pows/setup?session_token=<s>          → pow_token, url, sequence, timeout,
                                              work_type, work_config
POST /pows/started  {session_token, pow_token, round, time}
POST /pows/split    {…, split_num, execution_time, hash_rate, result: nonce}
POST /pows/check    {pow_token, session_token, hash_rate, execution_time,
                     transform, result: [{target_hash, attempt_count}]}
```

`check` answers one of:

- `{"action":"next", ...}` — carries a fresh `work_config` (and sometimes
  `pow_token`/`timeout`/`work_type`). Same `pow_token` normally continues; do
  **not** re-fetch `/pows/setup` mid-flight, that rotates the token and every
  later call 400s.
- `{"action":"challenge"}` — done, call `/fc/gfct/`.
- `{"action":"block"}` — risk refusal. See below.

Only splits listed in `work_config.report_splits` may be posted (observed `[1]`);
posting an unlisted split index produced a 500.

### The search itself (verified, not assumed)

`work_type: 1` is `TARGET_HASH`. The worker's inner loop, read out of the
obfuscated bundle and then confirmed with a patched loop condition:

```js
while (V !== P.target && elapsed <= timeout) { V = await sha512(P.seed + nonce); nonce++ }
```

So it is an **exact 512-bit preimage over the nonce range**, not hash-below-target:

- `P.seed` is one chunk of `b64(utf16le(JSON.stringify(seed)))`, split into
  `len(splits)` pieces.
- `P.target` is `splits[i].target_hash` (SHA-512 hex).
- The search resumes from the previous split's nonce, so the nonce increases
  monotonically across splits.

Confirmed in Python: `sha512(prefix + nonce) == target` for all five splits, where
`nonce` is the reported `iterations` **minus one** — the worker increments `U`
after hashing and reports `U`, so `attempt_count` is one past the winning nonce.
That off-by-one is in Arkose's own code; report it verbatim.

`hash_rate = (lastNonce - startingNonce) / elapsedMs`, computed by the worker.

### Running it without a browser

The worker is a plain Web Worker from a signed URL and touches no DOM, so Node
runs it: `tools/powrun.js` (~120 lines of shims: `self`, `postMessage`,
`crypto`, `TextEncoder`, `performance`, `location`). Do not reimplement the
transform — the bundle's DP pass over the seed is what produces `finalTransform`,
and it changes when Arkose ships a new `pow/2.5.0`.

Node's `crypto.subtle` is several times slower than Chrome's, so the reported
rate lands at 5–13 hashes/ms instead of 40–50. **Do not renormalise it.** It was
tried: inflating `execution_time`/`hash_rate` to browser-plausible values raised
the block rate to 4/4. The server weighs its own clock between `/pows/started`
and `/pows/check`, so an inflated figure is a stronger signal than a fast one.

### `block`

Observed on roughly half of exits, at any round. It is a risk decision about the
session/exit, not a bad nonce — the nonces verify. Treat it as retryable on a
fresh exit. It correlates with hot exits: hammering one proxy with repeated
attempts produces blocks and `403 DENIED ACCESS` on image fetches.

## `gfct` — the game

```
POST /fc/gfct/   (form)
  token=<session part of the gt2 token>
  sid=<region, e.g. us-east-1>
  render_type=canvas
  lang=
  isAudioGame=false
  is_compatibility_mode=false
  apiBreakerVersion=green
  analytics_tier=40
```

Returns `game_data`:

| field | meaning |
|---|---|
| `waves` | how many answers this challenge costs |
| `gameType` | `4` = match-the-key, `1` = tile pick, … |
| `instruction_string` | e.g. `MatchDiceIconCount` — the human sentence |
| `customGUI._challenge_imgs` | **one** signed image URL, `challenge=0` |
| `game_difficulty`, `answer_width/height`, `key_width/height` | layout geometry |
| `challengeID`, `session_token`, `sid` | needed by `/fc/ca/` |

`challengeID` from here is the `game_token` for `/fc/ca/`. `session_token` is a
**new** token, different from the gt2 one.

## Images

- `GET` the signed URL exactly as given. `challenge=N` is covered by the
  signature, so hand-editing the index returns `403 DENIED ACCESS`.
- The **referer must be the game frame**:
  `/fc/assets/ec-game-core/game-core/1.40.0/standard/index.html?session=<gfct session_token>`
  — note the *gfct* token, not the gt2 one.
- Wave `n>0` URLs are not in the initial payload; they arrive in each `/fc/ca/`
  response as `_challenge_imgs`.
- Strip is 2000×400 JPEG: ten answer tiles of 200 px across the top ~140 px, and
  the key sprite in the bottom-left (`key_width: 125`).
- The browser fetches this same endpoint, once per wave, and slices the returned
  JPEG into its ten tiles. It is not exempt from the grant — see `PROXIES.md`,
  *"The structural point, corrected"*.
- Whether a grant is issued is **stochastic per mint**, not a spendable budget —
  `RUNTIME.md`, *"The image gate is a lottery, not a quota"*.

## Mounting the puzzle without the Roblox viewport

`pyProjects3/RobloxPyApi3` (`REPOS.md`) opened a standalone challenge page so the
browser never loaded `www.roblox.com`. Worth reproducing, because the Roblox
viewport is ~200 assets and most free exits fail on page weight, not on Arkose.
Tested with `tools/mount_direct.py` on 2026-11-01 — **it does not work**, and the
reason is specific:

    /fc/assets/ec-game-core/game-core/1.40.0/standard/index.html?session=…&pk=…
        loads, boots, renders a hidden interstitial, never starts a challenge
    <gfct challengeURL>?session=…          (match-game-ui/0.33.0/standard/)
        boots further and throws
        TypeError: Cannot read properties of undefined (reading '4.intro_title')

The puzzle UI reads its `string_table` from the parent that instantiated it — the
same table `gfct` returns — so a top-level mount has to inject that config itself
before the chunk runs. Nothing in the shell's `main.js` (11 KB loader) reads
`window.name`, `document.referrer` or a `message` listener, so the injection point
is inside a later chunk.

Blocked rather than impossible, but it would not raise the image ceiling either
way: a standalone mount still fetches `/rtig/image` per wave. Its only payoff is
page weight, which matters for free-exit throughput and not for the grant rate.


## `/fc/ca/` — answering

Form-encoded:

```
session_token = gfct session_token
game_token    = gfct challengeID
sid           = gfct sid
render_type   = canvas
analytics_tier= 40
guess         = encryptECData(JSON.stringify(guesses), session_token)
tguess        = same plaintext after the dapib transform   (optional — 200 without)
bio           = b64 {"mbio":"","tbio":"","kbio":""}
ecdata        = b64 {"height":450,"width":400}
is_compatibility_mode = false
```

`guesses` **accumulates**: `[{"index":0}]`, then `[{"index":0},{"index":1}]`, …

Response: `response` (`"not answered"` while in progress), `solved`,
`incorrect_guess`, `decryption_key`, `_challenge_imgs` (next wave), `time_end*`.

### `encryptECData` = CryptoJS password cipher

Recovered from `encryptECData(JSON.stringify(guesses), sessionToken)` in the
game-core bundle and confirmed by decrypting live captures:

```
salt = 8 random bytes
key, iv = EvpKDF(password=session_token, salt, keySize=32, ivSize=16)
ct   = AES-256-CBC(Pkcs7(plaintext))
wire = {"ct": b64, "iv": hex, "s": hex}
```

EvpKDF is CryptoJS's MD5 chain: `block_n = MD5(block_{n-1} ‖ password ‖ salt)`.
Derived IV matches the sent IV on real captures, which is the check that the
scheme is right. Implemented in `roblox_creator/ca.py`.

## Difficulty varies with the client's credibility

| client | instruction | waves |
|---|---|---|
| real desktop Chrome (captured) | `MatchDiceIconCount` | **5** |
| replayed desktop capture, 6 attempts | `MatchDiceIconCount` | **5, 5, 5, 5, 6, 9** |
| our synthetic Android fingerprints | `MatchDiceIconCount` | **10** |

Same instruction, same exits, twice the work for mobile. Replaying the captured
desktop payload reproduces the browser's 5 waves, so the difference is the
*client*, not the exit or the moment. This is the main lever on how many visual
solves 50k accounts/day needs: desktop halves it.

Coherence repairs to the mobile fingerprints (self-consistent window geometry,
locale/timezone matched to the exit) did **not** change the wave count, but did
remove the `{"action":"block"}` refusals — 5/8 → 8/8 attempts reaching `gfct`.
Measure with `tools/waves_probe.py`.

## Traps worth remembering

- `gt2` without `data[blob]` → `400`; with a stale one → `401`. Both read as
  "denied access", only the status distinguishes them.
- `tls_client` occasionally throws `unexpected EOF` on `gt2` through SOCKS. Retry
  on a fresh session — a real rejection is a 401 *with a body*.
- The gt2 token's session part is `token.split("|")[0]`; the `r=` parameter is the
  region.
- `capi_mode`/`style_theme` are `lightbox`/`modal`; the browser sends these and
  the server does not appear to validate them strictly, but keep them.

## The consent screen before the challenge (2026-11-01)

The Arkose modal does not always open on a hold or a puzzle. On some exits it opens
on a bare consent screen — a title, one sentence, and a single green button:

```
Verification
Please solve this challenge so we know you are a real person
[ Start Puzzle ]
```

Nothing renders until that button is clicked. Measured: a run sat on this screen for
150 s polling for a hold widget that had not been asked for, and reported "no hold"
against a challenge that was one click from starting. The screenshot is in
`debug/hold_watch/20261101-175544/never_got_hold.png`.

`roblox_creator/arkose_ui.start_puzzle()` clicks it, and both `tools/browser_solver.py`
and `tools/hold_watch.py` call it before waiting. Child frames are searched first,
because when the button belongs to Arkose it lives in the enforcement iframe while
the host page holds only the wrapper with the same words.

The ladder therefore has four rungs, not three:

    consent screen -> hold -> proof of work -> puzzle

## The hold, as a mechanical question

The hold is the only rung a human passes and automation does not. The open question
was not "how long" but *when the widget leaves*: while the pointer is still down, or
only once it is released. `tools/hold_watch.py` presses, samples the widget every
400 ms, and screenshots either way. Guessing this wrong costs either an early
release (rejected) or a long hold that fills the ring and does nothing.

**Answered** (`debug/hold_watch/20261101-180238/`). The pill does not leave while
the button is down: at 1.2 s, 2.4 s and 4.0 s it still reads `Press and hold`, and
the 8 s sample is indistinguishable from the 1 s one. The instant the button comes
up, the pill is replaced by a spinner (`c1_c_after_release.png`) and the widget
disappears a second or so later.

So the sequence is press → hold ~2 s → **release** → wait for the widget to go. A
hold that never releases is a hold that never completes, which is exactly what the
first version did for 25 s per attempt. Confirmed by a second press cycles cleanly:
`cycle 2: no widget`.

`press_and_hold` now does that, and treats "still up after release" as *press
again* rather than *failed* — Arkose re-presents the rung, and the human passes it
with two consecutive presses.
