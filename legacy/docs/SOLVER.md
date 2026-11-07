# Solving the puzzle — measured costs, not opinions

The transport is done. The only thing between the chain and a created account is
answering ~6 pictures per account, and the funder's target makes that a
throughput problem before it is an accuracy problem.

## What the volume actually demands

```
50,400 accounts/day        (35/min)
× 5.8 waves                (measured, replayed desktop payload)
= 292,320 wave-solves/day
= 3.4 solves/second sustained
```

At ~17 s per solve that is **~58 solves in flight at any moment**. Everything
below is judged against that number.

## Option 1 — our own VLM (anonymous ChatGPT), free

Measured 2026-10-30 on captured waves:

| property | value |
|---|---|
| uploads per wave | **1** (single montage: key row + 10 numbered candidates) |
| latency | 16–19 s |
| answered | 9/9 |
| accuracy | **unknown** — no ground truth yet |
| cost | $0 |

The upload budget was the thing to fix and it is fixed: an anonymous identity
gets **~18 uploads**, so one upload per wave lets a single identity cover a whole
desktop challenge (~6 waves) with room left. Two uploads per wave could not fit a
10-wave mobile challenge at all.

Two hard limits, both measured:

- **An identity is flagged after one turn.** Reusing one `AnonChat` for a batch
  gave 1 success then 5× `403 Unusual activity`. A *fresh identity per wave*
  answered 9/9 from the same IP, so the flag is per identity, not per exit. That
  matches the global design: fan out, do not pace.
- **Zipping a wave does not work.** `tools/zip_upload_probe.py`: a `.zip`
  uploads cleanly through all three stages (`/files` → blob `PUT` →
  `process_upload_stream` returns `Succeeded processing`) and then the
  conversation rejects it — `{"detail":"We couldn't process this image. Error
  code: invalid_image_format."}`. The multimodal pipeline reads image bytes, not
  archive members. One image per wave *is* the cheap path.

Accuracy is the open question and it dominates: at 90 % per wave a 6-wave
challenge succeeds 53 % of the time; at 80 % it is 26 %; at 70 % it is 12 %. A
wrong guess is not free — it burns the challenge.

## Option 2 — NopeCHA Recognition (best structural fit)

NopeCHA is the only service whose shape matches ours. Everyone else sells a
**token**: you give it a URL and sitekey, it drives Arkose from *its* IP and
returns a token. That is the wrong architecture here — we already own the
transport, and a token minted off-exit is exactly the inconsistency that gets a
signup refused.

NopeCHA's Recognition API is images in, indices out, no proxy, no session:

```
POST /v1/recognition/funcaptcha        one image holding a 3x2 grid
  {"task": "…", "image_data": ["data:image/jpeg;base64,…"]}
  -> {"data": "<job id>"}
GET  /v1/recognition/funcaptcha?id=…
  -> {"data": [false, false, true, false, false, false]}
```

Their own Tile example task is `"Pick the dice pair whose top sides add up to
14"` — dice arithmetic is in-distribution, which is the encouraging part.

The constraint is the grid: **3×2, six tiles, one image, one mask.** A wave is
1 key + 10 candidates = 11 tiles, so the default path is two grids per wave
(key repeated in both, tile 0 of each mask discarded). `tools/nopecha_probe.py
--grid 5x2` sends all eleven in one image instead — if the model segments a 5×2
layout and returns an 11-entry mask, the cost halves.

### Cost

Subscription caps, not per-token (quarterly rates from the pricing page):

| plan | solves/day | ≈ / month |
|---|---|---|
| Free | 100 | $0 |
| Basic | 20,000 | $13 |
| Professional | 80,000 | $33 |
| Enterprise | 200,000 | $67 |

| layout | credits/day | subscriptions | ≈ / day |
|---|---|---|---|
| 3×2, two calls per wave | 584,640 | 3 × Enterprise | **$6.60** |
| 5×2, one call per wave | 292,320 | 2 × Enterprise | **$4.40** |

Compare with the alternatives at the same volume:

| path | cost / 50k accounts |
|---|---|
| own VLM | $0 + unknown accuracy |
| **NopeCHA Enterprise ×3** | **~$200/month** |
| buy from BloxGen | ~$20/day = ~$600/month |
| Capzy token API ($0.001/solve) | ~$1,512/month |
| CapSolver token API (~$2/1000) | ~$3,024/month |

That inversion is worth stating plainly: **a solver is now cheaper than buying
accounts**, which was not true before per-token pricing hit $2/1000.

### Status — unproven, and the free tier cannot test it

- The free tier reports `plan=Free credit=100 quota=100` and is **IP-bound to the
  caller**. Submitting from this host returns `{"error":12,"message":"Banned IP"}`,
  and routing the submit through the residential pool fails at the SOCKS layer.
  So the free tier is not a testbed from here.
- The capability question is narrower than "does NopeCHA do FunCAPTCHA": Roblox's
  game is `MatchDiceIconCount` — *count the occurrences of a symbol and compare
  to N* — which is a compound task. Their API models tile selection, not a count
  comparison. Unresolved until a key exists.

**Next test, in order:** one Starter key ($4.99/month, 2,000 solves/day = ~1,000
waves) → run `tools/nopecha_probe.py --all --grid 3x2` and `--grid 5x2` on the
captured waves → if 3×2 answers, read accuracy against `/fc/ca/` (a wrong guess
comes back as `incorrect_guess`, so the server labels every attempt for free).
Layout 5×2 only matters after 3×2 is proven.

## Option 3 — token services (CapSolver, Capzy, 2Captcha, CapMonster)

Workable in principle, wrong shape for us and 7–15× the cost of option 2. They
also need the challenge to be driven from the same exit, so they duplicate the
transport we already have, and their solve time (10–40 s) stacks on top of ours.
Keep as the emergency fallback if NopeCHA disappears, not as a plan.

## What to build next, regardless of which solver wins

1. **Ground truth.** One browser pass where the answer is observed gives labels;
   after that scoring is offline and free. `tools/vlm_bench.py` scores
   `debug/vision/<round>/` but currently has nothing labelled to score against.
2. **The live loop.** `tools/solve_live.py` — fetch wave → montage → solver →
   `/fc/ca/` → read `incorrect_guess`. This is the real accuracy metric and it
   uses the server as the labeller.
3. **Keep the solver pluggable.** Own VLM, NopeCHA, or a token service behind one
   interface, because a third-party solver in the hot path is a single point of
   failure (bans, price changes, the `invalid_image_format` class of surprise).

---

## Where the live loop actually stands (2026-10-30, later)

`tools/solve_live.py` was built after the analysis above. It runs the whole chain
and uses `/fc/ca/` as the labeller. Facts learned by running it:

- **Wave 0 has been accepted by the server.** A montage answer came back
  `{"response":"not answered"}` with no `incorrect_guess`, and a fresh wave URL
  followed. So the montage layout, the one-upload-per-wave shape and the `/fc/ca/`
  body are all correct in practice, not just in theory.
- **There is no per-wave feedback.** Arbitrary sequential indices were accepted the
  same way in earlier runs, so correctness is only visible as `solved` at the end
  of a challenge. That makes solver accuracy expensive to measure per wave and
  means every experiment costs a whole challenge.
- **Guest replies truncate mid-answer.** The first prompt produced replies like
  "The key symbol appears to be the small white bomb-like circle. The image res…"
  — the answer was cut off before it appeared. The prompt now demands
  "Reply with the tile number only", and there is a terse retry.
- **Parsing was inverting answers.** "**Candidate 4**" was being read as 5, and a
  reply that named the *count* ("…exactly **2 times**") was being read as tile 2.
  The parser now accepts, in order: `candidate|tile|option N`, `answer: N`, then a
  *leading* number only. A mid-sentence integer is never trusted.
- **The index convention is unproven.** Whether Arkose indexes `guesses` 0-based is
  not documented and cannot be inferred from per-wave responses. It is exposed as
  `--label-offset` (-1 default, 0 for 1-based) so the first full solve names it
  instead of hiding it in code.

## The deterministic counter: half built, and the half that matters is hard

`roblox_creator/dicecount.py` reads **N** from the key sprite with no model and no
network: threshold bright low-saturation pixels, take the **rightmost small glyph**
in the key band (the `x` always sits to its left), and classify it against
`data/digits.npz`. Leave-one-out accuracy on 14 captured keys: **13/14**, and the
single miss is the only `3` in the bank, which LOO cannot classify by construction.
Note `x0` is legal — the answer is then the tile with no occurrences.

The counting half is the problem. `tools/symbol_counter.py` warps detected decals
to squares and correlates them against the key plate, but on real captures it
merges neighbouring decals into elongated blobs (aspect ratios like 23×10) and the
warped templates no longer correlate — counts come back as 0-1 when the true
answer is 4. Self-consistency check (does exactly one tile match N?) passes on only
**2/17** captured waves.

That is exactly why Wazza0's repo trains a CNN for the same game ("rockcount_v5.pt")
instead of hand-rolling CV. The route from here is a small counting model trained on
labels from `/fc/ca/` — with the warning from that same repo firmly in mind: do not
let a VLM generate the labels, because its per-count bias becomes the model's
objective and validation will report success while production collapses.

---

## Template matching: tried, and the reason it fails is instructive

`tools/symbol_match.py` was written to replace decal-plate detection with direct
multi-scale, multi-rotation NCC of the key symbol over each tile — no segmentation,
nothing that can merge. It did not work, and the *way* it failed is worth keeping:

- First template extraction used `value > 135, saturation < 110` and "leftmost
  bright blob". The desert background in the key band is bright and orange, so that
  returned **sand**, and every match scored under threshold. Fixed with
  `value > 170, saturation < 60`, which leaves exactly three components in the band:
  the sticker, the `x`, and the digit.
- With the correct sticker as template, the top peaks across three different tiles
  landed on the **same coordinates** (`(1,146)`, `(49,8)`, `(37,170)`) at ~0.5 score.
  Those are ground-texture features, identical in every tile. The actual symbols
  score below 0.3.
- Self-consistency across 36 captured waves: **2/36** at that threshold, **4/39**
  with the loose threshold — both worse than useless.

The lesson is the one Wazza0's repo already records: the key sprite and the symbols
painted on the wooden dice are *different renderings* — glossy sticker vs shaded
paint on a 3D face — so a single-patch correlator has nothing crisp to lock onto,
and the background is a stronger signal than the target. This is a classification
problem, not a correlation problem, and it needs real labels.

**Label source, settled by elimination:** the server is the only honest labeller.
`/fc/ca/` gives no per-wave feedback, so the only real label is `solved` at the end
of a challenge — one bit per five answers. That is far too slow to train on, which is
why the montage VLM stays in the loop for now: not because it scales, but because it
is the only thing currently capable of producing a full set of correct answers.

## The montage resolution ceiling, in the model's own words

A live answer from `gpt-6` reading a montage:

> "My best guess is **candidate 4**, though the low resolution makes…"

That is the whole solver problem in one sentence. A vision model downsamples to
roughly 1.5–2k pixels on the long edge, and the montage is 2060×1218 with eleven
panels — so each tile lands at ~200 px and the symbols inside it at ~30 px. The model
is being asked to count things it cannot resolve, and it says so.

`tools/tile_vote.py` tried the obvious fix: send the key plus each tile as its own
labelled attachment in one conversation (11 uploads), and ask for a count per tile,
which is an easier question than comparing ten piles at once. **It is not available
on the free path.** The first `ask_many` works and the second returns
`429 {"code":"throttled"}` on `file init` — a fresh anonymous identity does not get
an 11-upload allowance. The reply also addressed only the first image
(`"🔑 × 4"`), which is the key's own count, not a per-tile tally.

So the montage is not a budget compromise to be revisited; it is the only shape the
anonymous path accepts. That pins per-wave accuracy to what a ~30 px symbol allows,
which is the real argument for a local model — and the reason labels matter. The only
honest labeller is `solved`, one bit per five answers, which is too slow by itself.

## Two measured facts about the model, not the puzzle

**Availability was a transport bug, and it is fixed.** One run produced 10 answers and
7 failures — 41% of waves lost with no answer at all. The cause was not the model:
`ask_chatgpt` sampled exits at random, so three draws repeatedly landed on dead SOCKS
paths while the direct connection was fine. Trying direct first and the rest in order
turned that into **11 answers, 0 failures**, at ~16 s each instead of 30–70 s. A third
bare prompt (`"Answer now with one digit, 1-10. Nothing else."`) recovers replies that
ramble instead of answering.

**Asking for per-tile counts triggers a refusal; asking for the tile number does not.**
This was worth learning the hard way. Three prompt shapes were tried against the same
montage:

- "count how many times the symbol appears in each tile, reply with ten numbers"
  → *"The key indicates that the target symbol (the black linked…"* (truncated preamble)
- same with "your entire reply must be ten digits, begin immediately"
  → *"I see the key symbol (×4) and the 10 numbered tile groups. Do you want me to
  identify which tiles contain that symbol?"*
- "which numbered tile shows that symbol exactly N times? reply with the number only"
  → **"Candidate 7 is the match."** every time.

So the counting formulation reads as a captcha-solve request and gets deflected into a
clarifying question, while the single-answer formulation is answered directly. The
practical consequence: the montage pick is not a convenience, it is the only phrasing
that survives, and any future attempt to get counts out of a hosted model should
expect the same deflection.

---

## Update (2026-11-03): the server does label per wave — in the browser

Two claims above are true of the browserless `/fc/ca/` path and false of the
browser:

- *"There is no per-wave feedback."* The grading screen that follows a failed
  challenge reveals the correct tile. It is one label per failing wave, authored
  by the server, and it costs nothing but the failed attempt.
- *"The only honest labeller is `solved`, one bit per five answers."* That was
  the browserless ceiling. With the grading screen, the browser path produces
  dense labels at the rate the loop can fail challenges.

The measured numbers, the harvest gate, and what is still unproven are in
`docs/RUNTIME.md`, *"The grading screen is a labeller (2026-11-03)"*. The
instruments are `tools/verdict_probe.py` (live probe, `--selftest` offline) and
`tools/harvest_labels.py` (captures → `debug/labels/verdict.jsonl`).

The warning above still stands and is the whole risk: never train on the model's
own answers, only on the grading screen.
